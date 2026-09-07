"""Unit tests for the deterministic analysis.

These are the tests that would have caught the mistakes the original hand-run
process actually made.
"""

from __future__ import annotations

from datetime import date

import pytest

from recomps.config.profile import (
    Denominator,
    PropertyType,
    Subject,
    improved_profile,
    vacant_land_profile,
)
from recomps.model.address import AddressKey, normalize_address
from recomps.model.comp import ActiveListing, Quadrant, SoldComp, acres_to_sqft
from recomps.pipeline import quadrants as quad
from recomps.pipeline.agents import analyze as analyze_agents
from recomps.pipeline.agents import brokerage_family
from recomps.pipeline.exclusions import apply_filters, in_core_view
from recomps.pipeline.ladder import build_bracket_ladder
from recomps.pipeline.size_bands import MIN_BAND, build_size_band_table
from recomps.pipeline.stats import sold_stats, sold_to_ask
from recomps.pipeline.valuation import similar_size_comps, value_subject
from recomps.plugin.market import MarketGeometry, Waypoint

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
    from recomps.config.profile import CompProfile

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
    from recomps.config.profile import Identification

    land = Identification(unit_suffix_is_significant=False)
    bare = SoldComp(address="607 Larkspur St")
    suffixed = SoldComp(address="607 Larkspur St #34")
    assert bare.key_for(land) == suffixed.key_for(land)


def test_a_condo_unit_is_an_identity_not_an_artifact():
    from recomps.config.profile import Identification

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


# ---------------------------------------------------------------------------
# The size/rate table (F12)
# ---------------------------------------------------------------------------
#
# The premise the primary valuation basis rests on -- smaller parcels carry a
# higher rate -- was acted on everywhere and shown nowhere. These pin the table
# that shows it.


def _land_profile_with_sizes():
    profile = vacant_land_profile("size-bands")
    profile.subject = Subject(lot_sqft=6000.0, label="Your lot")
    return profile


def _sold(address: str, lot: float, price: float) -> SoldComp:
    return SoldComp(address=address, lot_sqft=lot, sold_price=price, sold_date=date(2026, 7, 1))


def test_bands_hold_equal_counts_not_equal_widths():
    """Round edges put nineteen sales in one band and one in the next."""
    comps = [_sold(f"{i} Example St", 2000.0 + i * 200, 200_000.0) for i in range(20)]
    table = build_size_band_table(comps, _land_profile_with_sizes())
    counts = [r.count for r in table.rows]
    assert len(table.rows) == 5
    assert max(counts) - min(counts) <= 1, counts


def test_a_band_is_never_thinner_than_a_defensible_median():
    """Below three sales a median is an anecdote, so the band count steps down."""
    comps = [_sold(f"{i} Example St", 3000.0 + i * 500, 200_000.0) for i in range(7)]
    table = build_size_band_table(comps, _land_profile_with_sizes())
    assert all(r.count >= MIN_BAND for r in table.rows)
    assert len(table.rows) == 2


def test_a_market_too_thin_to_split_says_so_instead_of_inventing_bands():
    comps = [_sold(f"{i} Example St", 3000.0 + i * 500, 200_000.0) for i in range(4)]
    table = build_size_band_table(comps, _land_profile_with_sizes())
    assert len(table.rows) == 1
    assert any("too few" in n for n in table.notes)


def test_the_size_premium_is_measured_not_asserted():
    """Larger lots priced lower per sqft must show as a positive trend."""
    comps = [_sold(f"{i} Small St", 3000.0, 300_000.0) for i in range(5)]  # $100/sqft
    comps += [_sold(f"{i} Large St", 12_000.0, 600_000.0) for i in range(5)]  # $50/sqft
    table = build_size_band_table(comps, _land_profile_with_sizes())
    assert table.trend_pct == pytest.approx(100.0)
    assert any("size premium" in n for n in table.notes)


def test_a_market_without_the_premium_is_reported_as_such():
    """The premise the primary basis rests on does not always hold; say when."""
    comps = [_sold(f"{i} Small St", 3000.0, 150_000.0) for i in range(5)]  # $50/sqft
    comps += [_sold(f"{i} Large St", 12_000.0, 1_200_000.0) for i in range(5)]  # $100/sqft
    table = build_size_band_table(comps, _land_profile_with_sizes())
    assert table.trend_pct < 0
    assert any("does not hold" in n for n in table.notes)


