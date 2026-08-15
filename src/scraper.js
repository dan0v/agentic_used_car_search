const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const TurndownService = require('turndown');
puppeteer.use(StealthPlugin());

/**
 * Car listing data structure
 */
class CarListing {
    constructor(data) {
        this.title = data.title || null;
        this.price = data.price || null;
        this.priceDrop = data.priceDrop || null;
        this.msrp = data.msrp || null;
        this.monthlyPayment = data.monthlyPayment || null;
        this.mileage = data.mileage || null;
        this.dealerName = data.dealerName || null;
        this.dealerRating = data.dealerRating || null;
        this.location = data.location || null;
        this.dealRating = data.dealRating || null;
        this.exteriorColor = data.exteriorColor || null;
        // Spec fields, all from the card's embedded JSON
        this.vin = data.vin || null;
        this.year = data.year || null;
        this.make = data.make || null;
        this.model = data.model || null;
        this.trim = data.trim || null;
        this.bodyStyle = data.bodyStyle || null;
        this.drivetrain = data.drivetrain || null;
        this.fuelType = data.fuelType || null;
        this.isCertified = data.isCertified || false;
        this.delivery = data.delivery || null;
        this.awards = data.awards || [];
        this.thumbnail = data.thumbnail || null;
        this.url = data.url || null;
        this.source = data.source || null;
        // CarFax badges
        this.isOneOwner = data.isOneOwner || false;
        this.noAccidents = data.noAccidents || false;
        this.personalUse = data.personalUse || false;
    }

    format() {
        let result = `${this.title || 'Unknown Vehicle'}`;

        if (this.price) {
            result += `\n  Price: ${this.price}`;
            if (this.priceDrop) result += ` (dropped ${this.priceDrop})`;
            if (this.msrp) result += ` | MSRP: ${this.msrp}`;
        }
        if (this.monthlyPayment) result += `\n  Est. Payment: ${this.monthlyPayment}/mo`;
        if (this.mileage) result += `\n  Mileage: ${this.mileage}`;
        if (this.exteriorColor) result += `\n  Exterior Color: ${this.exteriorColor}`;

        // One line for the spec fields - each is short, and on its own line
        // they would triple the height of every listing.
        const specs = [this.trim, this.bodyStyle, this.drivetrain, this.fuelType].filter(Boolean);
        if (specs.length > 0) result += `\n  Specs: ${specs.join(' | ')}`;

        if (this.vin) result += `\n  VIN: ${this.vin}`;
        if (this.dealRating) result += `\n  Deal Rating: ${this.dealRating}`;

        const badges = [];
        if (this.isCertified) badges.push('Certified Pre-Owned');
        if (this.isOneOwner) badges.push('1-Owner');
        if (this.noAccidents) badges.push('No Accidents');
        if (this.personalUse) badges.push('Personal Use');
        if (badges.length > 0) result += `\n  Badges: ${badges.join(' | ')}`;

        if (this.awards.length > 0) result += `\n  Awards: ${this.awards.join(' | ')}`;

        if (this.dealerName) {
            result += `\n  Dealer: ${this.dealerName}`;
            if (this.dealerRating) result += ` (${this.dealerRating} stars)`;
        }
        if (this.location) result += `\n  Location: ${this.location}`;
        if (this.delivery) result += `\n  Delivery: ${this.delivery}`;
        if (this.source) result += `\n  Source: ${this.source}`;
        if (this.thumbnail) result += `\n  Photo: ${this.thumbnail}`;
        if (this.url) result += `\n  ${this.url}`;
        return result;
    }
}

/**
 * Launch browser with stealth settings
 */
async function launchBrowser() {
    return puppeteer.launch({
        headless: 'new',
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process'
        ]
    });
}

/**
 * Emit a progress message on a fixed interval while a slow scrape runs.
 * Scrapes take anywhere from a few seconds to ~90s (retries included), well
 * past most MCP clients' default tool-call timeout - periodic progress
 * notifications are what resets that timeout clock, not just start/end
 * messages. Returns a function that stops the heartbeat.
 */
