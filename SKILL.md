---
name: car-deals
description: Search for used car listings across US and UK car marketplaces (Cars.com, Autotrader US/UK, KBB, Motors.co.uk, Cinch, eBay Motors), inspect full listing details, and check UK MOT vehicle history via CLI.
---

# Car Deals Search Skill (Agent CLI & MCP)

This project provides **first-class support for both AI Agent Skills (CLI) and MCP (Model Context Protocol)**. Use this skill in autonomous agent workflows (e.g. Odysseus, OpenCode, subagents) or scripts to search for used car deals across major US and UK automotive marketplaces, inspect detailed vehicle specifications and seller notes from listing URLs, and verify UK MOT histories and safety recalls.

## Dual Workflows

- **Agent Skill / CLI**: Run `car-deals-mcp <search|detail|mot>` directly for instant terminal or subprocess execution with Markdown or structured `--json` output.
- **MCP Server**: Run `car-deals-mcp serve` (or `car-deals-mcp --country <US|UK>`) over stdio for MCP protocol clients.

## CLI Invocation

Commands can be invoked directly or via `uv run` / `python -m`:

```bash
car-deals-mcp <command> [options]
# or
uv run car-deals-mcp <command> [options]
# or
python3 -m car_deals_mcp <command> [options]
```

---

## Available Commands

### 1. `search` — Search Car Deals

Search used car listings across multiple marketplaces with optional location and vehicle filters.

```bash
car-deals-mcp search [OPTIONS]
```

#### Options:
- `--make <name>`: Car manufacturer (e.g. `Toyota`, `BMW`, `Ford`, `Honda`).
- `--model <name>`: Car model (e.g. `Camry`, `3 Series`, `Mustang`, `Civic`).
- `--country <US|UK>`: Search region (`US` default, or `UK`). Selects regional sources, currency, and default location code.
- `--zip <code|postcode>`: Location code (`90210` for US, `SW1A 1AA` for UK).
- `--max-distance <miles>`: Search radius in miles (`0` for nationwide).
- `--year-min <year>`: Minimum vehicle model year (e.g. `2018`).
- `--year-max <year>`: Maximum vehicle model year (e.g. `2024`).
- `--price-max <amount>`: Maximum price (USD in US, GBP in UK).
- `--mileage-max <miles>`: Maximum odometer mileage.
- `--max-results <count>`: Maximum listings per source (default: `10`).
- `--sources <src...>`: Marketplaces to query:
  - **US**: `cars.com`, `autotrader`, `kbb` (default: `cars.com`)
  - **UK**: `autotrader-uk`, `motors`, `cinch`, `ebay` (default: `autotrader-uk`)
- `--one-owner`: *(US only)* Filter for CARFAX 1-Owner vehicles.
- `--no-accidents`: *(US only)* Filter for vehicles with no reported accidents.
- `--personal-use`: *(US only)* Filter for personal use vehicles only.
- `--transmission <type>`: *(UK only)* Gearbox filter (e.g. `Manual`, `Automatic`).
- `--drivetrain <type>`: *(UK only)* Drivetrain filter (e.g. `AWD`, `FWD`, `RWD`, `4WD`).
- `--json`: Output results as structured JSON instead of Markdown.
- `--cloakbrowser-key <key>`: CloakBrowser license key (or via `CLOAKBROWSER_LICENSE_KEY`).
- `-v`, `--verbose`: Enable debug logging on stderr.

#### Examples:

```bash
# US search: 2020+ Toyota Camry under $25,000 near Los Angeles
car-deals-mcp search --make Toyota --model Camry --year-min 2020 --price-max 25000 --zip 90210 --max-distance 50

# US search with CARFAX badges across all US sources
car-deals-mcp search --make Honda --model Accord --one-owner --no-accidents --sources cars.com autotrader kbb

# UK search: Automatic BMW 3 Series under £20,000 near London
car-deals-mcp search --country UK --make BMW --model "3 Series" --zip "SW1A 1AA" --price-max 20000 --transmission Automatic

# UK search on Cinch (returns number plates for MOT checks)
car-deals-mcp search --country UK --make Volkswagen --model Golf --sources cinch

# Structured JSON output
car-deals-mcp search --make Ford --model F-150 --price-max 40000 --json
```

---

### 2. `detail` — Get Full Listing Details

Fetch and parse the full detail page for any single car listing URL (VIN, trim, engine, transmission, options, features, dealer history, seller notes).

```bash
car-deals-mcp detail <URL> [OPTIONS]
```

#### Options:
- `<URL>`: Listing detail page URL (positional or `--url <URL>`). Supported domains: `cars.com`, `autotrader.com`, `kbb.com`, `autotrader.co.uk`, `motors.co.uk`, `cinch.co.uk`, `ebay.co.uk`.
- `--include-links`: Retain hyperlink URLs in the markdown output.
- `--include-images`: Retain image URLs in the markdown output.
- `--max-length <chars>`: Truncate output at N characters (default: `30000`).
- `--json`: Output results as structured JSON.

#### Examples:

```bash
# Fetch details for a Cars.com listing
car-deals-mcp detail "https://www.cars.com/vehicledetail/12345678-abcd/"

# Fetch details from Autotrader UK with links
car-deals-mcp detail --include-links "https://www.autotrader.co.uk/car-details/202401011234567"

# JSON output
car-deals-mcp detail --json "https://www.cinch.co.uk/used-cars/bmw/3-series/..."
```

---

### 3. `mot` — Check UK MOT Vehicle History

Check the official GOV.UK MOT history and active safety recalls for a UK-registered vehicle using its registration number plate.

```bash
car-deals-mcp mot <REGISTRATION> [OPTIONS]
```

#### Options:
- `<REGISTRATION>`: UK number plate (e.g. `"YL08 NNV"` or `"YL08NNV"`, positional or `--registration <REG>`).
- `--json`: Output results as structured JSON (including vehicle details, test history, dangerous/major/minor defects, and recall status).

#### Examples:

```bash
# Check MOT history and outstanding defects
car-deals-mcp mot "YL08 NNV"

# Check MOT with JSON output
car-deals-mcp mot YL08NNV --json
```

---

### 4. `serve` — Run Stdio MCP Server

Start the stdio MCP server for MCP clients (Claude Desktop, Cursor, VS Code, etc.).

```bash
car-deals-mcp serve [--country US|UK] [--cloakbrowser-key <key>]
# or run directly with no subcommand
car-deals-mcp --country UK
```

---

## Agent Workflow Recommendations

1. **Finding Used Cars**:
   - Start with `car-deals-mcp search --make <Make> --model <Model> [filters]`.
   - In the UK, search `cinch` or view `autotrader.co.uk` detail pages to obtain the vehicle's registration plate (`vrm` / `registration`).

2. **Inspecting Vehicle History & Defects**:
   - For UK vehicles, take the registration plate and run `car-deals-mcp mot <REGISTRATION>` to verify MOT expiry, past failures, mileage progression, and outstanding safety recalls.

3. **Evaluating Listing Specifics**:
   - Run `car-deals-mcp detail "<URL>"` on candidate listings to inspect specific trim features, options, full vehicle history, and seller notes not included in summary cards.
