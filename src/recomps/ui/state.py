"""What the interface needs to know, kept out of the interface.

Streamlit re-runs the whole script on every interaction, so anything that took
time to produce has to be held deliberately rather than recomputed. Keeping
that bookkeeping here means the page code stays about layout, and the rules
about what is being looked at -- a fresh run, a saved one, an adjusted one --
are testable without a browser.

The distinction the whole interface turns on:

* **A saved run viewed as-is** shows the figures that run reported, read off
  disk. Nothing is recalculated, so what you saw in August is what you see.
* **A saved run with something changed** -- a comp excluded, a different
  subject size -- is recomputed from the rows that run collected. Still no
  network, still the same sales.

Both are always available; the interface just has to be honest about which one
is on screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from recomps.config.profile import CompProfile
from recomps.model.snapshot import Snapshot
from recomps.pipeline import reopen as reopen_mod
from recomps.pipeline.run import RunResult
from recomps.plugin.market import Market
from recomps.reporting import history as history_mod


@dataclass
class Adjustments:
    """What the user has changed about a run they are looking at."""

    excluded: set[str] = field(default_factory=set)
    subject_size: float | None = None

    @property
    def any(self) -> bool:
        return bool(self.excluded) or self.subject_size is not None

    def describe(self) -> str:
        parts = []
        if self.excluded:
            parts.append(f"{len(self.excluded)} comp(s) excluded")
        if self.subject_size:
            parts.append(f"subject size {self.subject_size:,.0f} sq ft")
        return ", ".join(parts)

    def clear(self) -> None:
        self.excluded.clear()
        self.subject_size = None


@dataclass
class Viewing:
    """Whatever is currently on screen, and where it came from."""

    #: "fresh" for a run just performed, "saved" for one opened from history.
    origin: str = "fresh"
    record: history_mod.RunRecord | None = None
    stored: reopen_mod.StoredRun | None = None
    result: RunResult | None = None
    adjustments: Adjustments = field(default_factory=Adjustments)

    @property
    def is_adjusted(self) -> bool:
        return self.origin == "saved" and self.adjustments.any

    @property
    def label(self) -> str:
        if self.origin == "fresh":
            return "this run"
        if self.record is not None:
            return f"the run of {self.record.label}"
        return "a saved run"

    @property
    def provenance(self) -> str:
        """One line telling the reader what they are looking at."""
        if self.origin == "fresh":
            return "Fresh run."
        if self.is_adjusted:
            return (
                f"Saved run from {self.record.label if self.record else 'earlier'}, "
                f"re-analyzed with your changes ({self.adjustments.describe()}). "
                "The sales themselves are unchanged and are as of that date."
            )
        return (
            f"Saved run from {self.record.label if self.record else 'earlier'}, "
            "shown exactly as it was reported. Nothing recalculated."
        )


def open_saved(record: history_mod.RunRecord) -> reopen_mod.StoredRun:
    """Read a saved run's own figures. No market plugin needed, no recompute."""
    if record.snapshot is None:
        raise FileNotFoundError(f"the run of {record.label} has no saved data")
    return reopen_mod.read_stored_path(str(record.snapshot))


def recompute(
    record: history_mod.RunRecord, market: Market, adjustments: Adjustments
) -> RunResult:
    """Re-analyze a saved run's rows under changed settings."""
    snapshot = Snapshot.load(str(record.snapshot))
    profile: CompProfile | None = None
    if adjustments.subject_size:
        profile = reopen_mod.profile_from_snapshot(snapshot)
        setattr(profile.subject, profile.metric.value, adjustments.subject_size)
    return reopen_mod.reopen(
        snapshot,
        market,
        profile=profile,
        exclude_addresses=sorted(adjustments.excluded) or None,
    )


def headline(view: Viewing) -> dict[str, Any]:
    """The handful of numbers that answer 'what is it worth', either source."""
    if view.result is not None:
        result = view.result
        primary = result.valuation.primary if result.valuation else None
        compete = at_market = None
        if result.guidance:
            compete = next(
                (s for s in result.guidance.strategies if s.recommended), None
            )
            at_market = next(
                (s for s in result.guidance.strategies if s.key == "at_market"), None
            )
        return {
            "sold_count": result.sold_stats.count,
            "active_count": result.active_stats.count,
            "median_ppsf": result.sold_stats.median_ppsf,
            "valuation": primary.value if primary else None,
            "bracket_count": primary.sample_size if primary else 0,
            "suggested_list": compete.list_price if compete else None,
            "at_market": at_market.list_price if at_market else None,
            "floor": result.guidance.floor if result.guidance else None,
            "warnings": result.warnings,
        }

    stored = view.stored
    if stored is None:
        return {}
    guidance = stored.guidance or {}
    compete = next(
        (s for s in guidance.get("strategies", []) if s.get("recommended")), {}
    )
    at_market = next(
        (s for s in guidance.get("strategies", []) if s.get("key") == "at_market"), {}
    )
    primary = next(
        (b for b in (stored.valuation or {}).get("bases", []) if b.get("is_primary")), {}
    )
    return {
        "sold_count": stored.sold_count,
        "active_count": stored.active_count,
        "median_ppsf": (stored.stats.get("sold") or {}).get("median_ppsf"),
        "valuation": primary.get("value"),
        "bracket_count": primary.get("sample_size", 0),
        "suggested_list": compete.get("list_price"),
        "at_market": at_market.get("list_price"),
        "floor": guidance.get("floor"),
        "warnings": stored.warnings,
    }


