"""Shared helpers for every site scraper: browser launch, heartbeat, the
`_scrape` skeleton, page-progress plumbing, UK filter normalisation, the
Cars.com slug rules, and the anti-bot interstitial waiter.

Per-site modules import what they need from here rather than reaching across
each other. Two transport strategies share this module: the browser scrapers
(Cars.com, Autotrader US, KBB, Motors.co.uk) use `launch_browser_async` +
`_scrape`; the direct-API scrapers (Autotrader UK, Cinch) use only the
heartbeat and progress helpers, with HTTP handled by `scrapers._http`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

import cloakbrowser

from ..logger import logger
from ..types import Config, ProgressSender, ScrapeResult, SearchParams

if TYPE_CHECKING:
    from playwright.async_api import Page


# ---------------------------------------------------------------------------
# Cars.com slug rules (US) - used by carscom.py
# ---------------------------------------------------------------------------


def carscom_slug(name: str) -> str:
    """Convert a human-readable make or model name into a Cars.com URL slug.

    Cars.com joins the make and model with a hyphen (`mercedes_benz-gle_450`),
    so every separator *inside* a name has to be an underscore instead. The
    mapping is mechanical - checked against the site's own filter vocabulary,
    all 98 makes and every model they list:

      space / hyphen / slash -> _   "Mercedes-Benz"     -> mercedes_benz
      & -> and                      "Town & Country"    -> town_and_country
      + -> plus                     "EQE 350+"          -> eqe_350_plus
      apostrophes vanish            "Li'l Red Express"  -> lil_red_express
      a period joining two           "ID.4"              -> id.4
      alphanumerics survives, a      "ID. Buzz"          -> id_buzz
      trailing one separates

    Deriving this beats shipping a lookup table: the vocabulary the page exposes
    truncates at 100 models per make, so a harvested list would be incomplete
    for exactly the makes people search most.

    Note the failure mode this guards against - Cars.com treats an unrecognized
    slug as *no filter at all* and serves a results page with zero cards, which
    is indistinguishable from the bot-check variant. A wrong slug therefore
    reads as scraping breakage rather than a bad query.
    """
    s = str(name)
    s = s.replace('&#39;', "'").replace('&amp;', '&').strip().lower()
    s = s.replace("'", '')
    s = s.replace('&', ' and ')
    s = s.replace('+', ' plus ')
    # A period only survives when followed by an alphanumeric, which is what
    # splits "ID.4" (id.4) from "ID. Buzz" (id_buzz). (It is never preceded by
    # one here - the input is already lower-cased with no separators.)
    s = re.sub(r'\.(?![a-z0-9])', ' ', s)
    # Everything that is not alphanumeric or a period becomes a separator.
    s = re.sub(r'[^a-z0-9.]+', '_', s)
    return s.strip('_')


# ---------------------------------------------------------------------------
# UK helpers - shared by autotrader_uk.py, motors_uk.py, cinch.py
# ---------------------------------------------------------------------------


def parse_price(s: str | None) -> int | None:
    """Parse a displayed price string into a comparable number. UK prices carry
    a pound sign and thousands separators; the digit strip handles both currencies.
    """
    if not s:
        return None
    digits = re.sub(r'[^\d]', '', s)
    return int(digits) if digits else None


def parse_mileage(s: str | None) -> int | None:
    """Parse a displayed mileage string. Motors.co.uk rounds to a "41.2k" form;
    the other sources spell it out (`68,148 miles`). Both shapes handled here.
    """
    if not s:
        return None
    m = re.match(r'([\d.]+)\s*k', s, re.I)
    if m:
        return round(float(m.group(1)) * 1000)
    digits = re.sub(r'[^\d]', '', s)
    return int(digits) if digits else None


def parse_year(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r'(19|20)\d{2}', s)
    return int(m.group(0)) if m else None


# Drivetrain phrasing differs by source: Autotrader UK's API/URL wants the
# long form with literal spaces ("Rear Wheel Drive"); Cinch's REST API wants
# the hyphenated display form ("Rear-wheel drive"). Abbreviations ("RWD") are
# what a caller is most likely to pass, so the table maps from those.
_DRIVETRAIN_NORMALISE: Final[dict[str, str]] = {
    'rwd': 'Rear Wheel Drive',
    'fwd': 'Front Wheel Drive',
    'awd': 'All Wheel Drive',
    '4wd': 'Four Wheel Drive',
}

# Cinch's `driveType` values, in the form its REST API expects (hyphenated,
# "drive" lower-cased). Used by normalise_drivetrain(form='cinch').
_DRIVETRAIN_CINCH: Final[dict[str, str]] = {
    'rwd': 'Rear-wheel drive',
    'fwd': 'Front-wheel drive',
    'awd': 'All-wheel drive',
    '4wd': 'Four-wheel drive',
}


def normalise_drivetrain(value: str | None, form: str = 'autotrader') -> str | None:
    """Normalise a drivetrain value to the phrasing a given source expects.

    `form='autotrader'` (default) -> "Rear Wheel Drive" (Autotrader UK GraphQL
    + URL filter). `form='cinch'` -> "Rear-wheel drive" (Cinch REST `driveType`).
    Accepts the abbreviation ("RWD"), a hyphenated variant ("Rear-Wheel-Drive"),
    or the long form ("Rear Wheel Drive"); returns None for an unknown form so
    a caller can decide whether to skip the filter or warn.
    """
    if not value:
        return None
    key = re.sub(r'[\s-]+', '', value).lower()
    table = _DRIVETRAIN_CINCH if form == 'cinch' else _DRIVETRAIN_NORMALISE
    if key in table:
        return table[key]
    # A long form the table doesn't list: normalise separators. Autotrader wants
    # Title Case With Spaces; Cinch wants hyphenated with a lower-case "drive".
    if form == 'cinch' and re.search(r'wheel[\s-]*drive', value, re.I):
        spaced = re.sub(r'[\s-]+', ' ', value).strip().title()
        return re.sub(r'\s*[Ww]heel\s*[Dd]rive', '-wheel drive', spaced)
    if re.search(r'wheel[\s-]*drive', value, re.I):
        return re.sub(r'[\s-]+', ' ', value).strip().title()
    return None


def apply_uk_filters(raw: list[dict[str, Any]], params: SearchParams) -> list[dict[str, Any]]:
    """Apply year/price/mileage filters client-side. The UK sources do not
    reliably accept every filter as a URL parameter (Motors.co.uk has no
    mileage filter, Cinch has neither postcode nor mileage), so what the site
    cannot narrow down is narrowed here.
    """
    out: list[dict[str, Any]] = []
    for item in raw:
        year = parse_year(item.get('year'))
        if params.year_min and year and year < params.year_min:
            continue
        if params.year_max and year and year > params.year_max:
            continue
        price = parse_price(item.get('price'))
        if params.price_max and price and price > params.price_max:
            continue
        mileage = parse_mileage(item.get('mileage'))
        if params.mileage_max and mileage and mileage > params.mileage_max:
            continue
        out.append(item)
    return out


async def accept_consent(page: Page, selectors: list[str]) -> None:
    """Accept a Sourcepoint/OneTrust-style cookie consent banner if shown.

    UK sites almost always gate the results behind one on first visit, and the
    banner can sit in a consent iframe rather than the top page, so both are
    checked. A missing banner is the normal case on a repeat visit - the swallow
    is intentional.
    """
    with contextlib.suppress(Exception):
        for sel in selectors:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                return
        for frame in page.frames:
            for sel in selectors:
                btn = await frame.query_selector(sel)
                if btn:
                    await btn.click()
                    return


# ---------------------------------------------------------------------------
# UK registration validation - used by mot.py
# ---------------------------------------------------------------------------


def normalise_registration(reg: str) -> str:
    """Normalise a UK registration. The service accepts either spaced or compact
    form; the URL uses compact, so spaces and lowercase are stripped here.
    """
    return re.sub(r'\s+', '', reg.strip().upper())


# UK registration formats. The GOV.UK MOT service only covers UK-registered
# vehicles, so an obviously non-UK plate (a US state-plate phrase, a VIN, a
# random string) should be rejected upfront rather than firing a browser at
# the GOV.UK page only to get a "no record" that looks indistinguishable from
# a real miss. The patterns cover the four UK formats since 1903:
#
#   current      AB12 CDE      (Sep 2001-, the overwhelming majority)
#   prefix       A123 BCD / A123 BCD  (Aug 1983 - Aug 2001)
#   suffix        ABC 123 D      (Feb 1963 - Aug 1982)
#   dateless    A1 / 1 A / AAB 123 / 123 AAB  (1903 - 1982)
#
# The check is deliberately permissive: a plate that *looks* like a UK plate
# passes through to the GOV.UK page, which is the source of truth. A plate
# that cannot be a UK plate is rejected here.
_UK_REG_PATTERNS: Final[list[re.Pattern[str]]] = [
    # Current (Sep 2001-): two letters, two year digits, three letters.
    re.compile(r'^[A-Z]{2}\d{2}[A-Z]{3}$'),
    # Prefix (Aug 1983 - Aug 2001): one letter, 1-3 digits, three letters.
    re.compile(r'^[A-Z]\d{1,3}[A-Z]{3}$'),
    # Suffix (Feb 1963 - Aug 1982): three letters, 1-3 digits, one letter.
    re.compile(r'^[A-Z]{3}\d{1,3}[A-Z]$'),
    # Dateless (1903 - 1982): up to three letters + up to three digits, or the
    # reverse. Northern Ireland: 2-3 area letters + up to 4 digits (e.g.
    # KZ 1234, BIL 1234). Jersey: J + up to 6 digits.
    re.compile(r'^[A-Z]{1,3}\d{1,3}$'),
    re.compile(r'^\d{1,3}[A-Z]{1,3}$'),
    re.compile(r'^[A-Z]{2,3}\d{1,4}$'),
    re.compile(r'^J\d{1,6}$'),
]


def is_uk_registration(reg: str | None) -> bool:
    """Return True if `reg` looks like a UK registration plate (post-2001
    current format, prefix, suffix, or dateless, including NI and Jersey).

    The MOT service is UK-only; this stops a non-UK plate from wasting a
    CloakBrowser launch only to come back with a misleading "no record". The
    check is permissive on purpose - the GOV.UK page is the source of truth,
    so anything that could plausibly be a UK plate is let through.
    """
    if not reg:
        return False
    normalised = normalise_registration(reg)
    if len(normalised) < 2 or len(normalised) > 8:
        return False
    return any(p.match(normalised) for p in _UK_REG_PATTERNS)


# ---------------------------------------------------------------------------
# Browser launch + heartbeat (browser scrapers only)
# ---------------------------------------------------------------------------

# CloakBrowser enforces a per-plan concurrent-session limit and raises
# `CloakBrowserLicenseError` the moment a second browser launches while another
# is still open. `asyncio.gather` in server.py fans out to every selected
# scraper at once, and every browser scraper used to launch independently, so
# running more than one browser-backed source (or the detail/MOT tool
# alongside a search) reliably hit the limit. Serialise the whole launch ->
# scrape -> close lifecycle behind one process-wide lock so only one
# CloakBrowser session exists at a time. The direct-API scrapers don't take
# this lock - they have no session to consume.
_BROWSER_LOCK = asyncio.Lock()


def launch_browser_async(config: Config | None = None) -> Any:
    """Launch a CloakBrowser Playwright Browser (async API). Every call site
    in the scraper layer is async; no sync launcher is kept.
    """
    key = config.cloakbrowser_key if config is not None else None
    return cloakbrowser.launch_async(headless=True, humanize=True, license_key=key)


def start_heartbeat(
    send_progress: ProgressSender | None, label: str, interval: float = 4.0
) -> Callable[[], None]:
    """Emit a progress message on a fixed interval while a slow scrape runs.

    Scrapes take anywhere from a few seconds to ~90s (retries included), well
    past most MCP clients' default tool-call timeout - periodic progress
    notifications are what reset that timeout clock, not just start/end
    messages. Returns a function that stops the heartbeat.
    """
    if send_progress is None:
        return lambda: None

    elapsed = 0.0
    running = True

    async def _beat() -> None:
        nonlocal elapsed
        while running:
            await asyncio.sleep(interval)
            elapsed += interval
            try:
                send_progress(f'{label} ({round(elapsed)}s)...')
            except Exception as err:  # noqa: BLE001
                logger.debug(f'Heartbeat send failed: {err}')

    task = asyncio.ensure_future(_beat())

    def stop() -> None:
        nonlocal running
        running = False
        if not task.done():
            task.cancel()

    return stop


async def new_page(browser: Any) -> Page:
    """Open a page with the standard desktop viewport used by every scraper."""
    page = await browser.new_page()
    await page.set_viewport_size({'width': 1920, 'height': 1080})
    return page


@contextlib.asynccontextmanager
async def browser_session(config: Config | None = None) -> Any:
    """Async context manager yielding a launched CloakBrowser, serialised
    behind `_BROWSER_LOCK` and guaranteed closed. For browser users that don't
    fit the `scrape()` skeleton (detail page, MOT history).
    """
    async with _BROWSER_LOCK:
        browser = await launch_browser_async(config)
        try:
            yield browser
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


async def scrape(
    label: str,
    send_progress: ProgressSender | None,
    body: Callable[[Page], Awaitable[ScrapeResult]],
    config: Config | None = None,
) -> ScrapeResult:
    """Run a scraper body against a fresh browser, wrapping any failure into a
    `RuntimeError` that preserves the cause chain. Every browser scraper shares
    this skeleton - launch, heartbeat, run body, close, rethrow with cause - so
    the per-site functions stay focused on URL building and extraction.
    """
    browser: Any = None
    stop_heartbeat = start_heartbeat(send_progress, f'Searching {label}')

    # Serialise browser usage: CloakBrowser caps concurrent sessions per plan,
    # and parallel scrapers each launching their own browser is exactly what
    # trips it. The lock spans close() so the session is fully released
    # before the next scraper's launch is attempted.
    async with _BROWSER_LOCK:
        try:
            browser = await launch_browser_async(config)
            page = await new_page(browser)
            return await body(page)
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(f'{label} scraping failed: {err}') from err
        finally:
            if browser is not None:
                with contextlib.suppress(Exception):
                    await browser.close()
            stop_heartbeat()


def progress(send_progress: ProgressSender | None, message: str) -> None:
    if send_progress is not None:
        send_progress(message)


# ---------------------------------------------------------------------------
# Anti-bot interstitial waiter (browser scrapers + detail page + MOT)
# ---------------------------------------------------------------------------

INTERSTITIAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'performing security verification|verifying you are (not a bot|human)'
    r'|just a moment|checking your browser|waiting for .* to respond',
    re.I,
)


async def wait_out_interstitial(
    page: Page, send_progress: ProgressSender | None = None, timeout_ms: int = 45000
) -> bool:
    """Block until an anti-bot interstitial hands off to the real page.

    The interstitial is short-lived (it says so: "Verification successful.
    Waiting for www.cars.com to respond") but outlives the page-load settle, so
    without this the extractor happily converts the challenge page to markdown
    and returns it as the listing.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    announced = False
    pattern_src = INTERSTITIAL_PATTERN.pattern

    while time.monotonic() < deadline:
        try:
            is_interstitial = await page.evaluate(
                """(patternSource) => {
                    const pattern = new RegExp(patternSource, 'i');
                    const text = (document.body?.innerText || '').slice(0, 2000);
                    // The real page is long; the challenge page is short with no content.
                    return text.length < 1200 && pattern.test(text);
                }""",
                pattern_src,
            )
        except Exception:  # noqa: BLE001
            is_interstitial = False
        if not is_interstitial:
            return True
        if not announced:
            progress(send_progress, 'Waiting out site bot check...')
            announced = True
        await asyncio.sleep(2.5)
    return False


__all__ = [
    'INTERSTITIAL_PATTERN',
    'accept_consent',
    'apply_uk_filters',
    'browser_session',
    'carscom_slug',
    'is_uk_registration',
    'launch_browser_async',
    'new_page',
    'normalise_drivetrain',
    'normalise_registration',
    'parse_mileage',
    'parse_price',
    'parse_year',
    'progress',
    'scrape',
    'start_heartbeat',
    'wait_out_interstitial',
]
