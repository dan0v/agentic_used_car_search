"""KBB scraper (browser, CloakBrowser).

Title/price/mileage/deal rating only. Shares the Cox Automotive markup with
Autotrader US (`[data-cmp="inventoryListing"]`) but with no dealer field and a
deal-rating line. Fragile - breaks when the site reshuffles.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from ..logger import logger
from ..types import CarListing, Config, ProgressSender, ScrapeResult, SearchParams
from ._base import progress, scrape

if TYPE_CHECKING:
    from playwright.async_api import Page


_EXTRACT_KBB_JS: Final[str] = r"""
() => {
    const results = [];
    const cards = document.querySelectorAll('[data-cmp="inventoryListing"]');
    cards.forEach(card => {
        const text = card.innerText;
        if (!text || text.length < 20) return;
        const lines = text.split('\n').filter(l => l.trim());
        let title = null, trim = null, price = null, mileage = null, dealRating = null;
        // The card links to the vehicle detail page (`/cars-for-sale/vehicle/<id>`);
        // without this the listing has no URL for the caller to investigate
        // further. When the site moves the link off the card (a markup reshuffle
        // made this null before), fall back to any detail URL anywhere on the
        // page by document order - cards and detail URLs appear in the same
        // order, so the Nth card pairs with the Nth unique detail URL.
        const linkEl = card.tagName === 'A' ? card : card.querySelector('a[href*="/cars-for-sale/vehicle/"]');
        let href = linkEl ? linkEl.getAttribute('href') : null;
        if (!href) {
            // De-dup by listing id: the same VDP appears several times with
            // different query strings/fragments (`?clickType=spotlight`,
            // `#purchaseConfidence`). Without this, positions off-by-one.
            const all = [...document.querySelectorAll('a[href*="/cars-for-sale/vehicle/"]')];
            const seen = new Set();
            const urls = [];
            for (const a of all) {
                const m = (a.getAttribute('href') || '').match(/\/cars-for-sale\/vehicle\/(\d+)/);
                if (m && !seen.has(m[1])) { seen.add(m[1]); urls.push(a.getAttribute('href')); }
            }
            href = urls[results.length] || null; // cards pushed so far
        }
        for (const line of lines) {
            const trimmed = line.trim();
            if (/^(19|20)\d{2}\s+\w+/.test(trimmed) && !title) { title = trimmed; continue; }
            if (title && !trim && /^[A-Z]{1,4}$/.test(trimmed)) { trim = trimmed; continue; }
            const priceMatch = trimmed.match(/^\$?([\d,]+)$/);
            if (priceMatch && !price && parseInt(priceMatch[1].replace(/,/g, '')) > 1000) {
                price = trimmed.startsWith('$') ? trimmed : `$${trimmed}`; continue;
            }
            if (/^\d+K?\s*mi$/i.test(trimmed) && !mileage) { mileage = trimmed; continue; }
            if (/^(good|great|fair|high)\s*(price|deal)/i.test(trimmed) && !dealRating) {
                dealRating = trimmed; continue;
            }
        }
        if (title) {
            if (trim) title = `${title} ${trim}`;
            results.push({
                title, price, mileage, dealRating,
                href
            });
        }
    });
    return results;
}
"""


async def scrape_kbb(
    params: SearchParams,
    max_results: int = 20,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> ScrapeResult:
    async def body(page: Page) -> ScrapeResult:
        make = params.make.lower() if params.make else ''
        model = params.model.lower() if params.model else ''
        zip_code = params.zip or '90210'

        url = 'https://www.kbb.com/cars-for-sale/all'
        if make:
            url += f'/{make}'
        if model:
            url += f'/{model}'
        url += f'/?zip={zip_code}'
        if params.year_min:
            url += f'&startYear={params.year_min}'
        if params.year_max:
            url += f'&endYear={params.year_max}'
        if params.price_max:
            url += f'&maxPrice={params.price_max}'
        if params.mileage_max:
            url += f'&maxMileage={params.mileage_max}'
        if params.max_distance:
            url += f'&searchRadius={params.max_distance}'
        logger.debug(f'KBB search URL: {url}')

        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(5)
        raw = await page.evaluate(f'({_EXTRACT_KBB_JS})()')

        listings = [
            CarListing(
                source='KBB',
                title=item.get('title'),
                price=item.get('price'),
                mileage=item.get('mileage'),
                deal_rating=item.get('dealRating'),
                url=f'https://www.kbb.com{item["href"]}' if item.get('href') else None,
            )
            for item in raw[:max_results]
        ]
        progress(send_progress, f'KBB: found {len(listings)} listing(s)')
        return ScrapeResult(listings=listings)

    return await scrape('KBB', send_progress, body, config)
