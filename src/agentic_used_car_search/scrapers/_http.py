"""Shared HTTP client + per-domain throttle for the direct-API scrapers.

Autotrader UK (GraphQL) and Cinch (REST) are reached via plain HTTP instead of
a browser - they are unauthenticated and return richer structured data than the
HTML cards. The browser scrapers (Cars.com, Autotrader US, KBB, Motors.co.uk)
stay on CloakBrowser because those sites are anti-bot-walled or have no API.

Direct HTTP is fast enough to hammer a host by accident, so every request goes
through a per-domain throttle: a minimum interval between consecutive requests
to the same netloc, enforced with an `asyncio.Lock` + a monotonic deadline.
Concurrent `asyncio.gather` fan-out therefore serialises per host while
different hosts still run in parallel. The browser scrapers are implicitly
throttled by browser-launch time (~seconds per call) and do not use this.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from ..logger import logger

# Default minimum seconds between consecutive requests to the same host. The
# browser scrapers wait 3-5s per page load already, so this only governs the
# direct-API sources. 1.0s is conservative enough to look human without making
# a single-source search slow.
DEFAULT_MIN_INTERVAL: float = 1.0

# A real-browser UA. Both APIs are happy with any plausible UA; this one
# matches what CloakBrowser's Chromium identifies as, so requests look
# consistent with the browser scrapers.
DEFAULT_USER_AGENT: str = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
)

# Maximum retries on a transient status (429/5xx). Exponential backoff with a
# cap; a 429 means the host is asking us to slow down, so we back off rather
# than hammer and surface the failure in the **Errors:** footer if it persists.
MAX_RETRIES: int = 3
RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class HttpThrottle:
    """Per-domain minimum-interval gate.

    Each netloc has its own lock and "next allowed time". A caller acquires its
    host's lock, sleeps until the deadline, sets the next deadline into the
    future, and releases. This serialises concurrent requests to the same host
    while leaving different hosts free to proceed in parallel.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL) -> None:
        self._min_interval = min_interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, netloc: str) -> asyncio.Lock:
        # The lock map is created lazily; guard its mutation so two coroutines
        # racing on a new host don't end up with two locks for it.
        async with self._locks_guard:
            lock = self._locks.get(netloc)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[netloc] = lock
            return lock

    async def acquire(self, netloc: str) -> None:
        """Block until at least `min_interval` has elapsed since the last
        request to `netloc`, then mark this slot as used.
        """
        lock = await self._lock_for(netloc)
        async with lock:
            now = time.monotonic()
            deadline = self._next_allowed.get(netloc, 0.0)
            if now < deadline:
                await asyncio.sleep(deadline - now)
            self._next_allowed[netloc] = time.monotonic() + self._min_interval


# A single shared throttle instance for the process. Module-level so the
# server's concurrent tool calls and `asyncio.gather` fan-out share one gate.
_throttle = HttpThrottle()


async def get_async_client() -> httpx.AsyncClient:
    """Return a process-wide `httpx.AsyncClient` with keep-alive and a sane
    timeout. Reusing one client across calls keeps connection pools warm; the
    browser scrapers don't use this.
    """
    global _client  # noqa: PLW0603 - single shared client, lazily built
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={'User-Agent': DEFAULT_USER_AGENT},
            follow_redirects=True,
        )
    return _client


_client: httpx.AsyncClient | None = None


