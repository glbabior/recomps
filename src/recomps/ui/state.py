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
        compete = None
        if result.guidance:
            compete = next(
                (s for s in result.guidance.strategies if s.recommended), None
            )
        return {
            "sold_count": result.sold_stats.count,
            "active_count": result.active_stats.count,
            "median_ppsf": result.sold_stats.median_ppsf,
            "valuation": primary.value if primary else None,
            "bracket_count": primary.sample_size if primary else 0,
            "suggested_list": compete.list_price if compete else None,
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
        "floor": guidance.get("floor"),
        "warnings": stored.warnings,
    }


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
