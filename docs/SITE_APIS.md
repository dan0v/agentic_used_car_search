# Car-site search APIs — research notes

Findings from investigating whether each source exposes a public or
reverse-engineerable search API that could replace browser scraping. The goal
is richer structured data and no anti-bot, but any direct-HTTP migration must
respect per-domain rate limits (see "Rate-limiting design" below).

Last verified: Aug 2026 (Autotrader UK detail-page harvest added 21 Aug 2026).

---

## Summary

| Site | API? | Type | Auth | Filters | Recommendation |
|------|------|------|------|---------|-----------------|
| **Autotrader UK (search)** | Yes | GraphQL | none (headers only) | all, incl. mileage/drivetrain/transmission | Migrate — best option |
| **Autotrader UK (detail)** | Yes | SSR JSON (`__staticRouterHydrationData`) | none | n/a (single advert by id) | Migrate — no GraphQL exists for details; harvest SSR instead |
| **Cinch** | Yes | REST JSON | none | all except mileage | Migrate (mileage client-side); returns the plate |
| **Motors.co.uk** | No (Cazoo SSR) | — | — | — | Keep HTML scraping |
| **Autotrader US** | No callable API | SSR (`__NEXT_DATA__`) | — | SSR facets | SSR JSON harvest (no browser) |
| **KBB** | No callable API | SSR (`__NEXT_DATA__`) | — | SSR facets | SSR JSON harvest (no browser) |
| **Cars.com** | No | Cloudflare-walled | — | — | Keep Playwright + `data-vehicle-details` |

---

## 1. Autotrader UK — `https://www.autotrader.co.uk/at-gateway`

Unauthenticated GraphQL gateway. The SPA (`sauron-search-results-app`,
React + Apollo) calls it directly; a bare `curl` POST returns real listings
with no cookies, no Cloudflare, no CORS wall. **Strictly better than HTML
scraping** — structured fields, every filter server-side, no anti-bot.

### Endpoint

```
POST https://www.autotrader.co.uk/at-gateway?opname=SearchResultsListingsGridQuery
```

- `Content-Type: application/json`
- `User-Agent`: a normal browser UA.
- `Origin` / `Referer`: `https://www.autotrader.co.uk`.
- **Required headers** (route to the search schema; found in the bundle as
  `x-sauron-app-name` / `x-sauron-app-version`):
  - `x-sauron-app-name: sauron-search-results-app`
  - `x-sauron-app-version: 4624a08064`  ← **changes per deploy** — re-read from
    any `/car-search` page's inline `window.AT_SPA_JS_CONFIG` JSON and cache
    with a TTL. Do NOT hard-code long-term.
- No API key, no cookies. Introspection is disabled, but field-level validation
  errors ("Did you mean…") let you reverse-engineer the schema by probing.

### Operation

```graphql
query SearchResultsListingsGridQuery(
    $filters: [FilterInput!]!
    $channel: Channel!
    $page: Int
    $sortBy: SearchResultsSort
    $listingType: [ListingType!]
    $searchId: String!
    $featureFlags: [FeatureFlag]
) {
    searchResults(input: {
        facets: []
        filters: $filters
        channel: $channel
        page: $page
        sortBy: $sortBy
        listingType: $listingType
        searchId: $searchId
        featureFlags: $featureFlags
    }) {
        listings {
            ... on SearchListing {
                advertId title subTitle attentionGrabber price
                vehicleLocation locationType discount images numberOfImages
                rrp sellerType dealerLink
                dealerReview { overallReviewRating }
                fpaLink hasDigitalRetailing preReg
                finance { monthlyPrice { priceFormattedAndRounded } }
                badges { type displayText }
                position canSaveAdvert
                trackingContext { ... }
            }
        }
    }
}
```

### Filter shape

Each filter is `{ "filter": "<snake_case_name>", "selected": ["<value>", ...] }`.
Note: the wire field is `selected`, **not** `values` (the internal state map
uses `values`, but `FilterInput` uses `selected`).

### Filter parameter names (all confirmed by count-change probing)

