"""Cinch scraper (direct REST API, no browser).

Cinch's Next.js SSR calls an unauthenticated internal REST search API that
is publicly reachable. It returns rich structured data including the
registration plate (`vrm` / `fullRegistration`) - the one UK source that
does, which lets a caller pipe a Cinch result straight into check_mot_history.

Mileage is NOT server-filterable on Cinch (every plausible param name was
probed and none changed the result count), so mileageMax is applied
client-side here. Postcode/radius have no effect (Cinch is national
delivery). See docs/SITE_APIS.md.
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import quote, urlencode

from ..logger import logger
from ..types import CarListing, Config, ProgressSender, ScrapeResult, SearchParams
from ._base import normalise_drivetrain, progress, start_heartbeat
from ._http import throttled_request

_CINCH_API: Final[str] = 'https://search-api.snc-prod.aws.cinch.co.uk/used-cars'


def _cinch_query_string(params: SearchParams, page_size: int = 50) -> str:
    """Build the inner query string that Cinch's API wraps inside its `url`
    parameter. Note the source-specific param names: `transmissionType` (not
    `transmission`), `driveType` (not `drivetrain`), `fuelType`, `bodyType`.
    """
    parts: list[tuple[str, str]] = []
    if params.make:
        parts.append(('make', params.make))
    if params.model:
        parts.append(('model', params.model))
    if params.price_max:
        parts.append(('toPrice', str(params.price_max)))
    if params.year_min:
        parts.append(('fromYear', str(params.year_min)))
    if params.year_max:
        parts.append(('toYear', str(params.year_max)))
    if params.transmission:
        parts.append(('transmissionType', params.transmission.capitalize()))
    drive = normalise_drivetrain(params.drivetrain, form='cinch')
    if drive:
        parts.append(('driveType', drive))
    parts.append(('pageNumber', '1'))
    parts.append(('pageSize', str(page_size)))
    return urlencode(parts)


def _cinch_listing(item: dict[str, Any]) -> CarListing:
    """Map one Cinch REST `vehicleListings` entry to a `CarListing`."""
    # Cinch returns the plate - the reason this source is worth the migration.
    reg = item.get('vrm') or item.get('fullRegistration')
    price = item.get('price')
    if price is not None:
        price = f'£{int(price):,}'
    year = item.get('modelYear') or item.get('vehicleYear')
    title_parts = [str(x) for x in (item.get('make'), item.get('model')) if x]
    if item.get('variant'):
        title_parts.append(str(item['variant']))
    return CarListing(
        source='Cinch',
        title=' '.join(title_parts) or None,
        price=price,
        mileage=f'{item["mileage"]:,} miles' if item.get('mileage') is not None else None,
        year=str(year) if year is not None else None,
        make=item.get('make'),
        model=item.get('model'),
        transmission=item.get('transmissionType'),
        drivetrain=item.get('driveType'),
        registration=reg,
        url=f'https://www.cinch.co.uk/vehicle/{item["vehicleId"]}'
        if item.get('vehicleId')
        else None,
    )


async def scrape_cinch(
    params: SearchParams,
    max_results: int = 20,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> ScrapeResult:
    """Search Cinch via its unauthenticated REST API.

    Replaces the browser scrape: structured fields including the registration
    plate (the one UK source that exposes it), no anti-bot. Mileage is not
    server-filterable on Cinch, so mileageMax is applied client-side here.
    """
    stop_heartbeat = start_heartbeat(send_progress, 'Searching Cinch')
    try:
        # Cinch's API takes the real query string URL-encoded inside a `url`
        # param - an odd quirk of the internal endpoint. Request a page big
        # enough to cover max_results in one call (no pagination needed for
        # the small result sets the tool returns).
        inner = _cinch_query_string(params, page_size=max(max_results, 50))
        api_url = f'{_CINCH_API}?url={quote(inner, safe="")}'
        logger.debug(f'Cinch API URL: {api_url}')

        response = await throttled_request(
            'GET', api_url, headers={'referer': 'https://www.cinch.co.uk/used-cars'}
        )
        body = response.json()

        raw_listings = body.get('vehicleListings') or []
        # Mileage is not server-filterable; narrow here.
        if params.mileage_max:
            raw_listings = [
                item for item in raw_listings if (item.get('mileage') or 0) <= params.mileage_max
            ]

        listings = [_cinch_listing(item) for item in raw_listings[:max_results]]
        progress(send_progress, f'Cinch: found {len(listings)} listing(s)')
        return ScrapeResult(listings=listings)
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f'Cinch scraping failed: {err}') from err
    finally:
        stop_heartbeat()
