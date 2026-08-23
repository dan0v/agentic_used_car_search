"""MCP server: tool registration, request handling, progress, output formatting.

Uses the MCP SDK's lowlevel `Server` (mcp 2.0). Three tools on stdio, no state,
no parsing - `server.py` maps tool arguments onto a `SearchParams` dataclass,
fans out to the selected scrapers with `asyncio.gather`, and concatenates
`listing.format()` output into one markdown text block. A failing scraper is
caught per-source and reported in an `**Errors:**` footer rather than failing the
call.

Handlers are built as closures over the startup `Config` (set once in `run`),
rather than reading a module global, so the server's country default and
CloakBrowser key are explicit dependencies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from .logger import attach_loop_exception_handler, install_global_handlers, logger
from .scrapers import (
    fetch_listing_details,
    fetch_mot_history,
    normalise_drivetrain,
    scrape_autotrader,
    scrape_autotrader_uk,
    scrape_carscom,
    scrape_cinch,
    scrape_ebay_uk,
    scrape_kbb,
    scrape_motors_uk,
)
from .types import (
    CarListing,
    Config,
    ModelSuggestions,
    MotRecord,
    ProgressSender,
    ScrapeResult,
    SearchParams,
)

install_global_handlers()

SERVER_NAME = 'car-deals-mcp'
SERVER_VERSION = '1.0.0'

# A search scraper: takes (params, max_results, send_progress, config) and
# returns a ScrapeResult. The server fans these out in parallel and aggregates.
# `config` is Optional on the scraper side so they stay callable from scripts
# with no startup config.
SearchScraper = Callable[
    [SearchParams, int, ProgressSender | None, Config | None],
    Awaitable[ScrapeResult],
]

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_SEARCH_TOOL = types.Tool(
    name='search_car_deals',
    description=(
        'Search for car deals across multiple sources. US sources (Cars.com, '
        'Autotrader, KBB) return price (with any price drop), estimated monthly '
        'payment, mileage, exterior color, trim, body style, drivetrain, fuel '
        'type, VIN, deal rating, Certified Pre-Owned and CARFAX badges, dealer '
        'name and star rating, location and distance, a photo, and a link. UK '
        'sources (Autotrader UK, Motors.co.uk, Cinch, eBay Motors) return '
        'title, price (GBP), mileage, and location/distance where available. '
        'Use get_listing_details on a listing URL for the full options and '
        'features list.'
    ),
    inputSchema={
        'type': 'object',
        'properties': {
            'make': {
                'type': 'string',
                'description': 'Car manufacturer (e.g., Toyota, Honda, Ford).',
            },
            'model': {'type': 'string', 'description': 'Car model (e.g., Camry, Civic, F-150).'},
            'country': {
                'type': 'string',
                'enum': ['US', 'UK'],
                'description': (
                    'Country to search in. "US" (default; the original and most '
                    'fully supported scope) searches Cars.com, Autotrader, KBB; '
                    '"UK" searches Autotrader UK, Motors.co.uk, Cinch, eBay '
                    'Motors. Selects the default `sources`, the default `zip`, '
                    'and the currency shown. Add new entries here alongside '
                    'matching per-country scrapers and source defaults to '
                    'support further regions.'
                ),
            },
            'zip': {
                'type': 'string',
                'description': 'Location-based search code. US: a ZIP code (default: 90210). UK: a postcode (default: SW1A 1AA).',
            },
            'maxDistance': {
                'type': 'integer',
                'description': 'Search radius in miles from the ZIP/postcode. US: from the ZIP code (e.g. 25, 50, 100, 500; use 0 for nationwide). UK (Autotrader UK): from the postcode (0 means whole UK). Default: the site default, roughly 30-50 miles. UK sources Motors.co.uk and Cinch do not honour this.',
            },
            'yearMin': {
                'type': 'integer',
                'description': 'Minimum model year (applied server-side where supported, otherwise client-side).',
            },
            'yearMax': {
                'type': 'integer',
                'description': 'Maximum model year (applied server-side where supported, otherwise client-side).',
            },
            'priceMax': {
                'type': 'integer',
                'description': 'Maximum price. US: in USD. UK: in GBP.',
            },
            'mileageMax': {
                'type': 'integer',
                'description': 'Maximum mileage. Applied client-side for sources that lack a server-side mileage filter (e.g. Motors.co.uk, Cinch).',
            },
            'maxResults': {
                'type': 'integer',
                'description': 'Maximum results per source (default: 10).',
            },
            'sources': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Sources to search. US: "cars.com", "autotrader", "kbb". UK: "autotrader-uk", "motors", "cinch", "ebay". Default: "cars.com" (US) or "autotrader-uk" (UK).',
            },
            'oneOwner': {
                'type': 'boolean',
                'description': 'US only. Filter for CARFAX 1-Owner vehicles. Ignored by UK sources.',
            },
            'noAccidents': {
                'type': 'boolean',
                'description': 'US only. Filter for vehicles with no accidents or damage reported. Ignored by UK sources.',
            },
            'personalUse': {
                'type': 'boolean',
                'description': 'US only. Filter for vehicles used for personal use only (not rental/fleet). Ignored by UK sources.',
            },
            'transmission': {
                'type': 'string',
                'description': 'UK only. Filter by gearbox (e.g. "Manual", "Automatic"). Applied server-side by Autotrader UK (GraphQL) and Cinch (REST). Motors.co.uk does not surface transmission - when this is set, that source is skipped with a warning rather than returning unfiltered listings.',
            },
            'drivetrain': {
                'type': 'string',
                'description': 'UK only. Filter by drivetrain. Accepts the long form ("Rear Wheel Drive") or the abbreviation ("RWD", "FWD", "AWD", "4WD"); normalised to the form each source expects. Applied server-side by Autotrader UK and Cinch. Motors.co.uk is skipped with a warning when this is set.',
            },
        },
        'required': [],
    },
)

_DETAIL_TOOL = types.Tool(
    name='get_listing_details',
    description=(
        'Fetch the full detail page for a single car listing and return it as '
        'markdown. Use the `url` from a search_car_deals result. Returns '
        'everything the listing page shows - VIN, trim, engine, drivetrain, '
        'MPG, exterior/interior colors, the full options and features list, '
        'warranty, vehicle history, seller notes and dealer info - which the '
        'search results do not include.'
    ),
    inputSchema={
        'type': 'object',
        'properties': {
            'url': {
                'type': 'string',
                'description': 'Listing detail page URL, as returned by search_car_deals. Must be on cars.com, autotrader.com, kbb.com, autotrader.co.uk, motors.co.uk, cinch.co.uk, or ebay.co.uk',
            },
            'includeLinks': {
                'type': 'boolean',
                'description': 'Keep hyperlink URLs in the markdown (default: false - link text is kept either way).',
            },
            'includeImages': {
                'type': 'boolean',
                'description': 'Keep image references in the markdown (default: false).',
            },
            'maxLength': {
                'type': 'integer',
                'description': 'Truncate the markdown at this many characters (default: 30000).',
            },
        },
        'required': ['url'],
    },
)

_MOT_TOOL = types.Tool(
    name='check_mot_history',
    description=(
        'UK only. Check the MOT history of a UK-registered vehicle via the '
        'GOV.UK service (check-mot.service.gov.uk) and surface any outstanding '
        'issues: the most recent test result, outstanding dangerous/major/minor '
        'defects and advisories, the MOT expiry date, and any active safety '
        'recalls. Intended as a follow-up to a UK `search_car_deals` call: run '
        "a UK search, then pass a resulting listing's registration plate here "
        'to assess the car before buying. The GOV.UK service only covers '
        'UK-registered vehicles, so a registration that does not look like a '
        'UK plate is rejected upfront. Note that Autotrader UK cards do not '
        'expose the plate; Cinch results do (via `vrm`/`fullRegistration`), so '
        'a Cinch listing can be piped straight in. Takes a UK registration '
        '(number plate), e.g. "YL08 NNV" or "YL08NNV".'
    ),
    inputSchema={
        'type': 'object',
        'properties': {
            'registration': {
                'type': 'string',
                'description': 'UK vehicle registration (number plate), with or without spaces. Must look like a UK plate (current/prefix/suffix/dateless format). e.g. "YL08 NNV" or "YL08NNV". Usually taken from a UK search_car_deals result.',
            },
        },
        'required': ['registration'],
    },
)

TOOLS = [_SEARCH_TOOL, _DETAIL_TOOL, _MOT_TOOL]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_result(text: str, is_error: bool = False) -> types.CallToolResult:
    # In mcp 2.0 `isError` is a required bool (pydantic rejects None). The JS
    # used `undefined` for success; here success is an explicit `False`.
    return types.CallToolResult(
        content=[types.TextContent(type='text', text=text)],
        isError=is_error,
    )


def _extract_progress_token(params: Any) -> str | int | None:
    """Pull the progress token out of a `tools/call` request.

    The spec puts it in `params._meta.progressToken` - a sibling of `name` and
    `arguments`, not inside the arguments object. The SDK normalizes `_meta` to a
    snake_case dict, so we read `progress_token`. Progress notifications are the
    standard way to keep a client's request-timeout clock alive on a slow tool
    call, which this one routinely is (real-site scraping, up to ~90s across
    retries) - without a token there's nowhere to send them.
    """
    meta = getattr(params, 'meta', None)
    if isinstance(meta, dict):
        token = meta.get('progress_token') or meta.get('progressToken')
        if isinstance(token, (str, int)):
            return token
    return None


def _make_send_progress(ctx: Any, progress_token: str | int | None) -> ProgressSender | None:
    """Build a `send_progress(message)` callable the scrapers can call.

    The MCP spec requires a strictly-increasing numeric `progress` field on
    every notification - clients validate incoming notifications against that
    schema and silently drop ones missing it. Returns None when there is no
    token (heartbeats then do nothing, scrapers stay callable from scripts).
    """
    if progress_token is None:
        return None
    session = ctx.session
    counter = {'n': 0}

    def send(message: str) -> None:
        counter['n'] += 1
        # Fire-and-forget: the heartbeat calls this from sync code. Schedule the
        # coroutine on the running loop so it doesn't block the scraper. The
        # loop is guaranteed running here - this only fires inside a tool call.
        try:
            asyncio.ensure_future(
                session.send_progress_notification(
                    progress_token=progress_token,
                    progress=counter['n'],
                    message=message,
                )
            )
        except Exception as err:  # noqa: BLE001
            logger.debug(f'Progress notification failed: {err}')

    return send


def _wrap(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap a request handler so any exception it lets escape is written to
    stderr before being re-thrown to the SDK. The SDK turns a thrown error into
    a JSON-RPC error response, which means the client learns about the failure
    and the server operator does not. Re-throwing after logging preserves the
    client-facing behaviour exactly.
    """

    async def wrapped(ctx: Any, params: Any) -> Any:
        try:
            return await handler(ctx, params)
        except Exception as err:  # noqa: BLE001
            tool = getattr(params, 'name', 'unknown')
            logger.error(f'Unhandled error in tool "{tool}"', err)
            raise

    return wrapped


