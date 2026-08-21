"""Shared typed structures for the scraper and server layers.

Keeps the scraper/server boundary explicit instead of passing untyped `dict`
arguments around: `SearchParams` is what every scraper takes, `ScrapeResult`
is what the search scrapers return (listings plus an optional did-you-mean
payload), and `ProgressSender` is the optional heartbeat callback the MCP
server threads through every scraper.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# A progress notification callback. Sync (`send_progress(message)`); the server
# wraps the async SDK call and schedules it fire-and-forget so scrapers can
# call this from sync code. `None` when there is no MCP request context, which
# keeps scrapers callable from ad-hoc scripts.
ProgressSender = Callable[[str], None]


@dataclass
class Config:
    """Startup configuration shared by the server and the scraper layer.

    `country` is the server default for `search_car_deals` - a per-call `country`
    argument still overrides it. `cloakbrowser_key` is read by `launch_browser`
    so the always-current Chromium build is used; None falls back to the free
    older build.
    """

    country: str = 'US'
    cloakbrowser_key: str | None = None


@dataclass
class CarListing:
    """Normalized car listing. `format()` is the sole definition of the
    user-visible search output shape - change output here, not in server.py.
    """

    source: str
    title: str | None = None
    price: str | None = None
    price_drop: str | None = None
    msrp: str | None = None
    monthly_payment: str | None = None
    mileage: str | None = None
    dealer_name: str | None = None
    dealer_rating: str | None = None
    location: str | None = None
    deal_rating: str | None = None
    exterior_color: str | None = None
    # Spec fields, all from the card's embedded JSON
    vin: str | None = None
    year: str | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    body_style: str | None = None
    drivetrain: str | None = None
    fuel_type: str | None = None
    is_certified: bool = False
    delivery: str | None = None
    awards: list[str] = field(default_factory=list)
    thumbnail: str | None = None
    url: str | None = None
    # CarFax badges
    is_one_owner: bool = False
    no_accidents: bool = False
    personal_use: bool = False
    # UK-only fields. Autotrader UK's GraphQL API returns transmission and
    # drivetrain as structured fields; Cinch's REST API returns those plus
    # `vrm`/`fullRegistration` (the number plate). Motors.co.uk exposes none
    # of these - the server skips it (with a warning) when transmission or
    # drivetrain filtering is requested. The plate lets a caller pipe a
    # Cinch result straight into `check_mot_history` without a second fetch.
    transmission: str | None = None
    registration: str | None = None

    def format(self) -> str:  # noqa: A003 - mirrors the JS method name
        result = self.title or 'Unknown Vehicle'

        if self.price:
            result += f'\n  Price: {self.price}'
            if self.price_drop:
                result += f' (dropped {self.price_drop})'
            if self.msrp:
                result += f' | MSRP: {self.msrp}'
        if self.monthly_payment:
            result += f'\n  Est. Payment: {self.monthly_payment}/mo'
        if self.mileage:
            result += f'\n  Mileage: {self.mileage}'
        if self.exterior_color:
            result += f'\n  Exterior Color: {self.exterior_color}'

        # One line for the spec fields - each is short, and on its own line
        # they would triple the height of every listing.
        specs = [s for s in (self.trim, self.body_style, self.drivetrain, self.fuel_type) if s]
        if specs:
            result += f'\n  Specs: {" | ".join(specs)}'

        if self.vin:
            result += f'\n  VIN: {self.vin}'
        if self.deal_rating:
            result += f'\n  Deal Rating: {self.deal_rating}'

        badges: list[str] = []
        if self.is_certified:
            badges.append('Certified Pre-Owned')
        if self.is_one_owner:
            badges.append('1-Owner')
        if self.no_accidents:
            badges.append('No Accidents')
        if self.personal_use:
            badges.append('Personal Use')
        if badges:
            result += f'\n  Badges: {" | ".join(badges)}'

        if self.awards:
            result += f'\n  Awards: {" | ".join(self.awards)}'

        if self.dealer_name:
            result += f'\n  Dealer: {self.dealer_name}'
            if self.dealer_rating:
                result += f' ({self.dealer_rating} stars)'
        if self.location:
            result += f'\n  Location: {self.location}'
        if self.delivery:
            result += f'\n  Delivery: {self.delivery}'
        # UK-only spec fields. Shown only when the source surfaces them; absent
        # on US cards and on Motors.co.uk, so format() never prints a bare
        # label for them. The plate is the one a buyer needs for an MOT check.
        if self.transmission:
            result += f'\n  Transmission: {self.transmission}'
        if self.drivetrain:
            result += f'\n  Drivetrain: {self.drivetrain}'
        if self.registration:
            result += f'\n  Registration: {self.registration}'
        if self.source:
            result += f'\n  Source: {self.source}'
        if self.thumbnail:
            result += f'\n  Photo: {self.thumbnail}'
        if self.url:
            result += f'\n  {self.url}'
        return result


@dataclass
class SearchParams:
    """Normalized search parameters. Built from the raw tool arguments; every
    scraper reads the same fields. `country` selects currency and (for the
    server) the default source set, but the scrapers themselves are country-
    agnostic - the server picks which scrapers to fan out to.
    """

    make: str
    model: str
    country: str = 'US'
    zip: str | None = None
    max_distance: int | None = None
    year_min: int | None = None
    year_max: int | None = None
    price_max: int | None = None
    mileage_max: int | None = None
    # CarFax history filters (US only; UK sources ignore these)
    one_owner: bool | None = None
    no_accidents: bool | None = None
    personal_use: bool | None = None
    # Drivetrain / transmission filters (UK only). Autotrader UK accepts both
    # as server-side URL parameters; Motors.co.uk and Cinch do not surface
    # either reliably, so the server skips those sources (with a warning)
    # rather than returning unfiltered results that look filtered.
    drivetrain: str | None = None
    transmission: str | None = None


@dataclass
class ModelSuggestion:
    """A single "did you mean" entry: a real model name on the site, with its
    inventory count, ranked by resemblance to the requested (invalid) name.
    """

    name: str
    count: str | None = None
    score: int = 0


@dataclass
class ModelSuggestions:
    """The did-you-mean payload a scraper attaches when it can prove the model
    name does not exist on that site (see `suggest_carscom_models`).
    """

    make: str
    input: str
    options: list[ModelSuggestion] = field(default_factory=list)
    source: str = 'Cars.com'


@dataclass
class ScrapeResult:
    """What a search scraper returns: the listings, plus an optional did-you-
    mean payload. Replaces the JS convention of hanging `modelSuggestions` off
    the returned array, which Python lists do not allow.
    """

    listings: list[CarListing] = field(default_factory=list)
    model_suggestions: ModelSuggestions | None = None


@dataclass
class DetailResult:
    """The `get_listing_details` tool's structured return."""

    url: str
    source: str
    title: str | None
    markdown: str
    truncated: bool


@dataclass
class MotRecord:
    """The `check_mot_history` tool's structured return."""

    registration: str
    found: bool
    url: str
    vehicle: dict[str, Any] | None = None
    mot_expiry: str | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)
    recalls: list[str] = field(default_factory=list)
    latest_test: dict[str, Any] | None = None
    outstanding_issues: dict[str, Any] | None = None
