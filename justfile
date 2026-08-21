default:
    @just --list

# Install dependencies
init:
    uv sync

# Lint + format check (the offline gate, run before committing)
check:
    uv run ruff check . && uv run ruff format --check . && uv run mypy src

# Lint and autofix what can be fixed
lint-fix:
    uv run ruff check --fix . && uv run ruff format .

# Import-smoke every source module to catch syntax/import errors
syntax:
    uv run python -c "import car_deals_mcp.server, car_deals_mcp.scrapers, car_deals_mcp.logger"

# Full end-to-end MCP client test - hits Cars.com / Autotrader UK / GOV.UK for real, takes minutes
test:
    uv run python test/test_mcp_client.py

# Quick Cars.com smoke - print a few listings
test-scraper:
    uv run python -c "import asyncio; from car_deals_mcp.scrapers import scrape_carscom; \
        from car_deals_mcp.types import SearchParams; \
        r = asyncio.run(scrape_carscom(SearchParams(make='Toyota', model='Camry'), 3)); \
        [print(l.format()) for l in r.listings]"

# check plus the live tests
check-all: check test

# Build the wheel + sdist (replaces npm pack)
build:
    uv build

# Run the MCP server on stdio, as an MCP client would
run:
    uv run car-deals-mcp

# Delete build output and installed dependencies
clean:
    rm -rf .venv dist/ build/ *.egg-info/ __pycache__/ src/*.egg-info/
