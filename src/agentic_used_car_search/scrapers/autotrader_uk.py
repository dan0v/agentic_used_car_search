"""Autotrader UK scraper (direct GraphQL API, no browser).

The SPA (`sauron-search-results-app`, React + Apollo) calls an unauthenticated
GraphQL gateway. A direct POST returns structured listings with every filter
applied server-side - no browser, no anti-bot, no card-selector fragility. The
one moving part is `x-sauron-app-version`, which changes per deploy; it is
fetched from the /car-search page and cached with a TTL (see `_http.py`).

See docs/SITE_APIS.md for the full reverse-engineering notes, including the
filter vocabulary. Filters are `{filter, selected: [...]}`; `selected` not
`values`. `price_search_type: "total"` is required or the gateway returns
INVALID_ARGUMENT.
"""

from __future__ import annotations

import re
from typing import Any, Final
from uuid import uuid4 as _uuid4

from ..logger import logger
from ..types import CarListing, Config, ProgressSender, ScrapeResult, SearchParams
from ._base import normalise_drivetrain, progress, start_heartbeat
from ._http import get_autotrader_uk_version, throttled_request

_AT_UK_GATEWAY: Final[str] = (
    'https://www.autotrader.co.uk/at-gateway?opname=SearchResultsListingsGridQuery'
)

_AT_UK_QUERY: Final[str] = (
    'query SearchResultsListingsGridQuery('
    '$filters:[FilterInput!]!,$channel:Channel!,$page:Int,'
    '$sortBy:SearchResultsSort,$listingType:[ListingType!],'
    '$searchId:String!,$featureFlags:[FeatureFlag]'
    '){searchResults(input:{facets:[],filters:$filters,channel:$channel,'
    'page:$page,sortBy:$sortBy,listingType:$listingType,searchId:$searchId,'
    'featureFlags:$featureFlags}){listings{... on SearchListing{advertId title '
    'subTitle attentionGrabber price vehicleLocation sellerType dealerLink '
    'fpaLink numberOfImages badges{type displayText} '
    'trackingContext{advertContext{make model year price condition} '
    'distance{distance distance_unit} card{pageNumber position}}}}}}'
)


def _at_uk_filters(params: SearchParams) -> list[dict[str, Any]]:
    """Build the Autotrader UK GraphQL filter list from SearchParams."""
    postcode = re.sub(r'\s+', '', params.zip or 'SW1A1AA')
    filters: list[dict[str, Any]] = [
        {'filter': 'postcode', 'selected': [postcode]},
        # price_search_type is mandatory; without it the gateway rejects with
        # INVALID_ARGUMENT. "total" = absolute price (vs monthly finance).
        {'filter': 'price_search_type', 'selected': ['total']},
    ]
    if params.make:
        filters.append({'filter': 'make', 'selected': [params.make]})
    if params.model:
        filters.append({'filter': 'model', 'selected': [params.model]})
    if params.year_min:
        filters.append({'filter': 'min_year_manufactured', 'selected': [str(params.year_min)]})
    if params.year_max:
        filters.append({'filter': 'max_year_manufactured', 'selected': [str(params.year_max)]})
    if params.price_max:
        filters.append({'filter': 'max_price', 'selected': [str(params.price_max)]})
    if params.mileage_max:
        filters.append({'filter': 'max_mileage', 'selected': [str(params.mileage_max)]})
    # Radius: 0 means nationwide in SearchParams; Autotrader UK's "whole UK"
    # cap is 1500 miles.
    if params.max_distance is not None:
        radius = 1500 if params.max_distance == 0 else params.max_distance
        filters.append({'filter': 'distance', 'selected': [str(radius)]})
    if params.transmission:
        filters.append({'filter': 'transmission', 'selected': [params.transmission.capitalize()]})
    drive = normalise_drivetrain(params.drivetrain, form='autotrader')
    if drive:
        filters.append({'filter': 'drivetrain', 'selected': [drive]})
    return filters


def _at_uk_badge_map(listing: dict[str, Any]) -> dict[str, str]:
    """Autotrader UK folds some spec fields (mileage, registered year) into a
    `badges` array of {type, displayText}. Index them by type for lookup.
    """
    return {
        str(b.get('type', '')).lower(): str(b.get('displayText', ''))
        for b in (listing.get('badges') or [])
        if b.get('type')
    }


def _grab(text: str, pattern: str) -> str | None:
    """First regex match in `text`, or None. Kept tiny because the API's
    `attentionGrabber` strip still needs label-word parsing for a few fields.
    """
    m = re.search(pattern, text, re.I)
    return m.group(0) if m else None


