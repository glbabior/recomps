"""The interface, driven without a browser.

Streamlit's own test harness runs the app script and lets the test click
things, so the interface is covered by the same suite as everything else
rather than being the one part nobody checks until it breaks in front of a
user.

What these assert is behaviour, not layout: that a saved run opens without
recomputing, that changing something recomputes and says so, and that the
provenance line always tells the reader which of those they are looking at.
Colour and column order are free to change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from recomps.markets.demoville import MARKET
from recomps.model.snapshot import build_snapshot
from recomps.pipeline.run import run_pipeline
from recomps.reporting import history as history_mod
from recomps.research.fixture import FixtureResearcher
from recomps.ui import state as ui_state
from recomps.workbook.builder import build_workbook

pytest.importorskip("streamlit", reason="the interface is an optional extra")

APP = Path(__file__).resolve().parents[1] / "src" / "recomps" / "ui" / "app.py"
AS_OF = date(2026, 8, 31)


@pytest.fixture(scope="module")
def result():
    return run_pipeline(
        MARKET, MARKET.profiles()["demo-lots"], FixtureResearcher(), window_end=AS_OF
    )


@pytest.fixture
def archived(tmp_path, result):
    """A saved run on disk, as the interface would find it."""
    when = datetime(2026, 8, 31, tzinfo=UTC)
    directory = history_mod.run_dir(tmp_path, "demoville", "demo-lots", when)
    directory.mkdir(parents=True)
    build_snapshot(result).write(directory / history_mod.SNAPSHOT_NAME)
    build_workbook(result).save(directory / history_mod.WORKBOOK_NAME)
    return history_mod.list_runs(tmp_path)[0]


# ---------------------------------------------------------------------------
# The app runs at all
# ---------------------------------------------------------------------------


def test_the_app_starts_without_error():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not app.exception, [str(e) for e in app.exception]


def test_it_opens_on_a_market_picker_rather_than_a_blank_page():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=60).run()
    assert app.sidebar.selectbox, "there should be a market chooser"
    picker = app.sidebar.selectbox[0]
    # The harness reports what a user would see, which is the description.
    assert any("Demoville" in str(o) for o in picker.options)
    assert any("folder" in str(o) for o in picker.options), (
        "a private market lives in a folder, so that route must be offered"
    )
    assert picker.value == "demoville", "the bundled market should be ready to try"


def test_choosing_a_market_offers_its_saved_searches():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=60).run()
    app.sidebar.selectbox[0].set_value("demoville").run()
    assert not app.exception, [str(e) for e in app.exception]
    profile_picker = app.sidebar.selectbox[1]
    assert "demo-lots" in profile_picker.options
    assert "demo-sfr" in profile_picker.options


def test_the_experimental_profile_is_labelled_as_such():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=60).run()
    app.sidebar.selectbox[0].set_value("demoville").run()
    app.sidebar.selectbox[1].set_value("demo-sfr").run()
    warnings = " ".join(w.value for w in app.sidebar.warning)
    assert "provisional" in warnings or "experimental" in warnings.lower()


# ---------------------------------------------------------------------------
# Viewing versus recomputing -- the distinction the interface exists to keep
# ---------------------------------------------------------------------------


def test_opening_a_saved_run_recomputes_nothing(archived, result):
    view = ui_state.Viewing(origin="saved", record=archived)
    view.stored = ui_state.open_saved(archived)

    figures = ui_state.headline(view)
    assert figures["sold_count"] == result.sold_stats.count
    assert figures["valuation"] == result.valuation.primary.value
    assert view.result is None, "viewing must not build a fresh result"


def test_the_provenance_line_says_which_numbers_these_are(archived):
    view = ui_state.Viewing(origin="saved", record=archived)
    view.stored = ui_state.open_saved(archived)
    assert "exactly as it was reported" in view.provenance
    assert "Nothing recalculated" in view.provenance

    view.adjustments.subject_size = 9000.0
    assert "re-analyzed with your changes" in view.provenance
    assert "sales themselves are unchanged" in view.provenance


def test_a_fresh_run_says_so():
    assert ui_state.Viewing(origin="fresh").provenance == "Fresh run."


def test_changing_the_subject_size_moves_the_valuation(archived, result):
    view = ui_state.Viewing(origin="saved", record=archived)
    view.adjustments.subject_size = 9000.0
    view.result = ui_state.recompute(archived, MARKET, view.adjustments)

    figures = ui_state.headline(view)
    assert figures["valuation"] > result.valuation.primary.value
    assert view.is_adjusted


def test_excluding_a_comp_drops_it_and_nothing_else(archived, result):
    victim = result.sold[0].address
    view = ui_state.Viewing(origin="saved", record=archived)
    view.adjustments.excluded = {victim}
    view.result = ui_state.recompute(archived, MARKET, view.adjustments)

    addresses = {c.address for c in view.result.sold}
    assert victim not in addresses
    assert len(addresses) == result.sold_stats.count - 1


def test_clearing_the_changes_returns_to_the_reported_figures(archived, result):
    view = ui_state.Viewing(origin="saved", record=archived)
    view.stored = ui_state.open_saved(archived)
    view.adjustments.subject_size = 9000.0
    view.result = ui_state.recompute(archived, MARKET, view.adjustments)
    assert view.is_adjusted

    view.adjustments.clear()
    view.result = None
    assert not view.is_adjusted
    assert ui_state.headline(view)["valuation"] == result.valuation.primary.value


def test_the_comps_table_reads_the_same_either_way(archived, result):
    saved = ui_state.Viewing(origin="saved", record=archived)
    saved.stored = ui_state.open_saved(archived)
    fresh = ui_state.Viewing(origin="fresh", result=result)

    from_saved = ui_state.comp_rows(saved)
    from_fresh = ui_state.comp_rows(fresh)
    assert len(from_saved) == len(from_fresh)
    assert [r["address"] for r in from_saved] == [r["address"] for r in from_fresh]
    assert from_saved[0]["sold_date"] == from_fresh[0]["sold_date"]


def test_the_adjustment_summary_is_readable():
    adjustments = ui_state.Adjustments(excluded={"1 A St"}, subject_size=7000.0)
    text = adjustments.describe()
    assert "1 comp(s) excluded" in text
    assert "7,000" in text


def test_no_adjustments_means_nothing_to_describe():
    assert ui_state.Adjustments().describe() == ""
    assert not ui_state.Adjustments().any


# ---------------------------------------------------------------------------
# What a bare `recomps` does
# ---------------------------------------------------------------------------


def test_bare_command_opens_the_interface_for_a_person(monkeypatch):
    """This is a tool people use by looking at it, so a bare invocation in a
    terminal should show them something rather than a menu."""
    from click.testing import CliRunner

    from recomps import cli

    launched = {}

    def fake_run(command, **kwargs):
        launched["command"] = command
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = CliRunner().invoke(cli.main, [])
    assert result.exit_code == 0, result.output

    command = launched["command"]
    assert "streamlit" in command and "run" in command
    assert any(part.endswith("app.py") for part in command), "must point at the app"
    port_at = command.index("--server.port")
    assert command[port_at + 1] == "8501"
    assert "localhost:8501" in result.output, "tell the user where to look"


def test_bare_command_in_a_script_prints_help_instead(monkeypatch):
    """Silently starting a web server that never exits would be a trap in CI."""
    from click.testing import CliRunner

    from recomps import cli

    def explode(*args, **kwargs):
        raise AssertionError("must not launch a server when nobody is watching")

    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr("subprocess.run", explode)

    result = CliRunner().invoke(cli.main, [])
    assert result.exit_code == 0
    assert "Commands:" in result.output
    assert "ui " in result.output


def test_a_named_subcommand_still_runs_normally(monkeypatch):
    from click.testing import CliRunner

    from recomps import cli

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = CliRunner().invoke(cli.main, ["markets"])
    assert result.exit_code == 0
    assert "demoville" in result.output
