"""Autotrader US scraper (browser, CloakBrowser).

Fragile class-name/positional selectors - no equivalent of Cars.com's
`data-vehicle-details` JSON. Returns title/price/mileage/dealer only and breaks
often when the site reshuffles markup.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode

from ..logger import logger
from ..types import CarListing, Config, ProgressSender, ScrapeResult, SearchParams
from ._base import progress, scrape

if TYPE_CHECKING:
    from playwright.async_api import Page


_EXTRACT_AUTOTRADER_JS: Final[str] = r"""
() => {
    const results = [];
    const cards = document.querySelectorAll('[data-cmp="inventoryListing"], .inventory-listing');
    cards.forEach(card => {
        const titleEl = card.querySelector('h2, .text-bold');
        const priceEl = card.querySelector('[data-cmp="firstPrice"], .first-price');
        const mileageEl = card.querySelector('.text-subdued-lighter');
        const dealerEl = card.querySelector('.dealer-name, .text-subdued');
        const linkEl = card.querySelector('a[href*="/cars-for-sale/"]');
        const title = titleEl ? titleEl.innerText.trim() : null;
        const price = priceEl ? priceEl.innerText.trim() : null;
        let mileage = null;
        if (mileageEl) {
            const text = mileageEl.innerText;
            const match = text.match(/([\d,]+)\s*miles?/i);
            if (match) mileage = match[0];
        }
        if (title) {
            results.push({
                title, price, mileage,
                dealerName: dealerEl ? dealerEl.innerText.trim() : null,
                href: linkEl ? linkEl.getAttribute('href') : null
            });
        }
    });
    return results;
}
"""


async def scrape_autotrader(
    params: SearchParams,
    max_results: int = 20,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> ScrapeResult:
    async def body(page: Page) -> ScrapeResult:
        make = params.make.lower() if params.make else ''
        model = params.model.lower() if params.model else ''
        zip_code = params.zip or '90210'

        url = 'https://www.autotrader.com/cars-for-sale/all-cars'
        if make:
            url += f'/{make}'
        if model:
            url += f'/{model}'
        url += f'/beverly-hills-ca-{zip_code}'

        qp: list[tuple[str, str]] = []
        if params.year_min:
            qp.append(('startYear', str(params.year_min)))
        if params.year_max:
            qp.append(('endYear', str(params.year_max)))
        if params.price_max:
            qp.append(('maxPrice', str(params.price_max)))
        if params.mileage_max:
            qp.append(('maxMileage', str(params.mileage_max)))
        if params.max_distance:
            qp.append(('searchRadius', str(params.max_distance)))
        if qp:
            url += '?' + urlencode(qp)
        logger.debug(f'Autotrader search URL: {url}')

        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(5)
        raw = await page.evaluate(f'({_EXTRACT_AUTOTRADER_JS})()')

        listings = [
            CarListing(
                source='Autotrader',
                title=item.get('title'),
                price=item.get('price'),
                mileage=item.get('mileage'),
                dealer_name=item.get('dealerName'),
                url=f'https://www.autotrader.com{item["href"]}' if item.get('href') else None,
            )
            for item in raw[:max_results]
        ]
        progress(send_progress, f'Autotrader: found {len(listings)} listing(s)')
        return ScrapeResult(listings=listings)

    return await scrape('Autotrader', send_progress, body, config)