function startHeartbeat(sendProgress, label, intervalMs = 4000) {
    if (!sendProgress) return () => {};
    let elapsed = 0;
    const id = setInterval(() => {
        elapsed += intervalMs;
        sendProgress(`${label} (${Math.round(elapsed / 1000)}s)...`);
    }, intervalMs);
    return () => clearInterval(id);
}

/**
 * Scrape Cars.com for car listings
 */
async function scrapeCarscom(params, maxResults = 20, sendProgress = null) {
    const listings = [];
    let browser;
    const stopHeartbeat = startHeartbeat(sendProgress, 'Searching Cars.com');

    try {
        browser = await launchBrowser();
        const page = await browser.newPage();
        await page.setViewport({ width: 1920, height: 1080 });

        // Build URL
        let url = 'https://www.cars.com/shopping/results/?';
        const urlParams = new URLSearchParams();
        urlParams.append('stock_type', 'used');
        if (params.make) urlParams.append('makes[]', params.make.toLowerCase());
        if (params.model) urlParams.append('models[]', `${params.make.toLowerCase()}-${params.model.toLowerCase()}`);
        if (params.zip) urlParams.append('zip', params.zip);
        if (params.yearMin) urlParams.append('year_min', params.yearMin);
        if (params.yearMax) urlParams.append('year_max', params.yearMax);
        if (params.priceMax) urlParams.append('list_price_max', params.priceMax);
        if (params.mileageMax) urlParams.append('mileage_max', params.mileageMax);
        // Search radius in miles. Cars.com wants the literal string "all" for
        // a nationwide search, not a large number.
        if (params.maxDistance !== undefined && params.maxDistance !== null) {
            urlParams.append('maximum_distance', params.maxDistance === 0 ? 'all' : params.maxDistance);
        }

        // CarFax history filters
        if (params.oneOwner) urlParams.append('one_owner', 'true');
        if (params.noAccidents) urlParams.append('no_accidents', 'true');
        if (params.personalUse) urlParams.append('personal_use', 'true');

        url += urlParams.toString();

        // Cars.com occasionally serves a card-less page to automated
        // traffic (probabilistic bot check, not a real "no results" case).
        // Reload once if the first attempt comes back empty.
        let rawListings = [];
        for (let attempt = 1; attempt <= 2; attempt++) {
            if (attempt > 1) sendProgress?.('Cars.com returned no results, retrying...');
            await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
            await page.waitForSelector('fuse-card[id^="vehicle-card-"]', { timeout: 15000 }).catch(() => {});
            rawListings = await extractListings(page);
            if (rawListings.length > 0) break;
        }

        async function extractListings(page) {
            // Extract listings from <fuse-card id="vehicle-card-..."> elements
            return page.evaluate(() => {
            const results = [];
            const cards = document.querySelectorAll('fuse-card[id^="vehicle-card-"]');

            cards.forEach(card => {
                const text = card.innerText;
                const lines = text.split('\n').filter(l => l.trim());

                let title = null;
                let price = null;
                let mileage = null;
                let dealRating = null;
                let location = null;

                for (const line of lines) {
                    const trimmed = line.trim();

                    // Title: optional condition prefix + Year Make Model
                    // (e.g., "Used 2020 Toyota Camry XSE", "Certified 2021 Toyota
                    // Camry SE", "2020 Toyota Camry XSE"). The prefix strip is a
                    // bounded literal match, not a wildcard - a greedy
                    // `certified[\w\s-]*` here previously ate past the year and
                    // model, leaving only the trailing trim (e.g. "SE") as title.
                    if (/^(?:(?:used|new|certified(?:\s+pre-owned)?)\s+)?(19|20)\d{2}\s+\S+/i.test(trimmed) && !title) {
                        title = trimmed.replace(/^(?:used|new|certified(?:\s+pre-owned)?)\s+/i, '');
                        continue;
                    }

                    // Price: "$XX,XXX" (may have "price drop" suffix)
                    const priceMatch = trimmed.match(/^\$[\d,]+/);
                    if (priceMatch && !price) {
                        price = priceMatch[0];
                        continue;
                    }

                    // Mileage: "XX,XXX mi."
                    if (/^[\d,]+\s*mi\.?$/i.test(trimmed) && !mileage) {
                        mileage = trimmed;
                        continue;
                    }

                    // Deal rating: "Good Deal", "Great Deal", etc.
                    if (/^(great|good|fair|high|no price)/i.test(trimmed) && !dealRating) {
                        dealRating = trimmed.split('|')[0].trim();
                        continue;
                    }

                    // Location: "City, ST (XX mi.)"
                    if (/^[A-Z][a-z]+.*,\s*[A-Z]{2}\s*\(/i.test(trimmed) && !location) {
                        location = trimmed;
                        continue;
                    }
                }

                // Dealer name has no distinct class anymore. It reliably sits
                // on the line right before the star-rating line (e.g. "4.8").
                // Only used as a fallback now - the card's JSON names the
                // seller outright, and this positional guess is exactly the
                // kind of thing that broke when Cars.com reshuffled its cards.
                const dealerMatch = card.querySelector('.dealer-name');
                let dealerName = dealerMatch ? dealerMatch.innerText.trim() : null;
                let dealerRating = null;
                for (let i = 0; i < lines.length; i++) {
                    if (/^\d+\.\d+$/.test(lines[i].trim())) {
                        dealerRating = lines[i].trim();
                        if (!dealerName && i > 0) {
                            const candidate = lines[i - 1].trim();
                            if (candidate && candidate !== dealRating && candidate !== location) {
                                dealerName = candidate;
                            }
                        }
                        break;
                    }
                }

                // Get URL from the card link
                const linkEl = card.querySelector('[data-card-href]');
                const href = linkEl ? linkEl.getAttribute('data-card-href') : null;

                // Everything below comes from the card's `data-vehicle-details`
                // JSON. None of it - not even the exterior color - is rendered
                // as visible card text, and the fields that ARE visible read
                // more reliably from here than from positional text parsing.
                let details = null;
                try {
                    details = JSON.parse(card.getAttribute('data-vehicle-details') || 'null');
                } catch { /* attribute missing or not JSON - fall back to text */ }

                const str = (v) => (typeof v === 'string' && v.trim()) ? v.trim() : null;
                const num = (v) => {
                    const n = Number(v);
                    return Number.isFinite(n) && n > 0 ? n : null;
                };

                let exteriorColor = str(details?.exteriorColor)
                    || card.querySelector('[data-exteriorcolor]')?.getAttribute('data-exteriorcolor')
                    || null;
                if (exteriorColor) {
                    exteriorColor = exteriorColor.trim().replace(/\b\w/g, c => c.toUpperCase());
                }

                // JSON gives raw numbers; the text gives them pre-formatted.
                // Prefer JSON and format here so both paths look the same.
                const jsonPrice = num(details?.price);
                if (jsonPrice) price = `$${jsonPrice.toLocaleString()}`;
                const jsonMileage = num(details?.mileage);
                if (jsonMileage) mileage = `${jsonMileage.toLocaleString()} mi.`;

                // MSRP is `"0"` on most used listings - only meaningful when set.
                const msrpValue = num(details?.msrp);
                const msrp = msrpValue ? `$${msrpValue.toLocaleString()}` : null;

                // Title from structured fields beats stripping prefixes off a
                // display string, but keep the text-parsed one as fallback.
                const jsonTitle = [details?.year, details?.make, details?.model, details?.trim]
                    .map(str).filter(Boolean).join(' ');
                if (jsonTitle) title = jsonTitle;

                if (str(details?.seller?.dealerName)) dealerName = str(details.seller.dealerName);

                // "Front-wheel Drive" is most of a line on its own.
                const DRIVETRAIN_ABBR = {
                    'front-wheel drive': 'FWD',
                    'rear-wheel drive': 'RWD',
                    'all-wheel drive': 'AWD',
                    'four-wheel drive': '4WD'
                };
                const drivetrainRaw = str(details?.drivetrain);
                const drivetrain = drivetrainRaw
                    ? (DRIVETRAIN_ABBR[drivetrainRaw.toLowerCase()] || drivetrainRaw)
                    : null;

                // Monthly payment has its own attribute, carrying a popover
                // payload; only the headline label is wanted.
                let monthlyPayment = null;
                try {
                    const mp = JSON.parse(card.querySelector('[data-monthly-payment]')?.getAttribute('data-monthly-payment') || 'null');
                    monthlyPayment = str(mp?.label);
                } catch { /* absent on some cards - fine */ }

                // A price drop renders as a bare dollar amount on the line
                // directly after the price, with no class or attribute of its
                // own ("$22,995" / "$290"). Position is the only signal, so
                // this is deliberately narrow: the very next line, nothing else.
                let priceDrop = null;
                const priceLineIndex = lines.findIndex(l => /^\$[\d,]+$/.test(l.trim()));
                if (priceLineIndex >= 0 && lines[priceLineIndex + 1]) {
                    const next = lines[priceLineIndex + 1].trim();
                    if (/^\$[\d,.]+K?$/i.test(next)) priceDrop = next;
                }

                // Award badges, matched against a bounded literal list rather
                // than a catch-all - a loose pattern would sweep up dealer
                // taglines and promo copy.
                const awards = lines
                    .map(l => l.trim())
                    .filter(l => /(american-made index|award finalist|award winner|best of|top safety pick|10best)/i.test(l));

                // Shipping/delivery, present on a minority of listings.
                const shipPrice = details?.shipPrice;
                const deliveryType = str(details?.deliveryType);
                let delivery = null;
                if (deliveryType || (typeof shipPrice === 'number')) {
                    const cost = typeof shipPrice === 'number'
                        ? (shipPrice > 0 ? `$${shipPrice.toLocaleString()}` : 'free')
                        : null;
                    delivery = [deliveryType, cost].filter(Boolean).join(' - ') || null;
                }

                // Check for CarFax badges
                const fullText = text.toLowerCase();
                const isOneOwner = fullText.includes('1-owner') || fullText.includes('one owner');
                const noAccidents = fullText.includes('no accident') || fullText.includes('clean');
                const personalUse = fullText.includes('personal use');
                const isCertified = details?.cpoIndicator === true
                    || details?.banners?.cpo === true
                    || fullText.includes('certified pre-owned');

                if (title) {
                    results.push({
                        title, price, priceDrop, msrp, monthlyPayment, mileage, dealRating,
                        dealerName, dealerRating, location, href, exteriorColor,
                        vin: str(details?.vin),
                        year: str(details?.year),
                        make: str(details?.make),
                        model: str(details?.model),
                        trim: str(details?.trim),
                        bodyStyle: str(details?.bodyStyle),
                        drivetrain,
                        fuelType: str(details?.fuelType),
                        isCertified,
                        delivery,
                        awards,
                        thumbnail: str(details?.primaryThumbnail),
                        isOneOwner, noAccidents, personalUse
                    });
                }
            });

                return results;
            });
        }

        for (const item of rawListings.slice(0, maxResults)) {
            listings.push(new CarListing({
                ...item,
                // `location` was previously extracted and then dropped here,
                // which is why every listing reported a null location.
                url: item.href ? `https://www.cars.com${item.href}` : null,
                source: 'Cars.com'
            }));
        }

        sendProgress?.(`Cars.com: found ${listings.length} listing(s)`);
        await browser.close();
    } catch (err) {
        if (browser) await browser.close();
        throw new Error(`Cars.com scraping failed: ${err.message}`);
    } finally {
        stopHeartbeat();
    }

    return listings;
}

/**
 * Scrape Autotrader for car listings
 */
async function scrapeAutotrader(params, maxResults = 20, sendProgress = null) {
    const listings = [];
    let browser;
    const stopHeartbeat = startHeartbeat(sendProgress, 'Searching Autotrader');

    try {
        browser = await launchBrowser();
        const page = await browser.newPage();
        await page.setViewport({ width: 1920, height: 1080 });

        // Build URL
        const make = params.make ? params.make.toLowerCase() : '';
        const model = params.model ? params.model.toLowerCase() : '';
        const zip = params.zip || '90210';

        let url = `https://www.autotrader.com/cars-for-sale/all-cars`;
        if (make) url += `/${make}`;
        if (model) url += `/${model}`;
        url += `/beverly-hills-ca-${zip}`;

        // Add query params
        const urlParams = new URLSearchParams();
        if (params.yearMin) urlParams.append('startYear', params.yearMin);
        if (params.yearMax) urlParams.append('endYear', params.yearMax);
        if (params.priceMax) urlParams.append('maxPrice', params.priceMax);
        if (params.mileageMax) urlParams.append('maxMileage', params.mileageMax);
        if (params.maxDistance) urlParams.append('searchRadius', params.maxDistance);

        if (urlParams.toString()) {
            url += '?' + urlParams.toString();
        }

        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await new Promise(r => setTimeout(r, 5000));

        // Extract listings
        const rawListings = await page.evaluate(() => {
            const results = [];

            // Autotrader uses various selectors for listings
            const cards = document.querySelectorAll('[data-cmp="inventoryListing"], .inventory-listing');

            cards.forEach(card => {
                const titleEl = card.querySelector('h2, .text-bold');
                const priceEl = card.querySelector('[data-cmp="firstPrice"], .first-price');
                const mileageEl = card.querySelector('.text-subdued-lighter');
                const dealerEl = card.querySelector('.dealer-name, .text-subdued');
                const linkEl = card.querySelector('a[href*="/cars-for-sale/"]');

                const title = titleEl ? titleEl.innerText.trim() : null;
                const price = priceEl ? priceEl.innerText.trim() : null;

                // Get mileage from text
                let mileage = null;
                if (mileageEl) {
                    const text = mileageEl.innerText;
                    const match = text.match(/([\d,]+)\s*miles?/i);
                    if (match) mileage = match[0];
                }

                if (title) {
                    results.push({
                        title,
                        price,
                        mileage,
                        dealerName: dealerEl ? dealerEl.innerText.trim() : null,
                        href: linkEl ? linkEl.getAttribute('href') : null
                    });
                }
            });

            return results;
        });

        for (const item of rawListings.slice(0, maxResults)) {
            listings.push(new CarListing({
                title: item.title,
                price: item.price,
                mileage: item.mileage,
                dealerName: item.dealerName,
                url: item.href ? `https://www.autotrader.com${item.href}` : null,
                source: 'Autotrader'
            }));
        }

        sendProgress?.(`Autotrader: found ${listings.length} listing(s)`);
        await browser.close();
    } catch (err) {
        if (browser) await browser.close();
        throw new Error(`Autotrader scraping failed: ${err.message}`);
    } finally {
        stopHeartbeat();
    }

    return listings;
}

/**
 * Scrape KBB for car listings
 */
async function scrapeKBB(params, maxResults = 20, sendProgress = null) {
    const listings = [];
    let browser;
    const stopHeartbeat = startHeartbeat(sendProgress, 'Searching KBB');

    try {
        browser = await launchBrowser();
        const page = await browser.newPage();
        await page.setViewport({ width: 1920, height: 1080 });

        // Build URL
        const make = params.make ? params.make.toLowerCase() : '';
        const model = params.model ? params.model.toLowerCase() : '';
        const zip = params.zip || '90210';

        let url = `https://www.kbb.com/cars-for-sale/all`;
        if (make) url += `/${make}`;
        if (model) url += `/${model}`;
        url += `/?zip=${zip}`;

        // Add filters
        if (params.yearMin) url += `&startYear=${params.yearMin}`;
        if (params.yearMax) url += `&endYear=${params.yearMax}`;
        if (params.priceMax) url += `&maxPrice=${params.priceMax}`;
        if (params.mileageMax) url += `&maxMileage=${params.mileageMax}`;
        if (params.maxDistance) url += `&searchRadius=${params.maxDistance}`;

        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await new Promise(r => setTimeout(r, 5000));

        // Extract listings - KBB uses inventoryListing data-cmp
        const rawListings = await page.evaluate(() => {
            const results = [];

            const cards = document.querySelectorAll('[data-cmp="inventoryListing"]');

            cards.forEach(card => {
                const text = card.innerText;
                if (!text || text.length < 20) return;

                const lines = text.split('\n').filter(l => l.trim());

                let title = null;
                let trim = null;
                let price = null;
                let mileage = null;
                let dealRating = null;

                for (const line of lines) {
                    const trimmed = line.trim();

                    // Title: Year Make Model
                    if (/^(19|20)\d{2}\s+\w+/.test(trimmed) && !title) {
                        title = trimmed;
                        continue;
                    }

                    // Trim (usually follows title, like "XSE" or "LE")
                    if (title && !trim && /^[A-Z]{1,4}$/.test(trimmed)) {
                        trim = trimmed;
                        continue;
                    }

                    // Price: "$XX,XXX" or just "XX,XXX" (KBB sometimes omits $)
                    const priceMatch = trimmed.match(/^\$?([\d,]+)$/);
                    if (priceMatch && !price && parseInt(priceMatch[1].replace(/,/g, '')) > 1000) {
                        price = trimmed.startsWith('$') ? trimmed : `$${trimmed}`;
                        continue;
                    }

                    // Mileage: "XXK mi" or "XX,XXX mi"
                    if (/^\d+K?\s*mi$/i.test(trimmed) && !mileage) {
                        mileage = trimmed;
                        continue;
                    }

                    // Deal rating: "Good Price", "Great Price", "Fair Price"
                    if (/^(good|great|fair|high)\s*(price|deal)/i.test(trimmed) && !dealRating) {
                        dealRating = trimmed;
                        continue;
                    }
                }

                if (title) {
                    if (trim) title = `${title} ${trim}`;
                    results.push({ title, price, mileage, dealRating });
                }
            });

            return results;
        });

        for (const item of rawListings.slice(0, maxResults)) {
            listings.push(new CarListing({
                title: item.title,
                price: item.price,
                mileage: item.mileage,
                dealRating: item.dealRating,
                source: 'KBB'
            }));
        }

        sendProgress?.(`KBB: found ${listings.length} listing(s)`);
        await browser.close();
    } catch (err) {
        if (browser) await browser.close();
        throw new Error(`KBB scraping failed: ${err.message}`);
    } finally {
        stopHeartbeat();
    }

    return listings;
}

/**
 * Hosts a detail page may be fetched from. The tool takes a caller-supplied
 * URL and drives a real browser at it, so the host is checked against this
 * list first - otherwise the tool is an open fetch proxy pointed at whatever
 * the caller names, including private network addresses.
 */
const DETAIL_HOSTS = {
    'cars.com': 'Cars.com',
    'autotrader.com': 'Autotrader',
    'kbb.com': 'KBB'
};

function resolveDetailSource(rawUrl) {
    let parsed;
    try {
        parsed = new URL(rawUrl);
    } catch {
        throw new Error(`Not a valid URL: ${rawUrl}`);
    }
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
        throw new Error(`Unsupported URL scheme: ${parsed.protocol}`);
    }
    const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
    const source = DETAIL_HOSTS[host];
    if (!source) {
        throw new Error(
            `Unsupported host "${parsed.hostname}". Listing detail pages must be on: ${Object.keys(DETAIL_HOSTS).join(', ')}`
        );
    }
    return { source, url: parsed.toString() };
}

