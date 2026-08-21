# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

`just` wraps `uv`; either works.

```bash
just init          # uv sync
just check         # ruff lint + format check — the offline gate, run before committing
just lint-fix      # ruff check --fix + ruff format
just test          # uv run python test/test_mcp_client.py — full end-to-end MCP client test, hits Cars.com / Autotrader UK / GOV.UK live (minutes)
just test-scraper  # quick scrape_carscom smoke test, prints formatted listings
just check-all     # check + test
just build         # uv build (wheel + sdist; replaces npm pack)
just run           # start the server on stdio
```

There is no test framework and no unit tests. `test/test_mcp_client.py` is a
single async script asserting against real site responses — there is no way
to run "one test"; comment out sections if you need to narrow it. It fails when
the live sites change, which is intentional (see below).

Drive the server by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' | uv run car-deals-mcp
```

## Runner: `uv` / `uvx`

`uv` is the env/package manager and runner; `uvx` is the `npx` equivalent
(ephemeral, cached). Production runs via `uvx --from git+... car-deals-mcp`.
Dev runs via `uv run`. The package uses a `src/` layout so `uvx` can install it
as a console script (`car-deals-mcp = car_deals_mcp.__main__:main`).

### Startup args (the production launch contract)

The server runs on stdio. Two startup arguments are required (user-facing
contract — do not break it):

- `--country` — default country the server operates in. `"US"` or `"UK"`. Sets
  the default for `search_car_deals` when the client doesn't pass `country`.
  Allows a deployment to be UK-only or US-only by configuration without
  per-call args. A per-call `country` still overrides it. **The arg only sets a
  default — it does not restrict.**
- `--cloakbrowser-key` — CloakBrowser license key (free GitHub-sign-in key, or
  paid). Env fallback `CLOAKBROWSER_LICENSE_KEY`; CLI arg wins. Passed to every
  `cloakbrowser.launch_async(...)` so the always-current Chromium 151 build is
  used. Without a key, CloakBrowser falls back to the older free Chromium 146
  build (still works, ages over time).

Example production invocation:
```
uvx --from git+https://github.com/dan0v/car_deals_search_mcp car-deals-mcp --country UK --cloakbrowser-key cb_xxxxxxxx
```

## Architecture

A `scrapers/` package holds one module per site plus shared helpers;
`src/car_deals_mcp/server.py` is the MCP protocol layer;
`src/car_deals_mcp/logger.py` is leveled stderr logging that both import.
`__main__.py` is the CLI entry (argparse for the startup args, then
`server.run()`).

```
src/car_deals_mcp/
  server.py            MCP protocol layer (lowlevel Server, three tools)
  logger.py            leveled stderr logging
  types.py             CarListing, SearchParams, ScrapeResult, MotRecord, ...
  __main__.py          CLI entry (argparse for --country / --cloakbrowser-key)
  scrapers/
    __init__.py        public re-exports (scrape_carscom, fetch_mot_history, ...)
    _base.py           shared helpers: carscom_slug, parse_*, apply_uk_filters,
                       normalise_drivetrain, is_uk_registration, browser launch,
                       start_heartbeat, scrape (browser skeleton), wait_out_interstitial
    _http.py           shared httpx.AsyncClient + per-domain throttle +
                       Autotrader UK app-version cache (direct-API sources only)
    carscom.py         browser (CloakBrowser)
    autotrader_us.py   browser
    kbb.py             browser
    autotrader_uk.py   direct GraphQL API (/at-gateway)
    motors_uk.py       browser (no API; Cazoo stack)
    cinch.py           direct REST API (returns the registration plate)
    ebay_uk.py         direct Browse API (OAuth2, env creds) + browser fallback
    detail.py          get_listing_details: host allowlist, prune + markdownify
    mot.py             check_mot_history: GOV.UK, UK-plate validation
