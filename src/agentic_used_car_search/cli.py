"""Command-line interface for agentic-used-car-search.

Exposes all MCP tools as CLI commands (search, detail, mot, serve) for direct
use in AI agent skills (e.g. Odysseus) or standalone shell execution.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from typing import Any

from .logger import attach_loop_exception_handler, install_global_handlers, logger
from .types import Config, SearchParams

install_global_handlers()


def _loop_factory() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    attach_loop_exception_handler(loop)
    return loop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='agentic-used-car-search',
        description='Agentic Used Car Search CLI & MCP Server (US + UK car listings, details, and UK MOT history).',
    )
    parser.add_argument(
        '--country',
        choices=['US', 'UK'],
        default='US',
        help='Default country (US or UK).',
    )
    parser.add_argument(
        '--cloakbrowser-key',
        default=os.environ.get('CLOAKBROWSER_LICENSE_KEY'),
        help='CloakBrowser license key (env: CLOAKBROWSER_LICENSE_KEY).',
    )
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug log level.')
    parser.add_argument('--trace', action='store_true', help='Trace log level (verbosest).')
    parser.add_argument(
        '--check-update',
        action='store_true',
        help='Check if the local repository is behind origin and optionally update.',
    )

    subparsers = parser.add_subparsers(dest='command', help='Available subcommands')

    # search
    p_search = subparsers.add_parser(
        'search',
        aliases=['search-car-deals'],
        help='Search used car listings across US or UK car marketplaces.',
    )
    p_search.add_argument('--make', default='', help='Car manufacturer (e.g. Toyota, BMW, Ford).')
    p_search.add_argument('--model', default='', help='Car model (e.g. Camry, 3 Series, F-150).')
    p_search.add_argument(
        '--country',
        dest='sub_country',
        choices=['US', 'UK'],
        default=None,
        help='Country to search in (US or UK).',
    )
    p_search.add_argument(
        '--zip',
        '--postcode',
        dest='zip',
        default=None,
        help='ZIP code (US, default: 90210) or postcode (UK, default: SW1A 1AA).',
    )
    p_search.add_argument(
        '--max-distance',
        type=int,
        default=None,
        help='Search radius in miles (0 = nationwide).',
    )
    p_search.add_argument('--year-min', type=int, default=None, help='Minimum model year.')
    p_search.add_argument('--year-max', type=int, default=None, help='Maximum model year.')
    p_search.add_argument(
        '--price-max',
        type=int,
        default=None,
        help='Maximum price (USD for US, GBP for UK).',
    )
    p_search.add_argument('--mileage-max', type=int, default=None, help='Maximum mileage.')
    p_search.add_argument(
        '--max-results',
        type=int,
        default=10,
        help='Maximum results per source (default: 10).',
    )
    p_search.add_argument(
        '--sources',
        nargs='*',
        default=None,
        help='Sources to query (US: cars.com autotrader kbb; UK: autotrader-uk motors cinch ebay).',
    )
    p_search.add_argument(
        '--one-owner',
        action='store_true',
        help='US only: Filter for CARFAX 1-Owner vehicles.',
    )
    p_search.add_argument(
        '--no-accidents',
        action='store_true',
        help='US only: Filter for vehicles with no accidents reported.',
    )
    p_search.add_argument(
        '--personal-use',
        action='store_true',
        help='US only: Filter for personal use vehicles only.',
    )
    p_search.add_argument(
        '--transmission',
        default=None,
        help='UK only: Filter by gearbox (e.g. Manual, Automatic).',
    )
    p_search.add_argument(
        '--drivetrain',
        default=None,
        help='UK only: Filter by drivetrain (e.g. AWD, FWD, RWD, 4WD).',
    )
    p_search.add_argument(
        '--cloakbrowser-key',
        dest='sub_cloakbrowser_key',
        default=None,
        help='CloakBrowser license key.',
    )
    p_search.add_argument(
        '--json',
        action='store_true',
        help='Output structured JSON instead of Markdown.',
    )

    # detail
    p_detail = subparsers.add_parser(
        'detail',
        aliases=['details', 'get-listing-details'],
        help='Fetch full listing details (specs, features, dealer info) as Markdown or JSON.',
    )
    p_detail.add_argument('url', nargs='?', default=None, help='Listing detail page URL.')
    p_detail.add_argument(
        '--url',
        dest='url_flag',
        default=None,
        help='Listing detail page URL (flag format).',
    )
    p_detail.add_argument(
        '--include-links',
        action='store_true',
        help='Keep hyperlink URLs in markdown.',
    )
    p_detail.add_argument(
        '--include-images',
        action='store_true',
        help='Keep image references in markdown.',
    )
    p_detail.add_argument(
        '--max-length',
        type=int,
        default=30000,
        help='Truncate markdown character limit (default: 30000).',
    )
    p_detail.add_argument(
        '--cloakbrowser-key',
        dest='sub_cloakbrowser_key',
        default=None,
        help='CloakBrowser license key.',
    )
    p_detail.add_argument(
        '--json',
        action='store_true',
        help='Output structured JSON instead of Markdown.',
    )

    # mot
    p_mot = subparsers.add_parser(
        'mot',
        aliases=['check-mot-history'],
        help='Check UK MOT vehicle history and recalls from GOV.UK.',
    )
    p_mot.add_argument(
        'registration',
        nargs='?',
        default=None,
        help='UK vehicle registration (number plate, e.g. "YL08 NNV").',
    )
    p_mot.add_argument(
        '--registration',
        dest='reg_flag',
        default=None,
        help='UK vehicle registration (flag format).',
    )
    p_mot.add_argument(
        '--cloakbrowser-key',
        dest='sub_cloakbrowser_key',
        default=None,
        help='CloakBrowser license key.',
    )
    p_mot.add_argument(
        '--retry',
        action='store_true',
        help='Retry once after 2s if response has 0 tests or is limited.',
    )
    p_mot.add_argument(
        '--json',
        action='store_true',
        help='Output structured JSON instead of Markdown.',
    )

    # serve
    p_serve = subparsers.add_parser(
        'serve',
        help='Run the stdio MCP server for MCP clients.',
    )
    p_serve.add_argument(
        '--country',
        dest='sub_country',
        choices=['US', 'UK'],
        default=None,
        help='Default country the server operates in.',
    )
    p_serve.add_argument(
        '--cloakbrowser-key',
        dest='sub_cloakbrowser_key',
        default=None,
        help='CloakBrowser license key.',
    )

    return parser


async def _cli_search(args: argparse.Namespace, config: Config) -> int:
    from .server import execute_search, format_search_output

    country = (getattr(args, 'sub_country', None) or args.country or config.country or 'US').upper()
    is_uk = country == 'UK'
    params = SearchParams(
        make=args.make or '',
        model=args.model or '',
        country=country,
        zip=args.zip or ('SW1A 1AA' if is_uk else '90210'),
        max_distance=args.max_distance,
        year_min=args.year_min,
        year_max=args.year_max,
        price_max=args.price_max,
        mileage_max=args.mileage_max,
        one_owner=args.one_owner if args.one_owner else None,
        no_accidents=args.no_accidents if args.no_accidents else None,
        personal_use=args.personal_use if args.personal_use else None,
        transmission=args.transmission,
        drivetrain=args.drivetrain,
    )
    max_results = args.max_results or 10

    raw_sources = args.sources
    sources: list[str] | None = None
    if raw_sources:
        sources = []
        for s in raw_sources:
            sources.extend([item.strip() for item in s.split(',') if item.strip()])

    if not sources:
        sources = ['autotrader-uk'] if is_uk else ['cars.com']

    listings, suggestions, errors, skipped_filters, unsupported_all = await execute_search(
        params=params,
        sources=sources,
        max_results=max_results,
        send_progress=None,
        config=config,
    )

    if unsupported_all:
        msg = (
            'All selected sources were skipped because they do not support '
            'transmission/drivetrain filtering. Re-run with '
            '`sources: ["autotrader-uk"]` or `["cinch"]` (or omit `sources`) '
            'to use a filterable source.'
        )
        if args.json:
            print(json.dumps({'error': msg, 'skipped_sources': skipped_filters}, indent=2))
        else:
            sys.stderr.write(msg + '\n')
        return 1

    if args.json:
        output_data: dict[str, Any] = {
            'search': dataclasses.asdict(params),
            'total_listings': len(listings),
            'listings': [dataclasses.asdict(listing) for listing in listings],
            'model_suggestions': [dataclasses.asdict(s) for s in suggestions]
            if suggestions
            else [],
            'errors': errors,
            'skipped_sources': skipped_filters,
        }
        print(json.dumps(output_data, indent=2))
    else:
        output = format_search_output(params, listings, suggestions, errors, skipped_filters, is_uk)
        print(output)

    if not listings:
        return 1
    return 0


async def _cli_detail(args: argparse.Namespace, config: Config) -> int:
    from .scrapers import fetch_listing_details
    from .server import format_detail_output

    url = args.url or getattr(args, 'url_flag', None)
    if not url:
        sys.stderr.write('Error: URL is required for detail command.\n')
        return 1

    options = {
        'includeLinks': args.include_links,
        'includeImages': args.include_images,
        'maxLength': args.max_length,
    }

    try:
        details = await fetch_listing_details(url, options, None, config)
        if args.json:
            print(json.dumps(dataclasses.asdict(details), indent=2))
        else:
            print(format_detail_output(details))
        return 0
    except Exception as err:  # noqa: BLE001
        logger.error('detail command failed', err)
        if args.json:
            print(json.dumps({'error': str(err), 'url': url}, indent=2))
        else:
            sys.stderr.write(f'Error fetching listing details: {err}\n')
        return 1


async def _cli_mot(args: argparse.Namespace, config: Config) -> int:
    from .scrapers import fetch_mot_history
    from .server import format_mot_output

    registration = args.registration or getattr(args, 'reg_flag', None)
    if not registration:
        sys.stderr.write('Error: registration plate is required for mot command.\n')
        return 1

    max_retries = 2 if getattr(args, 'retry', False) else 1

    try:
        record = await fetch_mot_history(registration, None, config, max_retries=max_retries)
        if args.json:
            print(json.dumps(dataclasses.asdict(record), indent=2))
        else:
            print(format_mot_output(record))

        if not record.found or len(record.tests) == 0:
            return 1
        return 0
    except Exception as err:  # noqa: BLE001
        logger.error('mot command failed', err)
        if args.json:
            print(json.dumps({'error': str(err), 'registration': registration}, indent=2))
        else:
            sys.stderr.write(f'Error checking MOT history: {err}\n')
        return 1


async def _dispatch_async(args: argparse.Namespace, config: Config) -> int:
    from .scrapers._http import close_client

    try:
        if args.command in ('search', 'search-car-deals'):
            return await _cli_search(args, config)
        if args.command in ('detail', 'details', 'get-listing-details'):
            return await _cli_detail(args, config)
        if args.command in ('mot', 'check-mot-history'):
            return await _cli_mot(args, config)
        return 0
    finally:
        await close_client()


def _check_update() -> None:
    try:
        fetch_res = subprocess.run(
            ['git', 'fetch', 'origin', '--quiet'],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if fetch_res.returncode != 0:
            sys.stderr.write(f'[Update Check] git fetch failed: {fetch_res.stderr.strip()}\n')
            return

        rev_res = subprocess.run(
            ['git', 'rev-list', 'HEAD...origin/main', '--count'],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if rev_res.returncode == 0:
            count = int(rev_res.stdout.strip() or '0')
            if count > 0:
                sys.stderr.write(
                    f'[Update Check] Local repository is behind origin/main by {count} commit(s).\n'
                    'Run `git pull && uv sync` to update.\n'
                )
            else:
                sys.stderr.write('[Update Check] Repository is up to date with origin/main.\n')
        else:
            sys.stderr.write('[Update Check] Unable to determine commit distance to origin/main.\n')
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f'[Update Check] Warning: update check failed: {exc}\n')


def run_cli(argv: list[str] | None = None) -> int:
    from .server import run as run_server

    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, 'check_update', False):
        _check_update()

    sub_key = getattr(args, 'sub_cloakbrowser_key', None)
    cloak_key = sub_key or args.cloakbrowser_key or os.environ.get('CLOAKBROWSER_LICENSE_KEY')
    country = (getattr(args, 'sub_country', None) or args.country or 'US').upper()
    config = Config(country=country, cloakbrowser_key=cloak_key)

    if args.command is None or args.command == 'serve':
        run_server(config)
        return 0

    return asyncio.run(_dispatch_async(args, config), loop_factory=_loop_factory)


def main() -> None:
    code = run_cli()
    if code != 0:
        sys.exit(code)
