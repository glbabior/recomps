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


def _capture_launch(monkeypatch, tmp_path, interactive: bool = True) -> dict:
    """Run the CLI without actually starting a server or a browser."""
    from recomps import cli
    from recomps.config.store import APP_DIR_ENV

    monkeypatch.setenv(APP_DIR_ENV, str(tmp_path / "config"))
    launched: dict = {}

    class FakeProcess:
        pid = 4242

        def wait(self):
            return 0

        def terminate(self):
            launched["terminated"] = True

    def fake_popen(command, **kwargs):
        launched["command"] = command
        return FakeProcess()

    monkeypatch.setattr(cli, "_interactive", lambda: interactive)
    monkeypatch.setattr(cli, "_open_when_ready", lambda url, **kw: launched.setdefault("url", url))
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    # Never let a test reach a real port or a real process.
    monkeypatch.setattr(cli, "_stop_previous_instance", lambda port: False)
    return launched


def test_bare_command_opens_the_interface_for_a_person(monkeypatch, tmp_path):
    """This is a tool people use by looking at it, so a bare invocation in a
    terminal should show them something rather than a menu."""
    from click.testing import CliRunner

    from recomps import cli

    launched = _capture_launch(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli.main, [])
    assert result.exit_code == 0, result.output

    command = launched["command"]
    assert "streamlit" in command and "run" in command
    assert any(part.endswith("app.py") for part in command), "must point at the app"
    assert command[command.index("--server.port") + 1] == "8501"
    assert launched["url"] == "http://localhost:8501"
    assert "localhost:8501" in result.output, "tell the user where to look"


def test_the_interface_is_not_exposed_to_the_network(monkeypatch, tmp_path):
    """A page showing what your property is worth should not be reachable from
    every device on the network, which is what the default binding does."""
    from click.testing import CliRunner

    from recomps import cli

    launched = _capture_launch(monkeypatch, tmp_path)
    CliRunner().invoke(cli.main, [])

    command = launched["command"]
    assert command[command.index("--server.address") + 1] == "localhost"