```

**server.py** — registers three tools on a lowlevel `Server` from the official
`mcp` Python SDK (mcp 2.0; `FastMCP` no longer exists in 2.0 — the lowlevel
`Server` is the port target and mirrors the JS SDK's manual
`setRequestHandler` shape). It holds no state and does no parsing. It maps tool
arguments onto a `params` dict, fans out to the selected scrapers with
`asyncio.gather`, and concatenates `listing.format()` output into one markdown
text block. A failing scraper is caught per-source and reported in an
`**Errors:**` footer rather than failing the call. `search_car_deals` defaults
`sources` to `['cars.com']` (US) or `['autotrader-uk']` (UK) for reliability,
despite the schema advertising all sources. `country` (the server's
`--country` default, overridable per-call) selects the default source set,
default postcode/ZIP and currency; the handler is a single `is_uk` branch so a
future country can be added as another `elif`. `check_mot_history` (UK only)
drives the GOV.UK MOT service and returns a structured record rather than a
`CarListing`.

**scrapers/** — one `scrape_<site>(params, max_results, send_progress, config)`
async function per site (one module each), plus `fetch_listing_details` (in
`detail.py`) for the detail-page tool and `fetch_mot_history` (in `mot.py`) for
the UK MOT-history tool. All search scrapers normalize into `CarListing`, whose
`format()` method is the sole definition of the user-visible search-output
shape — change output there, not in server.py. `search_all_sources` is exported
from the package but unused by the server. Shared helpers (slug rules,
parse_*, accept_consent, apply_uk_filters, normalise_drivetrain,
is_uk_registration, browser launch, start_heartbeat, the `scrape` browser
skeleton, wait_out_interstitial) live in `_base.py`; the package `__init__.py`
re-exports the public surface so callers import from `car_deals_mcp.scrapers`.

### Two transport strategies, one package

Not every site is browser-scraped. Autotrader UK and Cinch expose
**unauthenticated** search APIs that are strictly better than scraping HTML:
structured fields, every filter server-side, no anti-bot, no card-selector
fragility. They use `scrapers/_http.py` (a shared `httpx.AsyncClient` + a
per-domain throttle) instead of a CloakBrowser launch. The browser scrapers
(Cars.com, Autotrader US, KBB, Motors.co.uk) stay on CloakBrowser because
those sites are Cloudflare-walled or have no discoverable API. See
`docs/SITE_APIS.md` for the full reverse-engineering notes.

- **Autotrader UK** — `/at-gateway` GraphQL. The one moving part is
  `x-sauron-app-version`, which changes per SPA deploy; it is scraped from the
  `/car-search` page's inline `window.AT_SPA_JS_CONFIG` JSON and cached with a
  TTL in `scrapers/_http.py.get_autotrader_uk_version()`, with a baked-in
  fallback. `price_search_type: "total"` is a mandatory filter or the gateway
  rejects with `INVALID_ARGUMENT`. Mileage, transmission, drivetrain, postcode,
  `distance` (radius, miles; 1500 = whole UK), make/model, year, price are all
  server-side. The API does **not** return the registration plate.
- **Cinch** — `search-api.snc-prod.aws.cinch.co.uk` REST. The query string is
  wrapped (URL-encoded) inside a `url=` param — an odd quirk. Returns the
  **registration plate** (`vrm`/`fullRegistration`), the one UK source that
  does — a Cinch result can be piped straight into `check_mot_history`. Note
  the source-specific param names: `transmissionType` (not `transmission`),
  `driveType` (not `drivetrain`). Mileage is **not** server-filterable on
  Cinch — it is applied client-side in `scrape_cinch`.
- **eBay Motors UK** — official **Browse API** (`/buy/browse/v1/item_summary/search`),
  the first *authenticated* source here. Unlike Autotrader UK/Cinch it needs an
  OAuth2 application token (client_credentials grant, ~2h lifetime, cached in
  `scrapers/ebay_uk.py._get_ebay_token()`), fetched with
  `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` from the environment. When the
  credentials are absent (or token fetch fails) the scraper **falls back to
  browser-scraping** `ebay.co.uk/sch/Cars-Trucks-Vehicles/...` via CloakBrowser
  — the same transport Motors.co.uk uses — so the source works out of the box
  and upgrades automatically when an operator registers an eBay app. The API
  path supports transmission (aspect filter) and year/price server-side;
  drivetrain is **not** a stable aspect on the UK Cars category, so the
  server skips `ebay` with a warning when `drivetrain` is requested (like
  `motors`). Neither path returns the UK registration plate.

### Rate-limiting (direct-API sources)

Direct HTTP is fast enough to hammer a host by accident; the browser scrapers
are implicitly throttled by browser-launch time. `scrapers/_http.py` enforces a
per-domain minimum interval (`DEFAULT_MIN_INTERVAL`, 1.0s) via an
`asyncio.Lock` + monotonic deadline per netloc, so concurrent `asyncio.gather`
fan-out serialises per host while different hosts run in parallel. 429/5xx
responses back off exponentially (1s/2s/4s, capped 8s) up to `MAX_RETRIES`
before surfacing in the `**Errors:**` footer rather than retrying instantly. A
single `httpx.AsyncClient` is reused across calls for keep-alive.

### The browser is CloakBrowser, not puppeteer-extra-plugin-stealth

The load-bearing dependency in the JS version was
`puppeteer-extra-plugin-stealth`. It is **abandoned since April 2023** and loses
to modern Imperva/Cloudflare checks. CloakBrowser (`pip install cloakbrowser`)
was spiked against the GOV.UK MOT page (Imperva-fronted) and **succeeded** — it
returned the real `BMW 3 SERIES` results page. CloakBrowser patches Chromium at
the C++ source level (canvas, WebGL, WebRTC, `navigator.webdriver`, CDP
detection) rather than JS injection. It is actively maintained (Chromium 151,
updated regularly) and returns a standard Playwright `Browser` object.

`launch_browser_async(config)` calls `cloakbrowser.launch_async(headless=True,
humanize=True, license_key=config.cloakbrowser_key)`. This is the *only* browser
code — no puppeteer, no stealth plugin, no `PUPPETEER_EXECUTABLE_PATH`. The
async launch matches FastMCP/lowlevel-Server's `async def` handlers.

**CloakBrowser enforces a per-plan concurrent-session limit** and raises
`CloakBrowserLicenseError` on a second overlapping launch. Since `server.py`
fans scrapers out with `asyncio.gather`, every browser flow is serialised
behind a process-wide `_BROWSER_LOCK` in `_base.py`: `scrape()` holds it for
launch → body → close, and `browser_session()` (used by `detail.py` and
`mot.py`) does the same for flows that don't fit the skeleton. The direct-API
scrapers (Autotrader UK, Cinch, eBay Browse API) never take the lock — they
have no session to consume. Only anti-bot-walled sites need CloakBrowser.

### `page.evaluate` payloads stay JavaScript

JS uses `page.evaluate(() => {...browser code...})`. In Playwright Python,
`page.evaluate` takes a JS string — there is no Python-in-browser. So the
extraction functions ported from `scraper.js` are kept **verbatim as JS string
literals** (`_EXTRACT_CARSCOM_JS`, `_EXTRACT_MOTORS_UK_JS`, `_PRUNE_DETAIL_JS`,
etc.). Only the wrapper logic is Python. This is a real gotcha — if you rewrite
an `evaluate` payload in Python it will not run.

### Progress notifications are load-bearing

Scrapes routinely run past MCP clients' default tool-call timeout. The client's
timeout clock resets on each `notifications/progress`, so `start_heartbeat()`
emits one every 4s for the life of a scrape. Two constraints that are easy to
break:

- The progress token lives at `params.meta["progress_token"]` (the SDK
  normalizes `_meta` to a snake_case dict — a sibling of `name`/`arguments`, not
  inside `arguments`). See `_extract_progress_token` in server.py.
- Every notification must carry a strictly increasing numeric `progress` field.
  Clients validate the schema and silently drop notifications missing it.

`send_progress` threads from the request handler down through every scraper;
keep it optional (`send_progress?.(...)` / `None`-guarded) so scrapers stay
callable from scripts. In server.py it calls
`ctx.session.send_progress_notification(progress_token, progress, message)`,
scheduled fire-and-forget on the running loop so the heartbeat does not block
the scraper.

### Logging

`src/car_deals_mcp/logger.py` — levels `silent` < `error` < `info` < `debug` <
`trace`, selected by `CAR_DEALS_LOG_LEVEL` (wins) or `--verbose` / `--trace` in
argv. Unlike `send_progress`, the logger is imported directly rather than
threaded through, so it is always available and needs no null-guard.

- `info` is the default and prints with a bare `[MCP]` prefix; `debug`/`trace`
  tag themselves (`[MCP:debug]`). Byte-identical to the JS so log-scraping keeps
  working. Put anything per-listing or per-attempt at `debug` — `info` should
  stay one line per tool call and per scraper outcome.
- `logger.error(message, err)` prints the stack **and the `__cause__` chain** at
  `debug` and above, and a single-line message at `info`.
- **Wrap rethrown errors with `raise X(...) from err`.** Every scraper turns a
  Playwright failure into `raise RuntimeError('X scraping failed: ...') from
  err`; without `from` the original stack is gone and the log tells you a
  navigation timed out but not which one.
- `logger.preview(value)` truncates before logging. Use it for anything
  caller-supplied or response-sized — a detail-page result is up to 30k chars.
- Never add a bare `print()`. Stdout is the transport; writing to stderr
  directly is tolerable but bypasses levels, so prefer the logger.

Exceptions must always reach stderr, even when the client is also told about
them. The tool handlers catch their own failures and return `isError`, and the
`_wrap` wrapper in server.py catches anything they miss and rethrows after
logging. `install_global_handlers()` covers `sys.excepthook` and the asyncio
exception handler.

The MCP SDK only forwards a fixed subset of env vars to a server subprocess, so
`CAR_DEALS_LOG_LEVEL` has to be set in the client's `env` block.
`test/test_mcp_client.py` forwards it explicitly for the same reason. The same
applies to the eBay Browse API credentials (`EBAY_CLIENT_ID` /
`EBAY_CLIENT_SECRET`); without them in the client's `env` block the `ebay`
source silently falls back to browser scraping.

### Scraping strategy differs per site, on purpose

- **Cars.com** is the only fully supported source. Cards embed a
  `data-vehicle-details` JSON payload carrying VIN, trim, body style,
  drivetrain, fuel type, exterior color, dealer identity and CPO flag. Read that
  payload first and fall back to parsing visible card text, so a markup
  reshuffle degrades results instead of emptying them. Cars.com also serves a
  card-less page to automated traffic at random, hence the two-attempt reload
  loop.
- **Autotrader and KBB** have no equivalent payload and use fragile
  class-name/positional selectors. They return title/price/mileage only and
  break often.
- **UK sources** are split by transport. Autotrader UK and Cinch use direct
  APIs (see "Two transport strategies" above) — every filter including
  transmission, drivetrain and mileage is server-side on Autotrader UK; Cinch
  filters transmission/drivetrain/price/year server-side but mileage
  client-side. Motors.co.uk is browser-scraped (no API; it's the Cazoo stack)
  and lacks server-side filters for some parameters, so `apply_uk_filters`
  narrows year/price/mileage client-side for it. It shows mileage rounded
  ("41.2k") — `parse_mileage` expands it. Consent banners
  (Sourcepoint/OneTrust) are dismissed by `accept_consent` before reading
  cards. When `transmission`/`drivetrain` filtering is requested, Motors.co.uk
  is skipped with a warning (its cards don't surface those fields); eBay is
  skipped only when `drivetrain` is set (no stable aspect), but kept for
  transmission (the Browse API `Transmission` aspect + visible card text);
  Autotrader UK and Cinch are not skipped because their APIs support the
  filters.

### An unrecognized Cars.com filter looks exactly like a broken scraper

Cars.com does not reject a bad `makes[]`/`models[]` slug — it drops the filter
and serves a results page with zero cards, which is indistinguishable from the
bot-check variant. So a *query* mistake shows up in the logs as
`vehicle cards never appeared`, and the obvious debugging path (blame the
selectors) is the wrong one. Two separate things have to be right:

- **Slug spelling** — `carscom_slug()`. Make and model are joined by a hyphen
  (`mercedes_benz-gle_450`), so every separator inside a name becomes `_`,
  `&`/`+` are spelled out, apostrophes vanish, and `.` survives (`ID.4` →
  `id.4`). Verified against the site's whole filter vocabulary. Do **not**
  replace this with a harvested lookup table: the vocabulary the page exposes
  truncates at 100 models per make, so a table is incomplete for exactly the
  makes people search most.
- **Name validity** — a well-formed slug can still name nothing. `GLE` is
  clean but Cars.com only has `gle_350`/`gle_450`/`gle_class`. On a zero-card
  result the scraper reads the make's real model list out of the page's
  `script#CarsWeb.SearchController.index` blob (`srp_filter.sections`) and
  attaches ranked `model_suggestions` to the returned array, which server.py
  renders as a did-you-mean. A missing blob means the interstitial, not a bad
  name — treat it as unknown and say nothing.

