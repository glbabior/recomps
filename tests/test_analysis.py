"""Unit tests for the deterministic analysis.

These are the tests that would have caught the mistakes the original hand-run
process actually made.
"""

from __future__ import annotations

from datetime import date

import pytest

from lotcomps.config.profile import (
    Denominator,
    PropertyType,
    Subject,
    improved_profile,
    vacant_land_profile,
)
from lotcomps.model.address import AddressKey, normalize_address
from lotcomps.model.comp import ActiveListing, Quadrant, SoldComp, acres_to_sqft
from lotcomps.pipeline import quadrants as quad
from lotcomps.pipeline.agents import analyze as analyze_agents
from lotcomps.pipeline.agents import brokerage_family
from lotcomps.pipeline.exclusions import apply_filters, in_core_view
from lotcomps.pipeline.stats import sold_stats, sold_to_ask
from lotcomps.pipeline.valuation import similar_size_comps, value_subject
from lotcomps.plugin.market import MarketGeometry, Waypoint

# ---------------------------------------------------------------------------
# Address identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("0 N Ironwood Ave", "0 North Ironwood Avenue"),
        ("125 W Thistle St.", "125 W Thistle Street"),
        ("742 E Kestrel St, Demoville, ZZ 00000", "742 E Kestrel St"),
        ("2339 Foundry Ct", "2339 Foundry Court"),
        ("410 Bellweather Rd #2", "410 Bellweather Road Unit 2"),
        ("410 Bellweather Rd #2", "410 Bellweather Rd, Apt. 2"),
    ],
)
def test_same_parcel_normalizes_identically(left, right):
    assert AddressKey.of(left) == AddressKey.of(right)


def test_different_parcels_stay_distinct():
    assert AddressKey.of("128 W Meadowlark St") != AddressKey.of("128 E Meadowlark St")


def test_unit_designators_are_kept_so_condos_do_not_merge():
    """Stripping the unit would collapse every home in a building into one."""
    assert AddressKey.of("410 Bellweather Rd #2") != AddressKey.of("410 Bellweather Rd")
    assert AddressKey.of("410 Bellweather Rd #2") != AddressKey.of("410 Bellweather Rd #3")


def test_unnumbered_parcel_keeps_its_zero():
    assert normalize_address("0 Larkspur Vista Rd").startswith("0 ")


def test_acre_conversion():
    assert acres_to_sqft(0.15) == pytest.approx(6534.0)


# ---------------------------------------------------------------------------
# Quadrant classification (F11)
# ---------------------------------------------------------------------------

GEOMETRY = MarketGeometry(
    ns_centerline=[
        Waypoint("west", -118.15899, 34.19828),
        Waypoint("mid", -118.13129, 34.19027),
        Waypoint("east", -118.11244, 34.19066),
    ],
    ew_longitude=-118.13129,
    ns_divider_name="Divider Dr",
    ew_divider_name="Divider Ave",
)


def test_centerline_interpolates_rather_than_using_a_constant_latitude():
    west = quad.interpolate_divider_lat(GEOMETRY, -118.15899)
    east = quad.interpolate_divider_lat(GEOMETRY, -118.11244)
    middle = quad.interpolate_divider_lat(GEOMETRY, -118.145)
    assert west != east
    # A curving divider means the midpoint is not the average of the endpoints.
    assert west > middle > quad.interpolate_divider_lat(GEOMETRY, -118.13129)


def test_centerline_holds_endpoint_latitude_outside_its_span():
    far_west = quad.interpolate_divider_lat(GEOMETRY, -118.30)
    assert far_west == pytest.approx(34.19828)


def test_east_named_street_can_be_west_of_the_divider():
    """The naming-grid trap. This is the test that matters most in this file."""
    comp = SoldComp(address="88 E Chandler St", lat=34.1950, lon=-118.1450)
    result = quad.classify(comp, GEOMETRY)
    assert result.quadrant is Quadrant.NW, "classified from the name, not the coordinate"


def test_parcel_on_the_divider_is_flagged_borderline():
    lon = -118.140
    comp = SoldComp(address="1 Divider Dr", lat=quad.interpolate_divider_lat(GEOMETRY, lon), lon=lon)
    result = quad.classify(comp, GEOMETRY)
    assert result.borderline
    assert result.quadrant is Quadrant.NW  # ties resolve north
    assert "divider" in result.reason


def test_ungeocoded_parcel_is_unclassified_not_guessed():
    assert quad.classify(SoldComp(address="somewhere"), GEOMETRY).quadrant is None