def test_usage_statistics_are_not_sent(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from recomps import cli

    launched = _capture_launch(monkeypatch, tmp_path)
    CliRunner().invoke(cli.main, [])
    command = launched["command"]
    assert command[command.index("--browser.gatherUsageStats") + 1] == "false"


def test_the_onboarding_prompt_is_declined_but_a_real_answer_is_kept(monkeypatch, tmp_path):
    """Left alone, Streamlit blocks a first launch asking for an email, which
    reads as the program being broken. An answer already given is not touched."""
    from recomps import cli

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cli._silence_streamlit_onboarding()
    written = tmp_path / ".streamlit" / "credentials.toml"
    assert written.exists() and 'email = ""' in written.read_text()

    written.write_text('[general]\nemail = "someone@example.com"\n', encoding="utf-8")
    cli._silence_streamlit_onboarding()
    assert "someone@example.com" in written.read_text(), "an existing answer must survive"


def test_bare_command_in_a_script_prints_help_instead(monkeypatch):
    """Silently starting a web server that never exits would be a trap in CI."""
    from click.testing import CliRunner

    from recomps import cli

    def explode(*args, **kwargs):
        raise AssertionError("must not launch a server when nobody is watching")

    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr("subprocess.Popen", explode)

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


# ---------------------------------------------------------------------------
# Reopening the market you used last
# ---------------------------------------------------------------------------
#
# The market that matters to a user is normally a private plugin used from a
# folder, so it is never in the dropdown by name and its path has to be typed.
# Typing it at every launch is friction the interface should not impose.


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    """Point config storage at a temp directory so tests never touch real config.

    Autouse, and it has to be: the interface now reopens the market it saw
    last, so any test that runs the app without this reads whatever market the
    developer happened to open, and passes or fails accordingly.
    """
    from recomps.config.store import APP_DIR_ENV

    monkeypatch.setenv(APP_DIR_ENV, str(tmp_path / "config"))
    return tmp_path / "config"


@pytest.fixture
def market_folder(tmp_path):
    """A minimal folder market, standing in for a private plugin."""
    directory = tmp_path / "somewhere-market"
    directory.mkdir()
    (directory / "market.toml").write_text(
        "\n".join(
            [
                "[market]",
                'name = "somewhere"',
                'description = "Somewhere (synthetic)"',
                "",
                "[profiles.a-search]",
                'property_type = "vacant_land"',
                'metric = "lot_sqft"',
                "",
                "[profiles.a-search.subject]",
                "lot_sqft = 6000",
                'label = "Your lot"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return directory


def test_the_market_opened_last_is_reselected_next_launch(config_dir, market_folder):
    from streamlit.testing.v1 import AppTest

    first = AppTest.from_file(str(APP), default_timeout=90).run()
    first.session_state["market_choice"] = "__path__"
    first.session_state["market_path"] = str(market_folder)
    first.run()
    assert not first.exception

    # A brand-new session, with nothing entered.
    second = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not second.exception
    market = next(s for s in second.selectbox if s.label == "Market")
    assert market.value == "__path__"
    assert second.session_state["market_path"] == str(market_folder)
    saved = next(s for s in second.selectbox if s.label == "Saved search")
    assert saved.value == "a-search"


def test_a_half_typed_path_is_not_what_the_next_launch_reopens(config_dir, market_folder):
    """Only a market that actually loaded is worth remembering."""
    from streamlit.testing.v1 import AppTest

    from recomps.config.store import last_market

    app = AppTest.from_file(str(APP), default_timeout=90).run()
    app.session_state["market_choice"] = "__path__"
    app.session_state["market_path"] = str(market_folder / "not-there-yet")
    app.run()
    assert not app.exception
    # The path that failed to load is not what gets reopened; the last market
    # that did load stands.
    assert last_market()[1] is None


def test_nothing_remembered_means_the_normal_first_run(config_dir):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not app.exception
    assert next(s for s in app.selectbox if s.label == "Market").value == "demoville"


def test_a_market_that_has_since_moved_does_not_break_the_interface(config_dir, market_folder):
    """The convenience must never be the reason the interface will not start."""
    from streamlit.testing.v1 import AppTest

    from recomps.config.store import remember_market

    remember_market("__path__", str(market_folder))
    for child in market_folder.iterdir():
        child.unlink()
    app = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not app.exception


def test_a_corrupt_record_is_treated_as_nothing_remembered(config_dir):
    from recomps.config.store import last_market, recent_path

    recent_path().parent.mkdir(parents=True, exist_ok=True)
    recent_path().write_text("{not json", encoding="utf-8")
    assert last_market() == (None, None)


# ---------------------------------------------------------------------------
# Relaunching replaces the instance that is already running
# ---------------------------------------------------------------------------
#
# Someone launching from a desktop shortcut has no terminal to press Ctrl+C in,
# so relaunching has to be the only control they need. That means killing a
# process, which means these tests are mostly about what must NOT be killed.


def _instance_env(monkeypatch, tmp_path, *, serving: bool = False, listening_pid=None):
    """Isolate the instance machinery from the real machine.

    Both outward-facing calls are stubbed by default -- the port probe and the
    kill -- so no test can reach a port a real interface might be serving on.
    A test that wants one of them opts in explicitly.
    """
    from recomps import cli
    from recomps.config.store import APP_DIR_ENV

    monkeypatch.setenv(APP_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setattr(cli, "_streamlit_is_serving", lambda port, timeout=0.4: serving)
    monkeypatch.setattr(cli, "_pid_listening_on", lambda port: listening_pid)

    def must_not_kill(pid, sig):
        raise AssertionError(f"killed pid {pid} when the test did not allow it")

    monkeypatch.setattr("os.kill", must_not_kill)


def test_a_running_instance_is_stopped_so_the_port_comes_free(monkeypatch, tmp_path):
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=True)
    cli._remember_instance(4242, 8501)
    killed = {}

    monkeypatch.setattr(
        cli, "_streamlit_is_serving", lambda port, timeout=0.4: not killed
    )
    monkeypatch.setattr(cli, "_is_our_process", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.setdefault("pid", pid))

    assert cli._stop_previous_instance(8501) is True
    assert killed["pid"] == 4242
    assert not cli._instance_record().exists(), "a stopped instance must not be recorded"


def test_nothing_is_killed_when_no_streamlit_answers_on_the_port(monkeypatch, tmp_path):
    """A recorded PID alone is not evidence: PIDs are reused."""
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=False)
    cli._remember_instance(4242, 8501)
    monkeypatch.setattr(cli, "_is_our_process", lambda pid: True)

    assert cli._stop_previous_instance(8501) is False
    assert not cli._instance_record().exists(), "the stale record should be cleared"


def test_nothing_is_killed_when_the_pid_is_no_longer_python(monkeypatch, tmp_path):
    """The clinching guard against PID reuse killing an unrelated program."""
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=True)
    cli._remember_instance(4242, 8501)
    monkeypatch.setattr(cli, "_is_our_process", lambda pid: False)

    assert cli._stop_previous_instance(8501) is False


def test_an_instance_on_a_different_port_is_left_alone(monkeypatch, tmp_path):
    """`recomps ui --port` twice is two deliberate instances, not a mistake."""
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=True)
    cli._remember_instance(4242, 8501)
    monkeypatch.setattr(cli, "_is_our_process", lambda pid: True)

    assert cli._stop_previous_instance(9999) is False


def test_no_record_means_nothing_to_stop(monkeypatch, tmp_path):
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=False)
    assert cli._stop_previous_instance(8501) is False