def _at_uk_listing(listing: dict[str, Any]) -> CarListing | None:
    """Map one Autotrader UK GraphQL `SearchListing` to a `CarListing`."""
    advert_id = listing.get('advertId')
    if not advert_id:
        return None
    tracking = listing.get('trackingContext') or {}
    advert = tracking.get('advertContext') or {}
    distance = tracking.get('distance') or {}
    badges = _at_uk_badge_map(listing)

    # The API does not return a registration plate; like the cards, the plate
    # lives only on the detail page. Mileage and the registered year arrive as
    # badge displayText ("45,000 miles", "2019 (19)").
    mileage = badges.get('mileage')
    year = advert.get('year') or badges.get('registered_year')

    # The spec strip (transmission/drivetrain/fuel/body) is server-folded into
    # the free-text `attentionGrabber` even in the API response; pull what we
    # can out of it so the listing shows the filters the caller asked for.
    strip = listing.get('attentionGrabber') or ''
    transmission = _grab(strip, r'\b(manual|automatic|semi[-\s]?automatic)\b')
    drivetrain = _grab(
        strip,
        r'\b(front[-\s]?wheel[-\s]?drive|rear[-\s]?wheel[-\s]?drive'
        r'|all[-\s]?wheel[-\s]?drive|four[-\s]?wheel[-\s]?drive|FWD|RWD|AWD|4WD)\b',
    )

    # Price comes as a formatted string ("£24,995") from the API; fall back to
    # the structured advert price (an int) if the formatted one is missing.
    price = listing.get('price')
    if not price and advert.get('price'):
        price = f'£{int(advert["price"]):,}'

    fpa_link = listing.get('fpaLink') or ''
    url = f'https://www.autotrader.co.uk{fpa_link}' if fpa_link else None

    location = listing.get('vehicleLocation')
    if distance.get('distance') is not None:
        # vehicleLocation often already embeds the distance ("Westminster
        # (2 miles)"); only synthesise a suffix when it does not.
        if location and re.search(r'\(\s*[\d.]+\s*mi', location):
            pass  # distance already shown in the location string
        else:
            unit = distance.get('distance_unit') or 'miles'
            suffix = f'{distance["distance"]} {unit} away'
            location = f'{location} ({suffix})' if location else suffix

    title = listing.get('title') or ''
    subtitle = listing.get('subTitle')
    if subtitle:
        title = f'{title} {subtitle}'.strip()

    return CarListing(
        source='Autotrader UK',
        title=title or None,
        price=price,
        mileage=mileage,
        year=str(year) if year is not None else None,
        make=advert.get('make'),
        model=advert.get('model'),
        location=location,
        transmission=transmission,
        drivetrain=drivetrain,
        url=url,
    )


async def scrape_autotrader_uk(
    params: SearchParams,
    max_results: int = 20,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> ScrapeResult:
    """Search Autotrader UK via its unauthenticated GraphQL gateway.

    Replaces the browser scrape: every filter (transmission, drivetrain,
    mileage, postcode+radius, make/model, year, price) is applied server-side,
    and the response is structured JSON rather than card HTML. The one moving
    part is `x-sauron-app-version` (per-deploy), fetched once and cached.
    """
    stop_heartbeat = start_heartbeat(send_progress, 'Searching Autotrader UK')
    try:
        version = await get_autotrader_uk_version()
        filters = _at_uk_filters(params)
        payload = {
            'operationName': 'SearchResultsListingsGridQuery',
            'variables': {
                'filters': filters,
                'channel': 'cars',
                'page': 1,
                'sortBy': 'relevance',
                'listingType': None,
                'searchId': str(_uuid4()),
                'featureFlags': [],
            },
            'query': _AT_UK_QUERY,
        }
        headers = {
            'content-type': 'application/json',
            'accept': '*/*',
            'x-sauron-app-name': 'sauron-search-results-app',
            'x-sauron-app-version': version,
            'referer': 'https://www.autotrader.co.uk/car-search',
            'origin': 'https://www.autotrader.co.uk',
        }
        logger.debug(f'Autotrader UK GraphQL filters: {filters}')

        response = await throttled_request(
            'POST', _AT_UK_GATEWAY, headers=headers, json_body=payload
        )
        body = response.json()

        # The gateway wraps errors per-query in an `errors` array; raise so
        # _scrape-style cause chaining can attribute the failure to AT UK.
        if isinstance(body, list):
            body = body[0] if body else {}
        if body.get('errors'):
            raise RuntimeError(f'Autotrader UK GraphQL errors: {body["errors"]}')

        search_results = (body.get('data') or {}).get('searchResults') or {}
        raw_listings = search_results.get('listings') or []

        listings: list[CarListing] = []
        for item in raw_listings:
            if item.get('__typename') == 'SearchListing' or 'advertId' in item:
                mapped = _at_uk_listing(item)
                if mapped is not None:
                    listings.append(mapped)
            if len(listings) >= max_results:
                break

        progress(send_progress, f'Autotrader UK: found {len(listings)} listing(s)')
        return ScrapeResult(listings=listings)
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f'Autotrader UK scraping failed: {err}') from err
    finally:
        stop_heartbeat()