def test_a_straight_line_divider_would_misclassify_an_edge_parcel():
    """Why the centerline is piecewise rather than one latitude."""
    lon = -118.11244
    parcel_lat = 34.19050  # just under the true divider here, above the west end's latitude
    comp = SoldComp(address="edge", lat=parcel_lat, lon=lon)
    assert quad.classify(comp, GEOMETRY).quadrant is Quadrant.SE
    naive_divider = GEOMETRY.ns_centerline[1].lat  # a single mid-market latitude
    assert parcel_lat > naive_divider, "a constant-latitude divider would have said North"


# ---------------------------------------------------------------------------
# Exclusions and the core view (F3)
# ---------------------------------------------------------------------------


def _land_profile(subject_sqft=6450.0):
    profile = vacant_land_profile()
    profile.subject = Subject(lot_sqft=subject_sqft)
    return profile


def test_over_cap_parcel_is_excluded_with_a_reason():
    profile = _land_profile()
    result = apply_filters(
        [SoldComp(address="big", lot_sqft=acres_to_sqft(3.9), sold_price=900000)], [], profile
    )
    assert result.sold == []
    assert "exceeds" in result.excluded[0].reason


def test_comp_without_a_size_is_kept_and_logged():
    profile = _land_profile()
    result = apply_filters([SoldComp(address="no size", sold_price=505000)], [], profile)
    assert len(result.sold) == 1, "must not be silently dropped"
    assert result.missing_metric == ["no size"]
    assert sold_stats(result.sold, profile).missing_metric_count == 1


def test_closed_listing_moves_off_the_active_side():
    profile = _land_profile()
    sold = [SoldComp(address="12 Foundry St", lot_sqft=6000, sold_price=500000)]
    active = [ActiveListing(address="12 Foundry Street", lot_sqft=6000, list_price=515000)]
    result = apply_filters(sold, active, profile)
    assert result.active == []
    assert result.reconciled == ["12 Foundry Street"]


def test_outliers_stay_in_the_dataset_and_only_leave_the_core_view():
    profile = _land_profile()
    comps = [
        SoldComp(address="cheap", lot_sqft=10000, sold_price=200000),   # $20/sqft
        SoldComp(address="normal", lot_sqft=6000, sold_price=480000),   # $80/sqft
        SoldComp(address="dear", lot_sqft=4000, sold_price=800000),     # $200/sqft
    ]
    stats = sold_stats(comps, profile)
    assert stats.count == 3, "nothing is removed from the dataset"
    assert stats.core_count == 1
    assert not in_core_view(20.0, profile) and in_core_view(80.0, profile)


# ---------------------------------------------------------------------------
# Bracket selection and valuation (F5)
# ---------------------------------------------------------------------------


def test_bracket_uses_explicit_bounds_when_the_market_pins_them():
    profile = _land_profile()
    profile.similar_bracket.explicit_range = (5000.0, 8000.0)
    comps = [SoldComp(address=str(s), lot_sqft=s, sold_price=s * 80) for s in
             (3920, 5000, 6450, 8000, 9148)]
    selected, low, high = similar_size_comps(comps, profile)
    assert (low, high) == (5000.0, 8000.0)
    assert [c.address for c in selected] == ["5000", "6450", "8000"]


def test_bracket_falls_back_to_a_tolerance_around_the_subject():
    profile = _land_profile(6000.0)
    profile.similar_bracket.tolerance = 0.25
    comps = [SoldComp(address=str(s), lot_sqft=s, sold_price=s * 80) for s in
             (4000, 4500, 6000, 7500, 8000)]
    selected, low, high = similar_size_comps(comps, profile)
    assert (low, high) == (4500.0, 7500.0)
    assert [c.address for c in selected] == ["4500", "6000", "7500"]


def test_valuation_multiplies_unrounded_rates():
    """Rounding $/sqft before multiplying shifts the answer by tens of dollars."""
    profile = _land_profile(6450.0)
    profile.similar_bracket.explicit_range = (5000.0, 8000.0)
    # Two comps averaging exactly 82.3344.../sqft.
    comps = [
        SoldComp(address="a", lot_sqft=6000, sold_price=6000 * 80.0),
        SoldComp(address="b", lot_sqft=7000, sold_price=7000 * 84.6688),
    ]
    valuation = value_subject(comps, [], profile)
    primary = valuation.primary
    assert primary.ppsf == pytest.approx(82.3344)
    assert primary.value == pytest.approx(6450 * 82.3344)
    assert primary.value != pytest.approx(6450 * round(82.3344, 2)), "rate was rounded first"


def test_all_four_bases_are_reported():
    profile = _land_profile()
    sold = [SoldComp(address="a", lot_sqft=6000, sold_price=480000)]
    active = [ActiveListing(address="b", lot_sqft=6000, list_price=540000)]
    keys = {b.key for b in value_subject(sold, active, profile).bases}
    assert keys == {"similar_size_avg", "all_sold_median", "all_sold_avg", "active_median"}


