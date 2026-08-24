"""Cars.com scraper (browser, CloakBrowser).

The only fully supported US source. Cards embed a `data-vehicle-details`
JSON payload carrying VIN, trim, body style, drivetrain, fuel type, exterior
color, dealer identity and CPO flag. Read that payload first and fall back to
parsing visible card text, so a markup reshuffle degrades results instead of
emptying them. Cars.com also serves a card-less page to automated traffic at
random, hence the two-attempt reload loop.

`page.evaluate` payloads stay as JS string literals - Playwright Python runs
them in the browser, so they cannot be rewritten in Python.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode

from ..logger import logger
from ..types import (
    CarListing,
    Config,
    ModelSuggestion,
    ModelSuggestions,
    ProgressSender,
    ScrapeResult,
    SearchParams,
)
from ._base import carscom_slug, progress, scrape

if TYPE_CHECKING:
    from playwright.async_api import Page


# Every Cars.com results page embeds its filter vocabulary - makes, models and
# their inventory counts - in this JSON blob. It is the same data behind the
# site's own filter panel, so it is authoritative about which names exist.
CARSCOM_FILTER_BLOB: Final[str] = 'script#CarsWeb\\.SearchController\\.index'

# JS payload that reads one filter's options ({name, value, count}) out of the
# embedded blob. Returns null when the blob is missing - the normal case on the
# anti-bot interstitial; callers must treat that as "unknown", not "empty".
_READ_CARSCOM_FILTER_OPTIONS_JS: Final[str] = r"""
(selector, title) => {
    const raw = document.querySelector(selector)?.textContent;
    if (!raw) return null;
    let data;
    try { data = JSON.parse(raw); } catch { return null; }
    for (const section of data?.srp_filters?.sections || []) {
        for (const item of section.items || []) {
            if (item.title !== title) continue;
            // Options arrive grouped (popular vs. all); flatten both shapes.
            return (item.listing_search_filter?.options || [])
                .flatMap(group => group.options || [group])
                .filter(opt => opt && opt.value && opt.value !== 'all')
                .map(opt => ({ name: opt.name, value: opt.value, count: opt.summary }));
        }
    }
    return null;
}
"""


async def read_carscom_filter_options(page: Page, filter_title: str) -> list[dict[str, Any]] | None:
    result = await page.evaluate(
        f'({_READ_CARSCOM_FILTER_OPTIONS_JS})({CARSCOM_FILTER_BLOB!r}, {filter_title!r})'
    )
    return result


def _inventory(count: Any) -> int:
    return int(re.sub(r'[^\d]', '', str(count or ''))) or 0


def suggest_carscom_models(
    requested: str, options: list[dict[str, Any]], limit: int = 6
) -> list[ModelSuggestion]:
    """Rank a make's real model names against what the caller asked for.

    This exists because a correct slug is not the same as a valid one: "GLE"
    slugs cleanly to `gle`, but Cars.com has no such model - only `gle_350`,
    `gle_450`, `gle_class` and friends - so the search silently returns nothing.
    Turning that dead end into "did you mean" is the difference between a broken
    tool and a narrow miss.
    """
    want = carscom_slug(requested)
    if not want:
        return []

    scored: list[ModelSuggestion] = []
    for opt in options:
        have = carscom_slug(opt.get('name', ''))
        if have == want:
            score = 100
        elif have.startswith(f'{want}_'):
            score = 80  # gle -> gle_450
        elif want.startswith(f'{have}_'):
            score = 60  # gle_450_4matic -> gle_450
        elif want in have:
            score = 40  # gle -> amg_gle_63
        else:
            continue
        scored.append(
            ModelSuggestion(name=opt.get('name', ''), count=opt.get('count'), score=score)
        )

    # Same score - prefer whichever the market actually has more of.
    scored.sort(key=lambda o: (o.score, _inventory(o.count)), reverse=True)
    return scored[:limit]


# JS extraction payload. Kept verbatim from scraper.js - Playwright Python runs
# this in the browser, so it stays JavaScript. Returns a list of raw card dicts.
_EXTRACT_CARSCOM_JS: Final[str] = r"""
() => {
    const results = [];
    const cards = document.querySelectorAll('fuse-card[id^="vehicle-card-"]');

    cards.forEach(card => {
        const text = card.innerText;
        const lines = text.split('\n').filter(l => l.trim());

        let title = null;
        let price = null;
        let mileage = null;
        let dealRating = null;
        let location = null;

        for (const line of lines) {
            const trimmed = line.trim();

            // Title: optional condition prefix + Year Make Model. The prefix
            // strip is a bounded literal match, not a wildcard - a greedy
            // `certified[\w\s-]*` here previously ate past the year and model,
            // leaving only the trailing trim (e.g. "SE") as title.
            if (/^(?:(?:used|new|certified(?:\s+pre-owned)?)\s+)?(19|20)\d{2}\s+\S+/i.test(trimmed) && !title) {
                title = trimmed.replace(/^(?:used|new|certified(?:\s+pre-owned)?)\s+/i, '');
                continue;
            }
            // Price: "$XX,XXX" (may have "price drop" suffix)
            const priceMatch = trimmed.match(/^\$[\d,]+/);
            if (priceMatch && !price) { price = priceMatch[0]; continue; }
            // Mileage: "XX,XXX mi."
            if (/^[\d,]+\s*mi\.?$/i.test(trimmed) && !mileage) { mileage = trimmed; continue; }
            // Deal rating: "Good Deal", "Great Deal", etc.
            if (/^(great|good|fair|high|no price)/i.test(trimmed) && !dealRating) {
                dealRating = trimmed.split('|')[0].trim(); continue;
            }
            // Location: "City, ST (XX mi.)"
            if (/^[A-Z][a-z]+.*,\s*[A-Z]{2}\s*\(/i.test(trimmed) && !location) {
                location = trimmed; continue;
            }
        }

        // Dealer name positional fallback only - the JSON names the seller.
        const dealerMatch = card.querySelector('.dealer-name');
        let dealerName = dealerMatch ? dealerMatch.innerText.trim() : null;
        let dealerRating = null;
        for (let i = 0; i < lines.length; i++) {
            if (/^\d+\.\d+$/.test(lines[i].trim())) {
                dealerRating = lines[i].trim();
                if (!dealerName && i > 0) {
                    const candidate = lines[i - 1].trim();
                    if (candidate && candidate !== dealRating && candidate !== location) {
                        dealerName = candidate;
                    }
                }
                break;
            }
        }

        const linkEl = card.querySelector('[data-card-href]');
        const href = linkEl ? linkEl.getAttribute('data-card-href') : null;

        // Everything below comes from data-vehicle-details. None of it is
        // visible card text, and the visible fields read more reliably here.
        let details = null;
        try { details = JSON.parse(card.getAttribute('data-vehicle-details') || 'null'); }
        catch { /* attribute missing or not JSON - fall back to text */ }

        const str = (v) => (typeof v === 'string' && v.trim()) ? v.trim() : null;
        const num = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : null; };

        let exteriorColor = str(details?.exteriorColor)
            || card.querySelector('[data-exteriorcolor]')?.getAttribute('data-exteriorcolor')
            || null;
        if (exteriorColor) exteriorColor = exteriorColor.trim().replace(/\b\w/g, c => c.toUpperCase());

        const jsonPrice = num(details?.price);
        if (jsonPrice) price = `$${jsonPrice.toLocaleString()}`;
        const jsonMileage = num(details?.mileage);
        if (jsonMileage) mileage = `${jsonMileage.toLocaleString()} mi.`;

        // MSRP is "0" on most used listings - only meaningful when set.
        const msrpValue = num(details?.msrp);
        const msrp = msrpValue ? `$${msrpValue.toLocaleString()}` : null;

        const jsonTitle = [details?.year, details?.make, details?.model, details?.trim]
            .map(str).filter(Boolean).join(' ');
        if (jsonTitle) title = jsonTitle;

        if (str(details?.seller?.dealerName)) dealerName = str(details.seller.dealerName);

        const DRIVETRAIN_ABBR = {
            'front-wheel drive': 'FWD', 'rear-wheel drive': 'RWD',
            'all-wheel drive': 'AWD', 'four-wheel drive': '4WD'
        };
        const drivetrainRaw = str(details?.drivetrain);
        const drivetrain = drivetrainRaw
            ? (DRIVETRAIN_ABBR[drivetrainRaw.toLowerCase()] || drivetrainRaw) : null;

        // Monthly payment has its own attribute, carrying a popover payload.
        let monthlyPayment = null;
        try {
            const mp = JSON.parse(card.querySelector('[data-monthly-payment]')?.getAttribute('data-monthly-payment') || 'null');
            monthlyPayment = str(mp?.label);
        } catch { /* absent on some cards - fine */ }

        // A price drop renders as a bare dollar amount on the line directly
        // after the price. Position is the only signal, so this is narrow:
        let priceDrop = null;
        const priceLineIndex = lines.findIndex(l => /^\$[\d,]+$/.test(l.trim()));
        if (priceLineIndex >= 0 && lines[priceLineIndex + 1]) {
            const next = lines[priceLineIndex + 1].trim();
            if (/^\$[\d,.]+K?$/i.test(next)) priceDrop = next;
        }

        // Award badges, matched against a bounded literal list rather than a
        // catch-all - a loose pattern would sweep up dealer taglines.
        const awards = lines.map(l => l.trim())
            .filter(l => /(american-made index|award finalist|award winner|best of|top safety pick|10best)/i.test(l));

        // Shipping/delivery, present on a minority of listings.
        const shipPrice = details?.shipPrice;
        const deliveryType = str(details?.deliveryType);
        let delivery = null;
        if (deliveryType || (typeof shipPrice === 'number')) {
            const cost = typeof shipPrice === 'number'
                ? (shipPrice > 0 ? `$${shipPrice.toLocaleString()}` : 'free') : null;
            delivery = [deliveryType, cost].filter(Boolean).join(' - ') || null;
        }

        const fullText = text.toLowerCase();
        const isOneOwner = fullText.includes('1-owner') || fullText.includes('one owner');
        const noAccidents = fullText.includes('no accident') || fullText.includes('clean');
        const personalUse = fullText.includes('personal use');
        const isCertified = details?.cpoIndicator === true
            || details?.banners?.cpo === true
            || fullText.includes('certified pre-owned');

        if (title) {
            results.push({
                title, price, priceDrop, msrp, monthlyPayment, mileage, dealRating,
                dealerName, dealerRating, location, href, exteriorColor,
                vin: str(details?.vin), year: str(details?.year),
                make: str(details?.make), model: str(details?.model),
                trim: str(details?.trim), bodyStyle: str(details?.bodyStyle),
                drivetrain, fuelType: str(details?.fuelType),
                isCertified, delivery, awards,
                thumbnail: str(details?.primaryThumbnail),
                isOneOwner, noAccidents, personalUse
            });
        }
    });
    return results;
}
"""


def _carscom_listing(item: dict[str, Any]) -> CarListing:
    return CarListing(
        source='Cars.com',
        title=item.get('title'),
        price=item.get('price'),
        price_drop=item.get('priceDrop'),
        msrp=item.get('msrp'),
        monthly_payment=item.get('monthlyPayment'),
        mileage=item.get('mileage'),
        deal_rating=item.get('dealRating'),
        dealer_name=item.get('dealerName'),
        dealer_rating=item.get('dealerRating'),
        location=item.get('location'),
        exterior_color=item.get('exteriorColor'),
        vin=item.get('vin'),
        year=item.get('year'),
        make=item.get('make'),
        model=item.get('model'),
        trim=item.get('trim'),
        body_style=item.get('bodyStyle'),
        drivetrain=item.get('drivetrain'),
        fuel_type=item.get('fuelType'),
        is_certified=bool(item.get('isCertified')),
        delivery=item.get('delivery'),
        awards=item.get('awards') or [],
        thumbnail=item.get('thumbnail'),
        url=f'https://www.cars.com{item["href"]}' if item.get('href') else None,
        is_one_owner=bool(item.get('isOneOwner')),
        no_accidents=bool(item.get('noAccidents')),
        personal_use=bool(item.get('personalUse')),
    )


async def scrape_carscom(
    params: SearchParams,
    max_results: int = 20,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> ScrapeResult:
    """Scrape Cars.com for car listings."""

    async def body(page: Page) -> ScrapeResult:
        # Build URL. See carscom_slug - single-word names (toyota-camry) survive
        # a plain toLowerCase(), which is why the test suite never caught this.
        qp: list[tuple[str, str]] = [('stock_type', 'used')]
        if params.make:
            qp.append(('makes[]', carscom_slug(params.make)))
        if params.make and params.model:
            qp.append(('models[]', f'{carscom_slug(params.make)}-{carscom_slug(params.model)}'))
        if params.zip:
            qp.append(('zip', params.zip))
        if params.year_min:
            qp.append(('year_min', str(params.year_min)))
        if params.year_max:
            qp.append(('year_max', str(params.year_max)))
        if params.price_max:
            qp.append(('list_price_max', str(params.price_max)))
        if params.mileage_max:
            qp.append(('mileage_max', str(params.mileage_max)))
        # Search radius in miles. Cars.com wants the literal "all" for
        # nationwide, not a large number.
        if params.max_distance is not None:
            qp.append(
                (
                    'maximum_distance',
                    'all' if params.max_distance == 0 else str(params.max_distance),
                )
            )
        # CarFax history filters
        if params.one_owner:
            qp.append(('one_owner', 'true'))
        if params.no_accidents:
            qp.append(('no_accidents', 'true'))
        if params.personal_use:
            qp.append(('personal_use', 'true'))

        url = 'https://www.cars.com/shopping/results/?' + urlencode(qp)
        logger.debug(f'Cars.com search URL: {url}')

        # Cars.com occasionally serves a card-less page to automated traffic
        # (probabilistic bot check, not a real "no results" case). Reload once
        # if the first attempt comes back empty.
        raw: list[dict[str, Any]] = []
        for attempt in range(1, 3):
            if attempt > 1:
                progress(send_progress, 'Cars.com returned no results, retrying...')
            logger.debug(f'Cars.com attempt {attempt}/2')
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            with contextlib.suppress(Exception):
                await page.wait_for_selector('fuse-card[id^="vehicle-card-"]', timeout=15000)
            raw = await page.evaluate(f'({_EXTRACT_CARSCOM_JS})()')
            logger.debug(f'Cars.com attempt {attempt} extracted {len(raw)} raw card(s)')
            if raw:
                break

        # Zero cards is ambiguous: a real empty result, a bot check, or a model
        # name Cars.com does not have. The filter vocabulary is already on the
        # page, so ask it which models this make actually offers before giving
        # up - that distinguishes the third case from the other two.
        suggestions: ModelSuggestions | None = None
        if not raw and params.make and params.model:
            try:
                options = await read_carscom_filter_options(page, 'Model')
            except Exception as err:  # noqa: BLE001
                logger.debug(f'Cars.com filter vocabulary unreadable: {err}')
                options = None
            if not options:
                logger.debug(
                    'Cars.com filter vocabulary absent - likely the bot-check page, not a bad model name'
                )
            else:
                matches = suggest_carscom_models(params.model, options)
                logger.debug(
                    f'Cars.com lists {len(options)} model(s) for {params.make}; '
                    f'{len(matches)} resemble {params.model!r}'
                )
                want = carscom_slug(params.model)
                exact = any(carscom_slug(m.name) == want for m in matches if m.name)
                if matches and not exact:
                    suggestions = ModelSuggestions(
                        make=params.make, input=params.model, options=matches, source='Cars.com'
                    )
                    logger.info(
                        f'Cars.com has no model {params.model!r} for {params.make} - '
                        f'closest: {", ".join(m.name for m in matches)}'
                    )

        listings = [_carscom_listing(item) for item in raw[:max_results]]
        progress(send_progress, f'Cars.com: found {len(listings)} listing(s)')
        return ScrapeResult(listings=listings, model_suggestions=suggestions)

    return await scrape('Cars.com', send_progress, body, config)