/**
 * Convert a listing detail page's HTML to markdown.
 *
 * Deliberately not a field-by-field parser: these sites reshuffle their
 * markup often (see the Cars.com card breakage), and the interesting part of
 * a detail page - the options/features list, warranty text, seller notes - has
 * no stable shape worth encoding. Handing the caller a pruned markdown
 * rendering keeps every detail the page shows without a selector to maintain.
 */
function buildTurndown({ includeLinks, includeImages }) {
    const turndown = new TurndownService({
        headingStyle: 'atx',
        bulletListMarker: '-',
        codeBlockStyle: 'fenced'
    });

    // Definition lists carry the spec table (VIN, drivetrain, engine, MPG,
    // colors). Turndown has no default rule for them and would flatten each
    // term and value onto separate lines, losing the pairing.
    turndown.addRule('definitionList', {
        filter: ['dl'],
        replacement: (_content, node) => {
            const pairs = [];
            let term = null;
            // Turndown's Node implementation (domino) exposes `children` as a
            // non-iterable HTMLCollection - index it explicitly.
            const children = Array.prototype.slice.call(node.children || []);
            for (const child of children) {
                const text = (child.textContent || '').trim().replace(/\s+/g, ' ');
                if (child.nodeName === 'DT') {
                    term = text;
                } else if (child.nodeName === 'DD') {
                    if (term || text) pairs.push(`- **${term || 'Detail'}:** ${text}`);
                    term = null;
                }
            }
            return pairs.length ? `\n\n${pairs.join('\n')}\n\n` : '';
        }
    });

    if (!includeImages) {
        turndown.addRule('dropImages', { filter: ['img', 'picture'], replacement: () => '' });
    }
    if (!includeLinks) {
        // Keep the anchor text, drop the URL. A VDP links out dozens of times
        // (photo gallery, financing, every "similar vehicle"); inlining those
        // URLs costs more than it tells the caller.
        turndown.addRule('flattenLinks', {
            filter: ['a'],
            replacement: (content) => content
        });
    }

    return turndown;
}

