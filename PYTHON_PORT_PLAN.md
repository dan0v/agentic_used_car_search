# Python Port Project Plan

Port the JS `car_deals_search_mcp` to Python on `uv` + `cloakbrowser` + the official Python MCP SDK, as a fresh parallel implementation (not a line-by-line translation). The JS implementation is the authoritative reference for behavior; this document specifies what to port and the conventions to preserve.

---

## 1. Why this port, and the decisions already made

**Context that's load-bearing — do not re-litigate:**

- The load-bearing dependency in the JS version was `puppeteer-extra-plugin-stealth` (clears Imperva/Cloudflare anti-bot). It is **abandoned since April 2023**. Scrapling (the top Python scraping framework, `patchright`-based) was spiked against the GOV.UK MOT page and **failed** — it got the "Pardon Our Interruption" Imperva interstitial.
- **CloakBrowser** (`pip install cloakbrowser`) was spiked against the same GOV.UK MOT page and **succeeded** — it returned the real `BMW 3 SERIES` results page (104KB HTML, `[data-test-id="vehicle-make-model"]` heading found). CloakBrowser patches Chromium at the C++ source level (canvas, WebGL, WebRTC, `navigator.webdriver`, CDP detection) rather than JS injection. It is actively maintained (Chromium 151, updated days ago) and has a Python API returning a standard Playwright `Browser` object.
- The Python MCP SDK is `modelcontextprotocol/python-sdk` (24k★, actively maintained). It has a `FastMCP` decorator API cleaner than the JS SDK's manual request-handler registration.
- `uv`/`uvx` is the runner: `uvx <tool>` is the `npx` equivalent (ephemeral, cached). Production will run via `uvx`. Dev runs via `uv run`.

**Decision locked:** Python 3.11+, `uv` for env/package/runner, `cloakbrowser` for stealth browsing, `mcp` (official Python SDK) for the MCP server, `uvx` for production launch. Do not reconsider unless a spike fails in implementation.

---

## 2. Production launch contract (must match this exactly)

The MCP server runs on stdio. **Two startup arguments are required** (user request):

- `--country` — default country the server operates in. One of `"US"` or `"UK"`. Sets the default for `search_car_deals` when the client doesn't pass `country`. Allows a deployment to be UK-only or US-only by configuration without per-call args.
- `--cloakbrowser-key` — the CloakBrowser license key (free GitHub-sign-in key, or paid). Can also be read from the `CLOAKBROWSER_LICENSE_KEY` env var; CLI arg wins. The server passes this to every `cloakbrowser.launch(...)` call so the always-current Chromium 151 build is used. Without a key, CloakBrowser falls back to the older free Chromium 146 build (still works, ages over time).

Example production invocation:
```
uvx --from git+https://github.com/dan0v/car_deals_search_mcp car-deals-mcp --country UK --cloakbrowser-key cb_xxxxxxxx
```

MCP client config (Claude Desktop etc.):
```json
{
  "mcpServers": {
    "car-deals": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dan0v/car_deals_search_mcp", "car-deals-mcp", "--country", "US", "--cloakbrowser-key", "cb_xxxxxxxx"]
    }
  }
}
```

The `--country` startup arg is the *server default*; a per-call `country` argument on `search_car_deals` still overrides it. The `--country` arg does **not** restrict — it only sets the default.

---

## 3. Project layout

```
car_deals_search_mcp/
  pyproject.toml          # uv project: deps, entry point, ruff config
  uv.lock                 # lockfile (committed)
  README.md               # ported + updated for Python/uv/uvx
  CLAUDE.md               # ported conventions, updated for Python
  .python-version         # 3.11
  src/car_deals_mcp/
    __init__.py
    __main__.py           # CLI entry: argparse for --country / --cloakbrowser-key, then server.run()
    server.py             # FastMCP server: tool registration, request handling, progress, output formatting
    scraper.py            # CarListing, launch_browser, all scrapers, fetch_listing_details, fetch_mot_history
    logger.py             # leveled stderr logging (CAR_DEALS_LOG_LEVEL env / --verbose / --trace)
  test/
    test_mcp_client.py    # end-to-end MCP client test (Python, against the live server subprocess)
  justfile                # just check / test / build / run, using uv
```

