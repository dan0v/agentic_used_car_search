"""Detail-page tool (`get_listing_details`).

Drives a real browser at a caller-supplied URL, so the host is checked against
an allowlist first (`resolve_detail_source`) - without it the tool is an open
fetch proxy to private network addresses. Deliberately not a field-by-field
parser: these sites reshuffle their markup often, and the interesting part
(options/features, warranty, seller notes) has no stable shape. The page is
pruned (nav, ads, scripts, carousels) and converted to markdown with
`markdownify` (a custom `<dl>` converter pairs `<dt>`/`<dd>` into
`- **Term:** Value`). That is the whole design: no selectors to maintain when
the sites reshuffle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any, Final
from urllib.parse import urlparse

# The Autotrader UK detail page (unlike the search GraphQL gateway) takes any
# plausible UA; reuse _http's Chromium UA so both UK sources look consistent.
from ..logger import logger
from ..types import Config, DetailResult, ProgressSender
from ._base import browser_session, new_page, progress, start_heartbeat, wait_out_interstitial
from ._http import DEFAULT_USER_AGENT as _AT_UK_UA
from ._http import throttled_request

# Hosts a detail page may be fetched from. The tool takes a caller-supplied URL
# and drives a real browser at it, so the host is checked against this list
# first - otherwise the tool is an open fetch proxy pointed at whatever the
# caller names, including private network addresses.
DETAIL_HOSTS: Final[dict[str, str]] = {
    'cars.com': 'Cars.com',
    'autotrader.com': 'Autotrader',
    'kbb.com': 'KBB',
    # UK sources. The MOT tool already proves UK sites are reachable via
    # CloakBrowser; the detail conversion is generic (prune + markdown), so
    # adding these lets `get_listing_details` reach the listing pages the UK
    # search scrapers return.
    'autotrader.co.uk': 'Autotrader UK',
    'motors.co.uk': 'Motors.co.uk',
    'cinch.co.uk': 'Cinch',
    'ebay.co.uk': 'eBay Motors',
    'ebay.com': 'eBay Motors',
}


class UnsupportedHostError(ValueError):
    """Raised when `get_listing_details` is given a URL whose host is not on
    the allowlist. Catching this distinctly from a network/scrape failure lets
    the server report it as a client error (the URL was bad) rather than a
    tool failure.
    """


def resolve_detail_source(raw_url: str) -> tuple[str, str]:
    """Validate a detail-page URL against the allowlist and return
    (source_label, normalized_url). Raises `UnsupportedHostError` for an
    unknown host or bad scheme - this is the security boundary that stops the
    tool being an open fetch proxy to private network addresses.
    """
    try:
        parsed = urlparse(raw_url)
    except Exception as err:  # noqa: BLE001
        raise UnsupportedHostError(f'Not a valid URL: {raw_url}') from err
    if parsed.scheme not in ('https', 'http'):
        raise UnsupportedHostError(f'Unsupported URL scheme: {parsed.scheme}')
    host = (parsed.hostname or '').lower().removeprefix('www.')
    source = DETAIL_HOSTS.get(host)
    if not source:
        raise UnsupportedHostError(
            f'Unsupported host "{parsed.hostname}". Listing detail pages must be on: '
            f'{", ".join(DETAIL_HOSTS)}'
        )
    return source, parsed.geturl()


# ---------------------------------------------------------------------------
# Autotrader UK detail — SSR JSON harvest (no browser, no GraphQL)
# ---------------------------------------------------------------------------
#
# `autotrader.co.uk/car-details/{id}` is a separate SPA from the search app
# (`product-page-web`, not `sauron-search-results-app`). Its bundle contains
# *zero* calls to the `at-gateway` GraphQL endpoint, and the gateway's `Query`
# root has no advert/vehicle/detail field - so there is no GraphQL detail
# query to call. Instead the whole advert ships embedded in the HTML as
# `window.__staticRouterHydrationData = JSON.parse("{...}")` (loaderData ->
# 'car-details' -> aggregatorAdvert). Unlike Cars.com, `autotrader.co.uk` is
# NOT Cloudflare-walled, so a plain HTTP GET harvests richer structured data
# (incl. the registration plate and MOT/history) than the browser prune-of-
# markdown can guarantee. If the harvest fails for any reason (schema change,
# the page was cloudflared after all), we fall back to the generic browser
# path below.


class FormatSourceError(RuntimeError):
    """Couldn't extract structured data from an HTML-harvest detail page -
    fall back to the browser prune path."""


def _extract_at_uk_hydration(html: str) -> dict[str, Any]:
    """Parse `window.__staticRouterHydrationData = JSON.parse("{...}")` out of
    the Autotrader UK SSR HTML and return the `aggregatorAdvert` dict.

    The assignment's payload is a JS double-quoted string literal (so quotes
    inside are `\\"`), wrapping a JSON document (so what's *inside* the JS
    string is itself JSON). Decode the JS string layer first, then the JSON
    layer. The string contains no single unescaped `"`, so we can scan for the
    next unescaped quote to find the end of the literal.
    """
    i = html.find('__staticRouterHydrationData')
    if i < 0:
        raise FormatSourceError('no __staticRouterHydrationData marker in HTML')
    qpos = html.find('"', html.find('JSON.parse(', i))
    if qpos < 0:
        raise FormatSourceError('no quoted payload after JSON.parse(')
    start = qpos + 1
    j = start
    while True:
        k = html.find('"', j)
        if k < 0:
            raise FormatSourceError('unterminated hydration string literal')
        # count preceding backslashes: an escaped quote has an odd number
        n = 0
        p = k - 1
        while p >= 0 and html[p] == '\\':
            n += 1
            p -= 1
        if n % 2 == 0:
            break
        j = k + 1
    raw = html[start:k]
    try:
        # Decode JS string-literal escapes by re-using a JSON parser on the
        # quoted string (their escape sets are interchangeable here).
        decoded = json.loads(f'"{raw}"')
        data = json.loads(decoded)
    except json.JSONDecodeError as err:
        raise FormatSourceError(f'could not decode hydration JSON: {err}') from err
    loader = (data.get('loaderData') or {}).get('car-details') or {}
    agg = loader.get('aggregatorAdvert') or {}
    if not agg.get('id'):
        raise FormatSourceError('aggregatorAdvert missing id - unusual payload')
    return agg


def _kv_lines(rows: list[tuple[str, Any]]) -> list[str]:
    """Format (label, value) rows as `- **Label:** value`, skipping empties."""
    out = []
    for label, value in rows:
        if value in (None, '', []):
            continue
        if isinstance(value, list):
            text = '; '.join(str(x) for x in value if x)
        else:
            text = str(value).strip()
        if text:
            out.append(f'- **{label}:** {text}')
    return out


def _format_heading(agg: dict[str, Any]) -> str:
    """Title/subtitle - what the caller asked about."""
    heading = agg.get('heading') or {}
    title = (heading.get('title') or agg.get('title') or '').strip()
    subtitle = (heading.get('subTitle') or heading.get('subtitle') or '').strip()
    return f'{title} {subtitle}'.strip()


def _at_uk_harvest_to_markdown(agg: dict[str, Any], max_length: int) -> tuple[str, str, bool]:
    """Turn a harvested Autotrader UK `aggregatorAdvert` into the markdown body
    the tool returns, mirroring the rendering style of `_html_to_markdown`.
    Kept deliberately *not* exhaustive: pick the sections a caller wants
    (price, key spec, running costs, history, description, features) and drop
    the dozens of "associated adverts / finance / branding" payloads the page
    ships for UI rendering.
    """
    lines: list[str] = []
    heading = agg.get('heading') or {}
    gallery = agg.get('gallery') or {}
    details = agg.get('details') or {}
    description = agg.get('description') or {}
    history = agg.get('history') or {}
    running = agg.get('runningCosts') or {}
    features_dict = agg.get('featuresWithDisclaimer') or {}

    # Price block
    cash = (
        ((details.get('pricing') or {}).get('cashPrice') or {}).get('formattedAmount')
        or ((heading.get('priceBreakdown') or {}).get('price') or {}).get('priceFormatted')
        or gallery.get('price')
    )
    market = (details.get('pricing') or {}).get('marketPriceRating') or {}
    price_extra = market.get('value') if market.get('value') not in (None, 'NOANALYSIS') else None
    price_line = cash or ''
    if price_extra:
        price_line += f' ({price_extra})'
    if price_line:
        lines += ['## Price', '', f'- **Cash price:** {price_line}', '']

    # Key specification - the "spec strip" users actually read.
    key_specs = agg.get('keySpecification') or []
    spec_rows = [(s.get('label'), s.get('value')) for s in key_specs if isinstance(s, dict)]
    if spec_rows:
        lines += ['## Key specification', ''] + _kv_lines(spec_rows) + ['']

    # Full specs (Performance / Dimensions) - category->items.
    specs = agg.get('specs') or []
    spec_sections: list[str] = []
    for cat in specs:
        cat_name = cat.get('category') or 'Other'
        items = [(i.get('name'), i.get('value')) for i in (cat.get('items') or [])]
        item_lines = _kv_lines(items)
        if item_lines:
            spec_sections.append(f'### {cat_name}\n\n' + '\n'.join(item_lines))
    if spec_sections:
        lines += ['## Full specifications', '', '\n\n'.join(spec_sections), '']

    # Running costs (mpg, tax, insurance, CO2) - plain K/V under grouped keys.
    rc_rows: list[tuple[str, Any]] = []
    for group in running.get('runningCostList') or []:
        for item in group.get('items') or []:
            name = item.get('name')
            value = item.get('value')
            grp = group.get('key')
            if name and value:
                rc_rows.append((f'{grp} — {name}' if grp else name, value))
    if rc_rows:
        lines += ['## Running costs', ''] + _kv_lines(rc_rows) + ['']

    # History - MOT status, owners, service/history checks.
    hist_rows: list[tuple[str, Any]] = []
    mot = history.get('mot') or {}
    if mot.get('status'):
        hist_rows.append(('MOT status', mot.get('status')))
    owners = (history.get('ownersData') or {}).get('value')
    if owners:
        hist_rows.append(('Owners', owners))
    service = history.get('serviceHistory') or {}
    if service.get('description'):
        hist_rows.append(('Service history', service.get('description')))
    vc = history.get('vehicleCheck') or {}
    checks = vc.get('basicChecks') or vc.get('detailChecks') or []
    failed = [c.get('label') for c in checks if c.get('status') not in (None, 'PASSED')]
    if vc.get('statusText'):
        hist_rows.append(('History check', vc.get('statusText')))
    for f in failed[:5]:
        if f:
            hist_rows.append(('History note', f))
    if hist_rows:
        lines += ['## History', ''] + _kv_lines(hist_rows) + ['']

    # Registration plate - the one field the UK search API really can't give you.
    vreg = description.get('vehicleRegistration')
    if vreg:
        lines += ['## Registration', '', f'- {vreg}', '']

    # Description text (seller's notes). Keep as-is - it is the authored body
    # the harvest is trying *not* to re-interpret.
    desc_text = description.get('text') or []
    if isinstance(desc_text, str):
        desc_text = [desc_text]
    desc_body = '\n\n'.join(t for t in desc_text if t).strip()
    if desc_body:
        lines += ['## Description'] + [''] + [desc_body, '']

    # Features - "see all features" equivalent, minus the HTML/button dance.
    features = features_dict.get('features') or []
    feat_lines: list[str] = []
    for cat in features:
        cat_name = cat.get('title') or cat.get('category') or 'Other'
        item_names = [
            f'{i.get("name")}'
            + (f' ({i.get("type")})' if i.get('type') not in (None, 'Standard') else '')
            for i in (cat.get('items') or [])
            if i.get('name')
        ]
        if item_names:
            feat_lines.append(f'### {cat_name}\n\n' + '\n'.join(f'- {x}' for x in item_names))
    if feat_lines:
        lines += ['## Features', ''] + ['\n\n'.join(feat_lines), '']

    markdown = '\n'.join(lines).strip()
    truncated = len(markdown) > max_length
    if truncated:
        markdown = f'{markdown[:max_length]}\n\n*[truncated at {max_length} characters]*'
    return _format_heading(agg), markdown, truncated


async def _fetch_at_uk_details_http(
    url: str, max_length: int, send_progress: ProgressSender | None
) -> DetailResult:
    """Autotrader UK detail page via direct-HTTP harvest (no browser).

    The harvest is best-effort: `FormatSourceError` (or any HTTP failure) is
    re-raised so `fetch_listing_details` can fall back to the browser path.
    Progress/lgging stay consistent with the search scrapers.
    """
    progress(send_progress, 'Fetching Autotrader UK listing...')
    response = await throttled_request('GET', url, headers={'User-Agent': _AT_UK_UA})
    agg = _extract_at_uk_hydration(response.text)
    progress(send_progress, 'Extracting details...')
    title, markdown, truncated = _at_uk_harvest_to_markdown(agg, max_length)
    return DetailResult(
        url=url,
        source='Autotrader UK',
        title=title or None,
        markdown=markdown,
        truncated=truncated,
    )


# JS that prunes a detail page to the spec/features subtree. Mirrors the JS
# `fetchListingDetails` page.evaluate: strip script/style/nav/ads, drop
# aria-hidden, drop elements whose class/id matches the ad/nav pattern.
_PRUNE_DETAIL_JS: Final[str] = r"""
() => {
    const STRIP_TAGS = 'script, style, noscript, template, svg, iframe, canvas, video, audio, form, input, select, textarea, nav, header, footer';
    const STRIP_PATTERN = /(^|[-_ ])(ad|ads|advert|banner|carousel|chat|cookie|consent|breadcrumb|disclaimer|footer|header|modal|dialog|nav|newsletter|popup|promo|recommend|related|similar|social|sponsor|subscribe|survey|toast|tooltip)([-_ ]|$)/i;
    const root = document.querySelector('main') || document.querySelector('[role="main"]') || document.body;
    const clone = root.cloneNode(true);
    clone.querySelectorAll(STRIP_TAGS).forEach(el => el.remove());
    clone.querySelectorAll('[aria-hidden="true"], [hidden]').forEach(el => el.remove());
    clone.querySelectorAll('[class], [id], [data-linkname]').forEach(el => {
        const tokens = `${el.getAttribute('class') || ''} ${el.getAttribute('id') || ''}`;
        if (STRIP_PATTERN.test(tokens)) el.remove();
    });
    return {
        title: (document.querySelector('h1')?.innerText || document.title || '').trim(),
        html: clone.innerHTML
    };
}
"""

_EXPAND_FEATURES_JS: Final[str] = r"""
() => {
    const pattern = /^(see|show|view)\s+(all|more)\b|^all features\b|^more details\b/i;
    const clickable = [...document.querySelectorAll('button, [role="button"], summary')];
    let count = 0;
    for (const el of clickable.slice(0, 40)) {
        const label = (el.innerText || el.textContent || '').trim();
        if (pattern.test(label)) {
            try { el.click(); count++; } catch { /* detached or intercepted */ }
        }
    }
    for (const details of document.querySelectorAll('details')) details.open = true;
    return count;
}
"""


def _html_to_markdown(html: str, include_links: bool, include_images: bool) -> str:
    """Convert pruned detail HTML to markdown, reproducing the JS Turndown config.

    The custom `<dl>` rule pairs each `<dt>` with the following `<dd>` as
    `- **Term:** Value` - the one non-trivial conversion (Turndown has no default
    `<dl>` rule, neither does markdownify).
    """
    from bs4 import BeautifulSoup
    from markdownify import MarkdownConverter

    class _DetailConverter(MarkdownConverter):
        # Definition lists carry the spec table (VIN, drivetrain, engine, MPG,
        # colors). Pair each <dt> with the following <dd> so the pairing is not
        # flattened onto separate lines.
        def convert_dl(self, el, text, parent_tags):  # type: ignore[no-untyped-def]
            pairs: list[str] = []
            term: str | None = None
            for child in el.find_all(['dt', 'dd'], recursive=False):
                txt = child.get_text(' ', strip=True)
                if child.name == 'dt':
                    term = txt
                elif child.name == 'dd':
                    if term or txt:
                        pairs.append(f'- **{term or "Detail"}:** {txt}')
                    term = None
            return '\n\n' + '\n'.join(pairs) + '\n\n' if pairs else ''

        if not include_images:

            def convert_img(self, el, text, parent_tags):  # type: ignore[no-untyped-def]
                return ''

        if not include_links:
            # Keep the anchor text, drop the URL. A VDP links out dozens of
            # times; inlining those URLs costs more than it tells the caller.
            def convert_a(self, el, text, parent_tags):  # type: ignore[no-untyped-def]
                return text

    soup = BeautifulSoup(html, 'html.parser')
    converter = _DetailConverter(heading_style='ATX', bullets='-', code_language='')
    md = converter.convert_soup(soup)
    md = re.sub(r'[ \t]+$', '', md, flags=re.M)
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip()


async def fetch_listing_details(
    raw_url: str,
    options: dict[str, Any] | None = None,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> DetailResult:
    """Fetch a single listing's detail page and return it as markdown.

    Deliberately not a field-by-field parser: these sites reshuffle their markup
    often, and the interesting part (options/features, warranty, seller notes)
    has no stable shape. Handing the caller a pruned markdown rendering keeps
    every detail the page shows without a selector to maintain.
    """
    options = options or {}
    source, url = resolve_detail_source(raw_url)
    include_links = bool(options.get('includeLinks'))
    include_images = bool(options.get('includeImages'))
    max_length = int(options.get('maxLength') or 30000)

    logger.debug(f'Detail fetch: {url} (source: {source}, maxLength: {max_length})')
    stop_heartbeat = start_heartbeat(send_progress, f'Loading {source} listing')

    # Autotrader UK detail pages are not Cloudflare-walled and ship the whole
    # advert in `__staticRouterHydrationData` inside the SSR HTML. Plain HTTP
    # harvest is faster, richer (incl. the registration plate) and avoids a
    # CloakBrowser slot. On any failure, fall through to the browser path.
    if source == 'Autotrader UK':
        try:
            result = await _fetch_at_uk_details_http(url, max_length, send_progress)
        except FormatSourceError as err:
            logger.debug(
                f'Autotrader UK hydration harvest failed ({err}) - falling back to browser'
            )
        except Exception as err:  # noqa: BLE001
            logger.debug(
                f'Autotrader UK HTTP detail fetch failed ({err}) - falling back to browser'
            )
        else:
            stop_heartbeat()
            return result

    try:
        async with browser_session(config) as browser:
            page = await new_page(browser)

            await page.goto(url, wait_until='domcontentloaded', timeout=45000)
            with contextlib.suppress(Exception):
                await page.wait_for_selector('h1', timeout=15000)
            # Detail pages lazy-render the spec and features sections after the
            # title paints; a short settle beats racing an unknown selector.
            await asyncio.sleep(3)

            # Detail pages routinely answer with a bot-check interstitial first.
            # It clears itself and navigates on after a few seconds, so poll rather
            # than treating the interstitial as the page.
            cleared = await wait_out_interstitial(page, send_progress)
            if not cleared:
                logger.error(
                    f'{source} bot-check interstitial did not clear within 45s - '
                    'extracted content is probably the challenge page, not the listing'
                )

            # The full options list usually sits behind a "see all features" toggle.
            # Best-effort: expanding it is the whole point of the tool, but a missing
            # or renamed button must not fail the fetch.
            try:
                expanded = await page.evaluate(f'({_EXPAND_FEATURES_JS})()')
            except Exception:  # noqa: BLE001
                expanded = 0
            logger.debug(f'Expanded {expanded} "see all features" control(s)')
            if expanded:
                await asyncio.sleep(1.5)

            progress(send_progress, f'Extracting details from {source} listing...')

            extracted = await page.evaluate(f'({_PRUNE_DETAIL_JS})()')

        markdown = _html_to_markdown(extracted.get('html', ''), include_links, include_images)
        truncated = len(markdown) > max_length
        if truncated:
            markdown = f'{markdown[:max_length]}\n\n*[truncated at {max_length} characters]*'

        progress(send_progress, f'{source}: extracted {len(markdown)} characters')

        return DetailResult(
            url=url,
            source=source,
            title=extracted.get('title'),
            markdown=markdown,
            truncated=truncated,
        )
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f'Failed to fetch listing details from {source}: {err}') from err
    finally:
        stop_heartbeat()