def test_thin_bracket_produces_a_warning():
    profile = _land_profile()
    sold = [SoldComp(address="a", lot_sqft=6000, sold_price=480000)]
    assert any("bracket" in w for w in value_subject(sold, [], profile).warnings)


# ---------------------------------------------------------------------------
# Sold-to-ask (F9)
# ---------------------------------------------------------------------------


def test_sold_to_ask_uses_the_final_ask_not_the_original():
    comp = SoldComp(
        address="cut twice", sold_price=600000,
        final_list_price=710000, original_list_price=899000,
    )
    assert comp.sold_to_ask() == pytest.approx(600000 / 710000)
    assert comp.was_reduced()


def test_sold_to_ask_distribution():
    comps = [
        SoldComp(address="over", sold_price=122, final_list_price=100),
        SoldComp(address="at", sold_price=100, final_list_price=100),
        SoldComp(address="under", sold_price=85, final_list_price=100),
        SoldComp(address="no ask", sold_price=500),
    ]
    stats = sold_to_ask(comps)
    assert stats.count == 3, "a sale with no ask contributes no ratio"
    assert stats.at_or_above_ask == 2
    assert stats.share_at_or_above_ask == pytest.approx(2 / 3)
    assert stats.at_or_above_105 == 1
    assert stats.below_90 == 1
    assert stats.max_ratio == pytest.approx(1.22)


# ---------------------------------------------------------------------------
# Agents and brokerages (F10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trading_name,family",
    [
        ("Berkshire Hathaway HomeServices", "Berkshire Hathaway HomeServices"),
        ("BHHS Golden Properties", "Berkshire Hathaway HomeServices"),
        ("KW Executive", "Keller Williams"),
        ("Keller Williams Realty", "Keller Williams"),
        ("eXp Realty of California Inc", "eXp Realty"),
        ("EXP Realty of California Inc", "eXp Realty"),
        ("Sotheby's International Realty", "Sotheby's International Realty"),
        ("Pacific Union Sotheby's", "Sotheby's International Realty"),
    ],
)
def test_brokerage_families_collapse_trading_names(trading_name, family):
    assert brokerage_family(trading_name) == family


def test_unknown_brokerage_keeps_its_own_name():
    assert brokerage_family("Federal Brokers") == "Federal Brokers"
    assert brokerage_family(None) is None


def test_agents_rank_by_closings_then_ratio():
    sold = [
        SoldComp(address="1", agent="Two Closer", brokerage="Compass",
                 sold_price=100, final_list_price=100),
        SoldComp(address="2", agent="Two Closer", brokerage="Compass",
                 sold_price=104, final_list_price=100),
        SoldComp(address="3", agent="One Closer", brokerage="Compass",
                 sold_price=122, final_list_price=100),
    ]
    analysis = analyze_agents(sold)
    assert [a.agent for a in analysis.agents] == ["Two Closer", "One Closer"]
    assert analysis.agents[0].flag == "shortlist"


def test_serial_cutter_is_flagged_caution():
    sold = [
        SoldComp(address="1", agent="Cutter", sold_price=512000,
                 final_list_price=599000, original_list_price=799000),
    ]
    assert analyze_agents(sold).agents[0].flag == "caution"


def test_dual_agency_is_flagged_and_raised_as_a_caveat():
    sold = [
        SoldComp(address="1", agent="Both Sides", buyer_agent="Both Sides",
                 sold_price=100, final_list_price=100),
    ]
    analysis = analyze_agents(sold)
    assert analysis.agents[0].dual_agency
    assert any("Dual agency" in c for c in analysis.caveats)


def test_unattributed_sales_are_counted_and_never_guessed():
    sold = [
        SoldComp(address="1", brokerage="Compass", agent="Someone"),
        SoldComp(address="2"),
    ]
    analysis = analyze_agents(sold)
    assert (analysis.attributed, analysis.total) == (1, 2)
    assert any("could not be attributed" in c for c in analysis.caveats)


# ---------------------------------------------------------------------------
# Profile validation (F0)
# ---------------------------------------------------------------------------


def test_land_profile_cannot_price_on_living_area():
    profile = vacant_land_profile()
    profile.subject = Subject(lot_sqft=6450)
    profile.metric = Denominator.LIVING_SQFT
    assert any("no living area" in p for p in profile.validate())


def test_improved_profile_cannot_price_on_lot_area():
    profile = improved_profile("sfr", PropertyType.SINGLE_FAMILY)
    profile.subject = Subject(lot_sqft=6450, living_sqft=1600)
    profile.metric = Denominator.LOT_SQFT
    assert any("living-area" in p for p in profile.validate())