def test_a_comp_without_a_size_is_counted_not_dropped():
    """'Not found' is a value: it carries no rate but it is still in the dataset."""
    comps = [_sold(f"{i} Example St", 3000.0 + i * 500, 200_000.0) for i in range(9)]
    comps.append(SoldComp(address="No Size Rd", sold_price=200_000.0, sold_date=date(2026, 7, 1)))
    table = build_size_band_table(comps, _land_profile_with_sizes())
    assert table.without_size == 1
    assert sum(r.count for r in table.rows) == 9
    assert any("no size" in n for n in table.notes)


def test_the_subject_s_own_band_is_marked():
    comps = [_sold(f"{i} Example St", 2000.0 + i * 1000, 200_000.0) for i in range(12)]
    table = build_size_band_table(comps, _land_profile_with_sizes())
    marked = [r for r in table.rows if r.holds_subject]
    assert len(marked) == 1
    assert marked[0].low <= 6000.0 <= marked[0].high


def test_every_sale_with_a_size_lands_in_exactly_one_band():
    """Band edges must not double-count a comp sitting exactly on one."""
    comps = [_sold(f"{i} Example St", 5000.0, 400_000.0) for i in range(6)]
    comps += [_sold(f"{i} Other St", 9000.0, 500_000.0) for i in range(6)]
    table = build_size_band_table(comps, _land_profile_with_sizes())
    assert sum(r.count for r in table.rows) == 12


def test_the_table_does_not_change_what_the_dataset_contains():
    """It reports; it must never be mistaken for another filter."""
    profile = _land_profile_with_sizes()
    comps = [_sold(f"{i} Example St", 2000.0 + i * 900, 200_000.0) for i in range(15)]
    before = [c.address for c in comps]
    build_size_band_table(comps, profile)
    assert [c.address for c in comps] == before


# ---------------------------------------------------------------------------
# One address, two parcels
# ---------------------------------------------------------------------------
#
# Dedup exists because a source lists one sale twice, bare and with an MLS lot
# suffix. But two genuinely different parcels can share a street address, and
# collapsing those deletes a real sale from every statistic without a trace.


def _land_profile_no_limits():
    profile = vacant_land_profile("dedup")
    profile.subject = Subject(lot_sqft=6000.0, label="Your lot")
    profile.exclusions.max_lot_sqft = None
    profile.exclusions.explicit_address_keys = []
    return profile


def test_two_distinct_parcels_at_one_address_are_both_kept():
    """Observed live: a 3.9-acre and a 5.14-acre parcel at one address, priced
    two hundred thousand apart."""
    profile = _land_profile_no_limits()
    result = apply_filters(
        [
            SoldComp(address="1 Example Rd", lot_sqft=169_884.0, sold_price=1_095_000.0,
                     sold_date=date(2026, 7, 1)),
            SoldComp(address="1 Example Rd", lot_sqft=223_898.0, sold_price=895_000.0,
                     sold_date=date(2026, 7, 2)),
        ],
        [], profile,
    )
    assert len(result.sold) == 2
    assert not result.duplicates
    assert result.distinct_at_one_address, "sharing an address must be reported, not silent"


def test_the_mls_lot_suffix_duplicate_still_collapses():
    """The case dedup was written for: one sale listed twice, a dollar apart."""
    profile = _land_profile_no_limits()
    result = apply_filters(
        [
            SoldComp(address="12 Example St", lot_sqft=6000.0, sold_price=500_000.0,
                     sold_date=date(2026, 7, 1)),
            SoldComp(address="12 Example St #18", lot_sqft=6000.0, sold_price=500_001.0,
                     sold_date=date(2026, 7, 3), agent="A. Agent"),
        ],
        [], profile,
    )
    assert len(result.sold) == 1
    assert result.duplicates
    assert result.sold[0].agent == "A. Agent", "the richer row must survive"
    assert not result.distinct_at_one_address


