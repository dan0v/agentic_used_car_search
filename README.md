# Car Deals Search MCP

> **Search used car listings from Cars.com, Autotrader, and KBB (US) or Autotrader UK, Motors.co.uk, Cinch, and eBay Motors (UK) with AI assistants — plus UK MOT history checks**

An MCP (Model Context Protocol) server that aggregates and searches car listings from multiple sources across the US and UK. Scrapes listings in parallel, extracts price, mileage, dealer info, and applies optional CARFAX-style filters (1-owner, no accidents, personal use) for US sources. UK listings return title, price (GBP), mileage, and location/distance where available. A dedicated `check_mot_history` tool pulls a UK vehicle's MOT history from the GOV.UK service and surfaces any outstanding defects and safety recalls.

This is the **Python port**: `uv`/`uvx` runner, [CloakBrowser](https://github.com/cloakbrowser/cloakbrowser) for stealth browsing, the official [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 🚀 Quick Start

### Prerequisites

- **Python** 3.11+ (3.14 recommended; `uv` will fetch one for you)
- **[uv](https://github.com/astral-sh/uv)** installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- No Chrome install needed — CloakBrowser downloads its patched Chromium 151 build on first run (~200 MB, cached).

### Add to your MCP client (recommended)

No clone, no install. Add this to your client's MCP config and restart it — `uvx` fetches and runs the server on first launch:

```json
{
  "mcpServers": {
    "car-deals": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/dan0v/car_deals_search_mcp",
        "car-deals-mcp",
        "--country",
        "UK",
        "--cloakbrowser-key",
        "cb_xxxxxxxx"
      ]
    }
  }
}
```

Two startup args:

- `--country` — default country the server operates in (`"US"` or `"UK"`). Sets the default for `search_car_deals` when the client doesn't pass `country`. Allows a deployment to be UK-only or US-only by configuration without per-call args. A per-call `country` still overrides it.
- `--cloakbrowser-key` — CloakBrowser license key (free GitHub-sign-in key, or paid). Can also be read from the `CLOAKBROWSER_LICENSE_KEY` env var; the CLI arg wins. The key selects the always-current Chromium 151 build; without it CloakBrowser falls back to the older free Chromium 146 build (still works, ages over time).

Where that config lives:

| Client | Config file |
|--------|-------------|
| Claude Code | `.mcp.json` in your project, or `~/.claude.json` |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| VS Code / Copilot | `.vscode/mcp.json` (under a `servers` key rather than `mcpServers`) |
| Cursor | `~/.cursor/mcp.json` |

Notes:

- The transport is **stdio** — no port, no URL, nothing to start beforehand.
- `uvx` installs into its cache on first run, so the **first launch takes ~15-30s** (plus the one-time Chromium download). Later launches are fast until the cache is cleared.

Verify it works without any client:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  | uvx --from git+https://github.com/dan0v/car_deals_search_mcp car-deals-mcp --country US
```

You should see `Car Deals MCP Server running on stdio` on stderr followed by a JSON result naming `car-deals-mcp` on stdout.

### Run from a local clone instead

Use this if you want to modify the scrapers — edits take effect on the next client restart, with no `uvx` cache in the way:

```bash
git clone https://github.com/dan0v/car_deals_search_mcp.git
cd car_deals_search_mcp
uv sync
```

```json
{
  "mcpServers": {
    "car-deals": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/car_deals_search_mcp", "car-deals-mcp", "--country", "US"]
    }
  }
}
```

### CLI & Agent Skills Usage

All MCP tools are directly available via the `car-deals-mcp` CLI and as an AI agent Skill (`SKILL.md` / `skills/car-deals/SKILL.md`):

```bash
# 1. Search car deals (US or UK)
uv run car-deals-mcp search --make Toyota --model Camry --price-max 25000
uv run car-deals-mcp search --country UK --make BMW --model "3 Series" --transmission Automatic

# 2. Get full listing details as Markdown or JSON
uv run car-deals-mcp detail "https://www.cars.com/vehicledetail/..."
uv run car-deals-mcp detail --json "https://www.autotrader.co.uk/car-details/..."

# 3. Check UK vehicle MOT history & safety recalls
uv run car-deals-mcp mot "YL08 NNV"
uv run car-deals-mcp mot YL08NNV --json

# 4. Run MCP stdio server
uv run car-deals-mcp serve --country UK
```

See [SKILL.md](SKILL.md) for full parameter references and AI agent integration instructions.

### Testing Standalone

```bash
# Full end-to-end MCP client test (hits Cars.com / Autotrader UK / GOV.UK for real, takes minutes)
uv run python test/test_mcp_client.py

# Quick scraper smoke test
just test-scraper
# ...or by hand
uv run python -c "import asyncio; from car_deals_mcp.scrapers import scrape_carscom; \
  from car_deals_mcp.types import SearchParams; \
  r = asyncio.run(scrape_carscom(SearchParams(make='Toyota', model='Camry', one_owner=True), 5)); \
  [print(l.format()) for l in r.listings]"

# A UK search
uv run python -c "import asyncio; from car_deals_mcp.scrapers import scrape_autotrader_uk; \
  from car_deals_mcp.types import SearchParams; \
  r = asyncio.run(scrape_autotrader_uk(SearchParams(make='Toyota', model='Corolla', zip='SW1A 1AA', price_max=15000), 5)); \
  [print(l.format()) for l in r.listings]"

# A UK MOT history check
uv run python -c "import asyncio, json; from car_deals_mcp.scrapers import fetch_mot_history; \
  r = asyncio.run(fetch_mot_history('YL08 NNV')); print(json.dumps(r.outstanding_issues, indent=2))"
```

If you have [`just`](https://github.com/casey/just) installed, `just --list`
shows every task (`just check`, `just test`, `just build`, ...).

---

## ✨ Features

- **Multi-source aggregation**: Search Cars.com, Autotrader, and KBB (US) or Autotrader UK, Motors.co.uk, Cinch, and eBay Motors (UK) simultaneously
- **US & UK support**: Pass `country: "UK"` to search UK marketplaces with a postcode instead of a ZIP code
- **Smart filtering**: CARFAX-style filters (1-Owner, No Accidents, Personal Use) for US sources
- **MOT history checks (UK)**: Pull a UK vehicle's full MOT history from GOV.UK and get outstanding dangerous/major/minor defects, advisories, MOT expiry, and active safety recalls
- **Deal ratings**: Heuristic-based deal quality assessment (US sources)
- **Parallel scraping**: Fast concurrent queries across sources (`asyncio.gather`)
- **Stealth browsing**: CloakBrowser patches Chromium at the C++ source level (canvas, WebGL, WebRTC, `navigator.webdriver`, CDP detection) — clears the Imperva/Cloudflare checks that JS-injection stealth plugins lose to

---

## 📊 Supported Sources

### US (`country: "US"`, default)

| Source     | Price | Mileage | Color | VIN / Specs | Deal Rating | Dealer Info | CARFAX Filters |
|------------|:-----:|:-------:|:-----:|:-----------:|:-----------:|:-----------:|:--------------:|
| Cars.com   | ✅    | ✅      | ✅    | ✅          | ✅          | ✅          | ✅             |
| Autotrader | ✅    | ✅      | ❌    | ❌          | ⚠️ Limited   | ✅          | ⚠️ Limited     |
| KBB        | ✅    | ✅      | ❌    | ❌          | ✅          | ⚠️ Limited   | ⚠️ Limited     |

### UK (`country: "UK"`)

| Source        | Price | Mileage        | Location        | Notes                                            |
|---------------|:-----:|:--------------:|:---------------:|--------------------------------------------------|
| Autotrader UK | ✅    | ✅              | ✅              | Default UK source; direct GraphQL API (`/at-gateway`); server-side filters for price, year, mileage, transmission, drivetrain, distance; detail page exposes registration |
| Motors.co.uk  | ✅    | ✅ (approx.)    | ✅ (distance)   | Browser-scraped (Cazoo stack); mileage shown rounded (e.g. "41.2k"); client-side filters; skipped when transmission/drivetrain requested |
| Cinch         | ✅    | ✅              | ❌              | Direct REST API; returns registration plate (`vrm`/`fullRegistration`); nationwide delivery; server-side price, year, transmission, drivetrain; client-side mileage |
| eBay Motors   | ✅    | ✅ (browser)    | ⚠️              | Uses official Browse API when `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` are set; falls back to browser scraping otherwise. Server-side year, price, transmission; client-side mileage; skipped when drivetrain requested. No registration plate. |

Cars.com cards embed a `data-vehicle-details` JSON payload, which is where the
VIN, trim, body style, drivetrain, fuel type, exterior color, dealer identity and
CPO flag come from. The scraper reads that payload first and falls back to
parsing the visible card text, so a markup reshuffle degrades the results
instead of emptying them.

Two caveats:

- Exterior color in search results is a generic slug (`Silver`, `Gray`, `Blue`),
  not the manufacturer's marketing name. Cars.com only publishes the latter
  ("Predawn Gray Mica") on the detail page — use
  [`get_listing_details`](#-mcp-tool-get_listing_details) when the exact paint
  name matters. Interior color is detail-page only.
- Autotrader, KBB and the UK source cards carry no equivalent `data-vehicle-details`
  payload, so their listings return the basics only. The UK scrapers apply
  year/price/mileage filters client-side where the site lacks a server-side filter.

---

## 🔧 MCP Tool: `search_car_deals`

### Parameters

All parameters are optional.

| Parameter      | Type     | Description |
|----------------|----------|-------------|
| `make`         | string   | Car manufacturer (e.g., "Toyota", "Honda"). Recommended; searches all cars if omitted |
| `model`        | string   | Car model (e.g., "Camry", "Accord"). Recommended; searches all cars if omitted |
| `country`      | string   | `"US"` (default) or `"UK"`. Selects the default `sources`, default `zip`/postcode, and currency |
| `zip`          | string   | Location code. US: ZIP code (default: "90210"). UK: postcode (default: "SW1A 1AA") |
| `maxDistance`  | integer  | Search radius in miles. US: from ZIP code (0 = nationwide). UK: from postcode on Autotrader UK (0 = nationwide / 1500 mi). Motors.co.uk and Cinch do not honour this. Default: site default (~30-50 mi) |
| `yearMin`      | integer  | Minimum model year (applied server-side where supported, otherwise client-side) |
| `yearMax`      | integer  | Maximum model year (applied server-side where supported, otherwise client-side) |
| `priceMax`     | integer  | Maximum price. US: in USD. UK: in GBP |
| `mileageMax`   | integer  | Maximum mileage. Applied client-side for sources that lack a server-side mileage filter (e.g. Motors.co.uk, Cinch) |
| `maxResults`   | integer  | Max results per source (default: 10) |
| `sources`      | array    | Sources to query. US: `["cars.com", "autotrader", "kbb"]`. UK: `["autotrader-uk", "motors", "cinch", "ebay"]`. Default: `["cars.com"]` (US) or `["autotrader-uk"]` (UK) |
| `oneOwner`     | boolean  | US only. Filter for CARFAX 1-owner vehicles. Ignored by UK sources |
| `noAccidents`  | boolean  | US only. Filter for no accidents reported. Ignored by UK sources |
| `personalUse`  | boolean  | US only. Filter for personal use only (not rental/fleet). Ignored by UK sources |
| `transmission` | string   | UK only. Filter by gearbox (e.g. "Manual", "Automatic"). Applied server-side by Autotrader UK, Cinch, and eBay Motors. Motors.co.uk is skipped with a warning |
| `drivetrain`   | string   | UK only. Filter by drivetrain (e.g. "RWD", "FWD", "AWD", "4WD", or full names). Applied server-side by Autotrader UK and Cinch. Motors.co.uk and eBay Motors are skipped with a warning |

### Example Response

```
2024 Toyota Camry SE
  Price: $27,400 (dropped $1.4K)
  Est. Payment: $513/mo
  Mileage: 47,822 mi.
  Exterior Color: White
  Specs: SE | Sedan | FWD | Gasoline
  VIN: 4T1G11AK4RU902993
  Deal Rating: Good Deal
  Badges: Certified Pre-Owned | 1-Owner | No Accidents
  Awards: American-Made Index
  Dealer: North Hollywood Toyota (4.5 stars)
  Location: Los Angeles, CA (5 mi)
  Source: Cars.com
  Photo: https://platform.cstatic-images.com/in/v2/...jpg
  https://www.cars.com/vehicledetail/...
```

### Example UK Response

```
Toyota Corolla 1.8 VVT-h Icon CVT Euro 6 (s/s) 5dr
  Price: £14,309
  Mileage: 56,847 miles
  Location: Available from Portsmouth (62 miles)
  Source: Autotrader UK
  https://www.autotrader.co.uk/car-details/...
```

---

## 🔧 MCP Tool: `check_mot_history` (UK)

UK-only. Checks a UK-registered vehicle's MOT history via the GOV.UK service
(`check-mot.service.gov.uk`) and surfaces any **outstanding issues** — the most
recent test result, outstanding dangerous/major/minor defects and advisories,
the MOT expiry date, and any active safety recalls. Handy before buying a used
car listed on the UK sources.

The GOV.UK page is server-rendered with stable `data-test-id` attributes (the
service's own test hooks), so the scraper reads them directly rather than
fragile class names. The site sits behind an Imperva bot check, so a call can
take ~15-45s while it clears.

### Parameters

| Parameter       | Type    | Description |
|-----------------|---------|-------------|
| `registration`  | string  | **Required.** UK vehicle registration (number plate), with or without spaces. e.g. `"YL08 NNV"` or `"YL08NNV"` |

### Example Response (excerpt)

```markdown
# MOT History: BMW 3 SERIES

**Registration:** YL08NNV
**Colour:** Silver
**Fuel:** Petrol
**First registered:** 24 July 2008
**MOT valid until:** 17 May 2027
**Source:** GOV.UK (https://www.check-mot.service.gov.uk/results?registration=YL08NNV)

## Outstanding Issues

**Latest test (18 May 2026):** PASS
**Mileage at last test:** 62,334 miles

**Major defects (repair immediately):**
- Offside Front Coil spring fractured or broken (5.3.1 (b) (i))
**Minor defects (repair soon):**
- Front Suspension arm ball joint dust cover severely deteriorated n/s & o/s (5.3.4 (b) (i))
**Advisories (monitor and repair if necessary):**
- Rear Tyre worn close to legal limit/worn on edge both (5.2.3 (e))

## ⚠️ Safety Recall

This vehicle has been recalled by BMW. Contact your local BMW dealership to arrange a free repair.

No outstanding safety recalls.

## Full MOT History (19 tests)

### 18 May 2026 — PASS
- Mileage: 62,334 miles
- Test number: 6469 9395 8456
- Expiry date: 17 May 2027
- Advisories:
  - Rear Tyre worn close to legal limit/worn on edge both (5.2.3 (e))
  ...
```

If no MOT record exists for the registration, the tool returns a clear
"no record" message rather than failing.

---

## 🔧 MCP Tool: `get_listing_details`

Fetches a single listing's detail page and returns it as markdown. Search results
only carry the summary card (title, price, mileage, deal rating, dealer); the
detail page adds VIN, trim, engine, transmission, drivetrain, MPG, exterior and
interior colors, the full options/features list, price history, vehicle history
and seller notes.

For **Autotrader UK**, detail pages are harvested directly via HTTP SSR JSON hydration (`__staticRouterHydrationData`), returning rich structured data (including the vehicle registration plate, MOT status, and running costs) without consuming a browser session. For all other sites (or as fallback), the page is pruned (nav, ads, scripts, recommendation carousels removed) and converted to markdown with `markdownify` (a custom `<dl>` handler pairs each spec term/value into `- **Term:** Value`). Nothing to re-fix when the sites reshuffle their markup, and the caller sees every detail the page shows.

### Parameters

| Parameter       | Type    | Description |
|-----------------|---------|-------------|
| `url`           | string  | **Required.** Listing detail page URL from a `search_car_deals` result. Supported hosts: `cars.com`, `autotrader.com`, `kbb.com`, `autotrader.co.uk`, `motors.co.uk`, `cinch.co.uk`, `ebay.co.uk` (or `ebay.com`) |
| `includeLinks`  | boolean | Keep hyperlink URLs (link text is kept either way; default: `false`) |
| `includeImages` | boolean | Keep image references (default: `false`) |
| `maxLength`     | integer | Truncate markdown at N characters (default: 30000) |

### Example Response (excerpt)

```markdown
# Used 2020 Toyota Camry SE

**Source:** Cars.com
**URL:** https://www.cars.com/vehicledetail/...

## Features & specs

VIN: 4T1G11AK3LU858833 / Stock #: C131351

- Predawn Gray Mica exterior color
- Dynamic Force 2.5L I-4 port/direct injection, DOHC, VVT-iE/VVT-i engine
- 28-39 mpg
- Front-wheel Drive drivetrain
- 8-Speed Automatic transmission

### Safety
- Automatic Emergency Braking
- Lane Departure Warning
```

Detail pages are served behind an anti-bot interstitial more often than search
pages are; the tool waits it out (up to 45s) rather than returning the challenge
page, so a call can take ~20-60s.

---

## 🛠️ Technical Details

- **Scraping**: CloakBrowser (a patched Playwright `Browser`) — `launch_async` for the async MCP server, `humanize=True` for human-like input timing
- **Concurrency**: `asyncio.gather` for parallel multi-source queries
- **Protocol**: Implements MCP (Model Context Protocol) via the official Python SDK's lowlevel `Server`
- **Data extraction**: Source-specific parsers normalize listings into a common `CarListing` dataclass
- **HTML→Markdown**: `markdownify` (with a custom `<dl>` converter) for the detail tool
- **Stealth engine**: CloakBrowser downloads and caches patched Chromium (~200 MB under `~/.cache/cloakbrowser`). No local Chrome install or driver configuration is needed.

---

## 🔑 eBay Motors UK (Optional API Credentials)

The `ebay` UK source uses eBay's official **Browse API** when OAuth2
application credentials are present, and falls back to browser scraping
`ebay.co.uk` when they are not. The API path returns richer structured data
and avoids anti-bot checks entirely.

To enable the API path, register an application at the
[eBay Developer Portal](https://developer.ebay.com), then set the
`client_id` and `client_secret` (under *Application Keys*) as environment
variables in the client's `env` block:

```json
{
  "mcpServers": {
    "car-deals": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dan0v/car_deals_search_mcp", "car-deals-mcp", "--country", "UK"],
      "env": {
        "EBAY_CLIENT_ID": "your-ebay-client-id",
        "EBAY_CLIENT_SECRET": "your-ebay-client-secret"
      }
    }
  }
}
```

Without these, `ebay` still works via CloakBrowser scraping — just slower and
more fragile to markup changes. The token is cached in-process (~2h) so
repeated searches reuse it.

---

## 🪵 Logging & verbose mode

All diagnostics go to **stderr** — stdout is the MCP transport and anything
written there corrupts the protocol stream.

| Level | What you get |
|-------|--------------|
| `silent` | nothing |
| `error` | failures only |
| `info` *(default)* | one line per tool call and per scraper outcome |
| `debug` | request arguments, the built search URL for each site, per-scraper timings, retry attempts, progress-token resolution, and full stack traces |
| `trace` | everything in `debug`, plus a preview of the response payload |

Set the level with the `CAR_DEALS_LOG_LEVEL` environment variable, or pass
`--verbose` (equivalent to `debug`) or `--trace` on the command line. The
environment variable wins when both are given.

```json
{
  "mcpServers": {
    "car-deals": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dan0v/car_deals_search_mcp", "car-deals-mcp", "--country", "US"],
      "env": {
        "CAR_DEALS_LOG_LEVEL": "debug"
      }
    }
  }
}
```

Running it directly:

```bash
CAR_DEALS_LOG_LEVEL=debug uv run car-deals-mcp
uv run car-deals-mcp --verbose
```

Every exception the server hits is written to stderr, including ones that are
also reported to the client as a tool error, and ones that escape the tool
handlers entirely. Stack traces (and the `__cause__` chain that identifies
which browser call actually failed) appear at `debug` and above.

> **Note:** the MCP SDK forwards only a fixed subset of environment variables
> to a server subprocess. `CAR_DEALS_LOG_LEVEL` therefore has to be set in the
> client's `env` block as above — exporting it in your shell will not reach a
> client-launched server.

---

## 🧪 Development & Testing

```bash
uv sync                               # install deps
just check                            # ruff lint + format check + mypy type check (the offline gate)
just syntax                           # smoke-test all imports
just test-scraper                     # quick Cars.com scraper smoke test
just test                             # live end-to-end test (minutes)
CAR_DEALS_LOG_LEVEL=debug just test   # with server-side debug logging
```

---

## 🤝 Contributing

Contributions are welcome! Please follow this workflow:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Add tests for new functionality
4. Commit your changes (`git commit -m 'Add amazing feature'`)
5. Push to the branch (`git push origin feature/amazing-feature`)
6. Open a Pull Request

Please include test coverage for scraping/parsing changes to avoid regressions when source sites update.

---

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details

---

## 🔗 Links

- **Repository**: https://github.com/dan0v/car_deals_search_mcp
- **Issues**: https://github.com/dan0v/car_deals_search_mcp/issues
- **MCP Protocol**: https://modelcontextprotocol.io
