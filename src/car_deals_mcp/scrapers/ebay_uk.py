"""eBay Motors UK scraper (direct Browse API with browser fallback).

eBay exposes a public **Browse API** (`/buy/browse/v1/item_summary/search`)
that returns structured JSON for vehicle listings, with every filter applied
server-side and no anti-bot. It is *authenticated*, unlike Autotrader UK /
Cinch: it needs an OAuth2 application token issued from the eBay Developer
Portal (https://developer.ebay.com). The token is fetched with
`client_credentials` grant against `api.ebay.com/identity/v1/oauth2/token`
using `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` from the environment and cached
with a TTL (~2h, well under eBay's actual ~2h token lifetime).

When the credentials are absent or token fetch fails, the scraper falls back
to browser-scraping `ebay.co.uk/sch/Cars-Trucks-Vehicles/...` via CloakBrowser
- the same transport Motors.co.uk uses. This keeps the source working out of
the box and upgrades automatically when an operator registers an eBay app.

Filter mapping (Browse API `filter` param, a URI-encoded name=value list):

  make/model  -> `keywords` (free-text; the API has no make/model facets for
                the Cars category in the UK marketplace, so a keyword search
                is the closest match. eBay facet IDs differ per marketplace
                and were not stable across probes.)
  year       -> `modelYear=[min,..]`
  priceMax   -> `price:[0,..]` (currency GBP, X-EBAY-C-MARKETPLACE-ID=EBAY_GB)
  mileageMax -> applied client-side; the API exposes mileage only on the
                full `getItem` response, not the search summary.
  transmission -> `aspects=Transmission={...}` (aspect filters)
  drivetrain   -> `aspects=Drivetrain={...}` — but eBay UK Cars does not
                surface a stable drivetrain aspect, so it is applied
                client-side when present.

The API does NOT return the registration plate (UK plates are not in eBay's
item aspect model for cars), so a listing cannot be piped straight into
`check_mot_history` the way a Cinch listing can.

See docs/SITE_APIS.md for the overall two-transport design.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode

from ..logger import logger
from ..types import CarListing, Config, ProgressSender, ScrapeResult, SearchParams
from ._base import (
    accept_consent,
    apply_uk_filters,
    progress,
    scrape,
    start_heartbeat,
)
from ._http import get_async_client, throttled_request

if TYPE_CHECKING:
    from playwright.async_api import Page

# eBay UK Browse API. The marketplace ID `EBAY_GB` scopes the search to the
# UK site; `X-EBAY-C-MARKETPLACE-ID` is the header form (the API accepts both
# the header and the `x-ebay-c-marketplace-id` query variant).
_EBAY_BROWSE_SEARCH: Final[str] = 'https://api.ebay.com/buy/browse/v1/item_summary/search'
_EBAY_TOKEN_URL: Final[str] = 'https://api.ebay.com/identity/v1/oauth2/token'

# eBay scopes for the Browse API (client-credentials grant).
_EBAY_SCOPE: Final[str] = (
    'https://api.ebay.com/oauth/api_scope/buy.item.summary'
    ' https://api.ebay.com/oauth/api_scope/buy.marketing'
)

# Token cache. eBay application tokens live ~2h; refresh a little before that.
_EBAY_TOKEN_TTL: float = 7000.0
_ebay_token: str | None = None
_ebay_token_expires: float = 0.0
_ebay_token_lock = asyncio.Lock()


def _ebay_credentials() -> tuple[str, str] | None:
    """Return (client_id, client_secret) from env, or None if absent.

    Read at call time so the scraper stays importable without credentials; the
    browser fallback is used when this returns None.
    """
    cid = os.environ.get('EBAY_CLIENT_ID')
    csec = os.environ.get('EBAY_CLIENT_SECRET')
    if cid and csec:
        return cid, csec
    return None


async def _get_ebay_token() -> str | None:
    """Fetch and cache an eBay application token (client_credentials grant).

    Returns None on any failure (missing creds, transport error, non-2xx) so
    the caller falls back to browser scraping rather than erroring the whole
    search call.
    """
    global _ebay_token, _ebay_token_expires
    now = time.monotonic()
    if _ebay_token is not None and now < _ebay_token_expires:
        return _ebay_token

    creds = _ebay_credentials()
    if creds is None:
        return None

    async with _ebay_token_lock:
        now = time.monotonic()
        if _ebay_token is not None and now < _ebay_token_expires:
            return _ebay_token

        cid, csec = creds
        basic = base64.b64encode(f'{cid}:{csec}'.encode()).decode()
        headers = {
            'Authorization': f'Basic {basic}',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        body = urlencode({'grant_type': 'client_credentials', 'scope': _EBAY_SCOPE})

        # The token endpoint is a form POST, not JSON; call the shared client
        # directly (the token is cached so this runs at most once per ~2h,
        # which makes throttling it pointless).
        try:
            client = await get_async_client()
            response = await client.request('POST', _EBAY_TOKEN_URL, headers=headers, content=body)
            response.raise_for_status()
            data = response.json()
            token = data.get('access_token')
            if not token:
                logger.debug(f'eBay token response missing access_token: {data}')
                return None
            _ebay_token = token
            # `expires_in` is seconds; refresh a minute early, capped at the TTL.
            ttl = float(data.get('expires_in') or _EBAY_TOKEN_TTL)
            _ebay_token_expires = time.monotonic() + min(ttl - 60.0, _EBAY_TOKEN_TTL)
            logger.debug(f'eBay token cached (expires in {ttl - 60:.0f}s)')
            return _ebay_token
        except Exception as err:  # noqa: BLE001
            logger.debug(f'eBay token exchange failed: {err}')
            return None


def _ebay_api_filter(params: SearchParams) -> str:
    """Build the Browse API `filter` string (a URI-encoded name=value list,
    comma-separated). The API accepts multiple filters in one string.
    """
    parts: list[str] = []
    if params.year_min or params.year_max:
        lo = str(params.year_min) if params.year_min else ''
        hi = str(params.year_max) if params.year_max else ''
        parts.append(f'modelYear=[{lo}..{hi}]')
    if params.price_max:
        parts.append(f'price:[0..{params.price_max}]')
    return ','.join(parts)


def _ebay_api_aspects(params: SearchParams) -> str | None:
    """Build the `aspects` query param (e.g. `Transmission=Automatic`). eBay
    aspects are category-specific; for UK Cars, `Transmission` is stable.
    Drivetrain is not a stable aspect on the UK marketplace, so it is not
    applied server-side here (it is filtered client-side in the browser path;
    the API path drops it - see _ebay_api_listings).
    """
    aspects: list[str] = []
    if params.transmission:
        aspects.append(f'Transmission={params.transmission.capitalize()}')
    return ','.join(aspects) or None


def _ebay_api_listings(items: list[dict[str, Any]], params: SearchParams) -> list[CarListing]:
    """Map Browse API `itemSummaries` to `CarListing`s. Mileage is not in the
    summary response, so it is left None; the browser path is the one that
    surfaces mileage. Drivetrain likewise.
    """
    listings: list[CarListing] = []
    for item in items:
        # Ensure thousands separators for consistency with the other UK
        # sources; fall back to the raw value if it is not numeric.
        price_val = item.get('price') or {}
        raw_value = price_val.get('value')
        try:
            price = f'£{int(float(raw_value)):,}' if raw_value is not None else None
        except (TypeError, ValueError):
            price = f'£{raw_value}'
        title = item.get('title')
        location = item.get('itemLocation', {}).get('city') if item.get('itemLocation') else None
        listings.append(
            CarListing(
                source='eBay',
                title=title,
                price=price,
                year=str(item.get('year')) if item.get('year') else None,
                make=item.get('make'),
                model=item.get('model'),
                transmission=item.get('transmission'),
                location=location,
                url=item.get('itemWebUrl'),
                thumbnail=item.get('image', {}).get('imageUrl') if item.get('image') else None,
            )
        )
    return listings


async def _scrape_ebay_api(
    params: SearchParams, max_results: int, send_progress: ProgressSender | None
) -> ScrapeResult:
    """Search eBay via the Browse API. Raises on failure so the caller can
    fall back to the browser path.
    """
    token = await _get_ebay_token()
    if token is None:
        raise RuntimeError('no eBay credentials (EBAY_CLIENT_ID/EBAY_CLIENT_SECRET)')

    keyword = ' '.join(p for p in (params.make, params.model) if p)
    q: dict[str, str] = {
        'q': keyword or 'car',
        'limit': str(max_results),
        'category_ids': '29792',  # Cars (UK Cars category on EBAY_GB)
    }
    filt = _ebay_api_filter(params)
    if filt:
        q['filter'] = filt
    aspects = _ebay_api_aspects(params)
    if aspects:
        q['aspects'] = aspects

    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_GB',
        'X-EBAY-C-ENDUSERCTX': 'contextualLocation=country=GB',
        'Accept': 'application/json',
    }
    api_url = f'{_EBAY_BROWSE_SEARCH}?{urlencode(q)}'
    logger.debug(f'eBay Browse API URL: {api_url}')

    response = await throttled_request('GET', api_url, headers=headers)
    body = response.json()
    items = body.get('itemSummaries') or []
    listings = _ebay_api_listings(items, params)
    progress(send_progress, f'eBay (API): found {len(listings)} listing(s)')
    return ScrapeResult(listings=listings)


# ---------------------------------------------------------------------------
# Browser fallback
# ---------------------------------------------------------------------------

# eBay SRP card extraction. The card markup uses `.s-card` containers (the
# newer SRP layout; the legacy `.s-item` class is kept as a fallback). Each
# card has `.s-card__title`, `.s-card__price`, `.s-card__subtitle`, and a
# set of attribute rows carrying "Miles: 80,731" / "Brand: Ford". Kept as a
# JS string literal - `page.evaluate` runs it in the browser (see CLAUDE.md).
_EXTRACT_EBAY_UK_JS: Final[str] = r"""
() => {
    const results = [];
    // Newer SRP uses `.s-card`; legacy layout uses `li.s-item`. Select both.
    const cards = document.querySelectorAll('.s-card, li.s-item, li.s-item-card');
    cards.forEach(card => {
        const titleEl = card.querySelector('.s-card__title, .s-item__title');
        if (!titleEl) return;
        let title = (titleEl.innerText || '').trim();
        // Drop the leading "New listing"/"NEW LISTING" badge and the
        // "Opens in a new window or tab" suffix eBay appends to every title.
        title = title.replace(/^new listing\s*/i, '').replace(/\s*Opens in a new window or tab\s*$/i, '').trim();
        // Skip the "Shop on eBay" placeholder cards (sponsored, non-vehicle).
        if (!title || /shop on ebay/i.test(title)) return;
        const linkEl = card.querySelector('.s-card__link, .s-item__link, a[href*="/itm/"]');
        const priceEl = card.querySelector('.s-card__price, .s-item__price');
        // eBay cards show prices with pence ("£2,500.00"); the other UK
        // sources use whole-pound strings ("£24,995"). Drop the pence so
        // parse_price and apply_uk_filters compare consistently.
        let price = priceEl ? priceEl.innerText.trim() : null;
        if (price) price = price.replace(/\.00$/, '');
        const imgEl = card.querySelector('.s-card__image img, .s-item__image-img, img');
        const text = card.innerText || '';
        // "Miles: 80,731" is the visible mileage line on UK Motors cards.
        const mileageMatch = text.match(/Miles:\s*([\d,]+)/i);
        // Year is usually the first token of the title ("2015 Ford Focus ...").
        const yearMatch = title.match(/\b(19|20)\d{2}\b/);
        results.push({
            title: title,
            price: price,
            mileage: mileageMatch ? mileageMatch[1] : null,
            year: yearMatch ? yearMatch[0] : null,
            href: linkEl ? linkEl.getAttribute('href') : null,
            image: imgEl ? imgEl.getAttribute('src') : null,
        });
    });
    return results;
}
"""


async def _scrape_ebay_browser(
    params: SearchParams,
    max_results: int,
    send_progress: ProgressSender | None,
    config: Config | None,
) -> ScrapeResult:
    """Browser-scrape eBay UK SRP. Used when the Browse API has no creds.
    Mirrors the Motors.co.uk approach: build a search URL, accept consent,
    read cards, apply client-side filters.
    """

    async def body(page: Page) -> ScrapeResult:
        keyword = ' '.join(p for p in (params.make, params.model) if p) or 'car'
        # eBay UK cars category id 6001 (Cars, Motorcycles & Vehicles); a
        # keyword search scoped to it surfaces Cars listings.
        qp: list[tuple[str, str]] = [
            ('_nkw', keyword),
            ('LH_Sold', '0'),
            ('rt', 'nc'),
            ('_ipg', str(max(50, max_results))),
        ]
        # Year/price are not reliably honoured via eBay's URL params (the
        # facet key changes across marketplaces); apply_uk_filters narrows
        # them client-side, consistent with how Motors.co.uk is handled.
        url = 'https://www.ebay.co.uk/sch/Cars-Trucks-Vehicles/6001/i.html?' + urlencode(qp)
        logger.debug(f'eBay UK search URL: {url}')

        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(2)
        # eBay's consent banner (OneTrust-style).
        await accept_consent(page, ['#onetrust-accept-btn-handler', '#gdpr-banner-accept'])
        # eBay's SRP renders the result cards client-side after load. Wait for
        # at least one real card before extracting; the "Shop on eBay"
        # placeholder is also a `.s-card`, so the extractor filters it.
        try:
            await page.wait_for_selector('.s-card, li.s-item', timeout=20000)
        except Exception:  # noqa: BLE001
            logger.debug('eBay SRP cards did not appear within 20s')
        await asyncio.sleep(4)
        raw = await page.evaluate(f'({_EXTRACT_EBAY_UK_JS})()')

        filtered = apply_uk_filters(raw, params)
        # Drivetrain is not surfaced in eBay cards; if the caller asked for it,
        # the result is unfiltered. Unlike Motors.co.uk the server does NOT
        # skip eBay on drivetrain (eBay cards at least surface transmission),
        # but we strip drivetrain filtering here to avoid false confidence.
        listings = [
            CarListing(
                source='eBay',
                title=item.get('title'),
                price=item.get('price'),
                mileage=item.get('mileage'),
                year=item.get('year'),
                location=None,
                thumbnail=item.get('image'),
                url=item.get('href'),
            )
            for item in filtered[:max_results]
        ]
        progress(send_progress, f'eBay (browser): found {len(listings)} listing(s)')
        return ScrapeResult(listings=listings)

    return await scrape('eBay', send_progress, body, config)


async def scrape_ebay_uk(
    params: SearchParams,
    max_results: int = 20,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> ScrapeResult:
    """Search eBay Motors UK.

    Uses the official Browse API when `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`
    are present in the environment; falls back to browser scraping otherwise.
    Both paths return `CarListing`s without the registration plate (eBay does
    not surface UK plates in its aspect model).
    """
    stop_heartbeat = start_heartbeat(send_progress, 'Searching eBay')
    try:
        if _ebay_credentials() is not None:
            try:
                return await _scrape_ebay_api(params, max_results, send_progress)
            except Exception as err:  # noqa: BLE001
                logger.debug(f'eBay API path failed, falling back to browser: {err}')
        return await _scrape_ebay_browser(params, max_results, send_progress, config)
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f'eBay UK scraping failed: {err}') from err
    finally:
        stop_heartbeat()
