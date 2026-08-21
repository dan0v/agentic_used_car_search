"""Motors.co.uk scraper (browser, CloakBrowser).

No API (it's the Cazoo Next.js stack). Title, price, mileage and a distance
line, scoped to a postcode. Mileage is shown rounded ("41.2k"); the site has no
server-side mileage filter, so `mileageMax` is applied client-side via
`apply_uk_filters`. A OneTrust consent banner is dismissed before the cards
are read.

The cards do not surface transmission or drivetrain, so the server skips this
source (with a warning) when either of those filters is requested.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode

from ..logger import logger
from ..types import CarListing, Config, ProgressSender, ScrapeResult, SearchParams
from ._base import accept_consent, apply_uk_filters, progress, scrape

if TYPE_CHECKING:
    from playwright.async_api import Page


_EXTRACT_MOTORS_UK_JS: Final[str] = r"""
() => {
    const results = [];
    const cards = document.querySelectorAll('.result-card');
    cards.forEach(card => {
        const titleEl = card.querySelector('.result-card__header h3');
        if (!titleEl) return;
        const title = titleEl.innerText.trim().replace(',', '');
        const subtitleEl = card.querySelector('.result-card__header h4');
        const linkEl = card.querySelector('.result-card__link');
        const text = card.innerText;
        const priceMatch = text.match(/£[\d,]+/);
        const mileageMatch = text.match(/(\d+(?:\.\d+)?k)\s*\n\s*Miles/i);
        const distanceMatch = text.match(/\d+\s*miles away/i);
        if (title) {
            results.push({
                title: subtitleEl ? `${title} ${subtitleEl.innerText.trim()}` : title,
                price: priceMatch ? priceMatch[0] : null,
                mileage: mileageMatch ? mileageMatch[1] : null,
                year: subtitleEl ? subtitleEl.innerText.trim() : null,
                location: distanceMatch ? distanceMatch[0] : null,
                href: linkEl ? linkEl.getAttribute('href') : null,
            });
        }
    });
    return results;
}
"""


async def scrape_motors_uk(
    params: SearchParams,
    max_results: int = 20,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> ScrapeResult:
    """Scrape Motors.co.uk. Title, price, mileage and a distance line, scoped
    to a postcode. Mileage is shown rounded ("41.2k"); the site has no
    server-side mileage filter, so mileageMax is applied client-side. A OneTrust
    consent banner is dismissed before the cards are read.
    """

    async def body(page: Page) -> ScrapeResult:
        postcode = re.sub(r'\s+', '', params.zip or 'SW1A1AA')
        qp: list[tuple[str, str]] = []
        if params.make:
            qp.append(('make', params.make))
        if params.model:
            qp.append(('model', params.model))
        qp.append(('postcode', postcode))
        if params.year_min:
            qp.append(('MinYear', str(params.year_min)))
        if params.year_max:
            qp.append(('MaxYear', str(params.year_max)))
        if params.price_max:
            qp.append(('MaxPrice', str(params.price_max)))
        url = 'https://www.motors.co.uk/search/car/?' + urlencode(qp)
        logger.debug(f'Motors.co.uk search URL: {url}')

        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(3)
        await accept_consent(page, ['#onetrust-accept-btn-handler'])
        await asyncio.sleep(4)
        raw = await page.evaluate(f'({_EXTRACT_MOTORS_UK_JS})()')

        filtered = apply_uk_filters(raw, params)
        listings = [
            CarListing(
                source='Motors.co.uk',
                title=item.get('title'),
                price=item.get('price'),
                mileage=item.get('mileage'),
                location=item.get('location'),
                url=f'https://www.motors.co.uk{item["href"]}' if item.get('href') else None,
            )
            for item in filtered[:max_results]
        ]
        progress(send_progress, f'Motors.co.uk: found {len(listings)} listing(s)')
        return ScrapeResult(listings=listings)

    return await scrape('Motors.co.uk', send_progress, body, config)
