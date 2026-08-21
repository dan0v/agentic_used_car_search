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
import re
from typing import Any, Final
from urllib.parse import urlparse

from ..logger import logger
from ..types import Config, DetailResult, ProgressSender
from ._base import browser_session, new_page, progress, start_heartbeat, wait_out_interstitial

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