def test_rows_that_cannot_be_compared_collapse_as_before():
    """No price on either row means no evidence they differ; keep the old
    behaviour rather than inventing two sales."""
    profile = _land_profile_no_limits()
    result = apply_filters(
        [
            SoldComp(address="9 Example St", lot_sqft=6000.0, sold_date=date(2026, 7, 1)),
            SoldComp(address="9 Example St", lot_sqft=6000.0, sold_date=date(2026, 7, 1)),
        ],
        [], profile,
    )
    assert len(result.sold) == 1


def test_a_differing_size_alone_separates_two_parcels():
    """Price can be missing; a materially different size still means two lots."""
    profile = _land_profile_no_limits()
    result = apply_filters(
        [
            SoldComp(address="7 Example St", lot_sqft=5000.0, sold_date=date(2026, 7, 1)),
            SoldComp(address="7 Example St", lot_sqft=20_000.0, sold_date=date(2026, 7, 1)),
        ],
        [], profile,
    )
    assert len(result.sold) == 2


def test_the_finding_reaches_the_run_s_caveats():
    from recomps.pipeline.run import analyze
    from recomps.research.base import Dataset

    profile = _land_profile_no_limits()
    dataset = Dataset(sold=[
        SoldComp(address="1 Example Rd", lot_sqft=169_884.0, sold_price=1_095_000.0,
                 sold_date=date(2026, 7, 1)),
        SoldComp(address="1 Example Rd", lot_sqft=223_898.0, sold_price=895_000.0,
                 sold_date=date(2026, 7, 2)),
    ])
    from recomps.markets.demoville import MARKET

    run = analyze(dataset, MARKET, profile, date(2026, 4, 1), date(2026, 7, 31))
    assert any("more than one parcel" in c for c in run.caveats)


# ---------------------------------------------------------------------------
# Who is working the market now, not only who worked it before
# ---------------------------------------------------------------------------


def _sold_by(agent: str, brokerage: str) -> SoldComp:
    return SoldComp(
        address=f"{agent} House", lot_sqft=6000.0, sold_price=500_000.0,
        sold_date=date(2026, 7, 1), agent=agent, brokerage=brokerage,
    )


def _listed_by(agent: str | None, brokerage: str) -> ActiveListing:
    return ActiveListing(
        address=f"{agent or brokerage} Lot", lot_sqft=6000.0, list_price=520_000.0,
        agent=agent, brokerage=brokerage,
    )


def test_an_agents_live_listings_are_counted_beside_their_closings():
    analysis = analyze_agents(
        [_sold_by("A. Agent", "Compass")],
        active=[_listed_by("A. Agent", "Compass"), _listed_by("A. Agent", "Compass")],
    )
    row = next(a for a in analysis.agents if a.agent == "A. Agent")
    assert row.closings == 1
    assert row.active_listings == 2


def test_an_agent_with_listings_but_no_closings_still_appears():
    """They are working this market now, which is the question a seller asks."""
    analysis = analyze_agents(
        [_sold_by("A. Agent", "Compass")], active=[_listed_by("B. Newcomer", "Compass")]
    )
    names = {a.agent for a in analysis.agents}
    assert "B. Newcomer" in names
    newcomer = next(a for a in analysis.agents if a.agent == "B. Newcomer")
    assert newcomer.closings == 0
    assert newcomer.active_listings == 1
    assert newcomer.flag == "", "no closings cannot earn a shortlist"


def test_brokerages_carry_their_live_listing_count():
    analysis = analyze_agents(
        [_sold_by("A. Agent", "Compass")],
        active=[_listed_by(None, "Coldwell Banker Realty"),
                _listed_by(None, "Coldwell Banker Realty")],
    )
    families = {b.family: b for b in analysis.brokerages}
    assert families["Coldwell Banker"].active_listings == 2
    assert families["Coldwell Banker"].closings == 0
    assert families["Compass"].closings == 1


def test_a_listing_with_no_named_agent_still_counts_for_its_brokerage():
    """Most listings on one observed index name the firm and nobody else."""
    analysis = analyze_agents([], active=[_listed_by(None, "COMPASS")])
    assert not analysis.agents
    assert next(b for b in analysis.brokerages if b.family == "Compass").active_listings == 1