def test_a_corrupt_record_never_kills_anything(monkeypatch, tmp_path):
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=False)
    record = cli._instance_record()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{not json", encoding="utf-8")

    assert cli._stop_previous_instance(8501) is False


def test_launching_records_the_new_instance_and_clears_it_on_exit(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from recomps import cli

    launched = _capture_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_stop_previous_instance", lambda port: False)
    result = CliRunner().invoke(cli.main, [])
    assert result.exit_code == 0, result.output
    assert launched["command"]
    assert not cli._instance_record().exists(), "the record must not outlive the process"


def test_an_instance_started_before_this_feature_is_still_stopped(monkeypatch, tmp_path):
    """The interface already open when someone upgrades is exactly the one in
    the way, and it was never recorded. The port's owner is asked for instead."""
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=True, listening_pid=4242)
    monkeypatch.setattr(cli, "_is_our_process", lambda pid: True)
    killed = {}
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.setdefault("pid", pid))

    assert cli._instance_record().exists() is False, "no record, by construction"
    assert cli._stop_previous_instance(8501) is True
    assert killed["pid"] == 4242


def test_an_unrecorded_port_owner_that_is_not_python_is_left_alone(monkeypatch, tmp_path):
    """Without a record the only identity evidence is the image name. Trust it
    or kill someone else's server."""
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=True, listening_pid=4242)
    monkeypatch.setattr(cli, "_is_our_process", lambda pid: False)

    assert cli._stop_previous_instance(8501) is False


def test_a_serving_port_with_no_identifiable_owner_is_left_alone(monkeypatch, tmp_path):
    from recomps import cli

    _instance_env(monkeypatch, tmp_path, serving=True, listening_pid=None)
    assert cli._stop_previous_instance(8501) is False


# ---------------------------------------------------------------------------
# Browsing to a market folder
# ---------------------------------------------------------------------------
#
# A private market is used from a folder, and someone who has never typed a
# filesystem path should still be able to reach it.