# ---------------------------------------------------------------------------
# Tool handlers (built as closures over the startup Config)
# ---------------------------------------------------------------------------


def _build_handlers(
    config: Config,
) -> tuple[Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]]:
    """Build the tools/list and tools/call handlers closed over `config`."""

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOLS)

    async def call_tool(ctx: Any, params: Any) -> types.CallToolResult:
        name = params.name
        args = params.arguments or {}
        progress_token = _extract_progress_token(params)

        logger.info(f'Tool call: {name}')
        logger.debug(f'Arguments: {logger.preview(args)}')
        # A missing progress token silently disables the keepalive that stops
        # slow scrapes from tripping the client's tool-call timeout, and there
        # is no other symptom until a call mysteriously times out.
        logger.debug(
            'No progressToken on this request - progress notifications disabled'
            if progress_token is None
            else f'Progress token: {progress_token!r}'
        )

        send_progress = _make_send_progress(ctx, progress_token)

        handlers = {
            'search_car_deals': _handle_search,
            'get_listing_details': _handle_detail,
            'check_mot_history': _handle_mot,
        }
        handler = handlers.get(name)
        if handler is not None:
            return await handler(args, send_progress, config)
        # Reaching here means the SDK dispatched an unknown tool to us.
        logger.error(f'Unknown tool: {name}')
        raise ValueError(f'Unknown tool: {name}')

    return list_tools, call_tool