def test_no_actives_leaves_every_count_at_zero():
    analysis = analyze_agents([_sold_by("A. Agent", "Compass")])
    assert all(a.active_listings == 0 for a in analysis.agents)
    assert all(b.active_listings == 0 for b in analysis.brokerages)


# ---------------------------------------------------------------------------
# The bracket ladder (F13)
# ---------------------------------------------------------------------------
#
# Every valuation rests on a bandwidth choice nobody was asked to make. On one
# real run the estimate moved 14% between a tight reading of "similar" and the
# pinned one.


def _ladder_profile(size=6000.0, tolerance=0.25, explicit=None):
    profile = vacant_land_profile("ladder")
    profile.subject = Subject(lot_sqft=size, label="Your lot")
    profile.similar_bracket.tolerance = tolerance
    profile.similar_bracket.explicit_range = explicit
    return profile


def _spread_of_sales() -> list[SoldComp]:
    # Small lots dear, large lots cheap -- the premise the bracket corrects for.
    out = []
    for i in range(10):
        out.append(SoldComp(address=f"{i} Small St", lot_sqft=5500.0 + i * 50,
                            sold_price=550_000.0, sold_date=date(2026, 7, 1)))
    for i in range(10):
        out.append(SoldComp(address=f"{i} Large St", lot_sqft=15_000.0 + i * 100,
                            sold_price=750_000.0, sold_date=date(2026, 7, 1)))
    return out


def test_the_widest_rung_is_exactly_the_all_sold_basis():
    """The ladder has to reconcile with the figures reported elsewhere, or it
    is a third convention pretending to be evidence."""
    profile = _ladder_profile()
    sold = _spread_of_sales()
    ladder = build_bracket_ladder(sold, profile)
    valuation = value_subject(sold, [], profile)

    widest = ladder.rows[-1]
    assert widest.is_all_sold
    assert widest.count == len(sold)
    assert widest.value_from_median == pytest.approx(
        valuation.by_key("all_sold_median").value
    )
    assert widest.value_from_avg == pytest.approx(
        valuation.by_key("all_sold_avg").value
    )


def test_the_marked_rung_is_the_one_the_headline_uses():
    profile = _ladder_profile(explicit=(5000.0, 8000.0))
    sold = _spread_of_sales()
    ladder = build_bracket_ladder(sold, profile)
    valuation = value_subject(sold, [], profile)

    marked = [r for r in ladder.rows if r.is_profile_bracket]
    assert len(marked) == 1
    assert marked[0].value_from_avg == pytest.approx(valuation.primary.value)


def test_rungs_widen_monotonically_and_never_shrink_the_sample():
    profile = _ladder_profile()
    ladder = build_bracket_ladder(_spread_of_sales(), profile)
    counts = [r.count for r in ladder.rows]
    assert counts == sorted(counts), counts


def test_a_pinned_bracket_does_not_duplicate_a_coinciding_rung():
    """A +/-25% tolerance is the same rung as the ladder's own; one row, marked."""
    profile = _ladder_profile(tolerance=0.25, explicit=None)
    ladder = build_bracket_ladder(_spread_of_sales(), profile)
    widths = [(r.low, r.high) for r in ladder.rows if not r.is_all_sold]
    assert len(widths) == len(set(widths)), widths


def test_a_thin_rung_is_marked_rather_than_hidden():
    """Watching the sample grow is the point; a two-sale median still has to
    say it is a two-sale median."""
    profile = _ladder_profile()
    sold = [
        SoldComp(address="1 Only St", lot_sqft=6000.0, sold_price=500_000.0,
                 sold_date=date(2026, 7, 1)),
        SoldComp(address="2 Far St", lot_sqft=30_000.0, sold_price=900_000.0,
                 sold_date=date(2026, 7, 1)),
    ]
    ladder = build_bracket_ladder(sold, profile)
    assert ladder.rows[0].is_thin
    assert ladder.rows[0].count == 1


def test_the_spread_is_measured_and_stated_in_money():
    profile = _ladder_profile()
    ladder = build_bracket_ladder(_spread_of_sales(), profile)
    assert ladder.spread_pct is not None and ladder.spread_pct >= 0
    assert any("cost of the word" in n for n in ladder.notes)