| Filter | Name | Example value |
|--------|------|---------------|
| Postcode | `postcode` | `"SW1A 1AA"` (spaces optional) |
| Radius | `distance` | `"1500"` (miles; `"1500"` = whole UK) |
| Lat/long | `lat_long` | — |
| Free-text location | `location` | — |
| Make | `make` | `"BMW"` |
| Model | `model` | `"3 Series"` |
| Multi-model | `multi_model` | — |
| Trim | `aggregated_trim` | — |
| Year min | `min_year_manufactured` | `"2018"` |
| Year max | `max_year_manufactured` | `"2024"` |
| Price min | `min_price` | `"0"` |
| Price max | `max_price` | `"40000"` |
| Monthly price min | `min_monthly_price` | — |
| Monthly price max | `max_monthly_price` | — |
| **Mileage min** | `min_mileage` | `"0"` |
| **Mileage max** | `max_mileage` | `"80000"` (also `*_km` variants) |
| **Transmission** | `transmission` | `"Automatic"`, `"Manual"`, `"Semi-Automatic"` |
| **Drivetrain** | `drivetrain` | `"Four Wheel Drive"`, `"Rear Wheel Drive"`, `"Front Wheel Drive"`, `"All Wheel Drive"` |
| Fuel | `fuel_type` | `"Petrol"`, `"Diesel"`, `"Hybrid"`, `"Electric"` |
| Body | `body_type` | `"Saloon"`, `"Estate"`, `"SUV"` |
| Colour | `colour` | `"Black"` |
| Doors | `doors` | `"5"` |
| Seats | `seats` (or `min_seats`/`max_seats`) | `"5"` |
| Seller type | `seller_type` | — |
| Approved | `is_manufacturer_approved` | — |
| Write-off | `is_writeoff` | — |
| Keywords | `keywords` | — |
| Engine size | `engine_size` | — |
| Engine power | `engine_power` | — |
| CO2 | `co2_emission_values` | — |
| Battery range | `battery_range_values` | — |
| Acceleration | `acceleration_values` | — |
| **Required** | `price_search_type` | `"total"` (absolute) or `"monthly"` (finance). Without it → `INVALID_ARGUMENT`. |

### Other variables

- `channel`: `"cars"` (also `vans`, `bikes`, `motorhomes`, `caravans`, `trucks`, `plant`, `farm`, `cycles`).
- `page`: 1-indexed.
- `sortBy`: **lowercase** enum — `"relevance"`, `"price_asc"`, `"price_desc"`, `"mileage_asc"`, `"distance"`. (`RELEVANCE` is rejected.)
- `listingType`: `["NATURAL_LISTING"]` or `null`.
- `searchId`: any UUID string (invented is fine).
- `featureFlags`: `[]`.

### Concrete working request

```bash
curl -X POST 'https://www.autotrader.co.uk/at-gateway?opname=SearchResultsListingsGridQuery' \
  -H 'Content-Type: application/json' \
  -H 'x-sauron-app-name: sauron-search-results-app' \
  -H 'x-sauron-app-version: 4624a08064' \
  -H 'Origin: https://www.autotrader.co.uk' \
  -H 'Referer: https://www.autotrader.co.uk/car-search' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36' \
  --data '{
    "operationName": "SearchResultsListingsGridQuery",
    "variables": {
      "filters": [
        {"filter":"postcode","selected":["sw1a1aa"]},
        {"filter":"distance","selected":["1500"]},
        {"filter":"make","selected":["BMW"]},
        {"filter":"model","selected":["3 Series"]},
        {"filter":"min_year_manufactured","selected":["2018"]},
        {"filter":"max_year_manufactured","selected":["2024"]},
        {"filter":"min_price","selected":["0"]},
        {"filter":"max_price","selected":["40000"]},
        {"filter":"max_mileage","selected":["80000"]},
        {"filter":"transmission","selected":["Automatic"]},
        {"filter":"drivetrain","selected":["Four Wheel Drive"]},
        {"filter":"fuel_type","selected":["Petrol"]},
        {"filter":"price_search_type","selected":["total"]}
      ],
      "channel":"cars","page":1,"sortBy":"relevance",
      "listingType":null,"searchId":"any-uuid","featureFlags":[]
    },
    "query":"query SearchResultsListingsGridQuery($filters:[FilterInput!]!,$channel:Channel!,$page:Int,$sortBy:SearchResultsSort,$listingType:[ListingType!],$searchId:String!,$featureFlags:[FeatureFlag]){searchResults(input:{facets:[],filters:$filters,channel:$channel,page:$page,sortBy:$sortBy,listingType:$listingType,searchId:$searchId,featureFlags:$featureFlags}){listings{... on SearchListing{advertId title price mileage year}}}}"
  }'
```