async def execute_search(
    params: SearchParams,
    sources: list[str],
    max_results: int,
    send_progress: ProgressSender | None,
    config: Config,
) -> tuple[list[CarListing], list[ModelSuggestions], list[str], list[str], bool]:
    skipped_filters: list[str] = []
    unsupported: set[str] = set()
    if params.transmission or params.drivetrain:
        unsupported.add('motors')
    if params.drivetrain:
        unsupported.add('ebay')
    for key in unsupported:
        if key in sources:
            skipped_filters.append(key)
    filtered_sources = [s for s in sources if s not in unsupported]
    if skipped_filters and not filtered_sources:
        return [], [], [], skipped_filters, True

    what = f'{params.make} {params.model}'.strip() or 'all cars'
    logger.info(f'Searching for {what} in {params.zip} ({params.country})')
    logger.info(f'Sources: {", ".join(filtered_sources)}, Max: {max_results}')
    if skipped_filters:
        logger.info(
            f'Skipped {", ".join(skipped_filters)} (no transmission/drivetrain filter support)'
        )
    logger.debug(f'Resolved params: {logger.preview(params)}')
    if send_progress:
        send_progress(f'Searching {", ".join(filtered_sources)} for {what}...')

    async def _run_scraper(label: str, fn: SearchScraper) -> dict[str, Any]:
        logger.info(f'Starting {label} scraper...')
        started = asyncio.get_running_loop().time()

        def elapsed() -> str:
            return f'{(asyncio.get_running_loop().time() - started):.1f}s'

        try:
            result = await fn(params, max_results, send_progress, config)
            logger.info(f'{label} returned {len(result.listings)} listings')
            logger.debug(f'{label} finished in {elapsed()} (requested max {max_results})')
            return {'source': label, 'result': result}
        except Exception as err:  # noqa: BLE001
            logger.error(f'{label} error', err)
            logger.debug(f'{label} failed after {elapsed()}')
            return {'source': label, 'error': str(err), 'result': ScrapeResult()}

    scraper_map: dict[str, tuple[str, SearchScraper]] = {
        'cars.com': ('Cars.com', scrape_carscom),
        'autotrader': ('Autotrader', scrape_autotrader),
        'kbb': ('KBB', scrape_kbb),
        'autotrader-uk': ('Autotrader UK', scrape_autotrader_uk),
        'motors': ('Motors.co.uk', scrape_motors_uk),
        'cinch': ('Cinch', scrape_cinch),
        'ebay': ('eBay Motors', scrape_ebay_uk),
    }
    tasks = [
        _run_scraper(label, fn)
        for key, (label, fn) in scraper_map.items()
        if key in filtered_sources
    ]
    if not tasks:
        logger.error(f'No known sources selected (got: {logger.preview(filtered_sources)})')

    results = await asyncio.gather(*tasks)
    logger.info('All scrapers completed')

    suggestions: list[ModelSuggestions] = []
    all_listings: list[CarListing] = []
    errors: list[str] = []
    for result in results:
        scrape_result: ScrapeResult = result['result']
        if scrape_result.model_suggestions:
            suggestions.append(scrape_result.model_suggestions)
        all_listings.extend(scrape_result.listings)
        if result.get('error'):
            errors.append(f'{result["source"]}: {result["error"]}')

    logger.info(f'Total listings: {len(all_listings)}')
    if send_progress:
        send_progress(f'Done - found {len(all_listings)} listing(s) total')

    return all_listings, suggestions, errors, skipped_filters, False