def test_no_subject_size_means_no_ladder_rather_than_a_wrong_one():
    profile = _ladder_profile()
    profile.subject.lot_sqft = None
    ladder = build_bracket_ladder(_spread_of_sales(), profile)
    assert not ladder.rows
    assert ladder.notes


def test_rates_are_not_rounded_before_they_become_a_value():
    profile = _ladder_profile()
    ladder = build_bracket_ladder(_spread_of_sales(), profile)
    rung = next(r for r in ladder.rows if r.count and r.avg_ppsf)
    assert rung.value_from_avg == pytest.approx(rung.avg_ppsf * 6000.0)
    assert rung.value_from_avg != round(rung.avg_ppsf, 2) * 6000.0


def test_strategy_a_states_what_the_band_mechanic_costs_this_run():
    """A list price 8% under the estimate looks like a mistake unless the
    output says the gap is set by the band, not by the market."""
    from recomps.pipeline.guidance import build_guidance
    from recomps.pipeline.stats import SoldToAskStats
    from recomps.pipeline.valuation import Valuation, ValuationBasis

    def guidance_for(anchor: float):
        valuation = Valuation(
            subject_size=6000.0, bracket_low=5000.0, bracket_high=8000.0,
            bracket_count=5,
            bases=[
                ValuationBasis(key="similar_size_avg", label="x", ppsf=1.0,
                               value=anchor, sample_size=5, is_primary=True),
                ValuationBasis(key="all_sold_median", label="y", ppsf=1.0,
                               value=anchor * 0.93, sample_size=30),
            ],
        )
        return build_guidance(valuation, SoldToAskStats(), [])

    a = next(s for s in guidance_for(544_720).strategies if s.key == "compete")
    assert a.list_price == 499_000
    assert "$45,720" in a.tradeoff and "8.4%" in a.tradeoff
    assert "not by the market" in a.tradeoff


def test_the_band_mechanic_is_a_cliff_and_the_text_admits_it():
    """$549,999 lists at $499,000; $551,000 lists at $549,000. A thousand
    dollars of estimate moves the recommendation by a whole band."""
    from recomps.pipeline.guidance import build_guidance
    from recomps.pipeline.stats import SoldToAskStats
    from recomps.pipeline.valuation import Valuation, ValuationBasis

    def compete(anchor: float) -> float:
        valuation = Valuation(
            subject_size=6000.0, bracket_low=5000.0, bracket_high=8000.0,
            bracket_count=5,
            bases=[
                ValuationBasis(key="similar_size_avg", label="x", ppsf=1.0,
                               value=anchor, sample_size=5, is_primary=True),
            ],
        )
        strategies = build_guidance(valuation, SoldToAskStats(), []).strategies
        return next(s for s in strategies if s.key == "compete").list_price

    assert compete(549_999) == 499_000
    assert compete(551_000) == 549_000


def test_a_recording_replayed_through_a_later_window_says_what_it_lost(tmp_path):
    """A window slides with the run date; a recording does not. Replayed months
    later most of it silently disappears and every figure describes a smaller
    market that reads exactly like a real one."""
    import json

    from recomps.markets.demoville import MARKET
    from recomps.research.fixture import FixtureResearcher

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    rows = [
        {"address": f"{i} Example St", "lot_sqft": 6000, "sold_price": 500_000,
         "sold_date": f"2026-05-{i + 1:02d}"}
        for i in range(10)
    ] + [
        {"address": f"{i} Later St", "lot_sqft": 6000, "sold_price": 500_000,
         "sold_date": f"2026-08-{i + 1:02d}"}
        for i in range(5)
    ]
    (fixtures / "sold.json").write_text(json.dumps(rows), encoding="utf-8")

    researcher = FixtureResearcher(str(fixtures))
    dataset = researcher.gather(
        MARKET, MARKET.profiles()["demo-lots"], date(2026, 7, 1), date(2026, 9, 30)
    )
    assert len(dataset.sold) == 5
    note = " ".join(dataset.diagnostics.not_found)
    assert "10 of its 15 recorded sales" in note
    assert "2026-05-01 to 2026-08-05" in note
    # The last recorded sale is explained, not dropped in as a bare date.
    assert "is the last sale in it, not a setting" in note
    assert "--as-of" not in note, "a command-line flag is meaningless on a screen"
