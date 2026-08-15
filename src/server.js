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
server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const progressToken = extractProgressToken(request.params);

    // The MCP spec requires a numeric `progress` field on every notification
    // (must strictly increase) - clients validate incoming notifications
    // against that schema and silently drop ones missing it.
    let progressCount = 0;
    const sendProgress = progressToken
        ? (message) => server.notification({
            method: 'notifications/progress',
            params: { progressToken, progress: ++progressCount, message },
        }).catch(() => {})
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

            console.error(`[MCP] Searching for ${params.make} ${params.model} in ${params.zip}`);
            console.error(`[MCP] Sources: ${sources.join(', ')}, Max: ${maxResults}`);
            sendProgress?.(`Searching ${sources.join(', ')} for ${params.make} ${params.model}...`);

            const allListings = [];
            const errors = [];

            // Run selected scrapers
            const scraperPromises = [];

            if (sources.includes('cars.com')) {
                console.error('[MCP] Starting Cars.com scraper...');
                scraperPromises.push(
                    scrapeCarscom(params, maxResults, sendProgress)
                        .then(listings => {
                            console.error(`[MCP] Cars.com returned ${listings.length} listings`);
                            return { source: 'Cars.com', listings };
                        })
                        .catch(err => {
                            console.error(`[MCP] Cars.com error: ${err.message}`);
                            return { source: 'Cars.com', error: err.message, listings: [] };
                        })
                );
            }

            if (sources.includes('autotrader')) {
                console.error('[MCP] Starting Autotrader scraper...');
                scraperPromises.push(
                    scrapeAutotrader(params, maxResults, sendProgress)
                        .then(listings => {
                            console.error(`[MCP] Autotrader returned ${listings.length} listings`);
                            return { source: 'Autotrader', listings };
                        })
                        .catch(err => {
                            console.error(`[MCP] Autotrader error: ${err.message}`);
                            return { source: 'Autotrader', error: err.message, listings: [] };
                        })
                );
            }

            if (sources.includes('kbb')) {
                console.error('[MCP] Starting KBB scraper...');
                scraperPromises.push(
                    scrapeKBB(params, maxResults, sendProgress)
                        .then(listings => {
                            console.error(`[MCP] KBB returned ${listings.length} listings`);
                            return { source: 'KBB', listings };
                        })
                        .catch(err => {
                            console.error(`[MCP] KBB error: ${err.message}`);
                            return { source: 'KBB', error: err.message, listings: [] };
                        })
                );
            }

            const results = await Promise.all(scraperPromises);
            console.error(`[MCP] All scrapers completed`);

            for (const result of results) {
                allListings.push(...result.listings);
                if (result.error) {
                    errors.push(`${result.source}: ${result.error}`);
                }
            }

            console.error(`[MCP] Total listings: ${allListings.length}`);
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

            return {
                content: [
                    {
                        type: 'text',
                        text: output,
                    },
                ],
            };
        } catch (error) {
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
            console.error(`[MCP] Fetching listing details: ${args.url}`);
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

            console.error(`[MCP] Detail page extracted: ${details.markdown.length} chars`);

            let output = `# ${details.title || 'Listing Details'}\n\n`;
            output += `**Source:** ${details.source}\n`;
            output += `**URL:** ${details.url}\n\n---\n\n`;
            output += details.markdown;

            return {
                content: [
                    {
                        type: 'text',
                        text: output,
                    },
                ],
            };
        } catch (error) {
            console.error(`[MCP] Detail fetch error: ${error.message}`);
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
});

// Start server
async function main() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error('Car Deals MCP Server running on stdio');
}

main().catch(console.error);