const INTERSTITIAL_PATTERN = /performing security verification|verifying you are (not a bot|human)|just a moment|checking your browser|waiting for .* to respond/i;

/**
 * Block until an anti-bot interstitial hands off to the real page.
 *
 * The interstitial is short-lived (it says so: "Verification successful.
 * Waiting for www.cars.com to respond") but outlives the page-load settle, so
 * without this the extractor happily converts the challenge page to markdown
 * and returns it as the listing.
 */
async function waitOutInterstitial(page, sendProgress, timeoutMs = 45000) {
    const deadline = Date.now() + timeoutMs;
    let announced = false;

    while (Date.now() < deadline) {
        const isInterstitial = await page
            .evaluate((patternSource) => {
                const pattern = new RegExp(patternSource, 'i');
                const text = (document.body?.innerText || '').slice(0, 2000);
                // The real page is long and has a vehicle heading; the
                // challenge page is a few lines with no content behind it.
                return text.length < 1200 && pattern.test(text);
            }, INTERSTITIAL_PATTERN.source)
            .catch(() => false);

        if (!isInterstitial) return true;

        if (!announced) {
            sendProgress?.('Waiting out site bot check...');
            announced = true;
        }
        await new Promise(r => setTimeout(r, 2500));
    }

    return false;
}

