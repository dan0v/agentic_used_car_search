# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

`just` wraps the npm scripts; either works.

```bash
just init          # npm ci
just check         # lint + editorconfig + syntax parse — the offline gate, run before committing
just lint-fix      # eslint --fix
just test          # npm run test:mcp — full end-to-end MCP client test, hits Cars.com live (~60-90s)
just test-scraper  # quick scrapeCarscom smoke test, prints formatted listings
just check-all     # check + test
just build         # npm pack (no compile step; plain CommonJS)
just run           # start the server on stdio
```

There is no test framework and no unit tests. `test/mcp-client.test.js` is a
single node script asserting against a real Cars.com response — there is no way
to run "one test"; comment out sections if you need to narrow it. It fails when
the live site changes, which is intentional (see below).

Drive the server by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' | node src/server.js
```

## Architecture

Three files. `src/server.js` is the MCP protocol layer; `src/scraper.js` is
everything Puppeteer; `src/logger.js` is leveled stderr logging that both
import.

**server.js** — registers three tools on a stdio `Server` from
`@modelcontextprotocol/sdk`, holds no state, and does no parsing. It maps tool
arguments onto a `params` object, fans out to the selected scrapers with
`Promise.all`, and concatenates `listing.format()` output into one markdown
text block. A failing scraper is caught per-source and reported in an
`**Errors:**` footer rather than failing the call. `search_car_deals` defaults
`sources` to `['cars.com']` (US) or `['autotrader-uk']` (UK) for reliability,
despite the schema advertising all sources. `country` ("US" default, "UK")
selects the default source set, default postcode/ZIP and currency; the handler
is a single `isUK` branch so a future country can be added as another `else if`.
`check_mot_history` (UK only) drives the GOV.UK MOT service and returns a
structured record rather than a `CarListing`.

**scraper.js** — one `scrapeX(params, maxResults, sendProgress)` function per
site, each launching and closing its own browser, plus `fetchListingDetails`
for the detail-page tool and `fetchMotHistory` for the UK MOT-history tool. All
search scrapers normalize into `CarListing`, whose `format()` method is the
sole definition of the user-visible search-output shape — change output
there, not in server.js. `searchAllSources` is exported but unused by the
server.

### Progress notifications are load-bearing

Scrapes routinely run past MCP clients' default tool-call timeout. The client's
timeout clock resets on each `notifications/progress`, so `startHeartbeat()`
emits one every 4s for the life of a scrape. Two constraints that are easy to
break:

- The progress token lives at `params._meta.progressToken`, a sibling of
  `name`/`arguments` — see `extractProgressToken` in server.js.
- Every notification must carry a strictly increasing numeric `progress` field.
  Clients validate the schema and silently drop notifications missing it.

`sendProgress` threads from the request handler down through every scraper; keep
it optional (`sendProgress?.(...)`) so scrapers stay callable from scripts.

### Logging

`src/logger.js` — levels `silent` < `error` < `info` < `debug` < `trace`,
selected by `CAR_DEALS_LOG_LEVEL` (wins) or `--verbose` / `--trace` in argv.
Unlike `sendProgress`, the logger is imported directly rather than threaded
through, so it is always available and needs no null-guard.

- `info` is the default and prints with a bare `[MCP]` prefix; `debug`/`trace`
  tag themselves (`[MCP:debug]`). Put anything per-listing or per-attempt at
  `debug` — `info` should stay one line per tool call and per scraper outcome.
- `logger.error(message, err)` prints the stack **and the `cause` chain** at
  `debug` and above, and a single-line message at `info`.
- **Wrap rethrown errors with `{ cause: err }`.** Every scraper turns a
  Puppeteer failure into `new Error('X scraping failed: ...', { cause: err })`;
  without `cause` the original stack is gone and the log tells you a navigation
  timed out but not which one.
- `logger.preview(value)` truncates before logging. Use it for anything
  caller-supplied or response-sized — a detail-page result is up to 30k chars.
- Never add a bare `console.log`. Stdout is the transport; `console.error` is
  tolerable but bypasses levels, so prefer the logger.

Exceptions must always reach stderr, even when the client is also told about
them. The tool handlers catch their own failures and return `isError`, and
`logToolFailures` in server.js catches anything they miss and rethrows after
logging. `installGlobalHandlers()` covers `uncaughtException` and
`unhandledRejection`.

The MCP SDK only forwards `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`
to a server subprocess, so `CAR_DEALS_LOG_LEVEL` has to be set in the client's
`env` block. `test/mcp-client.test.js` forwards it explicitly for the same
reason.

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

### An unrecognized Cars.com filter looks exactly like a broken scraper

Cars.com does not reject a bad `makes[]`/`models[]` slug — it drops the filter
and serves a results page with zero cards, which is indistinguishable from the
bot-check variant. So a *query* mistake shows up in the logs as
`vehicle cards never appeared`, and the obvious debugging path (blame the
selectors) is the wrong one. Two separate things have to be right:

- **Slug spelling** — `carscomSlug()`. Make and model are joined by a hyphen
  (`mercedes_benz-gle_450`), so every separator inside a name becomes `_`,
  `&`/`+` are spelled out, apostrophes vanish, and `.` survives (`ID.4` →
  `id.4`). Verified against the site's whole filter vocabulary. Do **not**
  replace this with a harvested lookup table: the vocabulary the page exposes
  truncates at 100 models per make, so a table is incomplete for exactly the
  makes people search most.
- **Name validity** — a well-formed slug can still name nothing. `GLE` is
  clean but Cars.com only has `gle_350`/`gle_450`/`gle_class`. On a zero-card
  result the scraper reads the make's real model list out of the page's
  `script#CarsWeb.SearchController.index` blob (`srp_filters.sections`) and
  attaches ranked `modelSuggestions` to the returned array, which server.js
  renders as a did-you-mean. A missing blob means the interstitial, not a bad
  name — treat it as unknown and say nothing.

