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
        assert.equal(tools.length, 1, 'expected exactly one tool');

        const tool = tools[0];
        assert.equal(tool.name, 'search_car_deals');
        console.log('[PASS] tool discovered: search_car_deals');

        // --- schema shape ---
        const { properties, required } = tool.inputSchema;
        assert.deepEqual(required, ['make', 'model'], 'required params should be make + model');
        for (const key of ['make', 'model', 'zip', 'yearMin', 'yearMax', 'priceMax', 'mileageMax', 'maxResults', 'sources', 'oneOwner', 'noAccidents', 'personalUse']) {
            assert.ok(properties[key], `schema missing property: ${key}`);
        }
        console.log('[PASS] input schema has all documented parameters');

        // --- live search call ---
        console.log('[INFO] calling search_car_deals (make: Toyota, model: Camry, cars.com only) - this hits a real site, may take ~15-20s ...');
        const result = await client.callTool({
            name: 'search_car_deals',
            arguments: {
                make: 'Toyota',
                model: 'Camry',
                maxResults: 2,
                sources: ['cars.com'],
            },
        });

        assert.equal(result.isError, undefined, `tool call returned an error: ${JSON.stringify(result)}`);
        assert.equal(result.content.length, 1);
        assert.equal(result.content[0].type, 'text');

        const text = result.content[0].text;
        assert.match(text, /# Car Deals Search Results/);
        assert.match(text, /\*\*Search:\*\* Toyota Camry/);
        assert.doesNotMatch(text, /No listings found\./, 'live search returned zero listings - scraper may be broken or site layout changed');
        assert.match(text, /Source: Cars\.com/, 'expected at least one Cars.com listing in the response');

        console.log('[PASS] live search_car_deals call returned real Cars.com listings');
        console.log('\nAll checks passed.');
    } finally {
        await client.close();
    }
}

main().catch(err => {
    console.error('[FAIL]', err.message);
    process.exit(1);
});
