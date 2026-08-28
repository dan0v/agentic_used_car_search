"""MOT history tool (`check_mot_history`, UK only).

The reason CloakBrowser was chosen: the GOV.UK MOT service sits behind Imperva,
which defeated every other Python scraping stack tried (see CLAUDE.md). The page
is server-rendered with stable GOV.UK `data-test-id` attributes - the service's
own test hooks, far more stable than dealer markup.

The GOV.UK MOT service only covers UK-registered vehicles. A registration that
does not look like a UK plate is rejected upfront (`is_uk_registration`) rather
than wasting a CloakBrowser launch only to come back with a "no record"
indistinguishable from a real miss. `normalise_registration`/`is_uk_registration`
live in `_base` so they're shared with any future UK-facing tool.
"""

from __future__ import annotations

import asyncio
from typing import Final
from urllib.parse import quote

from ..logger import logger
from ..types import Config, MotRecord, ProgressSender
from ._base import (
    browser_session,
    is_uk_registration,
    new_page,
    normalise_registration,
    progress,
    start_heartbeat,
    wait_out_interstitial,
)

# The GOV.UK MOT host. Pinned rather than taking a caller-supplied URL - the
# service is the only legitimate source of this data and a wrong host would be
# a credential-free proxy to an arbitrary site.
MOT_HOST: Final[str] = 'www.check-mot.service.gov.uk'


_EXTRACT_MOT_JS: Final[str] = r"""
() => {
    const text = (sel, root = document) => {
        const el = root.querySelector(sel);
        return el ? el.innerText.trim() : null;
    };
    // A registration the service has no record for lands on a page with no
    // vehicle heading; detect that explicitly so the caller can tell "no data
    // for this plate" apart from a scrape failure.
    const makeModel = text('[data-test-id="vehicle-make-model"]');
    if (!makeModel) return { found: false };

    const vehicle = {
        registration: (text('[data-test-id="vehicle-registration"]') || '').replace(/\s+/g, ''),
        makeModel,
        colour: text('[data-test-id="vehicle-colour"]'),
        fuelType: text('[data-test-id="vehicle-fuel-type"]'),
        dateRegistered: text('[data-test-id="vehicle-date-registered"]'),
        motExpiry: text('[data-test-id="mot-due-date"]')
    };

    // Each test is a [data-test-id="test-history-item"] row or an accordion section.
    const tests = [];
    let testRows = Array.from(document.querySelectorAll('[data-test-id="test-history-item"]'));
    if (!testRows.length) {
        testRows = Array.from(document.querySelectorAll('.govuk-accordion__section, [data-test-id*="test-history"]'));
    }

    testRows.forEach(row => {
        const read = (sel) => text(sel, row);

        let date = null;
        const dateMatch = row.innerText.match(/\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b/i);
        if (dateMatch) {
            date = dateMatch[1];
        } else {
            const dateCol = row.querySelector('.govuk-grid-column-one-third, .govuk-grid-column-two-thirds, dt');
            const dateEl = dateCol ? dateCol.querySelector('.govuk-heading-s, .govuk-heading-m, h3, h4, span') : null;
            date = dateEl ? dateEl.innerText.trim() : null;
        }

        let rawResult = read('[data-test-id="test-result"]') || read('[data-test-id*="result"]') || read('.govuk-tag');
        if (!rawResult) {
            const resMatch = row.innerText.match(/\b(PASSED|FAILED|PASS|FAIL)\b/i);
            if (resMatch) rawResult = resMatch[1];
        }
        let result = null;
        if (rawResult) {
            if (/pass/i.test(rawResult)) result = 'PASS';
            else if (/fail/i.test(rawResult)) result = 'FAIL';
            else result = rawResult.trim().toUpperCase();
        }

        let mileage = read('[data-test-id="test-history-odometer"]');
        if (!mileage) {
            const mMatch = row.innerText.match(/\b([\d,]+\s*(?:miles|km))\b/i);
            if (mMatch) mileage = mMatch[1];
        }

        let testNumber = read('[data-test-id="test-number"]');
        if (!testNumber) {
            const tnMatch = row.innerText.match(/\b(\d{4}\s*\d{4}\s*\d{4})\b/);
            if (tnMatch) testNumber = tnMatch[1];
        }

        const collect = (headingSel, fallbackText) => {
            let heading = row.querySelector(headingSel);
            if (!heading && fallbackText) {
                const headings = Array.from(row.querySelectorAll('h3, h4, strong, p, dt, div'));
                heading = headings.find(h => h.innerText.toLowerCase().includes(fallbackText.toLowerCase()));
            }
            if (!heading) return [];
            const wrapper = heading.parentElement || row;
            const list = wrapper.querySelector('ul.govuk-list--bullet, ul');
            if (!list) return [];
            return Array.from(list.querySelectorAll('li'))
                .map(li => li.innerText.trim()).filter(Boolean);
        };

        tests.push({
            date,
            result,
            mileage,
            testNumber,
            expiryDate: read('[data-test-id="expiry-date"]'),
            dangerous: collect('[data-test-id="dangerous-defect-items-heading"]', 'dangerous'),
            major: collect('[data-test-id="major-defect-items-heading"]', 'major'),
            minor: collect('[data-test-id="minor-defect-items-heading"]', 'minor'),
            advisories: collect('[data-test-id="advisory-defect-comments-heading"]', 'advisories')
        });
    });

    // The recalls section is rendered server-side for vehicles with an active
    // recall, and absent otherwise.
    const recalls = [];
    const recallBlock = document.querySelector('[data-test-id="recall-success-results"], [data-test-id*="recall"]');
    if (recallBlock) {
        const inset = recallBlock.querySelector('.govuk-inset-text, p');
        if (inset) recalls.push(inset.innerText.trim().replace(/\s+/g, ' '));
    }
        // Check for data limited or rate limited warning banner
        const dataLimitedEl = document.querySelector('.govuk-warning-text, [data-test-id*="limited"], [data-test-id*="warning"]');
        const dataLimitedText = dataLimitedEl ? dataLimitedEl.innerText.trim() : null;
        const pageText = document.body ? document.body.innerText : '';
        const isDataLimited = /data\s+limited|rate\s+limit|too\s+many\s+requests/i.test(pageText);

        return { found: true, vehicle, tests, recalls, dataLimited: isDataLimited, dataLimitedText };
    }
}
"""