Single-word names (`toyota-camry`) survive a plain `toLowerCase()`, which is
why this went unnoticed: the test suite only searched Toyota Camry. Keep the
Mercedes-Benz cases in `test/test_mcp_client.py`.
- **Detail pages** (`fetch_listing_details`) are deliberately *not* field-parsed.
  The page is pruned (nav, ads, scripts, carousels) and converted to markdown
  with `markdownify` (a custom `<dl>` converter pairs `<dt>`/`<dd>` into
  `- **Term:** Value`). That is the whole design: no selectors to maintain when
  the sites reshuffle. Detail pages also sit behind an anti-bot interstitial more
  often than search pages, so `wait_out_interstitial` polls for up to 45s rather
  than converting the challenge page into the answer.

Text-parsing heuristics here have all broken at least once and carry comments
explaining what broke. Keep patterns bounded literals, never greedy wildcards —
a greedy `certified[\w\s-]*` title strip once ate past the year and model.

`fetch_listing_details` drives a real browser at a caller-supplied URL, so the
host is checked against the `DETAIL_HOSTS` allowlist first (`resolve_detail_source`);
without it the tool is an open fetch proxy reachable at private network addresses.

## Conventions

- Python 3.11+, single quotes (ruff `quote_style = "single"`), 4-space indent.
  `ruff check` + `ruff format` is the gate (run `just check`).
- `page.evaluate()` callbacks run in the browser, not Python. They stay as JS
  string literals — see the gotcha above.
- Diagnostics go to stderr (stdout is the MCP transport; anything written there
  corrupts the protocol stream).
- When you change what a scraper extracts, add an assertion to
  `test/test_mcp_client.py`. Fields sourced from `data-vehicle-details` (VIN,
  specs, color, dealer rating) silently vanish into the text fallback if
  Cars.com renames the attribute — the assertions are what catch that.

## Metadata files are metadata-only

`mcp.json` and `server.json` are not read at runtime (the transport is stdio,
no HTTP port). They are kept for registry/launcher catalogs and name the
Python entry, list all three tools, and drop the stale HTTP port from the JS
repo. Update them if publishing.
