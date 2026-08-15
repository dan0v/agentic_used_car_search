default:
    @just --list

# Install dependencies
init:
    npm ci

# Lint
lint:
    npm run lint

# Lint and autofix what can be fixed
lint-fix:
    npm run lint:fix

# Fail on .editorconfig violations (indentation, line endings, trailing space)
ec:
    npm run ec

# Parse every source file to catch syntax errors
syntax:
    npm run syntax

# Everything that runs without touching the network
check: lint ec syntax

# Full end-to-end MCP client test - hits Cars.com for real, takes ~60-90s
test:
    npm run test:mcp

# Scrape a few Cars.com listings and print them - quick scraper smoke test
test-scraper:
    npm run test

# check plus the live tests
check-all: check test

# Pack the publishable tarball (there is no compile step; this is a CJS package)
build:
    npm run build

# Run the MCP server on stdio, as an MCP client would
run:
    npm start

# Delete build output and installed dependencies
clean:
    rm -rf node_modules *.tgz