def format_search_output(
    params: SearchParams,
    all_listings: list[CarListing],
    suggestions: list[ModelSuggestions],
    errors: list[str],
    skipped_filters: list[str],
    is_uk: bool,
) -> str:
    currency_symbol = '£' if is_uk else '$'
    output = '# Car Deals Search Results\n\n'
    what = f'{params.make} {params.model}'.strip() or 'all cars'
    output += f'**Search:** {what}'
    if params.year_min or params.year_max:
        output += f' ({params.year_min or "any"}-{params.year_max or "any"})'
    if params.price_max:
        output += f' | Max Price: {currency_symbol}{params.price_max:,}'
    if params.mileage_max:
        output += f' | Max Mileage: {params.mileage_max:,}'
    if params.transmission:
        output += f' | {params.transmission.capitalize()}'
    if params.drivetrain:
        drive = normalise_drivetrain(params.drivetrain) or params.drivetrain
        output += f' | {drive}'

    active_filters: list[str] = []
    if params.one_owner:
        active_filters.append('1-Owner')
    if params.no_accidents:
        active_filters.append('No Accidents')
    if params.personal_use:
        active_filters.append('Personal Use')
    if active_filters:
        output += f'\n**CarFax Filters:** {", ".join(active_filters)}'

    output += f'\n**Location:** {params.zip}'
    if params.max_distance is not None:
        md = params.max_distance
        output += ' (nationwide)' if md == 0 else f' (within {md} mi)'
    output += '\n\n'

    if not all_listings:
        output += 'No listings found.\n'
        for hint in suggestions:
            output += (
                f'\n{hint.source or "Cars.com"} has no {hint.make} model named '
                f'"{hint.input}". Closest matches:\n'
            )
            for opt in hint.options:
                cnt = f' ({opt.count} listed)' if opt.count else ''
                output += f'- {opt.name}{cnt}\n'
            output += '\nRe-run the search with one of these as `model`.\n'
    else:
        output += f'Found **{len(all_listings)}** listings:\n\n'
        for listing in all_listings:
            output += listing.format() + '\n\n---\n\n'

    if errors:
        output += '\n**Errors:**\n'
        for err in errors:
            output += f'- {err}\n'

    if skipped_filters:
        output += '\n**Skipped sources:**\n'
        for key in skipped_filters:
            output += (
                f'- `{key}` does not support transmission/drivetrain filtering '
                '(no API; cards do not surface these fields) and was skipped.\n'
            )

    return output


