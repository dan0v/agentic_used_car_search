#!/usr/bin/env node

/**
 * Car Deals MCP Server
 *
 * An MCP server that searches for car deals from Cars.com, Autotrader, and KBB.
 */

const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const {
    CallToolRequestSchema,
    ListToolsRequestSchema,
} = require('@modelcontextprotocol/sdk/types.js');

const { scrapeCarscom, scrapeAutotrader, scrapeKBB, scrapeAutotraderUK, scrapeMotorsUK, scrapeCinch, fetchListingDetails, fetchMotHistory } = require('./scraper.js');
const { logger, installGlobalHandlers } = require('./logger.js');

installGlobalHandlers();

/**
 * Pull the progress token out of a `tools/call` request.
 *
 * The spec puts it in `params._meta.progressToken` - a sibling of `name` and
 * `arguments`, not inside the arguments object. Progress notifications are
 * the standard way to keep a client's request-timeout clock alive on a slow
 * tool call, which this one routinely is (real-site scraping, up to ~90s
 * across retries) - without a token there's nowhere to send them.
 */
function extractProgressToken(params) {
    if (!params || typeof params !== 'object') return undefined;
    const readToken = (source) => {
        if (!source || typeof source !== 'object') return undefined;
        const token = source._meta && source._meta.progressToken;
        return (typeof token === 'string' || typeof token === 'number') ? token : undefined;
    };
    return readToken(params) ?? readToken(params.arguments);
}

/**
 * Wrap a request handler so any exception it lets escape is written to stderr
 * before being re-thrown to the SDK.
 *
 * The SDK turns a thrown error into a JSON-RPC error response, which means the
 * client learns about the failure and the server operator does not. Re-throwing
 * after logging preserves the client-facing behaviour exactly.
 */
function logToolFailures(handler) {
    return async (request) => {
        try {
            return await handler(request);
        } catch (err) {
            logger.error(`Unhandled error in tool "${request?.params?.name ?? 'unknown'}"`, err);
            throw err;
        }
    };
}

// Create server instance
const server = new Server(
    {
        name: 'car-deals-mcp',
        version: '1.0.0',
    },
    {
        capabilities: {
            tools: {},
        },
    }
);