@pytest.fixture
def browsable(tmp_path):
    """A folder holding one market and one ordinary directory."""
    root = tmp_path / "code"
    (root / "engine").mkdir(parents=True)
    market = root / "somewhere-market"
    market.mkdir()
    (market / "market.toml").write_text(
        "\n".join(
            [
                "[market]",
                'name = "somewhere"',
                'description = "Somewhere (synthetic)"',
                "",
                "[profiles.a-search]",
                'property_type = "vacant_land"',
                'metric = "lot_sqft"',
                "",
                "[profiles.a-search.subject]",
                "lot_sqft = 6000",
                'label = "Your lot"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root


def _browsing_at(config_dir, folder):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=90).run()
    app.session_state["market_choice"] = "__path__"
    app.session_state["browse_dir"] = str(folder)
    return app.run()


def test_a_folder_holding_a_market_is_marked_as_one(config_dir, browsable):
    """An OS folder dialog cannot tell you which folder is a market. This can."""
    app = _browsing_at(config_dir, browsable)
    assert not app.exception
    labels = [b.label for b in app.button]
    assert any("somewhere-market" in label and "a market" in label for label in labels)
    assert any(label == "\U0001f4c1 engine" for label in labels), labels


def test_clicking_a_market_folder_opens_that_market(config_dir, browsable):
    app = _browsing_at(config_dir, browsable)
    market = next(b for b in app.button if "somewhere-market" in (b.label or ""))
    market.click().run()
    assert not app.exception
    assert app.session_state["market_path"] == str(browsable / "somewhere-market")
    assert next(s for s in app.selectbox if s.label == "Saved search").value == "a-search"


def test_clicking_an_ordinary_folder_navigates_into_it(config_dir, browsable):
    app = _browsing_at(config_dir, browsable)
    next(b for b in app.button if b.label == "\U0001f4c1 engine").click().run()
    assert not app.exception
    assert app.session_state["browse_dir"] == str(browsable / "engine")


def test_the_browser_can_go_back_up(config_dir, browsable):
    app = _browsing_at(config_dir, browsable / "engine")
    next(b for b in app.button if b.label.startswith("⬆")).click().run()
    assert not app.exception
    assert app.session_state["browse_dir"] == str(browsable)


def test_a_folder_that_cannot_be_listed_is_not_an_error(config_dir, browsable, monkeypatch):
    """Much of a system drive cannot be read; that is not a failure worth a message."""
    from recomps.ui import app as app_mod

    def deny(self):
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "iterdir", deny)
    assert app_mod._subfolders(browsable) == []


def test_the_browser_opens_beside_a_market_already_chosen(config_dir, browsable):
    """Its siblings are the likeliest next choice, so start one level up."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=90).run()
    app.session_state["market_choice"] = "__path__"
    app.session_state["market_path"] = str(browsable / "somewhere-market")
    app.run()
    assert not app.exception
    assert any("somewhere-market" in (b.label or "") for b in app.button)


# ---------------------------------------------------------------------------
# Excluded parcels are shown, not silently dropped
# ---------------------------------------------------------------------------


def test_excluded_parcels_and_their_reasons_reach_the_screen(result):
    """A run that quietly drops parcels reports a tidier market that reads
    exactly like a real one. The engine records the reason; the screen must
    show it."""
    view = ui_state.Viewing(result=result)
    rows = ui_state.exclusion_rows(view)
    assert rows, "the demo market excludes parcels by design"
    assert all(r["address"] and r["reason"] for r in rows)
    assert any("cap" in r["reason"] for r in rows)


def test_a_saved_run_shows_the_parcels_it_excluded_too(archived):
    """Reopening a past run must not lose why it was the size it was."""
    stored = ui_state.open_saved(archived)
    rows = ui_state.exclusion_rows(ui_state.Viewing(stored=stored))
    assert rows and all(r["reason"] for r in rows)


def test_no_exclusions_means_nothing_to_report():
    assert ui_state.exclusion_rows(ui_state.Viewing()) == []


# ---------------------------------------------------------------------------
# The core view reaches a screen
# ---------------------------------------------------------------------------
#
# Two controls in the editor set these bounds, and until now their entire
# output appeared only in the methodology document -- so from the one place a
# user sets them, they looked inert.


def test_the_core_view_figures_are_available_to_the_screen(result):
    view = ui_state.Viewing(result=result)
    figures = ui_state.core_figures(view)
    assert figures is not None
    assert figures["low"] and figures["high"]
    assert figures["sold_count"] <= figures["sold_total"]
    assert figures["sold_median_ppsf"] is not None


def test_a_reopened_run_still_knows_its_core_bounds(archived):
    """The bounds live in the profile the run used, not in the statistics."""
    stored = ui_state.open_saved(archived)
    figures = ui_state.core_figures(ui_state.Viewing(stored=stored))
    assert figures is not None
    assert figures["low"] and figures["high"]
    assert figures["sold_median_ppsf"] is not None


def test_a_search_with_no_core_bounds_shows_no_core_view(result):
    """No bounds, no second reading -- say nothing rather than show zeros."""
    from copy import deepcopy

    bare = deepcopy(result)
    bare.profile.exclusions.core_min_ppsf = None
    bare.profile.exclusions.core_max_ppsf = None
    assert ui_state.core_figures(ui_state.Viewing(result=bare)) is None


def test_the_workbook_core_view_recomputes_from_its_own_editable_bounds(result):
    """Static numbers would make the bounds decorative; the point is to move them."""
    from recomps.workbook.builder import build_workbook

    ws = build_workbook(result)["Summary"]
    cells = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    core = [v for v in cells if v.startswith("=SUMPRODUCT") and "Sold Comps" in v]
    assert core, "the core figures must be formulas, not values"
    bounds_row = next(
        r[0].row for r in ws.iter_rows()
        if r[0].value == "Bounds $/sq ft (editable low / high)"
    )
    assert any(f"$B${bounds_row}" in v for v in core), "must read the editable bounds"


def test_saving_a_profile_does_not_crash_on_the_sidebar_s_widget_key(
    config_dir, browsable
):
    """Regression: the editor wrote to the saved-search selectbox's own key
    after that widget had been drawn, which Streamlit refuses. Editing any
    existing search hit it."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=120).run()
    app.session_state["market_choice"] = "__path__"
    app.session_state["market_path"] = str(browsable / "somewhere-market")
    app.run()

    next(b for b in app.button if "Edit" in (b.label or "")).click().run()
    assert not app.exception, "the editor must open"

    next(n for n in app.number_input if "bigger than" in n.label).set_value(0.0)
    next(b for b in app.button if b.label == "Save").click().run()

    assert not app.exception, f"saving raised: {app.exception}"
    assert app.session_state["profile_name"] == "a-search"


# ---------------------------------------------------------------------------
# Agent statistics on screen
# ---------------------------------------------------------------------------


def test_agent_stats_carry_closings_and_distance_from_ask(result):
    analysis = ui_state.agent_analysis(ui_state.Viewing(result=result))
    assert analysis and analysis["agents"]
    top = analysis["agents"][0]
    assert top["closings"] >= 1
    assert top["avg_sold_to_ask"] is not None
    assert analysis["attributed"] <= analysis["total"]


def test_the_ratio_reaches_the_screen_unrounded(result):
    """Rounding here would be the same mistake the valuation rules forbid; the
    percentage is a rendering, not a stored figure."""
    analysis = ui_state.agent_analysis(ui_state.Viewing(result=result))
    ratios = [a["avg_sold_to_ask"] for a in analysis["agents"] if a["avg_sold_to_ask"]]
    assert any(r != round(r, 3) for r in ratios), "a ratio was rounded before display"


def test_the_shortlist_caveats_travel_with_the_table(result):
    """The table reads like a ranking and is not one. The caveats are part of
    the output, not decoration around it."""
    analysis = ui_state.agent_analysis(ui_state.Viewing(result=result))
    joined = " ".join(analysis["caveats"]).lower()
    assert "not an endorsement" in joined
    assert "interview" in joined


def test_a_reopened_run_still_has_its_agent_table(archived):
    stored = ui_state.open_saved(archived)
    analysis = ui_state.agent_analysis(ui_state.Viewing(stored=stored))
    assert analysis and analysis["agents"]
    assert analysis["agents"][0]["closings"] >= 1


def test_nothing_to_show_when_no_run_is_open():
    assert ui_state.agent_analysis(ui_state.Viewing()) is None


# ---------------------------------------------------------------------------
# Tracing a shortlisted agent's sales by colour
# ---------------------------------------------------------------------------


def test_each_shortlisted_agent_gets_its_own_colour(result):
    from recomps.ui import app as app_mod

    analysis = ui_state.agent_analysis(ui_state.Viewing(result=result))
    colors = app_mod._shortlist_colors(analysis)
    shortlisted = [a["agent"] for a in analysis["agents"] if a["flag"] == "shortlist"]
    assert colors, "the demo market shortlists agents by design"
    assert set(colors) == set(shortlisted[: len(app_mod.HIGHLIGHTS)])
    assert len(set(colors.values())) == len(colors), "two agents share a colour"


def test_only_shortlisted_agents_are_tinted(result):
    from recomps.ui import app as app_mod

    analysis = ui_state.agent_analysis(ui_state.Viewing(result=result))
    colors = app_mod._shortlist_colors(analysis)
    for row in analysis["agents"]:
        if row["flag"] != "shortlist":
            assert row["agent"] not in colors


def test_the_same_agent_gets_the_same_tint_in_both_tables(result):
    """The colour is the whole point: it has to mean one person across tables."""
    from recomps.ui import app as app_mod

    analysis = ui_state.agent_analysis(ui_state.Viewing(result=result))
    colors = app_mod._shortlist_colors(analysis)
    agent = next(iter(colors))
    style = app_mod._tint(colors, agent)
    assert colors[agent] in style
    assert app_mod.HIGHLIGHT_TEXT in style, "an unset foreground vanishes in one theme"
    assert app_mod._tint(colors, "Nobody At All") == ""


def test_a_run_with_more_shortlisted_agents_than_colours_does_not_reuse_one(result):
    from copy import deepcopy

    from recomps.ui import app as app_mod

    analysis = deepcopy(ui_state.agent_analysis(ui_state.Viewing(result=result)))
    analysis["agents"] = [
        {"agent": f"Agent {i}", "flag": "shortlist"} for i in range(20)
    ]
    colors = app_mod._shortlist_colors(analysis)
    assert len(colors) == len(app_mod.HIGHLIGHTS)
    assert len(set(colors.values())) == len(colors)


def test_unticking_still_drops_a_sale_when_the_editor_returns_a_frame(result):
    """Passing a Styler to st.data_editor makes it hand back a DataFrame, and
    iterating a DataFrame walks its column names -- a silent failure in which
    every sale stays in and the figures never move."""
    import pandas as pd

    from recomps.ui import app as app_mod

    rows = [
        {"Include": True, "Address": "1 Example St", "Agent": "A"},
        {"Include": False, "Address": "2 Example St", "Agent": "B"},
        {"Include": False, "Address": "3 Example St", "Agent": "C"},
    ]
    assert app_mod._unticked(pd.DataFrame(rows)) == {"2 Example St", "3 Example St"}
    assert app_mod._unticked(rows) == {"2 Example St", "3 Example St"}
    assert app_mod._unticked(pd.DataFrame(columns=["Include", "Address"])) == set()


def test_a_styled_table_keeps_every_row_and_column(result):
    """Styling must not quietly reshape the data underneath it."""
    from recomps.ui import app as app_mod

    rows = [{"Agent": "A", "Price": 1}, {"Agent": "B", "Price": 2}]
    styled = app_mod._styled(rows, {"A": "#FFD98E"}, "Agent")
    frame = styled.data
    assert list(frame.columns) == ["Agent", "Price"]
    assert len(frame) == 2
    # No colours to apply means no Styler at all, and a plain frame is fine.
    assert not hasattr(app_mod._styled(rows, {}, "Agent"), "data")


def test_a_run_archived_before_the_rate_columns_says_so(archived, monkeypatch):
    """An empty column reads as a broken feature. The snapshot simply predates it."""
    from recomps.ui import app as app_mod

    stored = ui_state.open_saved(archived)
    for row in stored.agents["agents"]:
        row.pop("avg_ppsf", None)
        row.pop("avg_size", None)
    analysis = ui_state.agent_analysis(ui_state.Viewing(stored=stored))
    assert all("avg_ppsf" not in a for a in analysis["agents"])
    # The colour mapping must still work on an old snapshot.
    assert isinstance(app_mod._shortlist_colors(analysis), dict)


def test_active_listings_reach_the_screen_separately_from_the_sold_comps(result):
    """An asking price is a claim and a sale is a fact; one table would invite
    reading the first as the second."""
    view = ui_state.Viewing(result=result)
    actives = ui_state.active_rows(view)
    solds = ui_state.comp_rows(view)
    assert actives and solds
    assert {a["address"] for a in actives}.isdisjoint({s["address"] for s in solds})
    assert all("list_price" in a for a in actives)


def test_a_saved_run_still_shows_what_was_on_the_market(archived):
    stored = ui_state.open_saved(archived)
    rows = ui_state.active_rows(ui_state.Viewing(stored=stored))
    assert rows and any(r["list_price"] for r in rows)


def test_the_area_table_reports_size_beside_rate(result):
    """A quadrant of larger parcels shows a lower $/sqft without being cheaper
    land, so the size column has to travel with the rate."""
    rows, _ = ui_state.area_rows(ui_state.Viewing(result=result))
    populated = [r for r in rows if r["sold_count"]]
    assert populated
    assert all(r["sold_avg_size"] is not None for r in populated)
    assert all(r["sold_median_ppsf"] is not None for r in populated)


def test_a_reopened_run_keeps_its_area_table(archived):
    stored = ui_state.open_saved(archived)
    rows, _ = ui_state.area_rows(ui_state.Viewing(stored=stored))
    assert any(r.get("sold_count") for r in rows)


def test_the_ladder_reaches_the_screen_with_its_marked_rung(result):
    rows, notes = ui_state.ladder_rows(ui_state.Viewing(result=result))
    assert rows
    assert sum(1 for r in rows if r["is_profile_bracket"]) == 1
    assert rows[-1]["is_all_sold"]
    assert notes


def test_a_reopened_run_keeps_its_ladder(archived):
    stored = ui_state.open_saved(archived)
    rows, _ = ui_state.ladder_rows(ui_state.Viewing(stored=stored))
    assert rows and rows[-1]["is_all_sold"]


def test_the_highlight_tints_are_dark_enough_for_white_text(result):
    """White on a pale tint is unreadable; the palette and the foreground have
    to be chosen together."""
    from recomps.ui import app as app_mod

    assert app_mod.HIGHLIGHT_TEXT.upper() == "#FFFFFF"
    for colour in app_mod.HIGHLIGHTS:
        r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        # Rec. 601 luma: comfortably below the midpoint on every swatch.
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        assert luma < 110, f"{colour} is too light for white text (luma {luma:.0f})"


def test_a_panel_missing_from_an_old_run_says_so_rather_than_vanishing(archived):
    """A section that simply is not there reads as a broken feature."""
    from recomps.ui import app as app_mod

    stored = ui_state.open_saved(archived)
    stored.ladder = {}
    view = ui_state.Viewing(origin="saved", record=archived, stored=stored)
    assert ui_state.ladder_rows(view) == ([], [])
    # Silence is only right for a fresh run, where absent means absent.
    fresh = ui_state.Viewing(origin="fresh")
    assert fresh.result is None and fresh.stored is None
    assert app_mod._predates(fresh, "anything") is None


def test_what_a_run_could_not_see_reaches_the_screen(result):
    """A window that excludes half a recorded dataset changes every figure, so
    it belongs beside them and not only in the run log."""
    from copy import deepcopy

    view = ui_state.Viewing(result=deepcopy(result))
    view.result.diagnostics.not_found.append("12 of 40 recorded sales fall outside")
    view.result.diagnostics.sources_failed.append("somewhere: 403")
    notes = ui_state.coverage_notes(view)
    assert any("12 of 40" in n for n in notes)
    assert any("Source unavailable" in n for n in notes)


def test_a_clean_run_reports_no_coverage_gaps(result):
    assert ui_state.coverage_notes(ui_state.Viewing(result=result)) == []


def test_both_list_prices_reach_the_headline(result):
    """The recommended one is deliberately under the estimate and looks like an
    error beside it without its sibling."""
    figures = ui_state.headline(ui_state.Viewing(result=result))
    assert figures["suggested_list"] and figures["at_market"]
    assert figures["at_market"] > figures["suggested_list"]


def test_a_saved_run_carries_both_list_prices_too(archived):
    stored = ui_state.open_saved(archived)
    figures = ui_state.headline(ui_state.Viewing(stored=stored))
    assert figures["suggested_list"] and figures["at_market"]


# ---------------------------------------------------------------------------
# One way to run, and one way to look back
# ---------------------------------------------------------------------------
#
# The data-source toggle invited comparing a live run against a recording made
# weeks earlier -- different sales, different dates, different agents -- and
# reading the differences as instability in the tool.


@pytest.fixture
def researchable(tmp_path):
    """A folder market that declares a source a live run could read."""
    directory = tmp_path / "live-market"
    directory.mkdir()
    (directory / "market.toml").write_text(
        "\n".join(
            [
                "[market]",
                'name = "liveville"',
                'description = "Liveville (synthetic)"',
                "",
                "[profiles.a-search]",
                'property_type = "vacant_land"',
                'metric = "lot_sqft"',
                "",
                "[profiles.a-search.subject]",
                "lot_sqft = 6000",
                'label = "Your lot"',
                "",
                "[[sources]]",
                'name = "an-index"',
                'adapter = "nextdata"',
                'url = "https://example.invalid/index"',
                "",
                "[sources.embedded]",
                'script_id = "__NEXT_DATA__"',
                'records_path = "props.homes"',
                "",
                "[sources.embedded.fields]",
                'address = { path = "addr", transform = "text" }',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return directory


def test_a_market_that_can_be_researched_offers_no_recorded_option(
    config_dir, researchable
):
    """AppTest re-executes the app from file, so this uses a market that is
    genuinely live-capable rather than a patched helper."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=120).run()
    app.session_state["market_choice"] = "__path__"
    app.session_state["market_path"] = str(researchable)
    app.run()
    assert not app.exception
    labels = [r.label for r in app.radio]
    assert "Where the data comes from" not in labels
    assert "Who pays for the reading" in labels


def test_a_market_with_no_live_sources_says_what_it_will_do(config_dir):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=120).run()
    app.session_state["market_choice"] = "demoville"
    app.run()
    assert not app.exception
    assert "Where the data comes from" not in [r.label for r in app.radio]
    assert any(
        "no live sources configured" in c.value for c in app.caption
    ), "a market that cannot research must say so, not offer a silent choice"


def test_can_research_reads_the_market_s_own_sources():
    from recomps.markets.demoville import MARKET
    from recomps.ui import app as app_mod

    assert app_mod._can_research(MARKET) is False

    class Broken:
        def sources(self):
            raise RuntimeError("plugin blew up")

    assert app_mod._can_research(Broken()) is False


def test_money_in_markdown_text_is_escaped_so_it_does_not_become_an_equation():
    """Streamlit renders captions, warnings and expander labels as Markdown,
    where a pair of dollar signs is LaTeX. 'runs $508,121 to $532,922' rendered
    the middle as a green serif equation."""
    from recomps.ui import app as app_mod

    note = "the estimate runs $508,121 to $532,922"
    assert app_mod.md(note) == r"the estimate runs \$508,121 to \$532,922"
    assert app_mod.md("no money here") == "no money here"
    assert app_mod.md("$30.00 to $150.00 per sq ft").count(r"\$") == 2


def test_every_engine_note_reaching_a_caption_is_escaped():
    """The notes carry money by nature, so the escaping cannot be per-string."""
    import re
    from pathlib import Path

    from recomps.ui import app as app_mod

    source = Path(app_mod.__file__).read_text(encoding="utf-8")
    unescaped = []
    for match in re.finditer(r"st\.(caption|warning)\(([a-z_]+)\)", source):
        if not match.group(2).startswith("md"):
            unescaped.append(match.group(0))
    assert not unescaped, f"engine text rendered without escaping: {unescaped}"



def test_past_runs_are_named_so_they_can_be_told_apart(archived):
    """A day of experimenting leaves several runs an hour apart, some
    researched and some replayed, holding different numbers of sales. Choosing
    between them by timestamp alone is guessing."""
    from recomps.ui import app as app_mod

    described = app_mod._describe_run(archived)
    assert archived.label in described
    assert "sold" in described and "listed" in described


def test_a_run_with_no_snapshot_still_names_itself(archived):
    from recomps.ui import app as app_mod

    class Bare:
        label = "2026-09-05 10:31"
        snapshot = None

    assert app_mod._describe_run(Bare()) == "2026-09-05 10:31  (no data)"


def test_the_run_label_does_not_name_a_source(archived):
    """Every run a market lists came from the same place, so saying so on each
    row is noise -- and the words for it confused a reader before."""
    from recomps.ui import app as app_mod

    described = app_mod._describe_run(archived)
    for word in ("recorded", "researched", "from the web", "fixture", "live"):
        assert word not in described, f"{word!r} is noise on every row"


def test_the_quadrant_dividers_are_shown_not_just_named(config_dir):
    """The area table reports NE and SW without saying where they are, which
    makes it a table nobody can check."""
    from recomps.markets.demoville import MARKET

    lines = ui_state.divider_lines(MARKET)
    assert lines
    joined = " ".join(lines)
    assert "North/south" in joined and "East/west" in joined
    assert "because the road curves" in joined
    # Each waypoint is listed, since a curve is not one number.
    assert sum(1 for line in lines if line.startswith("    ")) >= 2


def test_a_market_with_no_geometry_shows_no_dividers():
    class Flat:
        def geometry(self):
            from recomps.plugin.market import MarketGeometry

            return MarketGeometry()

    assert ui_state.divider_lines(Flat()) == []


def test_a_market_that_cannot_describe_itself_does_not_break_the_panel():
    class Broken:
        def geometry(self):
            raise RuntimeError("plugin blew up")

    assert ui_state.divider_lines(Broken()) == []


def test_a_rename_onto_a_taken_name_leaves_the_archive_alone(
    config_dir, tmp_path, monkeypatch
):
    """The archive move and the profile rekey are two steps and cannot be made
    atomic, so the one that can fail has to fail first. Moving the runs and
    then discovering the name was taken left the old search with no history and
    filed its runs under somebody else's -- while reporting failure."""
    from datetime import UTC, datetime

    from recomps.config.profile import Subject, vacant_land_profile
    from recomps.config.store import save_user_profile
    from recomps.reporting import history as history_mod

    def profile(name):
        p = vacant_land_profile(name)
        p.subject = Subject(lot_sqft=6450.0, label="Your lot")
        return p

    save_user_profile("demoville", profile("old-search"))
    save_user_profile("demoville", profile("taken"))
    directory = history_mod.run_dir(
        tmp_path, "demoville", "old-search", datetime(2026, 9, 1, tzinfo=UTC)
    )
    directory.mkdir(parents=True)
    (directory / history_mod.SNAPSHOT_NAME).write_text("{}", encoding="utf-8")

    # The collision check the editor now performs before touching anything.
    from recomps.config.store import available_profiles
    from recomps.markets.demoville import MARKET

    assert "taken" in available_profiles(MARKET)

    # And the move itself still refuses when the target already holds runs.
    other = history_mod.run_dir(
        tmp_path, "demoville", "taken", datetime(2026, 9, 1, tzinfo=UTC)
    )
    other.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        history_mod.rename_profile_runs(tmp_path, "demoville", "old-search", "taken")
    assert len(history_mod.list_runs(tmp_path, "demoville", "old-search")) == 1
