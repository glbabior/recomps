"""End-to-end tests against the bundled synthetic market.

The acceptance criterion these encode: a clean clone with no API key and no
network can run the whole pipeline and get a valid workbook.
"""

from __future__ import annotations

from datetime import date

import pytest
from openpyxl import load_workbook

from recomps.config.profile import Denominator
from recomps.markets.demoville import MARKET
from recomps.model.snapshot import Snapshot, build_snapshot
from recomps.pipeline.run import run_pipeline
from recomps.reporting import compare as compare_mod
from recomps.reporting import methodology
from recomps.research.fixture import FixtureResearcher
from recomps.testing.conformance import check_market, check_market_runs
from recomps.workbook.builder import build_workbook
from recomps.workbook.schema import active_schema, sold_schema

AS_OF = date(2026, 8, 31)

#: Functions that do not exist in pre-2007 Excel, or that produce dynamic
#: arrays. The workbook must open cleanly in old Excel and in simple viewers.
FORBIDDEN_FUNCTIONS = ("XLOOKUP", "FILTER(", "SORT(", "UNIQUE(", "SEQUENCE(", "LET(", "LAMBDA(")


@pytest.fixture(scope="module")
def land_result():
    return run_pipeline(
        MARKET, MARKET.profiles()["demo-lots"], FixtureResearcher(), window_end=AS_OF
    )


@pytest.fixture(scope="module")
def sfr_result():
    return run_pipeline(
        MARKET, MARKET.profiles()["demo-sfr"], FixtureResearcher(), window_end=AS_OF
    )


def _formulas(ws):
    return [
        c.value for row in ws.iter_rows() for c in row
        if isinstance(c.value, str) and c.value.startswith("=")
    ]


# ---------------------------------------------------------------------------
# The offline run
# ---------------------------------------------------------------------------


def test_offline_run_completes_and_finds_comps(land_result):
    assert land_result.sold_stats.count > 15
    assert land_result.active_stats.count > 10
    assert land_result.researcher == "fixture"


def test_run_applies_the_fixture_edge_cases(land_result):
    reasons = " ".join(e.reason for e in land_result.excluded)
    assert "exceeds" in reasons, "the over-cap parcel should be excluded"
    assert land_result.reconciled, "the stale active listing should have been reconciled"
    assert land_result.missing_metric, "the sale with no size should be logged, not dropped"
    addresses = {c.address for c in land_result.sold}
    assert "417 Foundry St" in addresses, "a sale with no size stays in the dataset"


def test_naming_grid_trap_survives_a_full_run(land_result):
    """The fixture parcel labelled East is classified west, end to end."""
    trap = next(c for c in land_result.sold if c.address == "88 E Chandler St")
    assert trap.area.value.endswith("W")


def test_divider_parcel_is_noted(land_result):
    parcel = next(c for c in land_result.sold if c.address == "2000 Divider Row")
    assert any("Borderline" in n for n in parcel.notes)


def test_unattributed_sale_is_not_guessed(land_result):
    parcel = next(c for c in land_result.sold if c.address == "0 Bellweather Rd")
    assert parcel.agent is None and parcel.brokerage is None


def test_valuation_and_guidance_are_produced(land_result):
    primary = land_result.valuation.primary
    assert primary.value and primary.sample_size > 5
    assert len(land_result.guidance.strategies) == 3
    assert land_result.guidance.floor


def test_strategy_a_sits_just_under_a_search_band(land_result):
    compete = next(s for s in land_result.guidance.strategies if s.key == "compete")
    assert compete.list_price % 50_000 == 49_000


# ---------------------------------------------------------------------------
# The workbook
# ---------------------------------------------------------------------------


def test_workbook_has_the_five_sheets(land_result):
    assert build_workbook(land_result).sheetnames == [
        "Summary", "Pricing Guidance", "Sold Comps", "Active Listings", "Agents",
    ]


def test_workbook_recalculates_on_load(land_result):
    """Without this the file renders blank in every viewer that does not compute."""
    assert build_workbook(land_result).calculation.fullCalcOnLoad is True


def test_summary_stats_are_live_formulas_not_baked_numbers(land_result):
    ws = build_workbook(land_result)["Summary"]
    formulas = " ".join(_formulas(ws))
    for function in ("MEDIAN(", "AVERAGE(", "COUNTA(", "COUNTIF(", "MIN(", "MAX(", "SUMPRODUCT("):
        assert function in formulas, f"{function} missing from Summary"
    assert "'Sold Comps'!" in formulas and "'Active Listings'!" in formulas