// List available tools
server.setRequestHandler(ListToolsRequestSchema, async () => {
    return {
        tools: [
            {
                name: 'search_car_deals',
                description: 'Search for car deals across multiple sources. US sources (Cars.com, Autotrader, KBB) return price (with any price drop), estimated monthly payment, mileage, exterior color, trim, body style, drivetrain, fuel type, VIN, deal rating, Certified Pre-Owned and CARFAX badges, dealer name and star rating, location and distance, a photo, and a link. UK sources (Autotrader UK, Motors.co.uk, Cinch) return title, price (GBP), mileage, and location/distance where available. Use get_listing_details on a listing URL for the full options and features list.',
                inputSchema: {
                    type: 'object',
                    properties: {
                        make: {
                            type: 'string',
                            description: 'Car manufacturer (e.g., Toyota, Honda, Ford)',
                        },
                        model: {
                            type: 'string',
                            description: 'Car model (e.g., Camry, Civic, F-150)',
                        },
                        country: {
                            type: 'string',
                            enum: ['US', 'UK'],
                            description: 'Country to search in. "US" (default; the original and most fully supported scope) searches Cars.com, Autotrader, KBB; "UK" searches Autotrader UK, Motors.co.uk, Cinch. Selects the default `sources`, the default `zip`, and the currency shown. Add new entries here alongside matching per-country scrapers and source defaults to support further regions.',
                        },
                        zip: {
                            type: 'string',
                            description: 'Location-based search code. US: a ZIP code (default: 90210). UK: a postcode (default: SW1A 1AA).',
                        },
                        maxDistance: {
                            type: 'integer',
                            description: 'US only. Search radius in miles from the ZIP code (e.g. 25, 50, 100, 500). Use 0 for nationwide. Default: the site default, roughly 30-50 miles. UK sources do not honour this.',
                        },
                        yearMin: {
                            type: 'integer',
                            description: 'Minimum model year (applied server-side where supported, otherwise client-side)',
                        },
                        yearMax: {
                            type: 'integer',
                            description: 'Maximum model year (applied server-side where supported, otherwise client-side)',
                        },
                        priceMax: {
                            type: 'integer',
                            description: 'Maximum price. US: in USD. UK: in GBP.',
                        },
                        mileageMax: {
                            type: 'integer',
                            description: 'Maximum mileage. Applied client-side for sources that lack a server-side mileage filter (e.g. Motors.co.uk, Cinch).',
                        },
                        maxResults: {
                            type: 'integer',
                            description: 'Maximum results per source (default: 10)',
                        },
                        sources: {
                            type: 'array',
                            items: { type: 'string' },
                            description: 'Sources to search. US: "cars.com", "autotrader", "kbb". UK: "autotrader-uk", "motors", "cinch". Default: "cars.com" (US) or "autotrader-uk" (UK).',
                        },
                        oneOwner: {
                            type: 'boolean',
                            description: 'US only. Filter for CARFAX 1-Owner vehicles. Ignored by UK sources.',
                        },
                        noAccidents: {
                            type: 'boolean',
                            description: 'US only. Filter for vehicles with no accidents or damage reported. Ignored by UK sources.',
                        },
                        personalUse: {
                            type: 'boolean',
                            description: 'US only. Filter for vehicles used for personal use only (not rental/fleet). Ignored by UK sources.',
                        },
                    },
                    required: ['make', 'model'],
                },
            },
            {
                name: 'get_listing_details',
                description: 'Fetch the full detail page for a single car listing and return it as markdown. Use the `url` from a search_car_deals result. Returns everything the listing page shows - VIN, trim, engine, drivetrain, MPG, exterior/interior colors, the full options and features list, warranty, vehicle history, seller notes and dealer info - which the search results do not include.',
                inputSchema: {
                    type: 'object',
                    properties: {
                        url: {
                            type: 'string',
                            description: 'Listing detail page URL, as returned by search_car_deals. Must be on cars.com, autotrader.com, or kbb.com',
                        },
                        includeLinks: {
                            type: 'boolean',
                            description: 'Keep hyperlink URLs in the markdown (default: false - link text is kept either way)',
                        },
                        includeImages: {
                            type: 'boolean',
                            description: 'Keep image references in the markdown (default: false)',
                        },
                        maxLength: {
                            type: 'integer',
                            description: 'Truncate the markdown at this many characters (default: 30000)',
                        },
                    },
                    required: ['url'],
                },
            },
            {
                name: 'check_mot_history',
                description: 'UK only. Check the MOT history of a UK-registered vehicle via the GOV.UK service (check-mot.service.gov.uk) and surface any outstanding issues: the most recent test result, outstanding dangerous/major/minor defects and advisories, the MOT expiry date, and any active safety recalls. Useful before buying a used car listed on the UK sources. Takes a UK registration (number plate), e.g. "YL08 NNV" or "YL08NNV".',
                inputSchema: {
                    type: 'object',
                    properties: {
                        registration: {
                            type: 'string',
                            description: 'UK vehicle registration (number plate), with or without spaces. e.g. "YL08 NNV" or "YL08NNV"',
                        },
                    },
                    required: ['registration'],
                },
            },
        ],
    };
});