async def throttled_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Make an HTTP request through the per-domain throttle, with bounded
    retry on 429/5xx. Raises `httpx.HTTPStatusError` on a non-retryable error
    status so the caller can catch it and surface it in the **Errors:** footer.
    """
    netloc = httpx.URL(url).host or url
    client = await get_async_client()

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        await _throttle.acquire(netloc)
        try:
            response = await client.request(
                method, url, headers=headers, json=json_body, params=params
            )
        except httpx.HTTPError as err:
            last_exc = err
            logger.debug(f'HTTP {method} {url} transport error (attempt {attempt}): {err}')
            await _backoff(attempt)
            continue

        if response.status_code in RETRY_STATUS_CODES:
            last_exc = httpx.HTTPStatusError(
                f'{response.status_code} {response.reason_phrase}',
                request=response.request,
                response=response,
            )
            logger.debug(
                f'HTTP {url} returned {response.status_code} (attempt {attempt}) - backing off'
            )
            await _backoff(attempt)
            continue

        # Raise for 4xx/5xx so the caller sees a real exception; the scraper
        # wraps it into a RuntimeError with the cause chain intact.
        response.raise_for_status()
        return response

    # Exhausted retries - re-raise the last transport/status error.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f'Exhausted retries for {url} without a response')


async def _backoff(attempt: int) -> None:
    """Exponential backoff: 1s, 2s, 4s ... capped at 8s."""
    delay = min(2 ** (attempt - 1), 8)
    await asyncio.sleep(delay)


async def close_client() -> None:
    """Close the shared client. Called on server shutdown; safe to call when
    no client was ever built.
    """
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Autotrader UK app-version cache
# ---------------------------------------------------------------------------

# The GraphQL gateway requires `x-sauron-app-version`, which changes per deploy
# of the SPA. Fetch the /car-search page once, scrape the version out of the
# inline `window.AT_SPA_JS_CONFIG` JSON, and cache it with a TTL so repeated
# search calls don't each re-fetch the page. If the page fetch fails or the
# version can't be parsed, fall back to a baked-in constant (the SPA keeps
# working for a while on an old version before the gateway rejects it).
_AT_UK_VERSION_TTL: float = 3600.0  # 1 hour
_AT_UK_VERSION_FALLBACK: str = '4624a08064'
_at_uk_version: str | None = None
_at_uk_version_expires: float = 0.0
_at_uk_version_lock = asyncio.Lock()

# `window.AT_SPA_JS_CONFIG = JSON.parse('{...appVersion...}')` - the inline
# script on every /car-search page. `appVersion` is the value we need.
_AT_UK_VERSION_RE: re.Pattern[str] = re.compile(r'"appVersion"\s*:\s*"([^"]+)"', re.I)


async def get_autotrader_uk_version() -> str:
    """Return the current Autotrader UK SPA app version, cached with a TTL.

    The version is scraped from the inline `window.AT_SPA_JS_CONFIG` JSON on
    the /car-search page (it ships in the SSR HTML, so a single GET is enough
    - no JS execution). On any failure, returns a baked-in fallback so a
    search still goes through rather than erroring on a missing header.
    """
    global _at_uk_version, _at_uk_version_expires
    now = time.monotonic()
    if _at_uk_version is not None and now < _at_uk_version_expires:
        return _at_uk_version

    async with _at_uk_version_lock:
        # Re-check inside the lock - another coroutine may have refreshed it.
        now = time.monotonic()
        if _at_uk_version is not None and now < _at_uk_version_expires:
            return _at_uk_version

        try:
            response = await throttled_request('GET', 'https://www.autotrader.co.uk/car-search')
            match = _AT_UK_VERSION_RE.search(response.text)
            if match:
                _at_uk_version = match.group(1)
                _at_uk_version_expires = now + _AT_UK_VERSION_TTL
                logger.debug(f'Autotrader UK app version: {_at_uk_version}')
                return _at_uk_version
            logger.debug('Autotrader UK appVersion not found in page HTML')
        except Exception as err:  # noqa: BLE001
            logger.debug(f'Autotrader UK version fetch failed: {err}')

        _at_uk_version = _AT_UK_VERSION_FALLBACK
        _at_uk_version_expires = now + _AT_UK_VERSION_TTL
        return _at_uk_version


__all__ = [
    'DEFAULT_MIN_INTERVAL',
    'DEFAULT_USER_AGENT',
    'HttpThrottle',
    'close_client',
    'get_async_client',
    'get_autotrader_uk_version',
    'throttled_request',
]