/**
 * Fetch a single listing's detail page and return it as markdown.
 */
async function fetchListingDetails(rawUrl, options = {}, sendProgress = null) {
    const { source, url } = resolveDetailSource(rawUrl);
    const {
        includeLinks = false,
        includeImages = false,
        maxLength = 30000
    } = options;

    let browser;
    const stopHeartbeat = startHeartbeat(sendProgress, `Loading ${source} listing`);

    try {
        browser = await launchBrowser();
        const page = await browser.newPage();
        await page.setViewport({ width: 1920, height: 1080 });

        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForSelector('h1', { timeout: 15000 }).catch(() => {});
        // Detail pages lazy-render the spec and features sections after the
        // title paints; a short settle beats racing an unknown selector.
        await new Promise(r => setTimeout(r, 3000));

        // Unlike the search results page, detail pages routinely answer with a
        // bot-check interstitial first ("Performing security verification").
        // It clears itself and navigates on to the real page after a few
        // seconds, so poll rather than treating the interstitial as the page.
        await waitOutInterstitial(page, sendProgress);

        // The full options list usually sits behind a "see all features"
        // toggle. Best-effort: expanding it is the whole point of the tool,
        // but a missing or renamed button must not fail the fetch.
        const expanded = await page.evaluate(async () => {
            const pattern = /^(see|show|view)\s+(all|more)\b|^all features\b|^more details\b/i;
            const clickable = [...document.querySelectorAll('button, [role="button"], summary')];
            let count = 0;
            for (const el of clickable.slice(0, 40)) {
                const label = (el.innerText || el.textContent || '').trim();
                if (pattern.test(label)) {
                    try { el.click(); count++; } catch { /* detached or intercepted - skip */ }
                }
            }
            for (const details of document.querySelectorAll('details')) details.open = true;
            return count;
        }).catch(() => 0);
        if (expanded > 0) await new Promise(r => setTimeout(r, 1500));

        sendProgress?.(`Extracting details from ${source} listing...`);

        const extracted = await page.evaluate(() => {
            const STRIP_TAGS = 'script, style, noscript, template, svg, iframe, canvas, video, audio, form, input, select, textarea, nav, header, footer';
            const STRIP_PATTERN = /(^|[-_ ])(ad|ads|advert|banner|carousel|chat|cookie|consent|breadcrumb|disclaimer|footer|header|modal|dialog|nav|newsletter|popup|promo|recommend|related|similar|social|sponsor|subscribe|survey|toast|tooltip)([-_ ]|$)/i;

            const root =
                document.querySelector('main') ||
                document.querySelector('[role="main"]') ||
                document.body;
            const clone = root.cloneNode(true);

            clone.querySelectorAll(STRIP_TAGS).forEach(el => el.remove());
            clone.querySelectorAll('[aria-hidden="true"], [hidden]').forEach(el => el.remove());
            clone.querySelectorAll('[class], [id], [data-linkname]').forEach(el => {
                const tokens = `${el.getAttribute('class') || ''} ${el.getAttribute('id') || ''}`;
                if (STRIP_PATTERN.test(tokens)) el.remove();
            });

            return {
                title: (document.querySelector('h1')?.innerText || document.title || '').trim(),
                html: clone.innerHTML
            };
        });

        await browser.close();
        browser = null;

        const turndown = buildTurndown({ includeLinks, includeImages });
        let markdown = turndown.turndown(extracted.html)
            .replace(/[ \t]+$/gm, '')
            .replace(/\n{3,}/g, '\n\n')
            .trim();

        const truncated = markdown.length > maxLength;
        if (truncated) markdown = `${markdown.slice(0, maxLength)}\n\n*[truncated at ${maxLength} characters]*`;

        sendProgress?.(`${source}: extracted ${markdown.length} characters`);

        return { url, source, title: extracted.title, markdown, truncated };
    } catch (err) {
        throw new Error(`Failed to fetch listing details from ${source}: ${err.message}`);
    } finally {
        if (browser) await browser.close().catch(() => {});
        stopHeartbeat();
    }
}

/**
 * Search all sources and combine results
 */
async function searchAllSources(params, maxResultsPerSource = 10, sendProgress = null) {
    const results = {
        listings: [],
        errors: []
    };

    // Run scrapers in parallel
    const scrapers = [
        { name: 'Cars.com', fn: () => scrapeCarscom(params, maxResultsPerSource, sendProgress) },
        { name: 'Autotrader', fn: () => scrapeAutotrader(params, maxResultsPerSource, sendProgress) },
        { name: 'KBB', fn: () => scrapeKBB(params, maxResultsPerSource, sendProgress) }
    ];

    const promises = scrapers.map(async scraper => {
        try {
            const listings = await scraper.fn();
            return { name: scraper.name, listings, error: null };
        } catch (err) {
            return { name: scraper.name, listings: [], error: err.message };
        }
    });

    const outcomes = await Promise.all(promises);

    for (const outcome of outcomes) {
        results.listings.push(...outcome.listings);
        if (outcome.error) {
            results.errors.push({ source: outcome.name, error: outcome.error });
        }
    }

    return results;
}

module.exports = {
    CarListing,
    scrapeCarscom,
    scrapeAutotrader,
    scrapeKBB,
    searchAllSources,
    fetchListingDetails
};
