"""Run-over-run comparison (F12).

What a seller actually wants from a re-run is not the new numbers; it is the
delta. Which sales are new, which listings closed, which asks were cut, and
whether the valuation moved enough to change the decision.

Comparison works on snapshots rather than live results so any two runs can be
diffed after the fact, including runs made by an older build -- provided the
snapshot schema matches, which `Snapshot.load` enforces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lotcomps.model.address import AddressKey
from lotcomps.model.snapshot import Snapshot


@dataclass
class Delta:
    label: str
    before: float | None
    after: float | None

    @property
    def change(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before

    @property
    def pct_change(self) -> float | None:
        if not self.before or self.after is None:
            return None
        return (self.after - self.before) / self.before


@dataclass
class ChangeReport:
    prior_label: str = ""
    current_label: str = ""
    new_sales: list[str] = field(default_factory=list)
    newly_closed: list[str] = field(default_factory=list)
    gone_from_active: list[str] = field(default_factory=list)
    new_active: list[str] = field(default_factory=list)
    price_cuts: list[tuple[str, float, float]] = field(default_factory=list)
    deltas: list[Delta] = field(default_factory=list)

    def as_cell_rows(self) -> list[tuple[str, str]]:
        """Rows for the Summary sheet's "what changed" block."""
        rows: list[tuple[str, str]] = [("Compared with", self.prior_label or "—")]
        rows.append(("New sales", _join(self.new_sales)))
        rows.append(("Listings that closed", _join(self.newly_closed)))
        rows.append(("New listings", _join(self.new_active)))
        if self.price_cuts:
            rows.append((
                "Price cuts",
                "; ".join(f"{a} ${b:,.0f} to ${c:,.0f}" for a, b, c in self.price_cuts[:8]),
            ))
        for delta in self.deltas:
            rows.append((delta.label, _format_delta(delta)))
        return rows

    def as_text(self) -> str:
        lines = [f"Comparing {self.current_label} against {self.prior_label}", ""]
        lines.append(f"  new sales:            {len(self.new_sales)}")
        lines.append(f"  listings that closed: {len(self.newly_closed)}")
        lines.append(f"  new listings:         {len(self.new_active)}")
        lines.append(f"  listings withdrawn:   {len(self.gone_from_active)}")
        lines.append(f"  price cuts:           {len(self.price_cuts)}")
        lines.append("")
        for delta in self.deltas:
            lines.append(f"  {delta.label:<34} {_format_delta(delta)}")
        return "\n".join(lines)


def _join(items: list[str], limit: int = 8) -> str:
    if not items:
        return "none"
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} (+{len(items) - limit} more)"


def _format_delta(delta: Delta) -> str:
    if delta.before is None or delta.after is None:
        return "—"
    arrow = "+" if (delta.change or 0) >= 0 else ""
    pct = f" ({arrow}{delta.pct_change:.1%})" if delta.pct_change is not None else ""
    return f"{delta.before:,.2f} to {delta.after:,.2f}{pct}"


def _keys(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(AddressKey.of(r.get("address", ""))): r for r in rows}


def _stat(snapshot: Snapshot, side: str, key: str) -> float | None:
    return (snapshot.payload.get("stats", {}).get(side) or {}).get(key)


def _valuation(snapshot: Snapshot, key: str) -> float | None:
    valuation = snapshot.payload.get("valuation") or {}
    for basis in valuation.get("bases", []):
        if basis.get("key") == key:
            return basis.get("value")
    return None


def compare(prior: Snapshot, current: Snapshot) -> ChangeReport:
    prior_sold, current_sold = _keys(prior.payload.get("sold", [])), _keys(
        current.payload.get("sold", [])
    )
    prior_active, current_active = _keys(prior.payload.get("active", [])), _keys(
        current.payload.get("active", [])
    )

    report = ChangeReport(prior_label=prior.label, current_label=current.label)
    report.new_sales = sorted(
        current_sold[k]["address"] for k in current_sold.keys() - prior_sold.keys()
    )
    report.newly_closed = sorted(
        current_sold[k]["address"]
        for k in (current_sold.keys() - prior_sold.keys()) & prior_active.keys()
    )
    report.new_active = sorted(
        current_active[k]["address"] for k in current_active.keys() - prior_active.keys()
    )
    report.gone_from_active = sorted(
        prior_active[k]["address"]
        for k in prior_active.keys() - current_active.keys() - current_sold.keys()
    )
    for key in prior_active.keys() & current_active.keys():
        before, after = prior_active[key].get("list_price"), current_active[key].get("list_price")
        if before and after and after < before:
            report.price_cuts.append((current_active[key]["address"], before, after))
    report.price_cuts.sort(key=lambda t: (t[2] - t[1]))

    report.deltas = [
        Delta("Sold count", _stat(prior, "sold", "count"), _stat(current, "sold", "count")),
        Delta("Sold median $/sqft", _stat(prior, "sold", "median_ppsf"),
              _stat(current, "sold", "median_ppsf")),
        Delta("Sold median price", _stat(prior, "sold", "median_price"),
              _stat(current, "sold", "median_price")),
        Delta("Active count", _stat(prior, "active", "count"), _stat(current, "active", "count")),
        Delta("Active median $/sqft", _stat(prior, "active", "median_ppsf"),
              _stat(current, "active", "median_ppsf")),
        Delta("Primary valuation", _valuation(prior, "similar_size_avg"),
              _valuation(current, "similar_size_avg")),
    ]
    return report