def format_detail_output(details: DetailResult) -> str:
    output = f'# {details.title or "Listing Details"}\n\n'
    output += f'**Source:** {details.source}\n'
    output += f'**URL:** {details.url}\n\n---\n\n'
    output += details.markdown
    return output


def format_mot_output(record: MotRecord) -> str:
    if not record.found:
        return (
            f'No MOT record was found for registration "{record.registration}".\n\n'
            'The vehicle may be unregistered, too new to have an MOT, or '
            f'the registration may be mistyped.\n\nCheck directly: {record.url}'
        )

    v = record.vehicle or {}
    latest = record.latest_test
    issues = record.outstanding_issues or {}

    output = f'# MOT History: {v.get("makeModel") or record.registration}\n\n'
    output += f'**Registration:** {v.get("registration") or record.registration}\n'
    if v.get('colour'):
        output += f'**Colour:** {v["colour"]}\n'
    if v.get('fuelType'):
        output += f'**Fuel:** {v["fuelType"]}\n'
    if v.get('dateRegistered'):
        output += f'**First registered:** {v["dateRegistered"]}\n'
    if record.mot_expiry:
        output += f'**MOT valid until:** {record.mot_expiry}\n'
    output += f'**Source:** GOV.UK ({record.url})\n\n'

    output += '## Outstanding Issues\n\n'
    if latest:
        raw_res = str(latest.get('result') or '').strip().upper()
        if 'PASS' in raw_res:
            result_badge = 'PASS'
        elif 'FAIL' in raw_res:
            result_badge = 'FAIL'
        else:
            result_badge = raw_res or 'UNKNOWN'
        test_date = latest.get('date') or 'Date unknown'
        output += f'**Latest test ({test_date}):** {result_badge}\n'
        if latest.get('mileage'):
            output += f'**Mileage at last test:** {latest["mileage"]}\n'
    else:
        output += '**Latest test:** none on record\n'

    open_count = (
        len(issues.get('dangerous') or [])
        + len(issues.get('major') or [])
        + len(issues.get('minor') or [])
        + len(issues.get('advisories') or [])
    )
    if open_count > 0:

        def _section(label: str, items: list[str]) -> None:
            nonlocal output
            if not items:
                return
            output += f'\n**{label}:**\n'
            for item in items:
                output += f'- {item}\n'

        _section(
            'Dangerous defects (do not drive until repaired)', issues.get('dangerous') or []
        )
        _section('Major defects (repair immediately)', issues.get('major') or [])
        _section('Minor defects (repair soon)', issues.get('minor') or [])
        _section('Advisories (monitor and repair if necessary)', issues.get('advisories') or [])
    elif latest:
        output += '\nNo outstanding defects or advisories recorded at the latest test.\n'

    if issues.get('has_outstanding_recall'):
        output += '\n## ⚠️ Safety Recall\n\n'
        for recall in record.recalls:
            output += f'{recall}\n\n'
    else:
        output += '\nNo outstanding safety recalls.\n'

    if record.tests:
        n = len(record.tests)
        output += f'\n## Full MOT History ({n} test{"s" if n != 1 else ""})\n\n'
        for test in record.tests:
            raw_r = str(test.get('result') or '').strip().upper()
            if 'PASS' in raw_r:
                result = 'PASS'
            elif 'FAIL' in raw_r:
                result = 'FAIL'
            else:
                result = raw_r or 'UNKNOWN'
            output += f'### {test.get("date") or "Date unknown"} — {result}\n'
            if test.get('mileage'):
                output += f'- Mileage: {test["mileage"]}\n'
            if test.get('testNumber'):
                output += f'- Test number: {test["testNumber"]}\n'
            if test.get('expiryDate'):
                output += f'- Expiry date: {test["expiryDate"]}\n'

            def _defect_lines(items: list[str]) -> str:
                return '\n'.join(f'  - {i}' for i in items) if items else ''

            if test.get('dangerous'):
                output += f'- Dangerous defects:\n{_defect_lines(test["dangerous"])}\n'
            if test.get('major'):
                output += f'- Major defects:\n{_defect_lines(test["major"])}\n'
            if test.get('minor'):
                output += f'- Minor defects:\n{_defect_lines(test["minor"])}\n'
            if test.get('advisories'):
                output += f'- Advisories:\n{_defect_lines(test["advisories"])}\n'
            output += '\n'

    return output


