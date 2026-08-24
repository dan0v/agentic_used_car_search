"""Scraper layer: one module per site + shared helpers.

Two transport strategies coexist here:
- **Browser scrapers** (`carscom`, `autotrader_us`, `kbb`, `motors_uk`, `detail`,
  `mot`) use CloakBrowser via `_base.launch_browser_async` + `_base.scrape`,
  serialised behind a process-wide `_BROWSER_LOCK` because CloakBrowser caps
  concurrent sessions per plan. These sites are anti-bot-walled or have no
  discoverable API.
- **Direct-API scrapers** (`autotrader_uk`, `cinch`) use plain HTTP via
  `_http.throttled_request` with a per-domain throttle. Unauthenticated, richer
  structured data, no anti-bot.

`page.evaluate` payloads stay as JS string literals in the browser scrapers -
Playwright Python runs them in the browser, so they cannot be rewritten in
Python. See CLAUDE.md and docs/SITE_APIS.md.

This package re-exports the public surface that `server.py` and tests import, so
`from agentic_used_car_search.scrapers import scrape_carscom` works without callers
needing to know the per-site module layout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..types import CarListing, Config, ProgressSender, ScrapeResult, SearchParams
from ._base import carscom_slug, is_uk_registration, normalise_drivetrain, normalise_registration
from .autotrader_uk import scrape_autotrader_uk
from .autotrader_us import scrape_autotrader
from .carscom import scrape_carscom, suggest_carscom_models
from .cinch import scrape_cinch
from .detail import (
    DETAIL_HOSTS,
    UnsupportedHostError,
    fetch_listing_details,
    resolve_detail_source,
)
from .ebay_uk import scrape_ebay_uk
from .kbb import scrape_kbb
from .mot import MOT_HOST, fetch_mot_history
from .motors_uk import scrape_motors_uk


async def search_all_sources(
    params: SearchParams,
    max_results_per_source: int = 10,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Aggregate every US source in parallel. Exported but unused by the
    server (which fans out to the caller's selected sources directly); kept for
    ad-hoc scripts.
    """
    results: dict[str, Any] = {'listings': [], 'errors': []}
    scrapers = [
        ('Cars.com', scrape_carscom),
        ('Autotrader', scrape_autotrader),
        ('KBB', scrape_kbb),
    ]

    async def _run(
        label: str, fn: Callable[..., Awaitable[ScrapeResult]]
    ) -> tuple[str, list[CarListing], str | None]:
        try:
            result = await fn(params, max_results_per_source, send_progress, config)
            return label, result.listings, None
        except Exception as err:  # noqa: BLE001
            return label, [], str(err)

    outcomes = await asyncio.gather(*[_run(label, fn) for label, fn in scrapers])
    for label, listings, error in outcomes:
        results['listings'].extend(listings)
        if error:
            results['errors'].append({'source': label, 'error': error})
    return results


__all__ = [
    'CarListing',
    'DETAIL_HOSTS',
    'MOT_HOST',
    'UnsupportedHostError',
    'carscom_slug',
    'fetch_listing_details',
    'fetch_mot_history',
    'is_uk_registration',
    'normalise_drivetrain',
    'normalise_registration',
    'resolve_detail_source',
    'scrape_autotrader',
    'scrape_autotrader_uk',
    'scrape_carscom',
    'scrape_cinch',
    'scrape_ebay_uk',
    'scrape_kbb',
    'scrape_motors_uk',
    'search_all_sources',
    'suggest_carscom_models',
]