Verified live: returned 24 listings, HTTP 200.

### Stability

High — this is the SPA's own contract. Risk points:
- `x-sauron-app-version` changes per deploy → fetch once, cache with TTL,
  reuse across calls (single-flight; don't re-fetch per request).
- A token check could be added; currently none.

### Gotchas

- The search card's spec strip (year/mileage/fuel/transmission/drivetrain) is
  server-folded into one free-text `attentionGrabber` string on the rendered
  card — there is no per-field `data-testid` for transmission/drivetrain on
  the card. The API returns structured fields, so migrating to the API
  removes the regex-parse fragility entirely.
- The search card does NOT show the registration plate. The API does not
  return it either in the fields I requested. But the detail page (see
  "Detail page" below) embeds it in the SSR hydration JSON, bypassing the
  issue entirely.

### Detail page — `https://www.autotrader.co.uk/car-details/{advertId}`

The detail page is a **separate SPA** (`product-page-web`, not
`sauron-search-results-app`) and its bundle makes **zero** calls to
`at-gateway` — so there is **no GraphQL "get advert by id" operation** to
call. Instead the whole advert ships embedded in the SSR HTML as:

```
window.__staticRouterHydrationData = JSON.parse("{\"loaderData\":{\"car-details\":{\"aggregatorAdvert\":{ ... }}}}")
```

Decode the JS string layer first (`json.loads('"' + raw + '"')`), then the
JSON layer. `aggregatorAdvert` carries everything the detail prune can give
you **plus** fields the browser prune can't guarantee and the search API
explicitly does not return:

| Key in `aggregatorAdvert` | Content |
|---|---|
| `heading`, `details` | title, subtitle, cash price, market-price rating |
| `keySpecification[]` | mileage, registration, owners, fuel, body, engine, gearbox, colour, etc. |
| `specs[]` | full Performance / Size-and-dimensions tables |
| `runningCosts.runningCostList[]` | mpg, tax, insurance group, CO₂ |
| `history` | MOT status, owners, service history, stolen/scrapped/write-off checks |
| `description.text[]`, `description.vehicleRegistration` | seller text **+ registration-plate date** |
| `featuresWithDisclaimer.features[]` | full "see all features" list (already expanded — no button) |

The domain is **not** behind Cloudflare at this path (unlike cars.com). A plain
`GET` via `_http.throttled_request` returns HTTP 200 with the hydration blob.
The `get_listing_details` tool dispatches on host to this path; if the harvest
fails (`FormatSourceError`, transient HTTP error, Cloudflare turns on),
it falls back to the generic browser-prune path unchanged.

---

## 2. Cinch — `https://search-api.snc-prod.aws.cinch.co.uk`

Unauthenticated REST JSON. Cinch's Next.js SSR calls this internally; it is
publicly reachable. Returns rich structured data **including the registration
plate** (`vrm` / `fullRegistration`), which solves the "no plate on cards"
problem for Cinch listings.

### Endpoint

```
GET https://search-api.snc-prod.aws.cinch.co.uk/used-cars?url=<URL-encoded query string>
```

- The `url` param wraps the actual filter query string, URL-encoded. Without
  it → `HTTP 400 "url query param is required"`.
- No auth, no cookies, no CORS issue for server-side calls.
- `application/json` response.
- Verified live: 110 BMW 3 Series results with full specs.

### Filter parameters (confirmed by `searchResultsCount` changes)

| Filter | Param | Example | Notes |
|--------|-------|---------|-------|
| Make | `make` | `BMW` | |
| Model | `model` | `3 Series` | |
| Price min | `fromPrice` | `0` | pounds (integer) |
| Price max | `toPrice` | `40000` | |
| Year min | `fromYear` | `2018` | |
| Year max | `toYear` | `2024` | |
| **Transmission** | `transmissionType` | `Automatic`, `Manual` | note the `Type` suffix; plain `transmission` is ignored |
| Fuel | `fuelType` | `Petrol`, `Diesel`, `Petrol plug-in hybrid` | `fuel_type` ignored |
| Body | `bodyType` | `Estate`, `Saloon`, `SUV` | |
| **Drivetrain** | `driveType` | `Rear-wheel drive`, `Four-wheel drive`, `Front-wheel drive` | |
| Colour | `colour` | `Black` | |
| Doors | `doors` | `5` | |
| Seats | `seats` | `5` | |
| Pagination | `pageNumber`, `pageSize` | `1`, `32` | |
| Tags | `tags` | `cars-on-offer` | |
| Finance | `financeType` | `any` | |

**⚠️ Mileage is NOT server-filterable.** Tested every plausible variant
(`mileageTo`, `maxMileage`, `toMileage`, `mileage_to`, `fromMileage`,
`minMileage`) — none changed the count. Cinch filters mileage client-side
only. A migrated scraper would still post-filter mileage in Python.

**Postcode/radius**: no effect (Cinch is national delivery, no location
search). Matches the existing `scrape_cinch` behaviour.

### Response shape (rich)

```json
{
  "pageNumber": 1, "pageSize": 32, "searchResultsCount": 110,
  "vehicleListings": [{
    "vehicleId": "db65143d-...",
    "modelYear": 2018, "vehicleYear": 2018,
    "bodyType": "Estate", "colour": "BLACK", "doors": 5,
    "engineCapacityCc": 1995, "fuelType": "Diesel",
    "vrm": "WF68HZJ",                 // ← registration plate
    "fullRegistration": "WF68HZJ",    // ← same, alternate field
    "mileage": 82125, "seats": 5, "trim": "SE",
    "variant": "320d SE 5dr Step Auto",
    "transmissionType": "Automatic",
    "milesPerGallon": 55,
    "driveType": "Rear-wheel drive",
    "price": 11009, "priceIncludingAdminFee": 11108,
    "make": "BMW", "model": "3 Series",
    "thumbnailUrl": "https://eu.cdn.autosonshow.tv/...",
    "site": "Corby", "isAvailable": true, "stockType": "cinch",
    "quoteType": "pcp", "quoteApr": 12.4,
    "quoteRegularPaymentInPence": 19112,
    "quoteTermMonths": 48, "quoteDepositInPence": 200000
  }]
}
```

### Concrete working request

```bash
curl 'https://search-api.snc-prod.aws.cinch.co.uk/used-cars?url=%3Fmake%3DBMW%26model%3D3%2BSeries%26fromPrice%3D0%26toPrice%3D40000%26fromYear%3D2018%26toYear%3D2024%26transmissionType%3DAutomatic%26fuelType%3DDiesel%26pageNumber%3D1' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36' \
  -H 'Referer: https://www.cinch.co.uk/used-cars'
```

The `url=` value is `?make=BMW&model=3+Series&fromPrice=0&toPrice=40000&fromYear=2018&toYear=2024&transmissionType=Automatic&fuelType=Diesel&pageNumber=1`, URL-encoded.

### Stability

Medium-high. The host is an internal AWS service exposed publicly; it could
gain auth or move. No anti-bot currently. **Better than HTML scraping** for
all fields except mileage. The `url=`-wrapping quirk is the main gotcha.

---

## 3. Motors.co.uk — no own API (Cazoo stack)

**Motors.co.uk's search page is the Cazoo platform.** The SSR HTML carries
the title `"Used cars for sale | Second Hand Cars | Cazoo"` and listing links
point to `cazoo.co.uk/cars-for-sale/<id>/`. Cazoo went into administration in
2024; Motors.co.uk is the surviving Constellation Automotive brand, sharing
the Cazoo Next.js codebase. No `__NEXT_DATA__` is exposed and no `/api/` or
GraphQL path was discoverable on the `motors.co.uk` domain.

**Recommendation:** keep HTML scraping. Cinch's API may largely overlap
Motors.co.uk's inventory (both draw from the BCA/Constellation stock pool) —
worth comparing inventory overlap before investing further in Motors scraping.

---

## 4. Autotrader US — SSR JSON harvest (no callable API)

Next.js SSR. The search results page embeds **full listing data in
`__NEXT_DATA__.props.pageProps.__eggsState.inventory`** as JSON — each listing
has `vin, mileage, price, year, make, model, trim, transmission, driveType,
fuelType, bodyStyles, color, doors, engine, dealerId, ownerId,
paymentServices, ...`. ~48 listings per SSR payload, plus `srp_results.activeResults`
(24) and `srp_spotlight` premium tiles.

The only client APIs found are finance-related, not search:
- `https://www.autotrader.com/rest/retailing/budget`
- `https://www.autotrader.com/rest/retailing/incentives`
- `https://www.autotrader.com/rest/retailing/payments`
- `https://upa.syndication.kbb.com/v2` (KBB price advisor)
- `https://fdpq.syndication.kbb.com/atc/index.html?apikey=5a343eb4-6397-421a-9edf-3adabf43e5f5`

Probing `/rest/jsp/listing/search`, `/rest/search`, `/rest/listings`,
`/api/listings`, `/rest/v1/listings` all return the SPA HTML shell (HTTP 200
but `text/html`), not JSON. **No documented or discoverable public listings
search API.**

### URL query params (SSR)

`market` (`USED`/`NEW`/`CPO`), `radius`/`searchRadius`, `zip`, `makeCode`
(e.g. `BMW`), `seriesCode` (e.g. `3_SERIES` — note underscore), `listingType`,
`city`, `state`, `location`, `dma`. Filter refinements (transmission, fuel,
price, year, mileage) are applied client-side via the `srp_filters` facet UI
after the initial SSR.

### Recommendation

Two options, both better than Playwright card-scraping:
1. **SSR JSON harvest** — fetch `/cars-for-sale/<make>/<model>?market=USED&radius=N&zip=ZIP`
   with a real-browser UA (no Cloudflare wall on the SSR page itself), parse
   `__NEXT_DATA__`, read `__eggsState.inventory`. First ~48 listings with full
   specs in one HTTP call. No browser.
2. Keep Playwright if you need client-side filter facets (transmission/fuel/
   price sliders) applied — those trigger client XHRs you'd reverse-engineer
   separately.

---

## 5. KBB.com — SSR JSON harvest (shares Cox Automotive stack with AT US)

KBB's `/cars-for-sale/...` page is the **identical Cox Automotive Next.js
stack** as Autotrader US — it even references `https://www.autotrader.com/cm-api/...`
and `https://www.autotrader.com/rest/retailing/*` for finance, and the same
`fdpq.syndication.kbb.com` widget. `__NEXT_DATA__` is present and carries the
same `__eggsState.inventory` shape (~48 listings, fields: `transmission,
driveType, fuelType, bodyStyles, color, doors, engine, mileage, year, vin,
trim, make, model, ...`) plus `srp_results.activeResults` (24) and
`srp_results.count`.

KBB-specific extras: `kbbVehicleId`, `kbbVRSData`, `kbbPageData`,
`priceAdvisorEnabled`. No separate JSON search API.

**Recommendation:** same as AT US — SSR JSON harvest from
`__NEXT_DATA__.__eggsState.inventory`, or keep Playwright. KBB and Autotrader
US inventory overlaps heavily (both Cox Automotive), so you may not need both.

---

## 6. Cars.com — no API (Cloudflare-walled)

Direct `curl` to `https://www.cars.com/shopping/results/?makes[]=bmw&models[]=bmw-3_series&...`
returns **HTTP 403** with the Cloudflare "Just a moment..." interstitial
(JS challenge). This is exactly the anti-bot behaviour CLAUDE.md describes, and
why the project uses CloakBrowser. **No discoverable JSON search API** —
search results are server-rendered HTML cards with the `data-vehicle-details`
JSON payload the scraper already parses.

**Recommendation:** keep the existing Playwright + `data-vehicle-details`
approach. No API improvement available. Cars.com remains the most anti-bot-
hardened of the six.

---

## Rate-limiting design (for any future direct-HTTP migration)

Any migration to direct API calls must avoid triggering rate limits. The
browser scrapers are already implicitly throttled by browser-launch time
(~seconds per call); direct HTTP is fast enough to hammer a host by accident.

- **Per-domain throttle** — minimum interval between requests to the same
  host (configurable, default ~1-2s), enforced via an `asyncio.Lock` +
  `monotonic` deadline per domain so concurrent `asyncio.gather` fan-out
  still serializes per host. A shared `HttpThrottle` keyed by netloc.
- **Single-flight version fetch** (Autotrader UK) — the `x-sauron-app-version`
  changes per deploy; fetch the `/car-search` page once, cache the version
  with a TTL, reuse across calls. Don't re-fetch per request.
- **No retry storms** — on `429` / `5xx`, back off (exponential, capped)
  rather than immediately retry; surface the failure in the `**Errors:**`
  footer instead of hammering.
- **`httpx`** added as a dependency (the project plan listed it as "only if
  any scraper needs raw HTTP; likely not" — a direct-API migration would
  change that). Reuse a single `httpx.AsyncClient` per host with keep-alive.
- **Bounded concurrency** — cap the number of in-flight requests per host
  (default 1-2) even when the caller fans out across sources.