Use a proper package (`src/` layout) so `uvx` can install it as a console script.

---

## 4. `pyproject.toml` essentials

- `[project]` name `car-deals-mcp`, version, `requires-python = ">=3.11"`
- `[project.scripts]` `car-deals-mcp = "car_deals_mcp.__main__:main"` (the `uvx`-able entry)
- Dependencies: `cloakbrowser`, `mcp>=1.30`, `selectolax` (fast HTML parsing — replace Turndown for detail-page markdown; see §8), `markdownify` (alternative — pick one), `httpx` (only if any scraper needs raw HTTP; likely not)
- `[tool.uv]` — pin if needed
- `[tool.ruff]` — lint config mirroring the JS eslint rules (no unused vars, etc.)
- Dev deps: `pytest`, `pytest-asyncio`, `ruff`

Use a single dependency set (no extras) to keep `uvx` first-run fast.

---

## 5. Startup args & config (`__main__.py`)

Use `argparse`:

- `--country` (default `"US"`, choices `["US", "UK"]`) — server default country
- `--cloakbrowser-key` (optional; env fallback `CLOAKBROWSER_LICENSE_KEY`)
- `--verbose` / `--trace` — log level flags (mirror JS)
- No `--port` (stdio only — never add HTTP; the JS `mcp.json` HTTP port was a stale bug, don't repeat it)

Store these in a small `Config` dataclass imported by `server.py` and `scraper.py`. The scraper's `launch_browser()` reads the key from config, not the environment directly, so it's testable.

---

## 6. Logging (`logger.py`) — port conventions exactly

The JS logger is load-bearing and idiosyncratic. Port its exact behavior:

- **Levels**: `silent` < `error` < `info` < `debug` < `trace`
- **Selection**, highest precedence first:
  1. `CAR_DEALS_LOG_LEVEL` env var (validated; unknown value → stderr warning + fall back to `info`)
  2. `--trace` argv flag
  3. `--verbose` / `-v` argv flag → `debug`
  4. default `info`
- **All output to stderr, never stdout** — stdout is the MCP transport. Use Python's `logging` to stderr, or a custom writer. Do NOT use `print()` for diagnostics.
- **Prefix**: `[MCP]` at info/error; `[MCP:debug]` / `[MCP:trace]` at verbose levels (byte-identical to JS so log-scraping keeps working).
- **`logger.error(msg, err)`**: prints message + error chain. Walk `__cause__` chain (Python's equivalent of JS `cause`) up to depth 5. At `info` level, a single readable line; at `debug+`, include stack/cause.
- **`preview(value, max_len=400)`**: truncate long values (detail pages are 30k chars) before logging.
- **Global handlers**: install `sys.excepthook` for uncaught exceptions, and an `asyncio` exception handler for unhandled rejections — both log to stderr and exit (uncaught) or log (unhandled). Mirror the JS `installGlobalHandlers()`.
- **MCP SDK env forwarding caveat**: the MCP SDK forwards only a fixed subset of env vars to a server subprocess. `CAR_DEALS_LOG_LEVEL` must be set in the client's `env` block — document this in README, and forward it explicitly in the test client.

---

## 7. Scraper layer (`scraper.py`) — port all functions

### 7.1 Browser launch (`launch_browser`)

Replace the JS `launchBrowser()` (puppeteer + stealth plugin) with CloakBrowser:

```python
from cloakbrowser import launch

def launch_browser(config):
    return launch(
        headless=True,
        license_key=config.cloakbrowser_key,   # may be None → free 146 build
        humanize=True,                          # human-like input; helps anti-bot
    )
```

CloakBrowser returns a Playwright `Browser` — so `page.goto()`, `page.evaluate()`, `page.query_selector()`, `wait_for_selector()` are all standard Playwright Python API. This is the *only* browser code; no puppeteer-extra, no stealth plugin.

**Important**: CloakBrowser is sync Playwright by default (`from cloakbrowser import launch`). There's also `launch_async` — pick sync to match the JS code's straightforward await model and avoid asyncio complexity in the scrapers. If the MCP server needs to be async (the Python MCP SDK's FastMCP tools are async by default), run sync browser calls in a thread executor or use `launch_async`. Decide during implementation; `launch_async` is the cleaner fit if FastMCP tools are `async def`.

### 7.2 `CarListing` dataclass + `format()`

Port the JS `CarListing` class as a Python `@dataclass`. **Fields (exact):**
`title, price, price_drop, msrp, monthly_payment, mileage, dealer_name, dealer_rating, location, deal_rating, exterior_color, vin, year, make, model, trim, body_style, drivetrain, fuel_type, is_certified, delivery, awards (list), thumbnail, url, source, is_one_owner, no_accidents, personal_use`.

`format()` must produce **byte-identical output** to the JS version (the test asserts on this text). Port the exact string-building logic from `scraper.js` lines 44-83:
- Title line first.
- `Price: X (dropped Y) | MSRP: Z` on the price line.
- `Est. Payment: X/mo`
- `Mileage:`, `Exterior Color:`, `Specs: trim | body | drivetrain | fuel`, `VIN:`, `Deal Rating:`
- `Badges: Certified Pre-Owned | 1-Owner | No Accidents | Personal Use`
- `Awards: ...`
- `Dealer: X (Y stars)`, `Location:`, `Delivery:`, `Source:`, `Photo:`, then the URL.

This is the **sole definition of user-visible search output** — change output here, not in `server.py`. (Mirror the JS convention.)

### 7.3 US scrapers (port each, preserve heuristics + comments)

Port these *with their comments*, because the comments document what broke and why — that knowledge is encoded in the code:

- `carscom_slug(name)` — the make/model slug rules. Read `scraper.js` lines 86-126. Mechanical: `&` → `and`, `+` → `plus`, apostrophes vanish, period survives only between two alphanumerics (`ID.4` → `id.4`, `ID. Buzz` → `id_buzz`), everything else → `_`. **The test asserts exact slug pairs** — port the test cases (see §10).
- `suggest_carscom_models()` — ranked did-you-mean from the page's embedded filter vocabulary.
- `read_carscom_filter_options()` — reads `script#CarsWeb.SearchController.index` JSON blob, returns model options with counts.
- `scrape_carscom(params, max_results, send_progress)` — the big one. Builds the URL with `makes[]`/`models[]` slugs, `zip`, year/price/mileage, `max_distance` (0 → `"all"`), CARFAX filters. Two-attempt reload loop (Cars.com serves card-less bot-check pages randomly). Reads `data-vehicle-details` JSON first, falls back to card text. Extracts: title, price, price_drop, MSRP, monthly payment, mileage, deal rating, dealer name+rating, location, exterior color, VIN, year/make/model/trim/body/drivetrain/fuel, CPO, delivery, awards, thumbnail, CARFAX badges. On zero results, reads the filter vocab for did-you-mean suggestions.
- `scrape_autotrader(params, max_results, send_progress)` — US Autotrader. Fragile selectors, title/price/mileage/dealer only.
- `scrape_kbb(params, max_results, send_progress)` — KBB. Title/price/mileage/deal rating only.

### 7.4 UK scrapers (port each)

- `accept_consent(page, selectors)` — dismisses Sourcepoint/OneTrust cookie banners (top page + consent iframe).
- `parse_price`, `parse_mileage`, `parse_year`, `apply_uk_filters(raw_listings, params)` — client-side filters for sources lacking server-side filters. `parse_mileage` handles Motors.co.uk's "41.2k" rounded form.
- `scrape_autotrader_uk(params, max_results, send_progress)` — postcode search, `[data-testid^="advertCard-"]` cards, title/subtitle/mileage/year/location/price. Dismisses `button[title="Accept All"]` consent.
- `scrape_motors_uk(...)` — `.result-card` cards, rounded "41.2k" mileage, distance line, OneTrust consent.
- `scrape_cinch(...)` — nationwide retailer (no postcode/distance), `li:has(a[data-testid="product-list-card-link"])` cards, label-then-value extraction.

### 7.5 Detail page tool (`fetch_listing_details`)

- `DETAIL_HOSTS` allowlist: `cars.com`, `autotrader.com`, `kbb.com` (and add the UK hosts? — **decide**: the JS only allowlisted US hosts. For the port, keep US-only unless you also port UK detail pages. Recommend: keep US-only for now, document as limitation.)
- `resolve_detail_source(url)` — validate scheme + host against allowlist; throw clear error otherwise. This is a **security boundary** — without it the tool is an open fetch proxy to private network addresses. Port exactly.
- `build_turndown` — JS uses Turndown with a custom `definitionList` rule (pairs `<dt>`/`<dd>` into `- **Term:** Value`). **In Python, replace with `markdownify`** (HTML→Markdown) or `selectolax` + manual markdown. The custom `<dl>` rule must be reproduced: pair each `<dt>` with the following `<dd>` as `- **{term}:** {value}`. This is the only non-trivial conversion in the detail tool.
- `fetch_listing_details(url, options, send_progress)` — prunes the page (strip script/style/nav/footer/ads by class+id pattern), expands "see all features" buttons, waits out the anti-bot interstitial (`waitOutInterstitial` — port the polling logic, pattern `/performing security verification|verifying you are (not a bot|human)|just a moment|checking your browser/i`), converts to markdown, truncates at `max_length` (default 30000).
- Returns `{url, source, title, markdown, truncated}`.

### 7.6 MOT history tool (`fetch_mot_history`) — the UK-specific feature

Read `scraper.js` lines 1112-1300. This is the newest feature and the reason CloakBrowser was chosen.

- `normalise_registration(reg)` — strip spaces, uppercase.
- `MOT_HOST = "www.check-mot.service.gov.uk"` — pinned host (do NOT take caller URLs; security).
- URL: `https://{MOT_HOST}/results?registration={reg}`
- Launch browser, goto, `wait_out_interstitial` (Imperva — CloakBrowser clears this, verified).
- Wait for `[data-test-id="vehicle-make-model"]` heading (15s). If absent → `{found: False}` (no record for that plate, not a scrape failure).
- Extract via `page.evaluate` (or selectolax on `page.content()`):
  - `vehicle`: registration, make_model, colour, fuel_type, date_registered, mot_expiry — from `[data-test-id="vehicle-*"]`.
  - `tests`: list from `[data-test-id="test-history-item"]` rows. Per test: date (first `.govuk-heading-s` in `.govuk-grid-column-one-third`), result (`[data-test-id="test-result"]`), mileage, test_number, expiry_date, and defects grouped by severity: `dangerous` (`[data-test-id="dangerous-defect-items-heading"]`), `major`, `minor`, `advisories`. Defects collected from the `<ul>` sibling of each heading.
  - `recalls`: from `[data-test-id="recall-success-results"]` `.govuk-inset-text` (server-rendered; present only if active recall).
- Returns:
  ```python
  {
    "registration", "found": True, "url",
    "vehicle": {...},
    "mot_expiry",
    "tests": [{date, result, mileage, test_number, expiry_date, dangerous[], major[], minor[], advisories[]}],
    "recalls": [str],
    "latest_test": tests[0] if tests else None,
    "outstanding_issues": {
      "latest_result", "dangerous", "major", "minor", "advisories",
      "open_defect_count", "has_outstanding_recall"
    }
  }
  ```
  or `{"registration", "found": False, "url"}`.

### 7.7 `start_heartbeat(send_progress, label, interval=4.0)`

Scrapes run 15-90s, past MCP clients' default tool-call timeout. Progress notifications reset that timeout. Emit a heartbeat every 4s for the life of each scrape. Port the JS `startHeartbeat`. Returns a stop function. In Python (async): an `asyncio.Task` with `asyncio.sleep`; in sync: a `threading.Timer` loop. **Progress token + strictly-increasing numeric `progress` field are mandatory** — clients validate the schema and drop notifications missing the `progress` field. See §9 for the MCP progress API in Python.

---

## 8. HTML→Markdown for detail pages (the one real translation task)

JS: Turndown with a custom `<dl>` rule. Python options:
- **`markdownify`** — simple, handles most HTML. May not pair `<dl>` correctly by default → write a custom converter for `<dl>` (iterate `<dt>`/`<dd>`, emit `- **{term}:** {value}`).
- **`selectolax`** — fast HTML parser; walk the pruned DOM and emit markdown manually. More control, more code.

Recommend: `markdownify` + a custom `dl` handler. Keep the same pruning (strip `script, style, noscript, template, svg, iframe, ...`, strip `[aria-hidden]`, strip elements whose class/id matches the ad/nav/footer/related pattern) before conversion. The pruning pattern (from `scraper.js` `fetchListingDetails`'s `page.evaluate`) must be ported.

---

## 9. MCP server (`server.py`) — FastMCP

The Python MCP SDK's `FastMCP` uses decorators:

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("car-deals-mcp")
```

### 9.1 Three tools — register all three

1. **`search_car_deals`** — port the JS handler. Args: `make, model, country, zip, max_distance, year_min, year_max, price_max, mileage_max, max_results, sources, one_owner, no_accidents, personal_use`. `country` defaults to **the server's `--country` startup default** (new behavior — this is why the startup arg exists). `is_uk` branch selects default sources (`["cars.com"]` US / `["autotrader-uk"]` UK), default zip (`90210` / `SW1A 1AA`), currency symbol (`$` / `£`). Fan out to selected scrapers with `asyncio.gather` (async) or sequential/threadpool (sync). Per-scraper try/except → errors collected into an `**Errors:**` footer, never fail the whole call. Concatenate `listing.format()` into one markdown text block. Render `model_suggestions` did-you-mean on zero results. **Output format must match the JS** (the test asserts on `**Search:**`, `**Location:**`, `Source:`, etc.).
2. **`get_listing_details`** — port the JS handler. Args: `url, include_links, include_images, max_length`. Host allowlist check, prune+convert to markdown, truncate.
3. **`check_mot_history`** — UK only. Args: `registration`. Call `fetch_mot_history`, format the structured record as markdown with an "Outstanding Issues" summary at top + full history below. Port the JS output formatting (`server.js` `check_mot_history` handler). Handle `found: False` distinctly from error.

### 9.2 Progress notifications (load-bearing — read carefully)

The JS extracts the progress token from `request.params._meta.progressToken` and emits `notifications/progress` with a strictly-increasing numeric `progress` field. **In the Python SDK, FastMCP tools receive a `ctx` parameter** — use `ctx.info(...)` / `ctx.report_progress(progress, total)` or `ctx.request_context.session.send_notification(...)`. **Verify the exact API in the installed `mcp` version during implementation** — the Python SDK's progress API has shifted between versions. The non-negotiable requirements:
- The progress token must be read from the request (the SDK may handle this automatically via `ctx`).
- Every notification must carry a strictly-increasing integer `progress` field.
- Heartbeats every 4s during long scrapes (see §7.7).
- `send_progress` must be optional (callable from scripts without an MCP context) — pass `None` when not in a request.

### 9.3 Error handling

- Each tool handler catches its own failures and returns `isError: True` (MCP `ToolError` or the SDK's error mechanism) rather than throwing.
- An outer wrapper logs any uncaught exception to stderr before re-raising (mirror `logToolFailures`).
- All exceptions reach stderr, even ones also reported to the client.

### 9.4 No state, no parsing in server.py

`server.py` maps tool args → `params` dict, fans out to scrapers, concatenates `listing.format()` output. It does no parsing and holds no state. (Mirror the JS convention exactly.)

---

## 10. Tests (`test/test_mcp_client.py`)

Port `test/mcp-client.test.js` to Python using the Python MCP client SDK. It's a single end-to-end script that spawns the server as a subprocess and drives it over stdio — no test framework required (use `assert`), but `pytest` is fine too.

**Must port:**
- The offline `carscom_slug` assertion pairs (Mercedes-Benz, GLE 450, Town & Country, EQE 350+, Li'l Red Express, ID.4, ID. Buzz, C10/K10, Camry).
- Tool discovery: `assert set of tool names == {"search_car_deals", "get_listing_details", "check_mot_history"}`.
- Schema shape: `search_car_deals` required `["make","model"]`; properties include `country` with enum `["US","UK"]`; `check_mot_history` requires `["registration"]`.
- Live search (US): Toyota Camry on cars.com → asserts progress notifications received, `Source: Cars.com`, `Exterior Color:`, `VIN: 17-char`, `Specs:`, `Location: .+, [A-Z]{2}`, `Dealer: ... (X.X stars)`.
- Live multi-word make (slug regression): Mercedes-Benz GLE 450 → listings returned.
- Unknown model did-you-mean: Mercedes-Benz GLE → suggestion block with `GLE 450`.
- Detail host allowlist: `example.com` URL → rejected with `Unsupported host`.
- Live detail page fetch: assert >3000 chars, contains `VIN`.
- **Live UK search** (port): Toyota Corolla, `country: UK` → `Location: SW1A 1AA`, GBP price `£[\d,]+`, `Source: Autotrader UK`.
- **Live MOT history** (port): `YL08 NNV` → `# MOT History:`, `Registration: YL08NNV`, `## Outstanding Issues`, `## Full MOT History (N tests`.

Forward `CAR_DEALS_LOG_LEVEL` explicitly to the subprocess env (the SDK drops unknown env vars — same caveat as JS).

The test hits real sites and takes minutes; there's no way to run "one test." Document this.

---

## 11. `justfile` / scripts (mirror the JS ones)

```
init:      uv sync
check:     ruff check . && ruff format --check . && python -c "compile stuff"
lint-fix:  ruff check --fix . && ruff format .
test:      uv run python test/test_mcp_client.py
test-scraper:  uv run python -c "from car_deals_mcp.scraper import scrape_carscom; ..."  # quick smoke
check-all: check test
build:     uv build   # produces wheel + sdist (replaces npm pack)
run:       uv run car-deals-mcp   # stdio server, as a client would
```

No `just ec` (editorconfig-checker is JS-only) — use `ruff format --check` instead.

---

## 12. README + metadata

Port `README.md`:
- Tagline: US + UK sources, plus UK MOT history.
- Quick start via `uvx --from git+...` (replaces the `npx -y github:...` snippet).
- Document `--country` and `--cloakbrowser-key` startup args.
- Document `CAR_DEALS_LOG_LEVEL` env var + the MCP-SDK env-forwarding caveat.
- Supported sources tables (US + UK) — port as-is.
- Three tools documented with parameters tables + example responses (port the US, UK, and MOT example responses).
- Chrome/Chromium requirement note → now "CloakBrowser downloads its patched Chromium on first run (~200MB, cached)."

Update `server.json` / `mcp.json` (if kept — they're metadata-only, not runtime): name the Python entry, list all three tools, drop the stale HTTP port. The JS repo noted these were stale; the port is a chance to fix them.

---

## 13. `CLAUDE.md` (agent guidance) — port + update

Port the architecture/conventions doc, updated for Python:
- Three modules: `server.py` (MCP layer), `scraper.py` (all browser/scraping), `logger.py` (leveled stderr logging).
- "Progress notifications are load-bearing" section — same content, Python API references.
- "Scraping strategy differs per site" — Cars.com `data-vehicle-details` JSON-first, fragile Autotrader/KBB selectors, UK sources with client-side filters.
- "An unrecognized Cars.com filter looks like a broken scraper" — the slug/did-you-mean story. Keep.
- "Detail pages are deliberately not field-parsed" — prune + markdown. Keep.
- Logging conventions — same levels, stderr-only, `cause` chain → Python `__cause__`.
- **New section**: CloakBrowser instead of puppeteer-extra-plugin-stealth; why; the GOV.UK spike result.
- **New section**: `uv`/`uvx` is the runner; production via `uvx`.
- **New section**: startup args `--country` and `--cloakbrowser-key`.

---

## 14. What NOT to port / drop

- `puppeteer-extra`, `puppeteer-extra-plugin-stealth` (replaced by cloakbrowser).
- `turndown` (replaced by markdownify/selectolax).
- The `npm audit` `extract-zip` vuln — gone, different binary chain.
- The stale `mcp.json` HTTP port (stdio only).
- `eslint.config.js`, `.editorconfig-checker.json`, `justfile` JS scripts — replaced by ruff + uv.

---

## 15. Open decisions to make during implementation

1. **Sync vs async CloakBrowser**: `launch` (sync) vs `launch_async`. FastMCP tools are `async def` → likely use `launch_async` + `asyncio.gather` for parallel scrapers. If sync is simpler, run sync browser calls via `asyncio.to_thread`. Decide based on what reads cleanest; `launch_async` is probably the right call.
2. **Detail-page markdown library**: `markdownify` (simple) vs `selectolax` (fast, manual). Recommend `markdownify` + custom `<dl>` handler; revisit only if output quality is poor.
3. **UK detail pages**: the JS only allowlisted US hosts for `get_listing_details`. Decide whether to add `autotrader.co.uk`, `motors.co.uk`, `cinch.co.uk` to the allowlist. Recommend: add them (the MOT tool proves UK sites are reachable via CloakBrowser), but keep the detail conversion generic.
4. **`page.evaluate` porting**: JS uses `page.evaluate(() => {...browser code...})`. In Playwright Python, `page.evaluate` takes a JS string (the browser code stays JS, passed as a string) — there's no Python-in-browser. So the extraction functions stay as JS strings. Port the wrapper logic to Python but keep the `evaluate` payloads as JS string literals. **This is a real gotcha — flag it prominently.**

---

## 16. Verification before declaring done

- `uv run ruff check .` clean.
- `uv run python -c "import car_deals_mcp.server"` (import smoke).
- `uv run car-deals-mcp --country US` starts and answers `initialize` over stdio with `serverInfo.name == "car-deals-mcp"`.
- `uv run python test/test_mcp_client.py` passes (live, takes minutes) — including the GOV.UK MOT assertion, which is the proof the CloakBrowser port works end-to-end.

---

## Reference: authoritative JS source files to port from

- `src/scraper.js` (1591 lines) — `CarListing`, `carscom_slug`, all scrapers, `fetch_listing_details`, `fetch_mot_history`, `startHeartbeat`, `waitOutInterstitial`, `DETAIL_HOSTS`, `applyUkFilters`, `acceptConsent`, `parsePrice`/`parseMileage`/`parseYear`.
- `src/server.js` (563 lines) — tool registration, handlers, progress, output formatting.
- `src/logger.js` (165 lines) — levels, `preview`, `formatError`/cause chain, `installGlobalHandlers`.
- `test/mcp-client.test.js` (268 lines) — test cases to port.
- `CLAUDE.md` — conventions to preserve (updated for Python).