def size_band_rows(view: Viewing) -> tuple[list[dict[str, Any]], list[str]]:
    """The size/rate bands and their notes, whichever source is on screen.

    A run saved before this table existed simply has no bands, which is why the
    caller checks for rows rather than assuming they are there.
    """
    if view.result is not None:
        table = view.result.size_bands
        return [r.to_dict() for r in table.rows], list(table.notes)
    if view.stored is None:
        return [], []
    stored = view.stored.size_bands or {}
    return list(stored.get("rows") or []), list(stored.get("notes") or [])


def core_figures(view: Viewing) -> dict[str, Any] | None:
    """The core view: the same market read with extreme rates set aside.

    Returns None when the search sets no bounds, which is the honest answer --
    there is no second reading to show.
    """
    if view.result is not None:
        exclusions = view.result.profile.exclusions
        low, high = exclusions.core_min_ppsf, exclusions.core_max_ppsf
        sold, active = view.result.sold_stats, view.result.active_stats
        sold_total, active_total = sold.count, active.count
        figures = {
            "sold_count": sold.core_count,
            "sold_median_ppsf": sold.core_median_ppsf,
            "sold_avg_ppsf": sold.core_avg_ppsf,
            "active_count": active.core_count,
            "active_median_ppsf": active.core_median_ppsf,
        }
    elif view.stored is not None:
        exclusions = (view.stored.profile or {}).get("exclusions") or {}
        low, high = exclusions.get("core_min_ppsf"), exclusions.get("core_max_ppsf")
        sold = view.stored.stats.get("sold") or {}
        active = view.stored.stats.get("active") or {}
        sold_total, active_total = sold.get("count") or 0, active.get("count") or 0
        figures = {
            "sold_count": sold.get("core_count"),
            "sold_median_ppsf": sold.get("core_median_ppsf"),
            "sold_avg_ppsf": sold.get("core_avg_ppsf"),
            "active_count": active.get("core_count"),
            "active_median_ppsf": active.get("core_median_ppsf"),
        }
    else:
        return None

    if low is None and high is None:
        return None
    return {"low": low, "high": high, "sold_total": sold_total,
            "active_total": active_total, **figures}


def agent_analysis(view: Viewing) -> dict[str, Any] | None:
    """The agent and brokerage tables, from a live run or a saved one.

    Sold-to-ask is carried through as the ratio the engine computes, not as a
    percentage: the conversion belongs at the point of display, and rounding it
    here would be the same mistake the valuation rules forbid.
    """
    if view.result is not None:
        return view.result.agent_analysis.to_dict()
    if view.stored is not None:
        return view.stored.agents or None
    return None


def active_rows(view: Viewing) -> list[dict[str, Any]]:
    """Listings currently on the market, whichever source is on screen.

    Kept apart from the sold comps rather than merged with a status column:
    an asking price is a claim and a sale is a fact, and a table that mixes
    them invites reading one as the other.
    """
    if view.result is not None:
        return [
            {
                "address": listing.address,
                "list_price": listing.list_price,
                "lot_sqft": listing.lot_sqft,
                "lot_size_is_rounded": listing.lot_size_is_rounded,
                "brokerage": listing.brokerage,
                "agent": listing.agent,
                "area": listing.area.value if listing.area else None,
            }
            for listing in view.result.active
        ]
    if view.stored is None:
        return []
    return [
        {
            "address": r.get("address"),
            "list_price": r.get("list_price"),
            "lot_sqft": r.get("lot_sqft"),
            "lot_size_is_rounded": bool(r.get("lot_size_is_rounded")),
            "brokerage": r.get("brokerage"),
            "agent": r.get("agent"),
            "area": r.get("area"),
        }
        for r in view.stored.active
    ]


