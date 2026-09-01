"""Demoville -- a synthetic market that ships with the engine.

Demoville does not exist. Its streets, firms and agents are invented, its
coordinates sit in empty farmland, and its fixtures are generated, not scraped.
That is the point: anyone can clone this repository and run the full pipeline
end to end with no API key, no network access, and no exposure to any listing
site's terms of service.

The fixture set is shaped to exercise the parts of the pipeline that are easy to
get wrong rather than to look realistic -- an over-cap parcel, a sale with no
size, an unattributed sale, a serial-cut listing, an overbid, a stale "active"
listing that has already closed, a dual-agency sale, a parcel on the divider,
and an address whose street name says East while its coordinates say west.

Two profiles ship: `demo-lots` (vacant land, the validated reference path) and
`demo-sfr` (single-family, experimental) so the profile layer can be shown
actually reshaping the output rather than relabelling it.
"""

from __future__ import annotations

from pathlib import Path

from recomps.config.profile import (
    Exclusions,
    SimilarBracket,
    Subject,
    improved_profile,
    vacant_land_profile,
)
from recomps.plugin.market import BaseMarket, MarketGeometry, SourceSpec, Waypoint

_HERE = Path(__file__).resolve().parent

#: A deliberately curved N/S divider. A market whose divider is a straight line
#: cannot demonstrate why the centerline exists.
_CENTERLINE = [
    Waypoint("Kestrel & Main", -100.06, 40.0180),
    Waypoint("Foundry & Main", -100.03, 40.0155),
    Waypoint("Center & Main", -100.00, 40.0140),
    Waypoint("Quarry & Main", -99.97, 40.0148),
    Waypoint("Pikestaff & Main", -99.94, 40.0152),
]


def _land_profile():
    profile = vacant_land_profile("demo-lots")
    profile.subject = Subject(lot_sqft=6450, label="Your lot")
    profile.similar_bracket = SimilarBracket(explicit_range=(5000.0, 8000.0))
    return profile


def _sfr_profile():
    profile = improved_profile("demo-sfr")
    profile.subject = Subject(living_sqft=1650, lot_sqft=6450, beds=3, label="Your house")
    # Improved types need their own bounds; these are illustrative only, which
    # is exactly why this profile ships marked experimental.
    profile.exclusions = Exclusions(max_lot_sqft=None, core_min_ppsf=200.0, core_max_ppsf=900.0)
    return profile


MARKET = BaseMarket(
    name="demoville",
    description="Demoville (synthetic demo market)",
    _profiles={"demo-lots": _land_profile(), "demo-sfr": _sfr_profile()},
    _default_profile="demo-lots",
    _geometry=MarketGeometry(
        ns_centerline=_CENTERLINE,
        ew_longitude=-100.0,
        ns_divider_name="Main St",
        ew_divider_name="Centre Ave",
    ),
    _sources=[
        SourceSpec(
            name="demo-aggregator",
            adapter="fixture",
            role="Sold comps and active listings",
            quirks=[
                (
                    "Publishes acreage rounded to two decimals, so small-parcel $/sqft "
                    "carries about +/-4% of error."
                ),
                (
                    "Renders only the first several results server-side; a fetched page "
                    "is not the whole page."
                ),
                "Lags closings by days, so some listings shown as active have sold.",
            ],
        ),
        SourceSpec(name="demo-geocoder", adapter="fixture", role="Coordinates for F11"),
    ],
    _fixture_dir=str(_HERE / "fixtures"),
    _data_dir=str(_HERE / "data"),
    _metadata={
        "synthetic": True,
        "note": "All data invented. No real property, person, or firm is represented.",
    },
)