async def _handle_search(
    args: dict[str, Any], send_progress: ProgressSender | None, config: Config
) -> types.CallToolResult:
    try:
        country = (args.get('country') or config.country or 'US').upper()
        is_uk = country == 'UK'
        params = SearchParams(
            make=args.get('make') or '',
            model=args.get('model') or '',
            country=country,
            zip=args.get('zip') or ('SW1A 1AA' if is_uk else '90210'),
            max_distance=args.get('maxDistance'),
            year_min=args.get('yearMin'),
            year_max=args.get('yearMax'),
            price_max=args.get('priceMax'),
            mileage_max=args.get('mileageMax'),
            one_owner=args.get('oneOwner'),
            no_accidents=args.get('noAccidents'),
            personal_use=args.get('personalUse'),
            transmission=args.get('transmission'),
            drivetrain=args.get('drivetrain'),
        )
        max_results = args.get('maxResults') or 10
        default_sources = ['autotrader-uk'] if is_uk else ['cars.com']
        sources = args.get('sources') or default_sources

        listings, suggestions, errors, skipped_filters, unsupported_all = await execute_search(
            params=params,
            sources=sources,
            max_results=max_results,
            send_progress=send_progress,
            config=config,
        )

        if unsupported_all:
            return _text_result(
                'All selected sources were skipped because they do not support '
                'transmission/drivetrain filtering. Re-run with '
                '`sources: ["autotrader-uk"]` or `["cinch"]` (or omit `sources`) '
                'to use a filterable source.',
                is_error=True,
            )

        output = format_search_output(
            params, listings, suggestions, errors, skipped_filters, is_uk
        )
        logger.trace(f'Response: {logger.preview(output)}')
        return _text_result(output)
    except Exception as error:  # noqa: BLE001
        logger.error('search_car_deals failed', error)
        return _text_result(f'Error searching for car deals: {error}', is_error=True)


