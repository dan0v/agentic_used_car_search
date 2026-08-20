# Car Deals Search MCP

> **Search used car listings from Cars.com, Autotrader, and KBB (US) or Autotrader UK, Motors.co.uk, and Cinch (UK) with AI assistants — plus UK MOT history checks**

An MCP (Model Context Protocol) server that aggregates and searches car listings from multiple sources across the US and UK. Scrapes listings in parallel, extracts price, mileage, dealer info, and applies optional CARFAX-style filters (1-owner, no accidents, personal use) for US sources. UK listings return title, price (GBP), mileage, and location/distance where available. A dedicated `check_mot_history` tool pulls a UK vehicle's MOT history from the GOV.UK service and surfaces any outstanding defects and safety recalls.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 🚀 Quick Start

### Prerequisites

- **Node.js** (v16 or higher)
- **Chrome/Chromium** browser installed (required by Puppeteer)
  - If Chrome is not in the default location, set `PUPPETEER_EXECUTABLE_PATH` environment variable to point to your Chrome/Chromium binary

### Add to your MCP client (recommended)

No clone, no install. Add this to your client's MCP config and restart it — `npx`
fetches and runs the server on first launch:

```json
{
  "mcpServers": {
    "car-deals": {
      "command": "npx",
      "args": ["-y", "github:ejlevin1/car_deals_search_mcp"]
    }
  }
}
```

If Chrome is not in the default location, point Puppeteer at it:

```json
{
  "mcpServers": {
    "car-deals": {
      "command": "npx",
      "args": ["-y", "github:ejlevin1/car_deals_search_mcp"],
      "env": {
        "PUPPETEER_EXECUTABLE_PATH": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      }
    }
  }
}
```

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
- `npx` installs into its cache on first run, so the **first launch takes ~15-20s**.
  Later launches are fast until the cache is cleared.
- This is not published to the npm registry; the `github:` spec above installs
  straight from this repo's default branch.

Verify it works without any client:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  | npx -y github:ejlevin1/car_deals_search_mcp
```

You should see `Car Deals MCP Server running on stdio` followed by a JSON result
naming `car-deals-mcp`.

### Run from a local clone instead

Use this if you want to modify the scrapers — edits take effect on the next
client restart, with no `npx` cache in the way:

```bash
git clone https://github.com/ejlevin1/car_deals_search_mcp.git
cd car_deals_search_mcp
npm ci
```

```json
{
  "mcpServers": {
    "car-deals": {
      "command": "node",
      "args": ["/absolute/path/to/car_deals_search_mcp/src/server.js"]
    }
  }
}
```

The path must be absolute — MCP clients do not launch servers from the repo
directory.

### Testing Standalone

```bash
# Full end-to-end MCP client test (hits Cars.com for real, ~60-90s)
npm run test:mcp

# Quick scraper smoke test
npm test

# Or test manually with a specific search
node -e "
const { scrapeCarscom } = require('./src/scraper.js');
scrapeCarscom({
  make: 'Toyota',
  model: 'Camry',
  oneOwner: true,
  noAccidents: true,
  personalUse: true
}, 5).then(listings => listings.forEach(l => console.log(l.format())));
"

# Or test a UK search
node -e "
const { scrapeAutotraderUK } = require('./src/scraper.js');
scrapeAutotraderUK({
  make: 'Toyota',
  model: 'Corolla',
  zip: 'SW1A 1AA',
  priceMax: 15000
}, 5).then(listings => listings.forEach(l => console.log(l.format())));
"

