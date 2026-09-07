"""The size/rate table (F12).

The premise the whole valuation rests on is that smaller parcels transact at a
higher rate per square foot than larger ones. Every other part of this pipeline
*acts* on that premise -- it is why the similar-size bracket leads, and why a
market-wide median understates a small subject -- but nothing until now made it
visible. A reader had to take it on trust.

This table shows it. Sizes are split into bands and each band reports its own
median and average $/sqft beside its median size, so the trend is read off the
column rather than assumed.

Two decisions worth the words:

*Bands hold equal counts, not equal widths.* Round edges (5,000-10,000,
10,000-15,000) read nicely and then put nineteen sales in one band and one in
the next, where a single view lot becomes an entire "trend". Quantile bands keep
every row's sample comparable, which is the only way the column means anything.
The real size range of each band is reported, so nothing is hidden by the
choice.

*A band is never smaller than three sales.* Below that a median is an anecdote.
The band count steps down rather than reporting rows that cannot support a
reading, and a market too thin to split at all gets one row and a note saying
so.

The subject's own band is marked, because "which row am I in" is the first
question anyone asks of this table.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from recomps.config.profile import CompProfile
from recomps.model.comp import SoldComp

#: A median below this many sales is an anecdote, not a rate.
MIN_BAND = 3
#: Most bands worth attempting, before the sample forces fewer.
MAX_BANDS = 5


@dataclass
class SizeBandRow:
    """One size band's rate, with the size it is a rate *for*.

    `median_size` sits beside the rates deliberately: a band's rate is only
    interpretable next to the size that produced it, and the point of the table
    is the relationship between the two columns.
    """

    label: str
    low: float
    high: float
    count: int = 0
    median_ppsf: float | None = None
    avg_ppsf: float | None = None
    median_size: float | None = None
    median_price: float | None = None
    holds_subject: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "low": self.low,
            "high": self.high,
            "count": self.count,
            "median_ppsf": self.median_ppsf,
            "avg_ppsf": self.avg_ppsf,
            "median_size": self.median_size,
            "median_price": self.median_price,
            "holds_subject": self.holds_subject,
        }


@dataclass
class SizeBandTable:
    rows: list[SizeBandRow] = field(default_factory=list)
    #: Sold comps with no size, so they can carry no rate. Counted, never dropped.
    without_size: int = 0
    #: Signed direction of the trend across bands, or None when it does not hold.
    trend_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "without_size": self.without_size,
            "trend_pct": self.trend_pct,
            "notes": self.notes,
        }


def _quantile_edges(sizes: list[float], bands: int) -> list[float]:
    """Cut points splitting `sizes` into `bands` groups of near-equal count."""
    ordered = sorted(sizes)
    edges = []
    for i in range(1, bands):
        edges.append(ordered[round(i * len(ordered) / bands)])
    return edges


def build_size_band_table(sold: list[SoldComp], profile: CompProfile) -> SizeBandTable:
    """Build the size/rate table for a run (F12)."""
    denom = profile.metric.value
    table = SizeBandTable()

    sized = [c for c in sold if c.metric_sqft(denom) is not None]
    table.without_size = len(sold) - len(sized)
    if not sized:
        table.notes.append("No sold comp carries a size, so no rate can be computed by size.")
        return table

    sizes = [c.metric_sqft(denom) for c in sized]
    # Step down until every band can hold a defensible median.
    bands = min(MAX_BANDS, len(sized) // MIN_BAND)
    if bands < 2:
        table.notes.append(
            f"{len(sized)} sold comps with a size is too few to split into bands of at "
            f"least {MIN_BAND}; the market is shown as one row."
        )
        bands = 1

    edges = _quantile_edges(sizes, bands) if bands > 1 else []
    bounds = [min(sizes), *edges, max(sizes)]

    subject_size = profile.subject.size_for(profile.metric)
    for i in range(bands):
        low, high = bounds[i], bounds[i + 1]
        last = i == bands - 1
        members = [
            c
            for c in sized
            if low <= c.metric_sqft(denom) <= high
            if last or c.metric_sqft(denom) < high
        ]
        if not members:
            continue
        ppsf = [v for c in members if (v := c.price_per_sqft(denom)) is not None]
        member_sizes = [c.metric_sqft(denom) for c in members]
        prices = [c.sold_price for c in members if c.sold_price is not None]
        table.rows.append(
            SizeBandRow(
                label=f"{low:,.0f}-{high:,.0f} sqft",
                low=low,
                high=high,
                count=len(members),
                median_ppsf=statistics.median(ppsf) if ppsf else None,
                avg_ppsf=statistics.fmean(ppsf) if ppsf else None,
                median_size=statistics.median(member_sizes),
                median_price=statistics.median(prices) if prices else None,
                holds_subject=(
                    subject_size is not None
                    and low <= subject_size <= high
                    and (last or subject_size < high)
                ),
            )
        )

    _describe_trend(table, profile)
    if table.without_size:
        table.notes.append(
            f"{table.without_size} sold comp(s) carry no size and so appear in no band. "
            "They are still in the dataset and in the sheets."
        )
    return table


def _describe_trend(table: SizeBandTable, profile: CompProfile) -> None:
    """State whether the smaller-is-dearer premise actually holds in this run.

    Stated as a measured fact about these sales, not asserted. When a market
    does not show the trend that is worth knowing -- it is the assumption the
    primary valuation basis rests on.
    """
    rated = [r for r in table.rows if r.median_ppsf is not None]
    if len(rated) < 2:
        return
    smallest, largest = rated[0].median_ppsf, rated[-1].median_ppsf
    if not largest:
        return
    table.trend_pct = (smallest - largest) / largest * 100.0
    unit = "living area" if profile.metric.value == "living_sqft" else "lot"
    if table.trend_pct > 0:
        table.notes.append(
            f"Smallest band sells at a {table.trend_pct:.0f}% higher rate per sqft than the "
            f"largest. This is the size premium the similar-size valuation exists to capture: "
            f"a market-wide {unit} rate would understate a small subject."
        )
    else:
        table.notes.append(
            f"Smallest band sells at a {abs(table.trend_pct):.0f}% *lower* rate per sqft than "
            "the largest, so the usual size premium does not hold in this run. The similar-size "
            "valuation still prices against comparable sizes, but treat the market-wide bases "
            "as the more informative comparison here."
        )