async def _handle_detail(
    args: dict[str, Any], send_progress: ProgressSender | None, config: Config
) -> types.CallToolResult:
    try:
        logger.info(f'Fetching listing details: {args.get("url")}')
        logger.debug(
            f'Detail options: includeLinks={args.get("includeLinks")}, '
            f'includeImages={args.get("includeImages")}, maxLength={args.get("maxLength")}'
        )
        if send_progress:
            send_progress('Loading listing detail page...')

        details = await fetch_listing_details(
            args.get('url') or '',
            {
                'includeLinks': args.get('includeLinks'),
                'includeImages': args.get('includeImages'),
                'maxLength': args.get('maxLength'),
            },
            send_progress,
            config,
        )

        logger.info(f'Detail page extracted: {len(details.markdown)} chars')
        logger.debug(
            f'Resolved source: {details.source}, title: {details.title or "(none)"}, '
            f'truncated: {details.truncated}'
        )

        output = format_detail_output(details)
        logger.trace(f'Response: {logger.preview(output)}')
        return _text_result(output)
    except Exception as error:  # noqa: BLE001
        logger.error('get_listing_details failed', error)
        return _text_result(f'Error fetching listing details: {error}', is_error=True)


async def _handle_mot(
    args: dict[str, Any], send_progress: ProgressSender | None, config: Config
) -> types.CallToolResult:
    try:
        registration = args.get('registration') or ''
        logger.info(f'Checking MOT history for {registration}')
        if send_progress:
            send_progress(f'Checking MOT history for {registration}...')

        record: MotRecord = await fetch_mot_history(registration, send_progress, config)
        output = format_mot_output(record)

        logger.info(
            f'MOT history returned: {len(record.tests)} test(s), latest '
            f'{(record.outstanding_issues or {}).get("latest_result") or "none"}'
            f'{", outstanding recall" if (record.outstanding_issues or {}).get("has_outstanding_recall") else ""}'
        )
        logger.trace(f'Response: {logger.preview(output)}')
        return _text_result(output)
    except Exception as error:  # noqa: BLE001
        logger.error('check_mot_history failed', error)
        return _text_result(f'Error checking MOT history: {error}', is_error=True)


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------


def build_server(config: Config) -> Server:
    """Build the lowlevel Server with handlers closed over `config`."""
    server = Server(SERVER_NAME, version=SERVER_VERSION)
    list_tools, call_tool = _build_handlers(config)
    server.add_request_handler('tools/list', types.PaginatedRequestParams, _wrap(list_tools))
    server.add_request_handler('tools/call', types.CallToolRequestParams, _wrap(call_tool))
    return server


def _loop_factory() -> asyncio.AbstractEventLoop:
    """Build the server's event loop with the global exception handler
    attached. `asyncio.run(..., loop_factory=...)` sets this factory's loop as
    the running one, so unhandled-task exceptions reach stderr instead of
    dying silently at interpreter shutdown.
    """
    loop = asyncio.new_event_loop()
    attach_loop_exception_handler(loop)
    return loop


async def _serve(config: Config) -> None:
    server = build_server(config)
    init_options = server.create_initialization_options(NotificationOptions())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, init_options)
    finally:
        # Flush the shared HTTP client's keep-alive pool so the process does
        # not linger on an open TLS connection after the transport closes.
        from .scrapers._http import close_client

        await close_client()


def run(config: Config) -> None:
    """Start the stdio MCP server. Called by `__main__.main`."""
    logger.info(f'Car Deals MCP Server running on stdio (log level: {logger.level})')
    if logger.level == 'info':
        logger.info(
            'Set CAR_DEALS_LOG_LEVEL=debug (or pass --verbose) for request '
            'arguments, search URLs, timings and stack traces.'
        )
    try:
        asyncio.run(_serve(config), loop_factory=_loop_factory)
    except KeyboardInterrupt:
        pass
    except Exception as err:  # noqa: BLE001
        logger.error('Server failed to start', err)
        raise