# Or test a UK MOT history check
node -e "
const { fetchMotHistory } = require('./src/scraper.js');
fetchMotHistory('YL08 NNV').then(r => console.log(JSON.stringify(r.outstandingIssues, null, 2)));
"
```

If you have [`just`](https://github.com/casey/just) installed, `just --list`
shows every task (`just check`, `just test`, `just build`, ...).

---

## ✨ Features

- **Multi-source aggregation**: Search Cars.com, Autotrader, and KBB (US) or Autotrader UK, Motors.co.uk, and Cinch (UK) simultaneously
- **US & UK support**: Pass `country: "UK"` to search UK marketplaces with a postcode instead of a ZIP code
- **Smart filtering**: CARFAX-style filters (1-Owner, No Accidents, Personal Use) for US sources
- **MOT history checks (UK)**: Pull a UK vehicle's full MOT history from GOV.UK and get outstanding dangerous/major/minor defects, advisories, MOT expiry, and active safety recalls
- **Deal ratings**: Heuristic-based deal quality assessment (US sources)
- **Parallel scraping**: Fast concurrent queries across sources
- **Stealth mode**: Puppeteer with anti-bot detection techniques

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
| Autotrader UK | ✅    | ✅              | ✅              | Default UK source; searched by postcode           |
| Motors.co.uk  | ✅    | ✅ (approx.)    | ✅ (distance)   | Mileage shown rounded (e.g. "41.2k")             |
| Cinch         | ✅    | ✅              | ❌              | Nationwide delivery retailer, no postcode/distance |

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

| Parameter    | Type     | Required | Description |
|--------------|----------|----------|-------------|
| `make`       | string   | ✅       | Car manufacturer (e.g., "Toyota", "Honda") |
| `model`      | string   | ✅       | Car model (e.g., "Camry", "Accord") |
| `country`    | string   | ❌       | `"US"` (default; the original and most fully supported scope) or `"UK"`. Selects the default `sources`, default `zip`, and currency |
| `zip`        | string   | ❌       | Location code. US: ZIP code (default: "90210"). UK: postcode (default: "SW1A 1AA") |
| `maxDistance`| integer  | ❌       | US only. Search radius in miles from the ZIP (e.g. 25, 50, 100, 500). `0` = nationwide. Default: site default (~30-50 mi). UK sources do not honour this |
| `yearMin`    | integer  | ❌       | Minimum model year (applied client-side where the source lacks a server-side filter) |
| `yearMax`    | integer  | ❌       | Maximum model year (applied client-side where the source lacks a server-side filter) |
| `priceMax`   | integer  | ❌       | Maximum price. US: in USD. UK: in GBP |
| `mileageMax` | integer  | ❌       | Maximum mileage. Applied client-side for sources that lack a server-side mileage filter (e.g. Motors.co.uk, Cinch) |
| `maxResults` | integer  | ❌       | Max results per source (default: 10) |
| `sources`    | array    | ❌       | Sources to query. US: `["cars.com","autotrader","kbb"]`. UK: `["autotrader-uk","motors","cinch"]`. Default: `"cars.com"` (US) or `"autotrader-uk"` (UK) |
| `oneOwner`   | boolean  | ❌       | US only. Filter for CARFAX 1-owner vehicles. Ignored by UK sources |
| `noAccidents`| boolean  | ❌       | US only. Filter for no accidents reported. Ignored by UK sources |
| `personalUse`| boolean  | ❌       | US only. Filter for personal use only (not rental/fleet). Ignored by UK sources |

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

| Parameter       | Type    | Required | Description |
|-----------------|---------|----------|-------------|
| `registration`  | string | ✅       | UK vehicle registration (number plate), with or without spaces. e.g. `"YL08 NNV"` or `"YL08NNV"` |

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

Rather than parsing each field with a selector, the page is pruned (nav, ads,
scripts, recommendation carousels removed) and converted to markdown. Nothing to
re-fix when the sites reshuffle their markup, and the caller sees every detail
the page shows.

### Parameters

| Parameter       | Type    | Required | Description |
|-----------------|---------|----------|-------------|
| `url`           | string  | ✅       | Detail page URL from a `search_car_deals` result. Must be on cars.com, autotrader.com, or kbb.com |
| `includeLinks`  | boolean | ❌       | Keep hyperlink URLs (link text is kept either way; default: `false`) |
| `includeImages` | boolean | ❌       | Keep image references (default: `false`) |
| `maxLength`     | integer | ❌       | Truncate markdown at N characters (default: 30000) |

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

- **Scraping**: Puppeteer (headless Chromium) with stealth plugin to bypass bot detection
- **Concurrency**: Parallel scraper workers for simultaneous multi-source queries
- **Protocol**: Implements MCP (Model Context Protocol) for AI assistant integration
- **Data extraction**: Source-specific parsers normalize listings into a common schema

### Chrome/Chromium Requirement

This project uses Puppeteer, which requires Chrome or Chromium to be installed:

- **macOS**: Chrome is typically at `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
- **Linux**: Usually auto-detected by Puppeteer or at `/usr/bin/chromium-browser`
- **Windows**: Typically at `C:\Program Files\Google\Chrome\Application\chrome.exe`

If Puppeteer cannot find your browser, set the environment variable:

```bash
export PUPPETEER_EXECUTABLE_PATH="/path/to/chrome"
```

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
      "command": "npx",
      "args": ["-y", "github:ejlevin1/car_deals_search_mcp"],
      "env": {
        "CAR_DEALS_LOG_LEVEL": "debug"
      }
    }
  }
}
```

Running it directly:

```bash
CAR_DEALS_LOG_LEVEL=debug node src/server.js
node src/server.js --verbose
```

Every exception the server hits is written to stderr, including ones that are
also reported to the client as a tool error, and ones that escape the tool
handlers entirely. Stack traces (and the `cause` chain that identifies which
Puppeteer call actually failed) appear at `debug` and above.

> **Note:** the MCP SDK forwards only a fixed subset of environment variables
> (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) to a server subprocess.
> `CAR_DEALS_LOG_LEVEL` therefore has to be set in the client's `env` block as
> above — exporting it in your shell will not reach a client-launched server.

---

## 🧪 Development & Testing

```bash
# Run tests
npm test

# Test individual scrapers
node src/scraper.js

# Run the end-to-end MCP test with server-side debug logging
CAR_DEALS_LOG_LEVEL=debug npm run test:mcp

# View code structure
ls -la src/
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

- **Repository**: https://github.com/SiddarthaKoppaka/car_deals_search_mcp
- **Issues**: https://github.com/SiddarthaKoppaka/car_deals_search_mcp/issues
- **MCP Protocol**: https://modelcontextprotocol.io