def test_workbook_avoids_modern_excel_functions(land_result):
    wb = build_workbook(land_result)
    for ws in wb.worksheets:
        for formula in _formulas(ws):
            for forbidden in FORBIDDEN_FUNCTIONS:
                assert forbidden not in formula.upper(), f"{forbidden} in {ws.title}: {formula}"


def test_editing_a_row_would_move_the_valuation(land_result, tmp_path):
    """The valuation chain must be formulas the whole way down, not values.

    Walks it: each of the four estimates multiplies the editable subject-size
    cell by a rate cell, and every one of those rate cells is itself a formula
    over the comp sheets. If any link were a baked number, correcting a comp
    would leave the valuation stale.
    """
    import re

    path = tmp_path / "wb.xlsx"
    build_workbook(land_result).save(path)
    ws = load_workbook(path)["Summary"]

    estimates = [
        c.value for row in ws.iter_rows() for c in row
        if isinstance(c.value, str) and re.fullmatch(r"=[A-Z]+\d+\*\$B\$\d+", c.value)
    ]
    assert len(estimates) == 4, f"expected four valuation bases, got {estimates}"

    size_ref = estimates[0].split("*")[1].replace("$", "")
    assert isinstance(ws[size_ref].value, (int, float)), "subject size must be an editable value"

    for estimate in estimates:
        rate_ref = estimate[1:].split("*")[0]
        rate = ws[rate_ref].value
        assert isinstance(rate, str) and rate.startswith("="), (
            f"{estimate} multiplies {rate_ref}, which holds {rate!r} rather than a formula"
        )
        assert "'Sold Comps'!" in rate or "'Active Listings'!" in rate or "SUMPRODUCT" in rate


def test_subject_size_is_an_editable_input(land_result, tmp_path):
    path = tmp_path / "wb.xlsx"
    build_workbook(land_result).save(path)
    ws = load_workbook(path)["Summary"]
    editable = [
        c for row in ws.iter_rows() for c in row
        if c.fill is not None and c.fill.fgColor.rgb == "00FFF2A8"
    ]
    assert editable, "no cells are marked editable"
    assert any(c.value == land_result.profile.subject_size() for c in editable)


def test_comp_sheet_row_counts_match_the_dataset(land_result):
    wb = build_workbook(land_result)
    assert wb["Sold Comps"].max_row >= land_result.sold_stats.count + 1
    assert wb["Active Listings"].max_row >= land_result.active_stats.count + 1


def test_missing_values_render_as_a_dash_not_a_guess(land_result):
    wb = build_workbook(land_result)
    ws = wb["Sold Comps"]
    schema = sold_schema(land_result.profile)
    column = schema.index("agent")
    values = [ws.cell(row=r, column=column).value for r in range(2, ws.max_row + 1)]
    assert "—" in values


# ---------------------------------------------------------------------------
# The profile layer actually reshapes the pipeline
# ---------------------------------------------------------------------------


def test_sfr_profile_prices_on_living_area(sfr_result):
    profile = sfr_result.profile
    assert profile.metric is Denominator.LIVING_SQFT
    comp = sfr_result.sold[0]
    assert comp.price_per_sqft("living_sqft") == pytest.approx(
        comp.sold_price / comp.living_sqft
    )


def test_sfr_schema_inserts_attributes_and_keeps_lot_as_secondary():
    land = sold_schema(MARKET.profiles()["demo-lots"])
    sfr = sold_schema(MARKET.profiles()["demo-sfr"])
    assert not land.has("beds")
    for key in ("beds", "baths", "year_built", "lot_sqft"):
        assert sfr.has(key), f"{key} missing from the improved-property schema"
    assert sfr.columns[sfr.index("metric") - 1].header == "Living Area (sq ft)"
    assert sfr.columns[sfr.index("lot_sqft") - 1].header == "Lot (sq ft)"


def test_ppsf_formula_follows_the_columns_rather_than_a_fixed_letter():
    """The whole point of the schema layer, in one assertion."""
    land = sold_schema(MARKET.profiles()["demo-lots"])
    sfr = sold_schema(MARKET.profiles()["demo-sfr"])
    land_formula = land.render("={price}{row}/{metric}{row}", 2)
    sfr_formula = sfr.render("={price}{row}/{metric}{row}", 2)
    assert land_formula == "=C2/D2"
    assert sfr_formula == "=C2/D2"  # same here, but resolved, not hardcoded
    # The sold/ask formula does move, because attribute columns shift it.
    assert land.render("={price}{row}/{final_list}{row}", 2) != sfr.render(
        "={price}{row}/{final_list}{row}", 2
    )


def test_sfr_bracket_keys_on_living_area(sfr_result):
    profile = sfr_result.profile
    assert profile.similar_bracket.attribute is Denominator.LIVING_SQFT
    low, high = sfr_result.valuation.bracket_low, sfr_result.valuation.bracket_high
    for comp in sfr_result.sold:
        if comp.living_sqft and low <= comp.living_sqft <= high:
            continue
        assert not (low <= (comp.living_sqft or 0) <= high)