// Handle tool calls
//
// Wrapped in `logToolFailures` so that nothing thrown here reaches the SDK
// without first being written to stderr. The per-tool handlers below catch
// their own failures and answer the client with `isError`; this outer wrapper
// is for everything they do not catch, which previously left no trace at all
// on the server side.
server.setRequestHandler(CallToolRequestSchema, logToolFailures(async (request) => {
    const { name, arguments: args } = request.params;
    const progressToken = extractProgressToken(request.params);

    logger.info(`Tool call: ${name}`);
    logger.debug(`Arguments: ${logger.preview(args)}`);
    // A missing progress token silently disables the keepalive that stops slow
    // scrapes from tripping the client's tool-call timeout, and there is no
    // other symptom until a call mysteriously times out.
    logger.debug(progressToken === undefined
        ? 'No progressToken on this request - progress notifications disabled'
        : `Progress token: ${JSON.stringify(progressToken)}`);

    // The MCP spec requires a numeric `progress` field on every notification
    // (must strictly increase) - clients validate incoming notifications
    // against that schema and silently drop ones missing it.
    let progressCount = 0;
    const sendProgress = progressToken
        ? (message) => server.notification({
            method: 'notifications/progress',
            params: { progressToken, progress: ++progressCount, message },
        }).catch(err => logger.debug(`Progress notification failed: ${err.message}`))
        : null;

    if (name === 'search_car_deals') {
        try {
            // `country` selects the default source set, the default postcode/ZIP
            // and the currency symbol shown in the response header. "US" is the
            // original and most fully supported scope (richer card data, CARFAX
            // history filters, maxDistance); "UK" is the first added region.
            // The structure is intentionally a single isUK branch so a future
            // country can be added as another `else if` with its own defaults
            // and scraper fan-out, rather than reworking the handler.
            const country = (args.country || 'US').toUpperCase();
            const isUK = country === 'UK';
            const params = {
                make: args.make,
                model: args.model,
                country,
                zip: args.zip || (isUK ? 'SW1A 1AA' : '90210'),
                maxDistance: args.maxDistance,
                yearMin: args.yearMin,
                yearMax: args.yearMax,
                priceMax: args.priceMax,
                mileageMax: args.mileageMax,
                // CarFax history filters (US only; UK sources ignore these)
                oneOwner: args.oneOwner,
                noAccidents: args.noAccidents,
                personalUse: args.personalUse,
            };
            const maxResults = args.maxResults || 10;
            // Default to a single source per country for reliability: cars.com
            // for US, autotrader-uk for UK. Mirrors the original per-country
            // default rather than fanning out to every source.
            const defaultSources = isUK ? ['autotrader-uk'] : ['cars.com'];
            const sources = args.sources || defaultSources;
            const currencySymbol = isUK ? '£' : '$';

            logger.info(`Searching for ${params.make} ${params.model} in ${params.zip} (${country})`);
            logger.info(`Sources: ${sources.join(', ')}, Max: ${maxResults}`);
            logger.debug(`Resolved params: ${logger.preview(params)}`);
            sendProgress?.(`Searching ${sources.join(', ')} for ${params.make} ${params.model}...`);

            const allListings = [];
            const errors = [];

            // Every source runs the same way; the only differences are the
            // label and the scraper function. Timing each one matters here -
            // the progress-heartbeat design exists precisely because these
            // routinely outrun a client's default timeout, and until now
            // nothing recorded how long one actually took.
            const runScraper = (label, scrape) => {
                logger.info(`Starting ${label} scraper...`);
                const startedAt = Date.now();
                const elapsed = () => `${((Date.now() - startedAt) / 1000).toFixed(1)}s`;
                return scrape(params, maxResults, sendProgress)
                    .then(listings => {
                        logger.info(`${label} returned ${listings.length} listings`);
                        logger.debug(`${label} finished in ${elapsed()} (requested max ${maxResults})`);
                        return { source: label, listings };
                    })
                    .catch(err => {
                        logger.error(`${label} error`, err);
                        logger.debug(`${label} failed after ${elapsed()}`);
                        return { source: label, error: err.message, listings: [] };
                    });
            };

            const scraperPromises = [];
            if (sources.includes('cars.com')) scraperPromises.push(runScraper('Cars.com', scrapeCarscom));
            if (sources.includes('autotrader')) scraperPromises.push(runScraper('Autotrader', scrapeAutotrader));
            if (sources.includes('kbb')) scraperPromises.push(runScraper('KBB', scrapeKBB));
            if (sources.includes('autotrader-uk')) scraperPromises.push(runScraper('Autotrader UK', scrapeAutotraderUK));
            if (sources.includes('motors')) scraperPromises.push(runScraper('Motors.co.uk', scrapeMotorsUK));
            if (sources.includes('cinch')) scraperPromises.push(runScraper('Cinch', scrapeCinch));

            if (scraperPromises.length === 0) {
                logger.error(`No known sources selected (got: ${logger.preview(sources)})`);
            }

            const results = await Promise.all(scraperPromises);
            logger.info('All scrapers completed');

            // A scraper may attach `modelSuggestions` when it can prove the
            // model name does not exist on that site (see suggestCarscomModels).
            // Spreading into allListings drops it, so collect it first.
            const suggestions = [];
            for (const result of results) {
                if (result.listings.modelSuggestions) suggestions.push(result.listings.modelSuggestions);
                allListings.push(...result.listings);
                if (result.error) {
                    errors.push(`${result.source}: ${result.error}`);
                }
            }

            logger.info(`Total listings: ${allListings.length}`);
            sendProgress?.(`Done - found ${allListings.length} listing(s) total`);

            // Format output
            let output = `# Car Deals Search Results\n\n`;
            output += `**Search:** ${params.make} ${params.model}`;
            if (params.yearMin || params.yearMax) {
                output += ` (${params.yearMin || 'any'}-${params.yearMax || 'any'})`;
            }
            if (params.priceMax) output += ` | Max Price: ${currencySymbol}${params.priceMax.toLocaleString()}`;
            if (params.mileageMax) output += ` | Max Mileage: ${params.mileageMax.toLocaleString()}`;

            // Show active CarFax filters
            const activeFilters = [];
            if (params.oneOwner) activeFilters.push('1-Owner');
            if (params.noAccidents) activeFilters.push('No Accidents');
            if (params.personalUse) activeFilters.push('Personal Use');
            if (activeFilters.length > 0) output += `\n**CarFax Filters:** ${activeFilters.join(', ')}`;

            output += `\n**Location:** ${params.zip}`;
            if (params.maxDistance !== undefined && params.maxDistance !== null) {
                output += params.maxDistance === 0
                    ? ` (nationwide)`
                    : ` (within ${params.maxDistance} mi)`;
            }
            output += `\n\n`;

            if (allListings.length === 0) {
                output += `No listings found.\n`;
                // Without this, an unrecognized model name is indistinguishable
                // from genuinely empty inventory - both just say "no listings".
                for (const hint of suggestions) {
                    output += `\n${hint.source || 'Cars.com'} has no ${hint.make} model named "${hint.input}". Closest matches:\n`;
                    for (const opt of hint.options) {
                        output += `- ${opt.name}${opt.count ? ` (${opt.count} listed)` : ''}\n`;
                    }
                    output += `\nRe-run the search with one of these as \`model\`.\n`;
                }
            } else {
                output += `Found **${allListings.length}** listings:\n\n`;

                for (const listing of allListings) {
                    output += listing.format() + '\n\n---\n\n';
                }
            }

            if (errors.length > 0) {
                output += `\n**Errors:**\n`;
                for (const err of errors) {
                    output += `- ${err}\n`;
                }
            }

            logger.trace(`Response: ${logger.preview(output)}`);

            return {
                content: [
                    {
                        type: 'text',
                        text: output,
                    },
                ],
            };
        } catch (error) {
            // Previously this returned isError to the client and wrote nothing
            // to stderr, so a failure in argument handling or output formatting
            // was invisible from the server side.
            logger.error('search_car_deals failed', error);
            return {
                content: [
                    {
                        type: 'text',
                        text: `Error searching for car deals: ${error.message}`,
                    },
                ],
                isError: true,
            };
        }
    }

    if (name === 'get_listing_details') {
        try {
            logger.info(`Fetching listing details: ${args.url}`);
            logger.debug(`Detail options: includeLinks=${args.includeLinks ?? false}, includeImages=${args.includeImages ?? false}, maxLength=${args.maxLength ?? 30000}`);
            sendProgress?.('Loading listing detail page...');

            const details = await fetchListingDetails(
                args.url,
                {
                    includeLinks: args.includeLinks,
                    includeImages: args.includeImages,
                    maxLength: args.maxLength,
                },
                sendProgress
            );

            logger.info(`Detail page extracted: ${details.markdown.length} chars`);
            logger.debug(`Resolved source: ${details.source}, title: ${details.title || '(none)'}, truncated: ${details.truncated}`);

            let output = `# ${details.title || 'Listing Details'}\n\n`;
            output += `**Source:** ${details.source}\n`;
            output += `**URL:** ${details.url}\n\n---\n\n`;
            output += details.markdown;

            logger.trace(`Response: ${logger.preview(output)}`);

            return {
                content: [
                    {
                        type: 'text',
                        text: output,
                    },
                ],
            };
        } catch (error) {
            logger.error('get_listing_details failed', error);
            return {
                content: [
                    {
                        type: 'text',
                        text: `Error fetching listing details: ${error.message}`,
                    },
                ],
                isError: true,
            };
        }
    }

    if (name === 'check_mot_history') {
        try {
            logger.info(`Checking MOT history for ${args.registration}`);
            sendProgress?.(`Checking MOT history for ${args.registration}...`);

            const record = await fetchMotHistory(args.registration, sendProgress);

            if (!record.found) {
                logger.info(`No MOT record found for ${args.registration}`);
                return {
                    content: [
                        {
                            type: 'text',
                            text: `No MOT record was found for registration "${args.registration}".\n\nThe vehicle may be unregistered, too new to have an MOT, or the registration may be mistyped.\n\nCheck directly: ${record.url}`,
                        },
                    ],
                };
            }

            const v = record.vehicle;
            const latest = record.latestTest;
            const issues = record.outstandingIssues;

            let output = `# MOT History: ${v.makeModel || args.registration}\n\n`;
            output += `**Registration:** ${v.registration || args.registration}\n`;
            if (v.colour) output += `**Colour:** ${v.colour}\n`;
            if (v.fuelType) output += `**Fuel:** ${v.fuelType}\n`;
            if (v.dateRegistered) output += `**First registered:** ${v.dateRegistered}\n`;
            if (record.motExpiry) output += `**MOT valid until:** ${record.motExpiry}\n`;
            output += `**Source:** GOV.UK (${record.url})\n\n`;

            // The outstanding-issues summary is the part a buyer actually wants
            // at the top; the full history follows for reference.
            output += `## Outstanding Issues\n\n`;
            if (latest) {
                const resultBadge = String(latest.result).toUpperCase() === 'PASS' ? 'PASS' : 'FAIL';
                output += `**Latest test (${latest.date}):** ${resultBadge}\n`;
                if (latest.mileage) output += `**Mileage at last test:** ${latest.mileage}\n`;
            } else {
                output += `**Latest test:** none on record\n`;
            }

            const hasOpenDefects = issues.dangerous.length + issues.major.length + issues.minor.length + issues.advisories.length > 0;
            if (hasOpenDefects) {
                const section = (label, items) => {
                    if (!items.length) return;
                    output += `\n**${label}:**\n`;
                    for (const item of items) output += `- ${item}\n`;
                };
                section('Dangerous defects (do not drive until repaired)', issues.dangerous);
                section('Major defects (repair immediately)', issues.major);
                section('Minor defects (repair soon)', issues.minor);
                section('Advisories (monitor and repair if necessary)', issues.advisories);
            } else if (latest) {
                output += `\nNo outstanding defects or advisories recorded at the latest test.\n`;
            }

            if (issues.hasOutstandingRecall) {
                output += `\n## ⚠️ Safety Recall\n\n`;
                for (const recall of record.recalls) output += `${recall}\n\n`;
            } else {
                output += `\nNo outstanding safety recalls.\n`;
            }

            // Full test history, most-recent-first as the page renders it.
            if (record.tests.length > 0) {
                output += `\n## Full MOT History (${record.tests.length} test${record.tests.length === 1 ? '' : 's'})\n\n`;
                for (const test of record.tests) {
                    const result = test.result ? String(test.result).toUpperCase() : 'UNKNOWN';
                    output += `### ${test.date || 'Date unknown'} — ${result}\n`;
                    if (test.mileage) output += `- Mileage: ${test.mileage}\n`;
                    if (test.testNumber) output += `- Test number: ${test.testNumber}\n`;
                    if (test.expiryDate) output += `- Expiry date: ${test.expiryDate}\n`;
                    const defectLines = (items) => items.length
                        ? items.map(i => `  - ${i}`).join('\n')
                        : '';
                    if (test.dangerous && test.dangerous.length) output += `- Dangerous defects:\n${defectLines(test.dangerous)}\n`;
                    if (test.major && test.major.length) output += `- Major defects:\n${defectLines(test.major)}\n`;
                    if (test.minor && test.minor.length) output += `- Minor defects:\n${defectLines(test.minor)}\n`;
                    if (test.advisories && test.advisories.length) output += `- Advisories:\n${defectLines(test.advisories)}\n`;
                    output += '\n';
                }
            }

            logger.info(`MOT history returned: ${record.tests.length} test(s), latest ${issues.latestResult || 'none'}${issues.hasOutstandingRecall ? ', outstanding recall' : ''}`);
            logger.trace(`Response: ${logger.preview(output)}`);

            return {
                content: [
                    {
                        type: 'text',
                        text: output,
                    },
                ],
            };
        } catch (error) {
            logger.error('check_mot_history failed', error);
            return {
                content: [
                    {
                        type: 'text',
                        text: `Error checking MOT history: ${error.message}`,
                    },
                ],
                isError: true,
            };
        }
    }

    throw new Error(`Unknown tool: ${name}`);
}));

// Start server
async function main() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    logger.info(`Car Deals MCP Server running on stdio (log level: ${logger.level})`);
    if (logger.level === 'info') {
        logger.info('Set CAR_DEALS_LOG_LEVEL=debug (or pass --verbose) for request arguments, search URLs, timings and stack traces.');
    }
}

main().catch(err => {
    logger.error('Server failed to start', err);
    process.exit(1);
});
