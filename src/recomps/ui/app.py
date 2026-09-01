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
    load_user_profiles,
    profile_origin,
    save_user_profile,
)
from recomps.model.snapshot import build_snapshot
from recomps.pipeline.run import run_pipeline
from recomps.plugin.loader import discover, load_market
from recomps.reporting import compare as compare_mod
from recomps.reporting import history as history_mod
from recomps.reporting import methodology
from recomps.research.fixture import FixtureResearcher
from recomps.ui import state as ui_state
from recomps.workbook.builder import build_workbook

st.set_page_config(page_title="REComps", page_icon=":house:", layout="wide")

MONEY = "${:,.0f}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def money(value: float | None) -> str:
    return MONEY.format(value) if value is not None else "—"


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


def sidebar() -> tuple[object, CompProfile | None]:
    st.sidebar.title("REComps")

    installed = discover()
    options = [m.name for m in installed] + ["__path__"]
    labels = {m.name: m.description or m.name for m in installed}
    labels["__path__"] = "From a folder…"

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
        )

    try:
        market = market_from_session()
    except Exception as exc:
        st.sidebar.error(str(exc))
        return None, None
    if market is None:
        st.sidebar.info("Choose a market to begin.")
        return None, None

    profiles = available_profiles(market)
    if not profiles:
        st.sidebar.warning("This market has no saved searches yet.")
        return market, None

    names = sorted(profiles)
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
        name = st.text_input(
            "Name", value=existing.name if editing else "my-property",
            disabled=editing,
            help="What you will call this search when you run it again.",
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

        st.markdown("**What counts as comparable**")
        base_bracket = existing.similar_bracket if editing else SimilarBracket()
        tolerance = st.slider(
            "Size tolerance, plus or minus", 5, 60,
            int(base_bracket.tolerance * 100), step=5, format="%d%%",
        ) / 100.0
        pin = st.checkbox(
            "Use exact size limits instead", value=bool(base_bracket.explicit_range)
        )
        pin_columns = st.columns(2)
        size_now = living if improved else lot
        with pin_columns[0]:
            pin_low = st.number_input(
                "Smallest", min_value=0.0, step=100.0,
                value=float((base_bracket.explicit_range or (size_now * 0.75, 0))[0]),
            )
        with pin_columns[1]:
            pin_high = st.number_input(
                "Largest", min_value=0.0, step=100.0,
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

    path = save_user_profile(market.name, profile)
    st.success(f"Saved “{name}”.")
    st.caption(f"Written to {path}")
    st.session_state.profile_name = name
    st.session_state.pop("editing_profile", None)
    st.rerun()


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run_panel(market, profile: CompProfile) -> None:
    st.subheader("Ask this search again")

    columns = st.columns([1, 1, 1])
    with columns[0]:
        source = st.radio(
            "Where the data comes from",
            ["Recorded data", "Research the web now"],
            help="Recorded data replays what a market has on file — instant and "
                 "free. Researching fetches current listings.",
        )
    live = source == "Research the web now"
    with columns[1]:
        via = st.radio(
            "Who pays for the reading",
            ["subscription", "api"],
            format_func=lambda v: "My Claude subscription" if v == "subscription"
            else "Metered API account",
            disabled=not live,
        )
    with columns[2]:
        cap = st.number_input(
            "Limit property lookups (0 for no limit)", min_value=0, value=0, step=5,
            disabled=not live,
            help="Nearly all the cost is per-property lookups. A small cap is a "
                 "cheap way to check the sources still work.",
        )

    if live:
        _explain_cost(via, cap)

    window_end = st.date_input(
        "Treat this date as today",
        value=date.today(),
        help="Set a past date to reproduce an earlier run exactly.",
    )

    if not st.button("Run", type="primary"):
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
        st.warning(
            f"Reading will be **billed to your API account**, roughly "
            f"${estimate:,.2f} for {cap or 40} property lookups. The index itself "
            "costs nothing."
        )


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


def history_panel(market, profile: CompProfile) -> None:
    st.subheader("Open a past run")
    records = history_mod.list_runs(market.data_dir(), market.name, profile.name)
    if not records:
        st.info(
            "No saved runs of this search yet. Every run you do is kept here, "
            "with its data and its spreadsheet."
        )
        return

    labels = {r.label: r for r in records}
    chosen = st.selectbox("Which run", list(labels), key="history_choice")
    record = labels[chosen]

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
                file_name=f"{market.name}_{profile.name}_{record.run_at:%Y-%m-%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                help="The file exactly as it was written that day.",
            )

    if len(records) > 1:
        st.markdown("**Compare two runs**")
        pair = st.columns(2)
        with pair[0]:
            earlier = st.selectbox("Earlier", list(labels), index=1, key="cmp_a")
        with pair[1]:
            later = st.selectbox("Later", list(labels), index=0, key="cmp_b")
        if st.button("Show what changed"):
            _show_comparison(labels[earlier], labels[later])


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


def results_panel(market, profile: CompProfile) -> None:
    current = view()
    if current.result is None and current.stored is None:
        return

    st.divider()
    st.subheader("Results")
    st.caption(current.provenance)

    figures = ui_state.headline(current)
    columns = st.columns(5)
    columns[0].metric("Sold comps", figures.get("sold_count", 0))
    columns[1].metric("Median $/sq ft", rate(figures.get("median_ppsf")))
    columns[2].metric(
        "Estimated value", money(figures.get("valuation")),
        help=f"From {figures.get('bracket_count', 0)} similar-size sales.",
    )
    columns[3].metric("Suggested list", money(figures.get("suggested_list")))
    columns[4].metric("Walk-away floor", money(figures.get("floor")))

    for warning in figures.get("warnings") or []:
        st.warning(warning)

    st.caption(
        "Market research, not an appraisal. Decision aids for a conversation "
        "with an agent."
    )

    _comps_table(market, profile, current)
    _downloads(market, profile, current)


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
    edited = st.data_editor(
        table,
        hide_index=True,
        width="stretch",
        disabled=[c for c in table[0] if c != "Include"] if editable else True,
        column_config={
            "Include": st.column_config.CheckboxColumn(
                "Use", help="Untick to leave this sale out of the figures."
            ),
            "Price": st.column_config.NumberColumn(format="$%d"),
            "Lot sq ft": st.column_config.NumberColumn(format="%d"),
            "$/sq ft": st.column_config.NumberColumn(format="$%.2f"),
        },
        key="comps_editor",
    )

    if not editable:
        return

    dropped = {r["Address"] for r in edited if not r["Include"]}
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

    buttons = st.columns([1, 1, 1, 5])
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

    run_tab, history_tab = st.tabs(["Run it", "Past runs"])
    with run_tab:
        run_panel(market, profile)
    with history_tab:
        history_panel(market, profile)

    results_panel(market, profile)


main()