Single-word names (`toyota-camry`) survive a plain `toLowerCase()`, which is
why this went unnoticed: the test suite only searched Toyota Camry. Keep the
Mercedes-Benz cases in `test/mcp-client.test.js`.
- **Detail pages** (`fetchListingDetails`) are deliberately *not* field-parsed.
  The page is pruned (nav, ads, scripts, carousels) and run through Turndown to
  markdown. That is the whole design: no selectors to maintain when the sites
  reshuffle. Detail pages also sit behind an anti-bot interstitial more often
  than search pages, so `waitOutInterstitial` polls for up to 45s rather than
  converting the challenge page into the answer.

Text-parsing heuristics here have all broken at least once and carry comments
explaining what broke. Keep patterns bounded literals, never greedy wildcards —
a greedy `certified[\w\s-]*` title strip once ate past the year and model.

`fetchListingDetails` drives a real browser at a caller-supplied URL, so the
host is checked against the `DETAIL_HOSTS` allowlist first; without it the tool
is an open fetch proxy reachable at private network addresses.

## Conventions

- CommonJS, 4-space indent, single quotes. `.editorconfig` is enforced in CI-less
  fashion by `just ec`.
- Bodies of `page.evaluate()` callbacks run in the browser, not Node. Browser
  globals are declared for `src/scraper.js` in `eslint.config.js` instead of
  sprinkling `eslint-disable` comments — add there if you need a new one.
- Diagnostics go to `console.error` (stdout is the MCP transport; anything
  written there corrupts the protocol stream).
- When you change what a scraper extracts, add an assertion to
  `test/mcp-client.test.js`. Fields sourced from `data-vehicle-details` (VIN,
  specs, color, dealer rating) silently vanish into the text fallback if
  Cars.com renames the attribute — the assertions are what catch that.

## Metadata files are stale

`mcp.json` and `server.json` both name the upstream fork
(`SiddarthaKoppaka`, `YOUR_USERNAME`) rather than this repo's owner, and
`server.json` advertises only `search_car_deals`. `mcp.json` also declares an
HTTP port that nothing serves — the transport is stdio only. Neither file is
read at runtime; update them if publishing.
