"""The interface.

Organised around the question a user actually arrives with: *what is my
property worth, and what changed since last time?* So the page reads
top to bottom as pick a saved search, then either ask it again or look at what
it said before, then act on what is on screen.

Two things it is careful about, because both are ways a tool like this can
mislead:

**Say which numbers these are.** A saved run shown as-is and a saved run
re-analyzed with a comp removed look identical on a screen and mean different
things. Every view states its provenance in words.

**Say what a run will cost before it runs.** The reading is paid for either by
a Claude subscription or by a metered API account, and the interface names
which one before the button is pressed.

Everything here calls the same functions the command line does. There is no
behaviour that exists only in the interface, and none that exists only outside
it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from recomps.config.profile import (
    CompProfile,
    Denominator,
    Exclusions,
    Identification,
    PropertyType,
    SimilarBracket,
    Subject,
)
from recomps.config.store import (
    available_profiles,
    delete_user_profile,
    describe_profile,
    last_market,
    load_user_profiles,
    profile_origin,
    remember_market,
    rename_user_profile,
    save_user_profile,
)
from recomps.model.snapshot import build_snapshot
from recomps.pipeline.run import run_pipeline
from recomps.plugin.loader import MARKET_FILE, discover, load_market
from recomps.reporting import compare as compare_mod
from recomps.reporting import history as history_mod
from recomps.reporting import methodology
from recomps.research.fixture import FixtureResearcher
from recomps.ui import state as ui_state
from recomps.workbook.builder import build_workbook

try:  # the research layer is an optional extra
    from recomps.research.live import LookupsNeedConfirmation
except ImportError:  # pragma: no cover - exercised by a bare install
    class LookupsNeedConfirmation(RuntimeError):
        """Stand-in so the interface imports without the live extra."""

        plan = None


st.set_page_config(page_title="REComps", page_icon=":house:", layout="wide")

# Streamlit's own Deploy button publishes an app to its public cloud. On a tool
# whose whole point is that a real market's data stays on one machine, that
# button is a one-click mistake with no legitimate use here, so it is removed.
# Only the deploy control: the menu beside it carries rerun and clear-cache,
# which are the only recovery a user without a terminal has.
st.markdown(
    """
    <style>
      [data-testid="stAppDeployButton"] { display: none; }
    </style>
    """,
    unsafe_allow_html=True,
)

MONEY = "${:,.0f}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def money(value: float | None) -> str:
    return MONEY.format(value) if value is not None else "—"


def md(text: str) -> str:
    """Escape a string for Streamlit's Markdown.

    Captions, expander labels, warnings and tooltips are Markdown, and a
    pair of dollar signs in one string is LaTeX: "runs $508,121 to $532,922"
    renders the middle as a green serif equation. Every figure this project
    prints is money, so almost any two of them in a sentence collide.
    """
    return text.replace("$", "\\$")


def rate(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


@st.cache_resource(show_spinner=False)
def _load_market(name: str | None, path: str | None):
    return load_market(name, path)


def market_from_session():
    choice = st.session_state.get("market_choice")
    path = st.session_state.get("market_path")
    if choice == "__path__":
        if not path:
            return None
        return _load_market(None, path)
    if not choice:
        return None
    return _load_market(choice, None)


def view() -> ui_state.Viewing:
    if "view" not in st.session_state:
        st.session_state.view = ui_state.Viewing()
    return st.session_state.view


# ---------------------------------------------------------------------------
# Sidebar: which market, which saved search
# ---------------------------------------------------------------------------


def _restore_last_market(options: list[str]) -> None:
    """Preselect the market this user opened last (once per session).

    Only seeded before the widgets exist, so it sets their initial value and
    never fights a choice made afterwards. A remembered market that has since
    been uninstalled, or a folder that has moved, falls through to the normal
    "choose a market" state rather than erroring.
    """
    if st.session_state.get("_market_restored"):
        return
    st.session_state["_market_restored"] = True
    choice, path = last_market()
    if choice not in options:
        return
    st.session_state.setdefault("market_choice", choice)
    if path:
        st.session_state.setdefault("market_path", path)


#: Enough of a directory to navigate; past this the sidebar is unusable anyway.
BROWSE_LIMIT = 60


def _is_market_folder(path: Path) -> bool:
    try:
        return (path / MARKET_FILE).is_file()
    except OSError:
        return False


def _subfolders(path: Path) -> list[Path]:
    """Navigable children of `path`, quietly skipping what cannot be read.

    A folder the user cannot open is not an error worth a message -- it is
    simply not somewhere they can go, and much of a system drive is like that.
    """
    try:
        children = sorted(
            (c for c in path.iterdir() if not c.name.startswith(".")),
            key=lambda c: c.name.lower(),
        )
    except OSError:
        return []
    folders = []
    for child in children:
        try:
            if child.is_dir():
                folders.append(child)
        except OSError:
            continue
    return folders


def _browse_start() -> Path:
    """Where the browser opens: nearest useful place, not the filesystem root."""
    for candidate in (st.session_state.get("market_path"), *_market_search_roots()):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            if path.is_dir():
                # Start beside a chosen market, so its siblings are one click away.
                return path.parent if _is_market_folder(path) else path
        except OSError:
            continue
    return Path.home()


def _market_search_roots() -> list[str]:
    """Places a market plugin is likely to be, most likely first.

    A private market is normally cloned beside the engine, so the engine's own
    parent is the first guess and is usually the only one needed.
    """
    here = Path.cwd()
    return [str(here.parent), str(here), str(Path.home())]


def _go_to(path: Path) -> None:
    st.session_state["browse_dir"] = str(path)


def _pick_folder(path: Path) -> None:
    """Choose `path` as the market folder.

    Written from a callback rather than inline: `market_path` backs a text
    input, and Streamlit only allows a widget's value to be set before the run
    that draws it.
    """
    st.session_state["market_path"] = str(path)
    st.session_state["market_choice"] = "__path__"
    st.session_state.pop("browse_dir", None)


def _folder_browser() -> None:
    """Navigate to a market folder without knowing how to type its path.

    A native OS dialog was the obvious alternative and is worse here: it opens
    on whichever machine runs the server, it can appear behind the browser
    window with nothing on screen explaining the wait, and -- the deciding
    point -- it cannot tell the user which folder is actually a market. This
    can, so every folder holding a ``market.toml`` is marked as it is listed.
    """
    current = Path(st.session_state.get("browse_dir") or _browse_start())
    try:
        current = current.resolve()
    except OSError:
        current = Path.home()

    st.caption(f"📂 {current}")

    if _is_market_folder(current):
        st.button(
            "Use this folder", type="primary", width="stretch",
            key="browse_use_current", on_click=_pick_folder, args=(current,),
        )

    parent = current.parent
    if parent != current:
        st.button(
            f"⬆ {parent.name or parent}", width="stretch",
            key="browse_up", on_click=_go_to, args=(parent,),
        )

    folders = _subfolders(current)
    for folder in folders[:BROWSE_LIMIT]:
        if _is_market_folder(folder):
            st.button(
                f"✓ {folder.name} — a market", width="stretch",
                key=f"browse_pick_{folder}", on_click=_pick_folder, args=(folder,),
            )
        else:
            st.button(
                f"📁 {folder.name}", width="stretch",
                key=f"browse_go_{folder}", on_click=_go_to, args=(folder,),
            )
    if not folders:
        st.caption("Nothing to open here.")
    elif len(folders) > BROWSE_LIMIT:
        st.caption(
            f"Showing {BROWSE_LIMIT} of {len(folders)} folders. Type the path above "
            "if the one you want is not listed."
        )


def sidebar() -> tuple[object, CompProfile | None]:
    st.sidebar.title("REComps")

    installed = discover()
    options = [m.name for m in installed] + ["__path__"]
    labels = {m.name: m.description or m.name for m in installed}
    labels["__path__"] = "From a folder…"

    _restore_last_market(options)

    st.sidebar.selectbox(
        "Market",
        options,
        format_func=lambda o: labels.get(o, o),
        key="market_choice",
        help="A market is a place: its boundaries, its sources, your property "
             "there. Private markets live in their own folder.",
    )
    if st.session_state.get("market_choice") == "__path__":
        st.sidebar.text_input(
            "Folder containing market.toml", key="market_path",
            placeholder="../my-market",
            help="Type it if you know it, or browse below.",
        )
        with st.sidebar.expander(
            "Browse…", expanded=not st.session_state.get("market_path")
        ):
            _folder_browser()

    try:
        market = market_from_session()
    except Exception as exc:
        st.sidebar.error(str(exc))
        return None, None
    if market is None:
        st.sidebar.info("Choose a market to begin.")
        return None, None

    # Only after it actually loaded: a half-typed path must not be what the
    # next launch tries to reopen.
    remember_market(
        st.session_state.get("market_choice"), st.session_state.get("market_path")
    )

    profiles = available_profiles(market)
    if not profiles:
        st.sidebar.warning("This market has no saved searches yet.")
        return market, None

    names = sorted(profiles)
    # A profile just saved by the editor becomes the selection, applied here
    # because this is the last moment before the widget owning the key exists.
    pending = st.session_state.pop("_select_profile", None)
    if pending in names:
        st.session_state["profile_name"] = pending
    default = market.default_profile()
    st.sidebar.selectbox(
        "Saved search",
        names,
        index=names.index(default) if default in names else 0,
        key="profile_name",
        help="A saved search is a question: what kind of property, whose "
             "property, how far back to look.",
    )
    profile = profiles[st.session_state.profile_name]
    st.sidebar.caption(describe_profile(profile))
    st.sidebar.caption(f"Source: {profile_origin(market, profile.name)}")
    if profile.is_experimental:
        st.sidebar.warning(
            "This property type is wired end to end but has not been checked "
            "against a real run. Treat its numbers as provisional."
        )
    return market, profile


# ---------------------------------------------------------------------------
# Managing saved searches
# ---------------------------------------------------------------------------


def profile_editor(market, existing: CompProfile | None) -> None:
    """Create or change a saved search. Same model the CLI wizard writes."""
    editing = existing is not None
    st.subheader("Change this search" if editing else "New search")

    with st.form("profile_form"):
        # A market's own search cannot be renamed here: the name lives in the
        # market definition, and renaming it would leave the original there and
        # a copy under the new name -- two searches where one was asked for.
        owned = editing and existing.name in load_user_profiles(market.name)
        name = st.text_input(
            "Name", value=existing.name if editing else "my-property",
            disabled=editing and not owned,
            help="What you will call this search when you run it again."
            if not editing or owned
            else "This search comes from the market definition, so its name "
                 "lives there. Save it under a new name to get one you own.",
        )
        types = list(PropertyType)
        chosen = st.selectbox(
            "Kind of property",
            types,
            index=types.index(existing.property_type) if editing else 0,
            format_func=lambda t: t.value.replace("_", " ")
            + ("" if t is PropertyType.VACANT_LAND else "  (experimental)"),
        )
        improved = chosen.is_improved
        metric = Denominator.LIVING_SQFT if improved else Denominator.LOT_SQFT
        st.caption(
            f"Compared on {'living area' if improved else 'lot size'}, per square foot."
        )

        prior = existing.subject if editing else Subject()
        label = st.text_input("What to call your property", value=prior.label)
        columns = st.columns(2)
        with columns[0]:
            if improved:
                living = st.number_input(
                    "Living area (sq ft)", min_value=0.0,
                    value=float(prior.living_sqft or 1500), step=50.0,
                )
                lot = st.number_input(
                    "Lot size (sq ft), optional", min_value=0.0,
                    value=float(prior.lot_sqft or 0), step=100.0,
                )
            else:
                lot = st.number_input(
                    "Lot size (sq ft)", min_value=0.0,
                    value=float(prior.lot_sqft or 6000), step=100.0,
                )
                living = 0.0
        with columns[1]:
            beds = st.number_input(
                "Bedrooms", min_value=0.0, value=float(prior.beds or 0), step=1.0
            ) if improved else 0.0
            baths = st.number_input(
                "Bathrooms", min_value=0.0, value=float(prior.baths or 0), step=0.5
            ) if improved else 0.0

        st.markdown("**Which sales anchor the headline estimate**")
        st.caption(
            "This filters nothing. Every sale found stays in your data and in the "
            "workbook, and the market-wide figures always use all of them. It only "
            "chooses which sales drive the *primary* estimate — the one that corrects "
            "for smaller properties selling at a higher rate per square foot. Widen it "
            "and that estimate drifts toward the market average."
        )
        base_bracket = existing.similar_bracket if editing else SimilarBracket()
        size_now = living if improved else lot
        size_word = "living area" if improved else "lot size"
        tolerance = st.slider(
            f"Anchor on sales within this percentage of your {size_word}", 5, 60,
            int(base_bracket.tolerance * 100), step=5, format="%d%%",
        ) / 100.0
        # A worked example rather than a live one: this is inside a form, so a
        # caption computed from the slider would show the previous value until
        # the form is submitted, which is worse than no caption at all.
        if size_now:
            saved_pct = base_bracket.tolerance
            st.caption(
                f"A percentage, not square feet. At {size_now:,.0f} sq ft, "
                f"±{saved_pct * 100:.0f}% anchors on sales from "
                f"{size_now * (1 - saved_pct):,.0f} to {size_now * (1 + saved_pct):,.0f} sq ft."
            )
        pin = st.checkbox(
            "Use an exact size range instead", value=bool(base_bracket.explicit_range)
        )
        pin_columns = st.columns(2)
        with pin_columns[0]:
            pin_low = st.number_input(
                "Anchor from (sq ft)", min_value=0.0, step=100.0,
                value=float((base_bracket.explicit_range or (size_now * 0.75, 0))[0]),
            )
        with pin_columns[1]:
            pin_high = st.number_input(
                "Anchor to (sq ft)", min_value=0.0, step=100.0,
                value=float((base_bracket.explicit_range or (0, size_now * 1.25))[1]),
            )

        st.markdown("**Window and exclusions**")
        window = st.number_input(
            "Days of sales to look back over", min_value=7, max_value=730,
            value=existing.window_days if editing else 90, step=1,
        )
        base_ex = existing.exclusions if editing else Exclusions()
        acres = st.number_input(
            "Ignore parcels bigger than (acres, 0 for no limit)",
            min_value=0.0, step=0.25,
            value=float((base_ex.max_lot_sqft or 0) / 43560),
        )
        core_columns = st.columns(2)
        with core_columns[0]:
            core_low = st.number_input(
                "Core view: ignore below $/sq ft", min_value=0.0, step=5.0,
                value=float(base_ex.core_min_ppsf or 0),
            )
        with core_columns[1]:
            core_high = st.number_input(
                "Core view: ignore above $/sq ft", min_value=0.0, step=5.0,
                value=float(base_ex.core_max_ppsf or 0),
            )
        st.caption(
            "The core view reports a second set of figures with the extremes set "
            "aside. Nothing is deleted from your data either way."
        )

        submitted = st.form_submit_button("Save", type="primary")

    if not submitted:
        return

    subject = Subject(label=label)
    if improved:
        subject.living_sqft = living or None
        subject.lot_sqft = lot or None
        subject.beds = beds or None
        subject.baths = baths or None
    else:
        subject.lot_sqft = lot or None

    profile = CompProfile(
        name=name,
        property_type=chosen,
        metric=metric,
        identification=Identification(
            requires_absent_bed_bath=not improved,
            type_labels=["LOT", "LAND"] if not improved else ["SINGLE_FAMILY", "HOUSE"],
            exclude_habitable_structure=not improved,
            unit_suffix_is_significant=chosen
            in (PropertyType.CONDO_TOWNHOME, PropertyType.MULTI_FAMILY),
        ),
        similar_bracket=SimilarBracket(
            attribute=metric,
            tolerance=tolerance,
            explicit_range=(pin_low, pin_high) if pin else None,
        ),
        exclusions=Exclusions(
            max_lot_sqft=(acres * 43560) if acres else None,
            explicit_address_keys=list(base_ex.explicit_address_keys),
            core_min_ppsf=core_low or None,
            core_max_ppsf=core_high or None,
        ),
        subject=subject,
        attributes=["beds", "baths", "year_built"] if improved else [],
        window_days=int(window),
    )

    problems = profile.validate()
    if problems:
        for problem in problems:
            st.error(problem)
        return

    if editing and existing.name != name:
        # Check the name is free BEFORE moving anything. The archive move and
        # the profile rekey are two steps and cannot be made atomic, so the one
        # that can fail has to fail first: moving the runs and then discovering
        # the name was taken left the old search with no history and filed its
        # runs under somebody else's, while telling the user it had failed.
        if name in available_profiles(market):
            st.error(f"Could not rename: a search named “{name}” already exists.")
            return
        try:
            moved = history_mod.rename_profile_runs(
                market.data_dir(), market.name, existing.name, name
            )
            renamed = rename_user_profile(market.name, existing.name, name)
        except (ValueError, FileExistsError, OSError) as exc:
            st.error(f"Could not rename: {exc}")
            return
        if renamed:
            st.caption(
                f"Renamed from “{existing.name}”"
                + (f", and moved {moved} past run(s) with it." if moved else ".")
            )

    path = save_user_profile(market.name, profile)
    st.success(f"Saved “{name}”.")
    st.caption(f"Written to {path}")
    # Not `st.session_state.profile_name` directly: the sidebar's saved-search
    # selectbox owns that key and was drawn earlier in this same run, and
    # Streamlit refuses a write to a widget's key after the widget exists.
    # Leave the choice here and let the sidebar apply it before it draws.
    st.session_state["_select_profile"] = name
    st.session_state.pop("editing_profile", None)
    st.rerun()


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _can_research(market) -> bool:
    """Whether this market has a source a live run could actually read."""
    try:
        return any(s.enabled and s.config.get("embedded") for s in market.sources())
    except Exception:
        # A market that cannot describe its sources cannot be researched.
        return False


def confirmation_panel() -> bool:
    """The run stopped to ask. Returns True when it is waiting on an answer.

    Rendered above the tabs so that whatever the page is waiting on is the
    first thing on it, but deliberately plain: this is a question with two
    answers, not an alarm.
    """
    pending = st.session_state.get("pending_plan")
    if not pending:
        return False

    st.warning(md(pending))
    st.caption(
        "The index has been read already and cost nothing. This is the part "
        "that spends, and the number of properties came from the site rather "
        "than from you — so it is worth seeing before it happens."
    )
    columns = st.columns([1, 1, 3])
    if columns[0].button("Go ahead", type="primary"):
        st.session_state["lookups_ok"] = True
        st.session_state.pop("pending_plan", None)
        st.session_state["run_now"] = True
        st.rerun()
    if columns[1].button("Stop"):
        st.session_state.pop("pending_plan", None)
        st.rerun()
    return True


def run_panel(market, profile: CompProfile) -> None:
    st.subheader("Ask this search again")

    # There is no choice of data source here on purpose. A market that can be
    # researched is researched; one that cannot replays its recording. Offering
    # both as a toggle invited comparing a live run against a recording made
    # months earlier -- different sales, different dates, different agents --
    # and reading the differences as though the tool were unstable.
    live = _can_research(market)

    if live:
        columns = st.columns([1, 1])
        with columns[0]:
            via = st.radio(
                "Who pays for the reading",
                ["subscription", "api"],
                format_func=lambda v: "My Claude subscription" if v == "subscription"
                else "Metered API account",
            )
        with columns[1]:
            cap = st.number_input(
                "Limit property lookups (0 for no limit)", min_value=0, value=0, step=5,
                help="Nearly all the cost is per-property lookups. A small cap is a "
                     "cheap way to check the sources still work.",
            )
        _explain_cost(via, cap)
    else:
        via, cap = "subscription", 0
        st.caption(
            "This market has no live sources configured, so a run replays its "
            "recorded data. To see what an earlier run found instead, open it "
            "under “Past runs”."
        )

    window_end = st.date_input(
        "Treat this date as today",
        value=date.today(),
        help="Set a past date to reproduce an earlier run exactly.",
    )

    go = st.button("Run", type="primary") or st.session_state.pop("run_now", False)
    if not go:
        return

    with st.status("Working…", expanded=True) as status:
        try:
            if live:
                from recomps.research.live import build_live_researcher
                from recomps.research.llm import Budget

                st.write("Setting up.")
                researcher = build_live_researcher(
                    str(Path(market.data_dir()) / "http-cache"),
                    via=via,
                    budget=Budget() if via == "api" else None,
                    max_lookups=int(cap) or None,
                    # None means "stop and ask". The run raises with its plan,
                    # this panel shows it, and a second attempt carries True.
                    # Asking again costs nothing: the index page is cached, so
                    # the retry reaches the same point without a new request.
                    confirm=True if st.session_state.pop("lookups_ok", False) else None,
                )
                if not researcher.extractor.available:
                    status.update(label="Cannot research", state="error")
                    st.error(
                        "The `claude` command is not signed in. Run `claude auth "
                        "login` in a terminal, or choose the metered API account "
                        "with ANTHROPIC_API_KEY set."
                        if via == "subscription"
                        else "Set ANTHROPIC_API_KEY to use the metered API."
                    )
                    return
                st.write("Reading the index, then each property. Pages are fetched "
                         "one at a time per site, so this takes a few minutes.")
            else:
                researcher = FixtureResearcher()
                st.write("Reading recorded data.")

            result = run_pipeline(
                market, profile, researcher, window_end=window_end
            )
            _archive(market, result)
            status.update(label="Done", state="complete")
        except LookupsNeedConfirmation as plan_needed:
            status.update(label="Waiting for you", state="complete")
            st.session_state["pending_plan"] = plan_needed.plan.describe()
            st.rerun()
        except Exception as exc:
            status.update(label="Failed", state="error")
            st.exception(exc)
            return
        finally:
            closer = getattr(getattr(researcher, "fetcher", None), "close", None)
            if closer:
                closer()

    current = view()
    current.origin = "fresh"
    current.result = result
    current.stored = None
    current.record = None
    current.adjustments.clear()
    st.rerun()


def _explain_cost(via: str, cap: int) -> None:
    if via == "subscription":
        from recomps.research.claude_code import subscription_status

        status = subscription_status()
        if status.get("loggedIn"):
            plan = status.get("subscriptionType", "your")
            st.info(
                f"Reading will use your **{plan} subscription** "
                f"({status.get('email', 'signed in')}). Usage counts against that "
                "plan; you are not billed per page."
            )
        else:
            st.warning(
                "Not signed in to a Claude subscription. Run `claude auth login` "
                "in a terminal first."
            )
    else:
        estimate = (cap or 40) * 0.04
        st.warning(md(
            f"Reading will be **billed to your API account**, roughly "
            f"${estimate:,.2f} for {cap or 40} property lookups. The index itself "
            "costs nothing."
        ))


def _archive(market, result) -> None:
    directory = history_mod.run_dir(
        market.data_dir(), market.name, result.profile.name, result.run_at
    )
    directory.mkdir(parents=True, exist_ok=True)
    build_snapshot(result).write(directory / history_mod.SNAPSHOT_NAME)
    build_workbook(result).save(directory / history_mod.WORKBOOK_NAME)
    methodology.write(result, directory / history_mod.METHODOLOGY_NAME)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def _run_summary(path: str, fingerprint: float) -> dict:
    """The few facts that tell one archived run from another.

    Cached on the file's own timestamp: an archive is immutable once written,
    and the history list is rebuilt on every interaction.
    """
    import json

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "researcher": (payload.get("run") or {}).get("researcher", ""),
        "sold": len(payload.get("sold") or []),
        "active": len(payload.get("active") or []),
    }


def _run_source(record) -> str:
    """Which researcher produced an archived run, or "" when unknown."""
    if record.snapshot is None:
        return ""
    try:
        summary = _run_summary(str(record.snapshot), record.snapshot.stat().st_mtime)
    except OSError:
        return ""
    return summary.get("researcher", "")


def _describe_run(record) -> str:
    """A past run, named so it can be told apart from its neighbours.

    Time alone is not enough: a day of experimenting leaves several runs an
    hour apart holding different numbers of sales, and choosing between them by
    timestamp is guessing. Where the data came from is deliberately absent --
    every run a market lists came from the same place, so saying so on each row
    is noise.
    """
    if record.snapshot is None:
        return f"{record.label}  (no data)"
    try:
        summary = _run_summary(str(record.snapshot), record.snapshot.stat().st_mtime)
    except OSError:
        summary = {}
    if not summary:
        return record.label
    return f"{record.label}  ·  {summary['sold']} sold, {summary['active']} listed"


def history_panel(market, profile: CompProfile) -> None:
    st.subheader("Open a past run")
    records = history_mod.list_runs(market.data_dir(), market.name, profile.name)
    if not records:
        st.info(
            "No saved runs of this search yet. Every run you do is kept here, "
            "with its data and its spreadsheet."
        )
        return

    # A market that researches the web should not offer replays of a recording
    # beside real runs: they hold different sales over different dates, and
    # picking between them by timestamp is how a reader ends up comparing a
    # July snapshot against yesterday's market. Hidden, not deleted -- the
    # August recording is the reference the golden numbers come from.
    # A market that researches the web shows only runs that did. Replays of the
    # frozen test recording hold different sales over different dates, and
    # offering them beside real runs -- even behind a checkbox -- is the same
    # trap as the data-source toggle: it invites comparing July's recording
    # against yesterday's market and reading the difference as instability.
    # They stay on disk, where the tests and the golden numbers need them.
    if _can_research(market):
        from_web = [r for r in records if _run_source(r) != "fixture"]
        if from_web:
            records = from_web
        else:
            st.info(
                "No runs from the web yet for this search. Run it above and the "
                "result is kept here."
            )
            return

    # Indexed, not keyed on the label: two runs a minute apart share a label,
    # and a dict silently kept only the last of them.
    options = list(range(len(records)))
    chosen = st.selectbox(
        "Which run",
        options,
        format_func=lambda i: _describe_run(records[i]),
        key="history_choice",
    )
    record = records[chosen]

    columns = st.columns([1, 1, 2])
    with columns[0]:
        if st.button("Open", type="primary"):
            current = view()
            try:
                current.stored = ui_state.open_saved(record)
            except Exception as exc:
                st.error(str(exc))
                return
            current.origin = "saved"
            current.record = record
            current.result = None
            current.adjustments.clear()
            st.rerun()
    with columns[1]:
        if record.workbook:
            st.download_button(
                "That day's spreadsheet",
                record.workbook.read_bytes(),
                file_name=f"{market.name}_{profile.name}_{record.local_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                help="The file exactly as it was written that day.",
            )

    if len(records) > 1:
        st.markdown("**Compare two runs**")
        pair = st.columns(2)
        with pair[0]:
            earlier = st.selectbox(
                "Earlier", options, index=1,
                format_func=lambda i: _describe_run(records[i]), key="cmp_a",
            )
        with pair[1]:
            later = st.selectbox(
                "Later", options, index=0,
                format_func=lambda i: _describe_run(records[i]), key="cmp_b",
            )
        if st.button("Show what changed"):
            _show_comparison(records[earlier], records[later])


def _show_comparison(earlier, later) -> None:
    from recomps.model.snapshot import Snapshot

    try:
        report = compare_mod.compare(
            Snapshot.load(str(earlier.snapshot)), Snapshot.load(str(later.snapshot))
        )
    except Exception as exc:
        st.error(str(exc))
        return

    columns = st.columns(4)
    for column, (label, items) in zip(
        columns,
        [
            ("New sales", report.new_sales),
            ("Listings that closed", report.newly_closed),
            ("New listings", report.new_active),
            ("Price cuts", report.price_cuts),
        ],
        strict=False,
    ):
        column.metric(label, len(items))

    rows = []
    for delta in report.deltas:
        rows.append(
            {
                "Figure": delta.label,
                "Before": delta.before,
                "After": delta.after,
                "Change": delta.pct_change,
            }
        )
    st.dataframe(
        rows, hide_index=True, width="stretch",
        column_config={"Change": st.column_config.NumberColumn(format="%.1f%%")},
    )
    if report.new_sales:
        st.caption("New sales: " + ", ".join(report.new_sales[:12]))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


#: A run archived within this many minutes is recent enough that somebody is
#: probably still looking for it.
RECENT_RUN_MINUTES = 60


def _offer_a_finished_run(market, profile: CompProfile) -> None:
    """Point at a run that finished but is not on screen.

    A run writes its snapshot and workbook to the archive before the interface
    displays anything, so the two can come apart: a long live run that loses
    its browser session completes on the server and leaves a finished archive
    nobody is shown. From the screen it is indistinguishable from a run that
    never happened, which is the worst way for a tool to be wrong -- the work
    is done and paid for and the reader is told nothing.
    """
    from datetime import UTC, datetime, timedelta

    from recomps.clock import to_local

    try:
        records = history_mod.list_runs(
            market.data_dir(), market.name, profile.name
        )
    except Exception:
        return
    if not records:
        return
    newest = records[0]
    cutoff = datetime.now(UTC) - timedelta(minutes=RECENT_RUN_MINUTES)
    if newest.run_at < cutoff or newest.snapshot is None:
        return

    st.info(
        f"A run of this search finished at {to_local(newest.run_at):%H:%M} and is "
        "saved, but is not on screen — a long run can complete after its page "
        "has gone. Nothing needs re-running."
    )
    if st.button(f"Show the run from {newest.label}", type="primary"):
        current = view()
        try:
            current.stored = ui_state.open_saved(newest)
        except Exception as exc:
            st.error(str(exc))
            return
        current.origin = "saved"
        current.record = newest
        current.result = None
        current.adjustments.clear()
        st.rerun()


def results_panel(market, profile: CompProfile) -> None:
    current = view()
    if current.result is None and current.stored is None:
        _offer_a_finished_run(market, profile)
        return

    st.divider()
    st.subheader("Results")
    st.caption(current.provenance)
    span = ui_state.span_note(current)
    if span:
        st.caption(md(span))

    figures = ui_state.headline(current)
    columns = st.columns(6)
    columns[0].metric("Sold comps", figures.get("sold_count", 0))
    columns[1].metric("Median $/sq ft", rate(figures.get("median_ppsf")))
    columns[2].metric(
        "Estimated value", money(figures.get("valuation")),
        help=f"From {figures.get('bracket_count', 0)} similar-size sales.",
    )
    # Both list prices, because the recommended one is deliberately under the
    # estimate and looks like an error beside it without its sibling.
    columns[3].metric(
        "Priced to compete", money(figures.get("suggested_list")),
        help="Strategy A: just under a search-band edge, to be found by the "
             "band below and bid up. Deliberately under the estimate — how far "
             "under depends on where the estimate sits inside its band.",
    )
    columns[4].metric(
        "Priced at market", money(figures.get("at_market")),
        help="Strategy B: a straightforward ask near the estimate. Moderate "
             "market time.",
    )
    columns[5].metric("Walk-away floor", money(figures.get("floor")))

    for note in ui_state.coverage_notes(current):
        st.warning(md(note))
    for warning in figures.get("warnings") or []:
        st.warning(md(warning))

    st.caption(
        "Market research, not an appraisal. Decision aids for a conversation "
        "with an agent."
    )

    _excluded(current)
    _core_view(current)
    _size_bands(current)
    _ladder(current)
    _areas(current, market)
    _agents(current)
    _comps_table(market, profile, current)
    _active_table(current)
    _downloads(market, profile, current)


def _excluded(current: ui_state.Viewing) -> None:
    """Say which parcels the search's rules removed, and why.

    Without this the interface breaks the rule the rest of the project keeps:
    a run that quietly drops two parcels reports a smaller, tidier market that
    reads exactly like a real one. The counts above are the ones this explains,
    so it sits directly beneath them.
    """
    rows = ui_state.exclusion_rows(current)
    if not rows:
        return
    with st.expander(f"{len(rows)} parcel(s) excluded by this search's rules"):
        st.caption(
            "Removed before every figure above — they are in none of the counts, "
            "medians, or the workbook. Change these rules under “Window and "
            "exclusions” in the search settings."
        )
        st.dataframe(
            [{"Parcel": r["address"], "Why it was excluded": r["reason"]} for r in rows],
            hide_index=True,
            width="stretch",
        )


def _core_view(current: ui_state.Viewing) -> None:
    """The same market read with extreme rates set aside.

    The two bounds in the search settings produced this and nothing else, and
    until now it appeared only in the methodology document -- so the controls
    looked inert from the one place someone would set them.
    """
    figures = ui_state.core_figures(current)
    if not figures:
        _predates(current, "the core view")
        return
    low, high = figures["low"], figures["high"]
    span = md(f"{rate(low)} to {rate(high)} per sq ft")
    with st.expander(f"Core view — {span}"):
        st.caption(
            "Nothing is removed. These are the same sales read again with the "
            "extremes set aside, so you can see how much the outliers move the "
            "headline figures above."
        )
        columns = st.columns(3)
        columns[0].metric(
            "Sold inside the bounds",
            f"{figures['sold_count']} of {figures['sold_total']}",
        )
        columns[1].metric("Core median $/sq ft", rate(figures["sold_median_ppsf"]))
        columns[2].metric("Core average $/sq ft", rate(figures["sold_avg_ppsf"]))
        if figures["active_total"]:
            st.caption(md(
                f"Active listings: {figures['active_count']} of "
                f"{figures['active_total']} inside the bounds, core median "
                f"{rate(figures['active_median_ppsf'])}."
            ))


def _size_bands(current: ui_state.Viewing) -> None:
    """Rate by size band -- the evidence for pricing against similar sizes.

    Shown next to the valuation rather than buried with the other tables,
    because it is what tells the reader whether the size premium the primary
    basis assumes is actually present in their market.
    """
    rows, notes = ui_state.size_band_rows(current)
    if not rows:
        _predates(current, "the rate-by-size table")
        return
    with st.expander("Rate by size", expanded=True):
        st.dataframe(
            [
                {
                    "Size band": r["label"] + ("  ← your property" if r["holds_subject"] else ""),
                    "Sold": r["count"],
                    "Median $/sq ft": r["median_ppsf"],
                    "Avg $/sq ft": r["avg_ppsf"],
                    "Median size": r["median_size"],
                    "Median price": r["median_price"],
                }
                for r in rows
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "Median $/sq ft": st.column_config.NumberColumn(format="$%.2f"),
                "Avg $/sq ft": st.column_config.NumberColumn(format="$%.2f"),
                "Median size": st.column_config.NumberColumn(format="%,d"),
                "Median price": st.column_config.NumberColumn(format="$%,d"),
            },
        )
        for note in notes:
            st.caption(md(note))
        st.caption(
            "Bands hold equal numbers of sales rather than equal size ranges, so every "
            "row's median rests on a comparable sample. Nothing here filters your data — "
            "every sale above is in the dataset and in the workbook."
        )


#: Highlight colours for shortlisted agents, so a row in the agent table and
#: that agent's sales in the comps table can be traced to each other by eye.
#: Backgrounds are pale and the text colour is set with them, because the page
#: renders in whichever theme the viewer has and an unset foreground goes
#: invisible on one of them. Colour is never the only cue: the agent's name is
#: in both tables regardless.
HIGHLIGHTS = ["#1F4E79", "#7B2D26", "#2D6A4F", "#5B3A82", "#8A5300", "#1F6F78"]
HIGHLIGHT_TEXT = "#FFFFFF"


def _shortlist_colors(analysis: dict | None) -> dict[str, str]:
    """One colour per shortlisted agent, in the order the table lists them."""
    if not analysis:
        return {}
    names = [a["agent"] for a in analysis.get("agents") or [] if a.get("flag") == "shortlist"]
    return {name: HIGHLIGHTS[i] for i, name in enumerate(names[: len(HIGHLIGHTS)])}


def _tint(colors: dict[str, str], agent: object) -> str:
    color = colors.get(agent) if isinstance(agent, str) else None
    return f"background-color: {color}; color: {HIGHLIGHT_TEXT};" if color else ""


def _styled(rows: list[dict], colors: dict[str, str], agent_column: str):
    """A frame whose rows carry their agent's highlight, ready for Streamlit.

    Returned as a Styler rather than a list of dicts because that is the only
    thing Streamlit will colour -- and in `st.data_editor` it colours exactly
    the non-editable columns, which is every column but the tick box.
    """
    frame = pd.DataFrame(rows)
    if not colors or agent_column not in frame.columns:
        return frame
    return frame.style.apply(
        lambda row: [_tint(colors, row[agent_column])] * len(row), axis=1
    )


def _unticked(edited) -> set[str]:
    """Addresses the user unticked, whatever shape the editor handed back.

    Worth its own function: passing a Styler in makes `st.data_editor` return a
    DataFrame, and iterating a DataFrame walks its *column names*. The bug that
    would cause is silent -- every sale stays in, and the figures simply never
    move.
    """
    if isinstance(edited, pd.DataFrame):
        if edited.empty:
            return set()
        return set(edited.loc[~edited["Include"].astype(bool), "Address"])
    return {row["Address"] for row in edited if not row["Include"]}


def _predates(current: ui_state.Viewing, what: str) -> None:
    """Explain an absent panel on a run archived before that panel existed.

    A section that simply is not there reads as a broken feature -- it did
    exactly that to one reader. A saved run holds what it held on the day; the
    honest answer is to say so and point at the fix, not to render nothing.
    Silence is right only for a fresh run, where absent means genuinely absent.
    """
    if current.result is not None or current.stored is None:
        return
    st.caption(f"This saved run predates {what}. Run the search again to see it.")


def _ladder(current: ui_state.Viewing) -> None:
    """How the estimate moves as "similar size" is drawn wider.

    Sits under the size bands because it answers the next question those raise:
    the bands show that size moves the rate, and this shows what that costs you
    in the one figure you are going to act on.
    """
    rows, notes = ui_state.ladder_rows(current)
    if not rows:
        _predates(current, "the widening-bracket table")
        return
    with st.expander("How wide is “similar”?", expanded=False):
        st.caption(
            "The same sales, read at widening size ranges around your property. "
            "Every row is a correct answer to a slightly different question — a "
            "tighter range is more relevant and thinner, a wider one is better "
            "evidenced and more diluted."
        )
        st.dataframe(
            [
                {
                    "Size range": (
                        r["label"]
                        + ("   ← the figures above" if r["is_profile_bracket"] else "")
                        + ("   (too thin to lead on)" if r["is_thin"] else "")
                    ),
                    "Sold": r["count"],
                    "Median $/sq ft": r["median_ppsf"],
                    "Avg $/sq ft": r["avg_ppsf"],
                    "Value at median": r["value_from_median"],
                    "Value at average": r["value_from_avg"],
                }
                for r in rows
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "Median $/sq ft": st.column_config.NumberColumn(format="$%.2f"),
                "Avg $/sq ft": st.column_config.NumberColumn(format="$%.2f"),
                "Value at median": st.column_config.NumberColumn(format="$%,d"),
                "Value at average": st.column_config.NumberColumn(
                    format="$%,d",
                    help="The convention the headline estimate uses: the average "
                         "rate of the sales inside the range, times your size.",
                ),
            },
        )
        for note in notes:
            st.caption(md(note))


def _areas(current: ui_state.Viewing, market=None) -> None:
    """Rate and size by quadrant.

    Size sits beside rate because a quadrant can carry higher absolute prices
    *and* a lower $/sqft purely because its parcels are larger. Without the
    size column a naive rate comparison inverts the real premium.
    """
    rows, notes = ui_state.area_rows(current)
    rows = [r for r in rows if r.get("sold_count") or r.get("active_count")]
    if not rows:
        return
    with st.expander("Rate by area", expanded=False):
        st.dataframe(
            [
                {
                    "Area": r["area"],
                    "Sold": r["sold_count"],
                    "Median $/sq ft": r["sold_median_ppsf"],
                    "Avg $/sq ft": r["sold_avg_ppsf"],
                    "Avg size": r["sold_avg_size"],
                    "Median price": r["sold_median_price"],
                    "Active": r["active_count"],
                    "Active avg $/sq ft": r["active_avg_ppsf"],
                }
                for r in rows
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "Median $/sq ft": st.column_config.NumberColumn(format="$%.2f"),
                "Avg $/sq ft": st.column_config.NumberColumn(format="$%.2f"),
                "Active avg $/sq ft": st.column_config.NumberColumn(format="$%.2f"),
                "Avg size": st.column_config.NumberColumn(
                    format="%,d",
                    help="Read the rate beside this. A quadrant of larger parcels "
                         "shows a lower $/sq ft without being cheaper land.",
                ),
                "Median price": st.column_config.NumberColumn(format="$%,d"),
            },
        )
        for note in notes:
            st.caption(md(note))
        lines = ui_state.divider_lines(market) if market is not None else []
        if lines:
            st.caption("**Where the quadrants divide**")
            for line in lines:
                st.caption(md(line))


def _active_table(current: ui_state.Viewing) -> None:
    """What is on the market now.

    Its own table rather than a status column on the sold comps: an asking
    price is a claim and a sale is a fact, and one table invites reading the
    first as the second.
    """
    rows = ui_state.active_rows(current)
    if not rows:
        _predates(current, "the listings table")
        return
    colors = _shortlist_colors(ui_state.agent_analysis(current))
    rounded = sum(1 for r in rows if r["lot_size_is_rounded"])
    sized = sum(1 for r in rows if r["lot_sqft"])

    st.markdown("**On the market now**")
    st.caption(
        "Asking prices, not transactions. These set the active-listing "
        "valuation basis and show what your property would be competing with."
    )
    table = [
        {
            "Address": r["address"],
            "Asking": r["list_price"],
            "Lot sq ft": r["lot_sqft"],
            "$/sq ft": (
                r["list_price"] / r["lot_sqft"]
                if r["list_price"] and r["lot_sqft"] else None
            ),
            "Brokerage": r["brokerage"] or "—",
            "Agent": r["agent"] or "—",
            "Area": r["area"] or "—",
        }
        for r in rows
    ]
    st.dataframe(
        _styled(table, colors, "Agent"),
        hide_index=True,
        width="stretch",
        column_config={
            "Asking": st.column_config.NumberColumn(format="$%,d"),
            "Lot sq ft": st.column_config.NumberColumn(format="%,d"),
            "$/sq ft": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    if sized < len(rows):
        st.caption(md(
            f"{len(rows) - sized} of {len(rows)} listings publish no lot size, so they "
            "carry no $/sq ft. They are counted, not dropped."
        ))
    if rounded:
        st.caption(md(
            f"{rounded} lot size(s) come from acreage rounded to two decimals — worth "
            "about ±4% of $/sq ft on a small parcel. Do not read them as exact."
        ))


def _agents(current: ui_state.Viewing) -> None:
    """Who is selling this market, and how their sales landed against ask.

    The caveats are rendered with the table rather than under a link, because
    the table is a shortlist to interview and reads like a ranking. One to three
    sales per agent is not a performance measure, and a high sold-to-ask ratio
    can reflect a deliberately low list price as much as skill.
    """
    analysis = ui_state.agent_analysis(current)
    if not analysis or not analysis.get("agents"):
        return

    attributed = analysis.get("attributed") or 0
    total = analysis.get("total") or 0
    with st.expander(f"Agents — {attributed} of {total} sales attributed", expanded=False):
        colors = _shortlist_colors(analysis)
        rows = [
            {
                "Agent": row["agent"],
                "Brokerage": row["brokerage"] or "—",
                "Closings": row["closings"],
                # Signed distance from ask reads directly; a ratio of 1.007
                # makes the reader do the subtraction.
                "vs asking": (
                    None if row["avg_sold_to_ask"] is None
                    else (row["avg_sold_to_ask"] - 1.0) * 100.0
                ),
                "$/sq ft": row.get("avg_ppsf"),
                "Avg size": row.get("avg_size"),
                "Live": row.get("active_listings") or 0,
                "Pattern": row["flag"] or "",
                "Both sides": "yes" if row["dual_agency"] else "",
            }
            for row in analysis["agents"]
        ]
        # A run archived before these figures existed carries no rate at all.
        # An empty column reads as broken; say what it is instead.
        if not any(r["$/sq ft"] is not None for r in rows):
            for row in rows:
                row.pop("$/sq ft")
                row.pop("Avg size")
            st.caption(md(
                "This saved run predates the $/sq ft and size columns. Run the search "
                "again to see them."
            ))

        st.dataframe(
            _styled(rows, colors, "Agent"),
            hide_index=True,
            width="stretch",
            column_config={
                "vs asking": st.column_config.NumberColumn(
                    format="%+.1f%%",
                    help="Average close against the final asking price, across that "
                         "agent's sales here.",
                ),
                "$/sq ft": st.column_config.NumberColumn(
                    format="$%.2f",
                    help="The rate this agent's sales actually achieved. Beating a low "
                         "ask is not the same as getting a good price — this is the "
                         "column that tells them apart.",
                ),
                "Avg size": st.column_config.NumberColumn(
                    format="%,d",
                    help="Read the rate beside it: an agent working smaller parcels "
                         "shows a higher $/sq ft without being a better agent.",
                ),
                "Live": st.column_config.NumberColumn(
                    format="%,d",
                    help="Listings this agent holds right now. Closings are history; "
                         "this is who is working the market today.",
                ),
                "Pattern": st.column_config.TextColumn(
                    help="A generic pattern in the numbers, not a judgement about a "
                         "person. 'shortlist': repeat closings at or above ask. "
                         "'caution': a listing cut from its original ask that still "
                         "closed below the reduced one.",
                ),
            },
        )
        if colors:
            st.caption(
                "Shortlisted agents are tinted, and their sales carry the same tint "
                "in the table below."
            )

        brokerages = analysis.get("brokerages") or []
        if brokerages:
            st.caption("By brokerage")
            st.dataframe(
                [
                    {
                        "Brokerage": row["family"],
                        "Closings": row["closings"],
                        "Share of attributed": row["share"],
                        "Live listings": row.get("active_listings") or 0,
                    }
                    for row in brokerages
                ],
                hide_index=True,
                width="stretch",
                column_config={
                    "Share of attributed": st.column_config.NumberColumn(format="percent"),
                    "Live listings": st.column_config.NumberColumn(
                        format="%,d",
                        help="Listings this firm holds right now.",
                    ),
                },
            )

        for caveat in analysis.get("caveats") or []:
            st.caption(md(caveat))


def _comps_table(market, profile: CompProfile, current: ui_state.Viewing) -> None:
    rows = ui_state.comp_rows(current)
    if not rows:
        return

    st.markdown("**The comparable sales**")
    if current.origin == "saved":
        st.caption(
            "Untick a sale you do not think is comparable, or change your "
            "property's size below, and the figures above recompute from these "
            "same sales. Nothing is re-fetched and nothing is lost."
        )

    excluded = current.adjustments.excluded
    table = [
        {
            "Include": row["address"] not in excluded,
            "Address": row["address"],
            "Sold": row["sold_date"],
            "Price": row["sold_price"],
            "Lot sq ft": row["lot_sqft"],
            "$/sq ft": (
                row["sold_price"] / row["lot_sqft"]
                if row["sold_price"] and row["lot_sqft"] else None
            ),
            "Brokerage": row["brokerage"] or "—",
            "Agent": row["agent"] or "—",
            "Area": row["area"] or "—",
        }
        for row in rows
    ]

    editable = current.origin == "saved"
    colors = _shortlist_colors(ui_state.agent_analysis(current))
    edited = st.data_editor(
        _styled(table, colors, "Agent"),
        hide_index=True,
        width="stretch",
        disabled=[c for c in table[0] if c != "Include"] if editable else True,
        column_config={
            "Include": st.column_config.CheckboxColumn(
                "Use", help="Untick to leave this sale out of the figures."
            ),
            "Price": st.column_config.NumberColumn(format="$%,d"),
            "Lot sq ft": st.column_config.NumberColumn(format="%,d"),
            "$/sq ft": st.column_config.NumberColumn(format="$%.2f"),
        },
        key="comps_editor",
    )

    if not editable:
        return

    dropped = _unticked(edited)
    size_now = current.adjustments.subject_size or profile.subject_size() or 0.0
    new_size = st.number_input(
        f"Your property's size ({profile.metric.value.replace('_', ' ')})",
        min_value=0.0, value=float(size_now), step=100.0,
        help="Try a different size and the valuation moves. The sales do not.",
    )

    changed = dropped != excluded or abs(new_size - size_now) > 0.5
    if changed and st.button("Recompute with these changes", type="primary"):
        current.adjustments.excluded = dropped
        current.adjustments.subject_size = new_size or None
        try:
            current.result = ui_state.recompute(
                current.record, market, current.adjustments
            )
        except Exception as exc:
            st.error(str(exc))
            return
        st.rerun()

    if current.is_adjusted and st.button("Back to what the run reported"):
        current.adjustments.clear()
        current.result = None
        st.rerun()


def _downloads(market, profile: CompProfile, current: ui_state.Viewing) -> None:
    st.markdown("**Take it away**")
    result = current.result
    if result is None and current.record is not None and not current.is_adjusted:
        st.caption(
            "This run's original spreadsheet is above under “That day's "
            "spreadsheet”. To make a new one with changes, adjust the sales "
            "above and recompute."
        )
        return
    if result is None:
        return

    import io

    buffer = io.BytesIO()
    build_workbook(result).save(buffer)
    suffix = "_adjusted" if current.is_adjusted else ""
    stem = f"{market.name}_{profile.name}{suffix}"

    columns = st.columns(2)
    columns[0].download_button(
        "Spreadsheet", buffer.getvalue(), file_name=f"{stem}_comps.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
    columns[1].download_button(
        "Write-up", methodology.render(result), file_name=f"{stem}_methodology.md",
        mime="text/markdown",
        help="What the run did, in words: sources, rules, counts, caveats.",
    )
    st.caption(
        f"Saved runs live in {history_mod.runs_root(market.data_dir())}"
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def main() -> None:
    market, profile = sidebar()
    if market is None:
        st.title("REComps")
        st.write(
            "Price a property against its comparable sales. Choose a market on "
            "the left to begin."
        )
        return

    st.title(market.description or market.name)

    if st.session_state.get("editing_profile") is not None:
        editing = st.session_state["editing_profile"]
        profile_editor(market, editing if isinstance(editing, CompProfile) else None)
        if st.button("Cancel"):
            st.session_state.pop("editing_profile", None)
            st.rerun()
        return

    # Proportional to the labels: equal columns clip the longest one first,
    # which is how "Edit this search" became "Edit this se...".
    buttons = st.columns([1.2, 1.8, 1.0, 4.0])
    if buttons[0].button("New search"):
        st.session_state["editing_profile"] = None
        st.rerun()
    if profile and buttons[1].button("Edit this search"):
        st.session_state["editing_profile"] = profile
        st.rerun()
    if (
        profile
        and profile.name in load_user_profiles(market.name)
        and buttons[2].button("Delete", help="Saved runs are kept.")
    ):
        delete_user_profile(market.name, profile.name)
        st.rerun()

    if profile is None:
        st.info("Create a search to begin.")
        return

    if confirmation_panel():
        return

    run_tab, history_tab = st.tabs(["Run it", "Past runs"])
    with run_tab:
        run_panel(market, profile)
    with history_tab:
        history_panel(market, profile)

    results_panel(market, profile)


main()