def test_sfr_workbook_builds_with_the_adapted_columns(sfr_result):
    wb = build_workbook(sfr_result)
    headers = [c.value for c in wb["Sold Comps"][1]]
    assert "Living Area (sq ft)" in headers
    assert "Beds" in headers and "Year Built" in headers
    assert "Lot (sq ft)" in headers
    assert headers.index("Living Area (sq ft)") < headers.index("Lot (sq ft)")


def test_sfr_run_is_marked_experimental(sfr_result):
    assert sfr_result.profile.is_experimental
    assert any("experimental" in w for w in sfr_result.warnings)


def test_active_schema_adapts_too():
    land = active_schema(MARKET.profiles()["demo-lots"])
    sfr = active_schema(MARKET.profiles()["demo-sfr"])
    assert len(sfr.columns) > len(land.columns)


# ---------------------------------------------------------------------------
# Snapshots, comparison, methodology
# ---------------------------------------------------------------------------


def test_snapshot_round_trips(land_result, tmp_path):
    path = build_snapshot(land_result).write(tmp_path / "snap.json")
    restored = Snapshot.load(path)
    assert restored.payload["stats"]["sold"]["count"] == land_result.sold_stats.count
    assert len(restored.payload["sold"]) == len(land_result.sold)


def test_snapshot_rejects_an_incompatible_schema(tmp_path):
    from recomps.model.snapshot import SnapshotIncompatible

    path = tmp_path / "old.json"
    path.write_text('{"schema_version": 0, "run": {}}', encoding="utf-8")
    with pytest.raises(SnapshotIncompatible):
        Snapshot.load(path)


def test_compare_detects_a_new_sale_and_a_price_cut(land_result):
    current = build_snapshot(land_result)
    prior = build_snapshot(land_result)
    prior.payload = {**prior.payload}
    prior.payload["sold"] = prior.payload["sold"][:-1]
    prior.payload["active"] = [dict(a) for a in prior.payload["active"]]
    prior.payload["active"][0]["list_price"] = (prior.payload["active"][0]["list_price"] or 0) * 2

    report = compare_mod.compare(prior, current)
    assert len(report.new_sales) == 1
    assert report.price_cuts, "a halved list price is a cut"
    assert any(d.label == "Primary valuation" for d in report.deltas)
    assert "new sales" in report.as_text()


def test_methodology_records_what_ran(land_result):
    text = methodology.render(land_result)
    assert "not an appraisal" in text
    assert "## Sources" in text and "## Results" in text
    assert "recomps run --market demoville" in text
    assert str(land_result.sold_stats.count) in text


def test_methodology_names_the_excluded_parcels(land_result):
    text = methodology.render(land_result)
    for record in land_result.excluded:
        assert record.address in text


# ---------------------------------------------------------------------------
# The plugin contract
# ---------------------------------------------------------------------------


def test_demoville_conforms():
    report = check_market(MARKET)
    assert not report.problems, report.summary()


def test_demoville_runs_under_the_conformance_kit():
    report = check_market_runs(MARKET, "demo-lots", as_of=AS_OF)
    assert not report.problems, report.summary()


def test_market_path_loading_round_trips(tmp_path):
    from recomps.plugin.loader import load_market_from_path

    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "sold.json").write_text("[]", encoding="utf-8")
    (tmp_path / "market.toml").write_text(
        """
[market]
name = "tinytown"
description = "A market loaded from a directory"
default_profile = "lots"

[profiles.lots]
property_type = "vacant_land"
metric = "lot_sqft"
window_days = 60

[profiles.lots.subject]
lot_sqft = 5000

[geometry]
ew_longitude = -100.0
ns_divider_name = "Main St"
ns_centerline = [
  { name = "west", lon = -100.05, lat = 40.01 },
  { name = "east", lon = -99.95, lat = 40.02 },
]

[[sources]]
name = "example"
adapter = "fixture"
""",
        encoding="utf-8",
    )
    market = load_market_from_path(tmp_path)
    assert market.name == "tinytown"
    assert market.profiles()["lots"].subject.lot_sqft == 5000
    assert market.geometry().is_configured()
    assert not check_market(market).problems


def test_invalid_market_directory_is_rejected(tmp_path):
    from recomps.plugin.loader import MarketInvalid, load_market_from_path

    (tmp_path / "market.toml").write_text("[market]\ndescription = 'no name'\n", encoding="utf-8")
    with pytest.raises((MarketInvalid, ValueError)):
        load_market_from_path(tmp_path)