def span_note(view: Viewing) -> str | None:
    """What the run asked for against what it actually found.

    Computed from the rows rather than read from a diagnostic, so it answers
    the question on runs archived before anyone thought to record it. A window
    is a request; a source answers with whatever it publishes, and the gap
    between the two is the difference between "nothing sold" and "nothing was
    listed for us to read".
    """
    rows = comp_rows(view)
    dates = sorted(r["sold_date"] for r in rows if r.get("sold_date"))
    if not dates:
        return None

    asked_start = asked_end = None
    if view.result is not None:
        asked_start, asked_end = view.result.window_start, view.result.window_end
    elif view.stored is not None:
        asked_start = _as_date(view.stored.window_start)
        asked_end = _as_date(view.stored.window_end)

    found = f"Sales found run {dates[0]} to {dates[-1]}."
    if asked_start is None:
        return found
    short = (dates[0] - asked_start).days
    if short <= 1:
        return f"Window {asked_start} to {asked_end}. {found}"
    return (
        f"Window {asked_start} to {asked_end}. {found} The first {short} days of "
        "the window returned nothing — the sources did not publish that far back, "
        "which is not the same as nothing having sold."
    )


def coverage_notes(view: Viewing) -> list[str]:
    """What this run could not see, from either source.

    These were only ever in the run log and the methodology document. A window
    that excludes half a recorded dataset changes every figure on the screen,
    so it belongs on the screen.
    """
    if view.result is not None:
        diagnostics = view.result.diagnostics
        return list(diagnostics.not_found) + [
            f"Source unavailable: {s}" for s in diagnostics.sources_failed
        ]
    if view.stored is None:
        return []
    diagnostics = view.stored.diagnostics or {}
    return list(diagnostics.get("not_found") or []) + [
        f"Source unavailable: {s}" for s in (diagnostics.get("sources_failed") or [])
    ]


def ladder_rows(view: Viewing) -> tuple[list[dict[str, Any]], list[str]]:
    """The widening-bracket table, from a live run or a saved one."""
    if view.result is not None:
        table = view.result.ladder
        return [r.to_dict() for r in table.rows], list(table.notes)
    if view.stored is None:
        return [], []
    stored = view.stored.ladder or {}
    return list(stored.get("rows") or []), list(stored.get("notes") or [])


def divider_lines(market) -> list[str]:
    """Where the quadrant boundaries actually are.

    The area table names NE and SW without ever saying where they are, which
    makes it a table nobody can check. The north/south divider is a centreline
    of geocoded intersections rather than one latitude, because real arterials
    curve -- so it is listed point by point rather than as a single number.
    """
    try:
        geometry = market.geometry()
    except Exception:
        return []
    if not geometry.is_configured():
        return []

    lines: list[str] = []
    if geometry.ns_centerline:
        name = geometry.ns_divider_name or "the north/south divider"
        lines.append(
            f"North/south: {name}, as a line through "
            f"{len(geometry.ns_centerline)} geocoded points. A parcel is compared "
            "against the latitude of that line at its own longitude, not against "
            "a single latitude, because the road curves."
        )
        lines.extend(
            f"    {w.name or 'point'}  ({w.lat:.5f}, {w.lon:.5f})"
            for w in geometry.ns_centerline
        )
    if geometry.ew_longitude is not None:
        name = geometry.ew_divider_name or "the east/west divider"
        lines.append(f"East/west: {name}, at longitude {geometry.ew_longitude:.5f}.")
    return lines


def area_rows(view: Viewing) -> tuple[list[dict[str, Any]], list[str]]:
    """Quadrant rows and their notes, from a live run or a saved one."""
    if view.result is not None:
        table = view.result.area_table
        return [r.to_dict() for r in table.rows], list(table.notes)
    if view.stored is None:
        return [], []
    stored = view.stored.areas or {}
    return list(stored.get("rows") or []), list(stored.get("notes") or [])


def exclusion_rows(view: Viewing) -> list[dict[str, Any]]:
    """Parcels the search's own rules removed, and why.

    These never reach a statistic, so a run that drops two parcels reports a
    smaller market with nothing on screen saying so. The engine has always
    recorded the reason; this is what lets a screen show it.
    """
    if view.result is not None:
        return [{"address": e.address, "reason": e.reason} for e in view.result.excluded]
    if view.stored is None:
        return []
    return [
        {"address": r.get("address"), "reason": r.get("reason")}
        for r in view.stored.excluded
    ]


def comp_rows(view: Viewing) -> list[dict[str, Any]]:
    """The sold comps as plain dicts, whichever source is on screen."""
    if view.result is not None:
        rows = []
        for comp in view.result.sold:
            rows.append(
                {
                    "address": comp.address,
                    "sold_date": comp.sold_date,
                    "sold_price": comp.sold_price,
                    "lot_sqft": comp.lot_sqft,
                    "brokerage": comp.brokerage,
                    "agent": comp.agent,
                    "final_list_price": comp.final_list_price,
                    "area": comp.area.value if comp.area else None,
                }
            )
        return rows
    if view.stored is None:
        return []
    return [
        {
            "address": r.get("address"),
            "sold_date": _as_date(r.get("sold_date")),
            "sold_price": r.get("sold_price"),
            "lot_sqft": r.get("lot_sqft"),
            "brokerage": r.get("brokerage"),
            "agent": r.get("agent"),
            "final_list_price": r.get("final_list_price"),
            "area": r.get("area"),
        }
        for r in view.stored.sold
    ]


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
