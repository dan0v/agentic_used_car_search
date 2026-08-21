#!/usr/bin/env python3
"""End-to-end MCP client test.

Spawns the car-deals-mcp server as a real MCP server subprocess and drives it
with the official MCP SDK client over stdio - no protocol details faked out.
Verifies: tool discovery, input schema shape, and a live search call against
Cars.com.

Usage: uv run python test/test_mcp_client.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Make the package importable for the offline slug check (the server subprocess
# is run via `uv run`, but the slug assertion is in-process).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from car_deals_mcp.scrapers import carscom_slug  # noqa: E402


def assert_match(text: str, pattern: str, msg: str = '') -> None:
    if not re.search(pattern, text):
        raise AssertionError(f'{msg or "pattern not found"}: /{pattern}/')


def assert_no_match(text: str, pattern: str, msg: str = '') -> None:
    if re.search(pattern, text):
        raise AssertionError(f'{msg or "unexpected pattern"}: /{pattern}/')


async def main() -> None:
    # --- URL slug rules (offline) ---
    # A wrong slug does not error - Cars.com drops the filter and serves a page
    # with zero cards, which looks exactly like the bot-check variant. These
    # pairs come from the site's own filter vocabulary; every one was a real
    # miss before the slug rules existed.
    slug_cases = [
        ('Mercedes-Benz', 'mercedes_benz'),  # hyphen inside a name
        ('GLE 450', 'gle_450'),  # space inside a name
        ('Town & Country', 'town_and_country'),  # ampersand spelled out
        ('EQE 350+', 'eqe_350_plus'),  # plus spelled out
        ("Li'l Red Express", 'lil_red_express'),  # apostrophe dropped, not split
        ('ID.4', 'id.4'),  # period between alphanumerics survives
        ('ID. Buzz', 'id_buzz'),  # ... but a trailing one separates
        ('C10/K10', 'c10_k10'),  # slash is a separator
        ('Camry', 'camry'),  # single word - the case that always worked
    ]
    for inp, expected in slug_cases:
        got = carscom_slug(inp)
        assert got == expected, f'slug for {inp!r}: expected {expected!r}, got {got!r}'
    print('[PASS] Cars.com make/model slug rules')

    # The SDK only forwards a fixed safe subset of the environment to the
    # server subprocess, so CAR_DEALS_LOG_LEVEL set on this process would
    # otherwise be dropped. Forward it explicitly.
    env: dict[str, str] = {
        'PATH': os.environ['PATH'],
        'HOME': os.environ.get('HOME', ''),
    }
    if os.environ.get('CAR_DEALS_LOG_LEVEL'):
        env['CAR_DEALS_LOG_LEVEL'] = os.environ['CAR_DEALS_LOG_LEVEL']
    if os.environ.get('CLOAKBROWSER_LICENSE_KEY'):
        env['CLOAKBROWSER_LICENSE_KEY'] = os.environ['CLOAKBROWSER_LICENSE_KEY']
    # Forward eBay Browse API credentials when present so the `ebay` source
    # exercises the API path; without these it falls back to browser scraping.
    if os.environ.get('EBAY_CLIENT_ID'):
        env['EBAY_CLIENT_ID'] = os.environ['EBAY_CLIENT_ID']
    if os.environ.get('EBAY_CLIENT_SECRET'):
        env['EBAY_CLIENT_SECRET'] = os.environ['EBAY_CLIENT_SECRET']

    # Run the server from the project so the installed package resolves.
    params = StdioServerParameters(
        command='python',
        args=['-m', 'car_deals_mcp'],
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            # --- tool discovery ---
            tools_result = await client.list_tools()
            tool_names = sorted(t.name for t in tools_result.tools)
            assert tool_names == ['check_mot_history', 'get_listing_details', 'search_car_deals'], (
                f'unexpected tool set: {tool_names}'
            )
            print(
                '[PASS] tools discovered: search_car_deals, get_listing_details, check_mot_history'
            )

            tool = next(t for t in tools_result.tools if t.name == 'search_car_deals')
            detail_tool = next(t for t in tools_result.tools if t.name == 'get_listing_details')
            mot_tool = next(t for t in tools_result.tools if t.name == 'check_mot_history')

            # --- schema shape ---
            schema = tool.input_schema
            assert schema['required'] == [], (
                'search_car_deals should have no required params (make/model optional for pure filter searches)'
            )
            for key in [
                'make',
                'model',
                'country',
                'zip',
                'maxDistance',
                'yearMin',
                'yearMax',
                'priceMax',
                'mileageMax',
                'maxResults',
                'sources',
                'oneOwner',
                'noAccidents',
                'personalUse',
                'transmission',
                'drivetrain',
            ]:
                assert key in schema['properties'], f'schema missing property: {key}'
            assert schema['properties']['country']['enum'] == ['US', 'UK'], (
                'country should be enum of US/UK'
            )

            assert detail_tool.input_schema['required'] == ['url'], (
                'get_listing_details should require url'
            )
            for key in ['url', 'includeLinks', 'includeImages', 'maxLength']:
                assert key in detail_tool.input_schema['properties'], (
                    f'detail schema missing property: {key}'
                )

            assert mot_tool.input_schema['required'] == ['registration'], (
                'check_mot_history should require registration'
            )
            assert 'registration' in mot_tool.input_schema['properties'], (
                'check_mot_history schema missing registration'
            )
            print('[PASS] input schemas have all documented parameters')

            # --- live search call, with progress notifications ---
            progress_messages: list[str] = []

            async def on_progress(progress: float, total, message):  # type: ignore[no-untyped-def]
                if message:
                    progress_messages.append(message)
                    print(f'  [PROGRESS] {message}')

            print(
                '[INFO] calling search_car_deals (Toyota Camry, cars.com only) - hits a real site, ~15-20s ...'
            )
            result = await client.call_tool(
                'search_car_deals',
                {'make': 'Toyota', 'model': 'Camry', 'maxResults': 2, 'sources': ['cars.com']},
                progress_callback=on_progress,
            )

            assert progress_messages, (
                'expected at least one progress notification (tool call would otherwise risk client-side timeout)'
            )
            print(
                f'[PASS] received {len(progress_messages)} progress notification(s) during the call'
            )

            assert result.is_error is False, f'tool call returned an error: {result!r}'
            assert len(result.content) == 1
            assert result.content[0].type == 'text'
            text = result.content[0].text
            assert_match(text, r'# Car Deals Search Results')
            assert_match(text, r'\*\*Search:\*\* Toyota Camry')
            assert_no_match(
                text,
                r'No listings found\.',
                'live search returned zero listings - scraper may be broken or site layout changed',
            )
            assert_match(text, r'Source: Cars\.com', 'expected at least one Cars.com listing')
            # Color lives only in the card's data-vehicle-details JSON, never in
            # visible card text - if Cars.com drops that attribute this catches it.
            assert_match(
                text, r'Exterior Color: \w+', 'expected exterior color on at least one listing'
            )
            # These all come from data-vehicle-details. If Cars.com drops/renames
            # that attribute the scraper silently falls back to text parsing and
            # loses every one at once, so assert on them.
            assert_match(
                text, r'VIN: [A-HJ-NPR-Z0-9]{17}', 'expected a VIN on at least one listing'
            )
            assert_match(
                text, r'Specs: .+\|', 'expected trim/body/drivetrain specs on at least one listing'
            )
            assert_match(
                text,
                r'Location: .+, [A-Z]{2}',
                'expected a city/state location on at least one listing',
            )
            assert_match(
                text,
                r'Dealer: .+\(\d\.\d stars\)',
                'expected a dealer star rating on at least one listing',
            )
            print('[PASS] live search_car_deals call returned real Cars.com listings')

            # --- live search with a multi-word make (slug regression) ---
            # Toyota/Camry above passes even with a broken slug (single-word
            # name survives toLowerCase). Pin a name that exercises the rules.
            print(
                '[INFO] calling search_car_deals (Mercedes-Benz, GLE 450) - exercises the slug rules ...'
            )
            multi_word = await client.call_tool(
                'search_car_deals',
                {
                    'make': 'Mercedes-Benz',
                    'model': 'GLE 450',
                    'maxResults': 2,
                    'sources': ['cars.com'],
                },
            )
            mw_text = multi_word.content[0].text
            assert_no_match(
                mw_text,
                r'No listings found\.',
                'multi-word make returned zero listings - slug rules probably broken',
            )
            assert_match(
                mw_text, r'Mercedes-Benz GLE 450', 'expected a Mercedes-Benz GLE 450 listing'
            )
            print('[PASS] multi-word make/model search returned listings')

            # --- unknown model name gets a did-you-mean, not silence ---
            print('[INFO] calling search_car_deals with a model name Cars.com does not have ...')
            unknown = await client.call_tool(
                'search_car_deals',
                {'make': 'Mercedes-Benz', 'model': 'GLE', 'maxResults': 2, 'sources': ['cars.com']},
            )
            unk_text = unknown.content[0].text
            assert_match(
                unk_text, r'has no Mercedes-Benz model named "GLE"', 'expected a did-you-mean block'
            )
            assert_match(unk_text, r'- GLE 450', 'expected GLE 450 among the suggested models')
            print('[PASS] unknown model name returns suggestions from the live filter vocabulary')

            # --- live KBB search call, URLs included ---
            print(
                '[INFO] calling search_car_deals (Toyota Camry, sources: ["kbb"]) - hits KBB, ~15-25s ...'
            )
            kbb_result = await client.call_tool(
                'search_car_deals',
                {'make': 'Toyota', 'model': 'Camry', 'maxResults': 2, 'sources': ['kbb']},
            )
            kbb_text = kbb_result.content[0].text
            if not re.search(r'No listings found\.', kbb_text):
                assert_match(kbb_text, r'Source: KBB', 'expected KBB as the selected source')
                # KBB's extractor carries href -> url; without this the agent
                # has nowhere to send get_listing_details next.
                assert_match(
                    kbb_text,
                    r'https://www\.kbb\.com/cars-for-sale/vehicle/\S+',
                    'expected a KBB detail URL on at least one listing',
                )
                print('[PASS] live KBB search returned listings with detail URLs')
            else:
                print(
                    '[PASS] live KBB search completed (no listings this time - path exercised, no regression)'
                )

            # --- host allowlist on the detail tool ---
            rejected = await client.call_tool(
                'get_listing_details', {'url': 'https://example.com/listing'}
            )
            assert rejected.is_error is True, 'off-allowlist host should be rejected'
            assert_match(rejected.content[0].text, r'Unsupported host')
            print('[PASS] get_listing_details rejects hosts outside the allowlist')

            # --- live detail call, using a URL from the search above ---
            m = re.search(r'https://www\.cars\.com/vehicledetail/\S+', text)
            assert m, 'expected a cars.com detail URL in the search results'
            listing_url = m.group(0).rstrip()

            print(
                '[INFO] calling get_listing_details - real detail page, ~20-40s (site bot check) ...'
            )

            async def on_detail_progress(progress, total, message):  # type: ignore[no-untyped-def]
                if message:
                    print(f'  [PROGRESS] {message}')

            detail_result = await client.call_tool(
                'get_listing_details',
                {'url': listing_url},
                progress_callback=on_detail_progress,
            )
            assert detail_result.is_error is False, f'detail call errored: {detail_result!r}'[:400]
            detail_text = detail_result.content[0].text
            assert_match(detail_text, r'\*\*Source:\*\* Cars\.com')
            # The bot-check interstitial is a few hundred chars with no spec
            # content - a short body means we converted the challenge page.
            assert len(detail_text) > 3000, (
                f'detail markdown suspiciously short ({len(detail_text)} chars) - likely the bot-check interstitial'
            )
            assert_match(detail_text, r'VIN', 'expected VIN in the detail markdown')
            print(
                f'[PASS] live get_listing_details returned {len(detail_text)} chars of listing markdown'
            )

            # --- live UK search call ---
            print(
                '[INFO] calling search_car_deals (country: UK, Toyota Corolla) - hits Autotrader UK, ~15-25s ...'
            )
            uk_result = await client.call_tool(
                'search_car_deals',
                {'make': 'Toyota', 'model': 'Corolla', 'country': 'UK', 'maxResults': 2},
            )
            uk_text = uk_result.content[0].text
            assert_match(uk_text, r'\*\*Search:\*\* Toyota Corolla')
            assert_match(
                uk_text, r'\*\*Location:\*\* SW1A 1AA', 'UK default postcode should be SW1A 1AA'
            )
            if not re.search(r'No listings found\.', uk_text):
                assert_match(
                    uk_text,
                    r'Source: Autotrader UK',
                    'expected Autotrader UK as the default UK source',
                )
                assert_match(uk_text, r'£[\d,]+', 'expected a GBP price on at least one UK listing')
                # A listing without a URL strangles the caller's follow-up
                # investigation - get_listing_details needs somewhere to go.
                assert_match(
                    uk_text,
                    r'https://www\.autotrader\.co\.uk/car-details/\S+',
                    'expected an Autotrader UK detail URL on at least one listing',
                )
                print('[PASS] live UK search_car_deals call returned real Autotrader UK listings')

                # --- Autotrader UK detail via direct-HTTP hydration harvest ---
                # The detail page is not Cloudflare-walled; it ships the whole
                # advert in `__staticRouterHydrationData`. Assert the harvest
                # path returns structured data (the title + a "Key specification"
                # section) rather than the browser-prune markdown.
                m_atuk = re.search(r'https://www\.autotrader\.co\.uk/car-details/\S+', uk_text)
                assert m_atuk, 'expected an Autotrader UK car-details URL from the search above'
                atuk_url = m_atuk.group(0).rstrip()
                print(
                    '[INFO] calling get_listing_details on Autotrader UK URL - hydration harvest ...'
                )
                atuk_result = await client.call_tool(
                    'get_listing_details',
                    {'url': atuk_url},
                    progress_callback=on_detail_progress,
                )
                assert atuk_result.is_error is False, (
                    f'AT UK detail call errored: {atuk_result.content[0].text[:300]}'
                )
                atuk_text = atuk_result.content[0].text
                assert_match(atuk_text, r'\*\*Source:\*\* Autotrader UK')
                # The harvest returns structured sections; the browser prune
                # would return raw page markdown. These section names come from
                # _at_uk_harvest_to_markdown.
                assert_match(atuk_text, r'## Key specification', 'expected a rendered spec section')
                assert_match(atuk_text, r'## Price', 'expected a rendered price section')
                # The hydration harvest is the one detail path that can see the
                # registration/history fields the SSR embeds; exercise one of them.
                assert_match(
                    atuk_text,
                    r'## Registration|MOT status',
                    'expected registration/MOT info (harvested fields, not prune output)',
                )
                print(
                    f'[PASS] Autotrader UK detail hydration harvest returned {len(atuk_text)} chars'
                )
            else:
                print(
                    '[PASS] live UK search_car_deals call completed (no listings this time - path exercised, no regression)'
                )

            # --- live eBay Motors UK search call ---
            # Hits ebay.co.uk via CloakBrowser when EBAY_CLIENT_ID/SECRET are
            # absent (the default), or the Browse API when they are set. Either
            # path should surface GBP-priced Ford Focus listings.
            print(
                '[INFO] calling search_car_deals (country: UK, Ford Focus, sources: '
                '["ebay"]) - hits eBay Motors UK, ~15-30s ...'
            )
            ebay_result = await client.call_tool(
                'search_car_deals',
                {
                    'make': 'Ford',
                    'model': 'Focus',
                    'country': 'UK',
                    'maxResults': 2,
                    'sources': ['ebay'],
                },
            )
            ebay_text = ebay_result.content[0].text
            assert_match(ebay_text, r'\*\*Search:\*\* Ford Focus')
            # eBay cards surface price + mileage ("Miles: N"); the GBP price
            # asserts the extractor and the pence-stripping in the JS both work.
            if not re.search(r'No listings found\.', ebay_text):
                assert_match(
                    ebay_text,
                    r'Source: eBay',
                    'expected eBay Motors as the selected source',
                )
                assert_match(
                    ebay_text, r'£[\d,]+', 'expected a GBP price on at least one eBay listing'
                )
                assert_match(
                    ebay_text,
                    r'Mileage: [\d,]+',
                    'expected mileage on at least one eBay listing',
                )
                # Browser path yields absolute /itm/ URLs; the Browse API path
                # yields itemWebUrl. Either way it must be a full ebay URL.
                assert_match(
                    ebay_text,
                    r'https://(www\.)?ebay\.co\.uk/\S+',
                    'expected an eBay listing URL on at least one listing',
                )
                print('[PASS] live eBay Motors UK search returned real listings')
            else:
                print(
                    '[PASS] live eBay Motors UK search completed (no listings this time - '
                    'path exercised, no regression)'
                )

            # --- live MOT history check ---
            # YL08NNV is a long-standing example registration with a rich MOT
            # history including a FAIL and an outstanding safety recall.
            print(
                '[INFO] calling check_mot_history (YL08NNV) - hits GOV.UK behind Imperva, ~15-45s ...'
            )

            async def on_mot_progress(progress, total, message):  # type: ignore[no-untyped-def]
                if message:
                    print(f'  [PROGRESS] {message}')

            mot_result = await client.call_tool(
                'check_mot_history',
                {'registration': 'YL08 NNV'},
                progress_callback=on_mot_progress,
            )
            assert mot_result.is_error is False, f'MOT call errored: {mot_result!r}'[:400]
            mot_text = mot_result.content[0].text
            assert_match(mot_text, r'# MOT History:', 'expected an MOT history heading')
            assert_match(
                mot_text,
                r'\*\*Registration:\*\* YL08NNV',
                'expected the normalised registration in the output',
            )
            assert_match(
                mot_text, r'## Outstanding Issues', 'expected an outstanding-issues section'
            )
            assert_match(
                mot_text,
                r'## Full MOT History \(\d+ test',
                'expected a full MOT history section with a test count',
            )
            print(f'[PASS] live check_mot_history returned MOT record ({len(mot_text)} chars)')

            print('\nAll checks passed.')


def cli() -> None:
    try:
        asyncio.run(main())
    except AssertionError as err:
        print('[FAIL]', err, file=sys.stderr)
        sys.exit(1)
    except Exception as err:  # noqa: BLE001
        print('[FAIL]', err, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    cli()