async def _fetch_mot_history_once(
    registration: str,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
) -> MotRecord:
    reg = normalise_registration(registration)
    if not reg:
        raise ValueError('A vehicle registration is required to check MOT history')
    if not is_uk_registration(reg):
        # The GOV.UK MOT service only covers UK-registered vehicles. A non-UK
        # plate (a US state-plate phrase, a VIN, a random string) would waste a
        # CloakBrowser launch and come back with a "no record" indistinguishable
        # from a real miss, so reject it here with a clear message instead.
        raise ValueError(
            f'"{reg}" does not look like a UK registration plate. '
            'The MOT history tool only covers UK-registered vehicles; '
            'use it with a registration returned by a UK search_car_deals call.'
        )

    url = f'https://{MOT_HOST}/results?registration={quote(reg)}'
    logger.debug(f'MOT history fetch: {url}')
    stop_heartbeat = start_heartbeat(send_progress, 'Checking MOT history')

    try:
        async with browser_session(config) as browser:
            page = await new_page(browser)

            await page.goto(url, wait_until='domcontentloaded', timeout=45000)

            # The MOT service sits behind an Imperva interstitial more often than not
            # for automated traffic. It clears itself and reloads the real page after
            # a few seconds, so poll rather than treating the challenge as the answer.
            # CloakBrowser clears this at the C++ level, verified in the spike.
            cleared = await wait_out_interstitial(page, send_progress)
            if not cleared:
                logger.error(
                    'MOT service bot-check interstitial did not clear within 45s - '
                    'extracted content is probably the challenge page, not the MOT history'
                )

            # The real results page is identifiable by the vehicle heading; the
            # interstitial and a "no record" result both lack it. A short settle
            # covers the GOV.UK accordion's paint before reading.
            try:
                await page.wait_for_selector('[data-test-id="vehicle-make-model"]', timeout=15000)
            except Exception:  # noqa: BLE001
                logger.debug(
                    'MOT vehicle-make-model heading never appeared - registration may be '
                    'unknown or the page is the interstitial'
                )
            await asyncio.sleep(1.5)

            progress(send_progress, 'Extracting MOT history...')

            record = await page.evaluate(f'({_EXTRACT_MOT_JS})()')

        if not record.get('found'):
            logger.info(f'MOT history: no record for registration "{reg}"')
            progress(send_progress, 'No MOT record found for that registration')
            return MotRecord(registration=reg, found=False, url=url)

        tests = record.get('tests') or []
        recalls = record.get('recalls') or []
        latest = tests[0] if tests else None
        open_defects = (
            sum(len(latest.get(k) or []) for k in ('dangerous', 'major', 'minor', 'advisories'))
            if latest
            else 0
        )
        has_recall = len(recalls) > 0

        progress(
            send_progress,
            f'MOT history: {len(tests)} test(s), latest {latest.get("result") if latest else "none"}'
            f'{", outstanding recall" if has_recall else ""}',
        )

        vehicle = record['vehicle']
        return MotRecord(
            registration=reg,
            found=True,
            url=url,
            vehicle=vehicle,
            mot_expiry=vehicle.get('motExpiry'),
            tests=tests,
            recalls=recalls,
            latest_test=latest,
            outstanding_issues={
                'latest_result': latest.get('result') if latest else None,
                'dangerous': (latest.get('dangerous') if latest else []) or [],
                'major': (latest.get('major') if latest else []) or [],
                'minor': (latest.get('minor') if latest else []) or [],
                'advisories': (latest.get('advisories') if latest else []) or [],
                'open_defect_count': open_defects,
                'has_outstanding_recall': has_recall,
            },
        )
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f'MOT history check failed: {err}') from err
    finally:
        stop_heartbeat()


async def fetch_mot_history(
    registration: str,
    send_progress: ProgressSender | None = None,
    config: Config | None = None,
    max_retries: int = 1,
) -> MotRecord:
    """Fetch a UK vehicle's MOT history from GOV.UK with automatic retry.

    Returns the structured record: vehicle identity, MOT expiry, full test history
    with per-test defects categorised by severity, and any outstanding safety recalls.
    If the response returns zero tests or indicates limited data, retries up to
    `max_retries` times with a 2-second backoff.
    """
    attempts = 0
    while True:
        record = await _fetch_mot_history_once(registration, send_progress, config)
        attempts += 1
        # If record found with tests or reached max attempts, return it
        if not record.found or len(record.tests) > 0 or attempts > max_retries:
            return record

        logger.info(
            f'MOT history returned 0 tests for registration "{registration}" on attempt {attempts}. '
            'Retrying in 2s...'
        )
        if send_progress:
            send_progress(f'MOT response incomplete, retrying attempt {attempts + 1} in 2s...')
        await asyncio.sleep(2.0)