def test_profile_without_a_subject_size_is_unusable():
    assert any("subject" in p for p in vacant_land_profile().validate())


def test_profile_round_trips_through_a_dict():
    from lotcomps.config.profile import CompProfile

    profile = improved_profile("sfr-3bd")
    profile.subject = Subject(living_sqft=1650, beds=3)
    restored = CompProfile.from_dict(profile.to_dict())
    assert restored.to_dict() == profile.to_dict()
    assert restored.metric is Denominator.LIVING_SQFT


def test_land_identification_is_the_absence_of_bed_bath():
    profile = vacant_land_profile()
    assert profile.identification.matches(has_bed_bath=False)
    assert not profile.identification.matches(has_bed_bath=True)
    assert profile.identification.matches(has_bed_bath=True, label="LOT/LAND")


# ---------------------------------------------------------------------------
# By-area table (F11)
# ---------------------------------------------------------------------------


def test_area_table_exposes_size_mix_that_average_rate_alone_hides():
    """Bigger parcels, higher prices, lower rate -- the inversion the table exists for."""
    profile = _land_profile()
    east = [
        SoldComp(address="e1", lot_sqft=10000, sold_price=750000, area=Quadrant.NE,
                 sold_date=date(2026, 7, 1)),
        SoldComp(address="e2", lot_sqft=10000, sold_price=760000, area=Quadrant.NE),
    ]
    west = [
        SoldComp(address="w1", lot_sqft=6000, sold_price=600000, area=Quadrant.NW),
        SoldComp(address="w2", lot_sqft=6000, sold_price=590000, area=Quadrant.NW),
    ]
    table = quad.build_area_table(east + west, [], profile, GEOMETRY)
    rows = {r.area: r for r in table.rows}
    assert rows["NE"].sold_median_price > rows["NW"].sold_median_price
    assert rows["NE"].sold_avg_ppsf < rows["NW"].sold_avg_ppsf
    assert rows["NE"].sold_avg_size > rows["NW"].sold_avg_size


def test_ungeocoded_parcels_are_reported_not_hidden():
    profile = _land_profile()
    table = quad.build_area_table(
        [SoldComp(address="nowhere", lot_sqft=6000, sold_price=480000)], [], profile, GEOMETRY
    )
    assert table.unclassified_sold == 1
    assert any("could not be geocoded" in n for n in table.notes)


# ---------------------------------------------------------------------------
# MLS lot-number suffixes (observed in real source data)
# ---------------------------------------------------------------------------


def test_a_lot_suffix_is_an_artifact_not_an_identity():
    """MLS appends a lot number to parcel addresses; the same sale appears
    both with and without it, sometimes as two rows on one page."""
    from lotcomps.config.profile import Identification

    land = Identification(unit_suffix_is_significant=False)
    bare = SoldComp(address="607 Larkspur St")
    suffixed = SoldComp(address="607 Larkspur St #34")
    assert bare.key_for(land) == suffixed.key_for(land)


def test_a_condo_unit_is_an_identity_not_an_artifact():
    from lotcomps.config.profile import Identification

    condo = Identification(unit_suffix_is_significant=True)
    two = SoldComp(address="410 Bellweather Rd #2")
    three = SoldComp(address="410 Bellweather Rd #3")
    assert two.key_for(condo) != three.key_for(condo)


def test_the_same_sale_listed_twice_is_counted_once():
    """The failure this prevents: every statistic shifts on a phantom sale."""
    profile = _land_profile()
    comps = [
        SoldComp(address="607 Larkspur St", lot_sqft=6534, sold_price=550000,
                 sold_date=date(2026, 7, 13)),
        SoldComp(address="607 Larkspur St #34", lot_sqft=6534, sold_price=550000,
                 sold_date=date(2026, 7, 13), brokerage="Cornerpost Real Estate"),
    ]
    result = apply_filters(comps, [], profile)
    assert len(result.sold) == 1
    assert result.duplicates == ["607 Larkspur St #34"]
    # The surviving row is the one carrying more information.
    assert result.sold[0].brokerage == "Cornerpost Real Estate"


def test_a_suffixed_listing_reconciles_against_a_bare_sale():
    """Attribution joins across sources fail if the suffix blocks the match."""
    profile = _land_profile()
    sold = [SoldComp(address="125 W Thistle St", lot_sqft=5227, sold_price=475000)]
    active = [ActiveListing(address="125 W Thistle St #18", lot_sqft=5227,
                            list_price=425000)]
    result = apply_filters(sold, active, profile)
    assert result.active == []
    assert result.reconciled == ["125 W Thistle St #18"]
