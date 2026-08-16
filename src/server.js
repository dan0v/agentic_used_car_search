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

const { scrapeCarscom, scrapeAutotrader, scrapeKBB, fetchListingDetails } = require('./scraper.js');
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
                description: 'Search for car deals across multiple sources (Cars.com, Autotrader, KBB). Cars.com listings return price (with any price drop), estimated monthly payment, mileage, exterior color, trim, body style, drivetrain, fuel type, VIN, deal rating, Certified Pre-Owned and CARFAX badges, dealer name and star rating, location and distance, a photo, and a link. Use get_listing_details on a listing URL for the full options and features list.',
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
                        zip: {
                            type: 'string',
                            description: 'ZIP code for location-based search (default: 90210)',
                        },
                        maxDistance: {
                            type: 'integer',
                            description: 'Search radius in miles from the ZIP code (e.g. 25, 50, 100, 500). Use 0 for nationwide. Default: the site default, roughly 30-50 miles',
                        },
                        yearMin: {
                            type: 'integer',
                            description: 'Minimum model year',
                        },
                        yearMax: {
                            type: 'integer',
                            description: 'Maximum model year',
                        },
                        priceMax: {
                            type: 'integer',
                            description: 'Maximum price in dollars',
                        },
                        mileageMax: {
                            type: 'integer',
                            description: 'Maximum mileage',
                        },
                        maxResults: {
                            type: 'integer',
                            description: 'Maximum results per source (default: 10)',
                        },
                        sources: {
                            type: 'array',
                            items: { type: 'string' },
                            description: 'Sources to search: "cars.com", "autotrader", "kbb". Default: all',
                        },
                        oneOwner: {
                            type: 'boolean',
                            description: 'Filter for CARFAX 1-Owner vehicles only',
                        },
                        noAccidents: {
                            type: 'boolean',
                            description: 'Filter for vehicles with no accidents or damage reported',
                        },
                        personalUse: {
                            type: 'boolean',
                            description: 'Filter for vehicles used for personal use only (not rental/fleet)',
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
            const params = {
                make: args.make,
                model: args.model,
                zip: args.zip || '90210',
                maxDistance: args.maxDistance,
                yearMin: args.yearMin,
                yearMax: args.yearMax,
                priceMax: args.priceMax,
                mileageMax: args.mileageMax,
                // CarFax history filters
                oneOwner: args.oneOwner,
                noAccidents: args.noAccidents,
                personalUse: args.personalUse,
            };
            const maxResults = args.maxResults || 10;
            // Default to just cars.com for reliability
            const sources = args.sources || ['cars.com'];

            logger.info(`Searching for ${params.make} ${params.model} in ${params.zip}`);
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
            if (params.priceMax) output += ` | Max Price: $${params.priceMax.toLocaleString()}`;
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
