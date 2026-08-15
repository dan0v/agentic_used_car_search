#!/usr/bin/env node

/**
 * End-to-end MCP client test.
 *
 * Spawns src/server.js as a real MCP server subprocess and drives it with
 * the official MCP SDK client over stdio - no protocol details faked out.
 * Verifies: tool discovery, input schema shape, and a live search call
 * against Cars.com.
 *
 * Usage: node test/mcp-client.test.js
 */

const assert = require('node:assert/strict');
const path = require('node:path');
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { StdioClientTransport } = require('@modelcontextprotocol/sdk/client/stdio.js');
const { CallToolResultSchema } = require('@modelcontextprotocol/sdk/types.js');

const SERVER_PATH = path.join(__dirname, '..', 'src', 'server.js');

async function main() {
    const transport = new StdioClientTransport({
        command: process.execPath,
        args: [SERVER_PATH],
        stderr: 'inherit',
    });

    const client = new Client({ name: 'car-deals-mcp-test-client', version: '1.0.0' });
    await client.connect(transport);

    try {
        // --- tool discovery ---
        const { tools } = await client.listTools();
        assert.deepEqual(
            tools.map(t => t.name).sort(),
            ['get_listing_details', 'search_car_deals'],
            'unexpected tool set',
        );
        console.log('[PASS] tools discovered: search_car_deals, get_listing_details');

        const tool = tools.find(t => t.name === 'search_car_deals');
        const detailTool = tools.find(t => t.name === 'get_listing_details');

        // --- schema shape ---
        const { properties, required } = tool.inputSchema;
        assert.deepEqual(required, ['make', 'model'], 'required params should be make + model');
        for (const key of ['make', 'model', 'zip', 'maxDistance', 'yearMin', 'yearMax', 'priceMax', 'mileageMax', 'maxResults', 'sources', 'oneOwner', 'noAccidents', 'personalUse']) {
            assert.ok(properties[key], `schema missing property: ${key}`);
        }

        assert.deepEqual(detailTool.inputSchema.required, ['url'], 'get_listing_details should require url');
        for (const key of ['url', 'includeLinks', 'includeImages', 'maxLength']) {
            assert.ok(detailTool.inputSchema.properties[key], `detail schema missing property: ${key}`);
        }
        console.log('[PASS] input schemas have all documented parameters');

        // --- live search call, with progress notifications ---
        console.log('[INFO] calling search_car_deals (make: Toyota, model: Camry, cars.com only) - this hits a real site, may take ~15-20s ...');
        const progressMessages = [];
        const result = await client.callTool(
            {
                name: 'search_car_deals',
                arguments: {
                    make: 'Toyota',
                    model: 'Camry',
                    maxResults: 2,
                    sources: ['cars.com'],
                },
            },
            CallToolResultSchema,
            {
                onprogress: (p) => {
                    progressMessages.push(p.message);
                    console.log(`  [PROGRESS] ${p.message}`);
                },
            },
        );

        assert.ok(progressMessages.length > 0, 'expected at least one progress notification (tool call would otherwise risk client-side timeout on slow scrapes)');
        console.log(`[PASS] received ${progressMessages.length} progress notification(s) during the call`);

        assert.equal(result.isError, undefined, `tool call returned an error: ${JSON.stringify(result)}`);
        assert.equal(result.content.length, 1);
        assert.equal(result.content[0].type, 'text');

        const text = result.content[0].text;
        assert.match(text, /# Car Deals Search Results/);
        assert.match(text, /\*\*Search:\*\* Toyota Camry/);
        assert.doesNotMatch(text, /No listings found\./, 'live search returned zero listings - scraper may be broken or site layout changed');
        assert.match(text, /Source: Cars\.com/, 'expected at least one Cars.com listing in the response');
        // Color lives only in the card's data-vehicle-details JSON, never in
        // the visible card text - if Cars.com drops that attribute this is the
        // assertion that catches it.
        assert.match(text, /Exterior Color: \w+/, 'expected exterior color on at least one listing');
        // These all come from the card's data-vehicle-details JSON. If
        // Cars.com drops or renames that attribute the scraper silently falls
        // back to text parsing and loses every one of them at once, so assert
        // on them rather than trusting the call to have "succeeded".
        assert.match(text, /VIN: [A-HJ-NPR-Z0-9]{17}/, 'expected a VIN on at least one listing');
        assert.match(text, /Specs: .+\|/, 'expected trim/body/drivetrain specs on at least one listing');
        assert.match(text, /Location: .+, [A-Z]{2}/, 'expected a city/state location on at least one listing');
        assert.match(text, /Dealer: .+\(\d\.\d stars\)/, 'expected a dealer star rating on at least one listing');

        console.log('[PASS] live search_car_deals call returned real Cars.com listings');

        // --- host allowlist on the detail tool ---
        const rejected = await client.callTool(
            { name: 'get_listing_details', arguments: { url: 'https://example.com/listing' } },
            CallToolResultSchema,
        );
        assert.equal(rejected.isError, true, 'off-allowlist host should be rejected');
        assert.match(rejected.content[0].text, /Unsupported host/);
        console.log('[PASS] get_listing_details rejects hosts outside the allowlist');

        // --- live detail call, using a URL from the search above ---
        const listingUrl = text.match(/https:\/\/www\.cars\.com\/vehicledetail\/\S+/)?.[0];
        assert.ok(listingUrl, 'expected a cars.com detail URL in the search results');

        console.log('[INFO] calling get_listing_details - real detail page, may take ~20-40s (site bot check) ...');
        const detailResult = await client.callTool(
            { name: 'get_listing_details', arguments: { url: listingUrl } },
            CallToolResultSchema,
            { onprogress: (p) => console.log(`  [PROGRESS] ${p.message}`) },
        );

        assert.equal(detailResult.isError, undefined, `detail call errored: ${JSON.stringify(detailResult).slice(0, 400)}`);
        const detailText = detailResult.content[0].text;
        assert.match(detailText, /\*\*Source:\*\* Cars\.com/);
        // The bot-check interstitial is a few hundred characters and has none
        // of the spec content - a short body means we converted the challenge
        // page instead of the listing.
        assert.ok(detailText.length > 3000, `detail markdown suspiciously short (${detailText.length} chars) - likely the bot-check interstitial`);
        assert.match(detailText, /VIN/i, 'expected VIN in the detail markdown');
        console.log(`[PASS] live get_listing_details returned ${detailText.length} chars of listing markdown`);

        console.log('\nAll checks passed.');
    } finally {
        await client.close();
    }
}

main().catch(err => {
    console.error('[FAIL]', err.message);
    process.exit(1);
});
