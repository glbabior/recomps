"""Core statistics (F4) and sold-to-ask analysis (F9).

Two rules that the golden numbers depend on:

*Averages are means of per-comp ratios*, not aggregate price over aggregate
size. The workbook computes ``AVERAGE(E2:E47)`` over a column of ``C/D``
formulas, and the pipeline has to agree with the spreadsheet exactly.

*Nothing is rounded before it is used.* Every valuation downstream multiplies a
$/sqft figure by a size. Rounding $/sqft to the two decimals that get displayed
before doing that multiplication shifts the resulting valuation by tens of
dollars and silently breaks the regression numbers. Rounding is a presentation
concern and lives in the renderers.

Medians lead the headline messaging: lot quality skews means (F4).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from recomps.config.profile import CompProfile
from recomps.model.comp import ActiveListing, SoldComp
from recomps.pipeline.exclusions import in_core_view

#: An aggregator that publishes acreage rounded to 0.01 ac introduces roughly
#: this much error in $/sqft on a small lot (F4).
ACREAGE_ROUNDING_ERROR = 0.04


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


@dataclass
class SideStats:
    """Statistics for one side of the market (sold or active)."""

    count: int = 0
    avg_ppsf: float | None = None
    median_ppsf: float | None = None
    avg_price: float | None = None
    median_price: float | None = None
    avg_size: float | None = None
    min_ppsf: float | None = None
    max_ppsf: float | None = None
    #: Same figures with out-of-bounds $/sqft filtered out (F3 core view).
    core_count: int = 0
    core_avg_ppsf: float | None = None
    core_median_ppsf: float | None = None
    #: Comps that could not contribute a $/sqft value.
    missing_metric_count: int = 0

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "avg_ppsf": self.avg_ppsf,
            "median_ppsf": self.median_ppsf,
            "avg_price": self.avg_price,
            "median_price": self.median_price,
            "avg_size": self.avg_size,
            "min_ppsf": self.min_ppsf,
            "max_ppsf": self.max_ppsf,
            "core_count": self.core_count,
            "core_avg_ppsf": self.core_avg_ppsf,
            "core_median_ppsf": self.core_median_ppsf,
            "missing_metric_count": self.missing_metric_count,
        }


@dataclass
class SoldToAskStats:
    """F9. In this kind of market a list price is bait, not a ceiling.

    Under-priced listings get bid up; over-priced ones suffer serial cuts and
    close below even the reduced ask. Reporting the mean alone hides both tails,
    so the share at/above ask and the tail counts travel with it.
    """

    count: int = 0
    mean_ratio: float | None = None
    median_ratio: float | None = None
    at_or_above_ask: int = 0
    share_at_or_above_ask: float | None = None
    at_or_above_105: int = 0
    below_90: int = 0
    max_ratio: float | None = None
    min_ratio: float | None = None
    reduced_count: int = 0

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "mean_ratio": self.mean_ratio,
            "median_ratio": self.median_ratio,
            "at_or_above_ask": self.at_or_above_ask,
            "share_at_or_above_ask": self.share_at_or_above_ask,
            "at_or_above_105": self.at_or_above_105,
            "below_90": self.below_90,
            "max_ratio": self.max_ratio,
            "min_ratio": self.min_ratio,
            "reduced_count": self.reduced_count,
        }


def sold_stats(comps: list[SoldComp], profile: CompProfile) -> SideStats:
    denominator = profile.metric.value
    ppsf = [v for c in comps if (v := c.price_per_sqft(denominator)) is not None]
    prices = [c.sold_price for c in comps if c.sold_price is not None]
    sizes = [v for c in comps if (v := c.metric_sqft(denominator)) is not None]
    core = [v for v in ppsf if in_core_view(v, profile)]
    return SideStats(
        count=len(comps),
        avg_ppsf=_mean(ppsf),
        median_ppsf=_median(ppsf),
        avg_price=_mean(prices),
        median_price=_median(prices),
        avg_size=_mean(sizes),
        min_ppsf=min(ppsf) if ppsf else None,
        max_ppsf=max(ppsf) if ppsf else None,
        core_count=len(core),
        core_avg_ppsf=_mean(core),
        core_median_ppsf=_median(core),
        missing_metric_count=len(comps) - len(ppsf),
    )


def active_stats(listings: list[ActiveListing], profile: CompProfile) -> SideStats:
    denominator = profile.metric.value
    ppsf = [v for c in listings if (v := c.price_per_sqft(denominator)) is not None]
    prices = [c.list_price for c in listings if c.list_price is not None]
    sizes = [v for c in listings if (v := c.metric_sqft(denominator)) is not None]
    core = [v for v in ppsf if in_core_view(v, profile)]
    return SideStats(
        count=len(listings),
        avg_ppsf=_mean(ppsf),
        median_ppsf=_median(ppsf),
        avg_price=_mean(prices),
        median_price=_median(prices),
        avg_size=_mean(sizes),
        min_ppsf=min(ppsf) if ppsf else None,
        max_ppsf=max(ppsf) if ppsf else None,
        core_count=len(core),
        core_avg_ppsf=_mean(core),
        core_median_ppsf=_median(core),
        missing_metric_count=len(listings) - len(ppsf),
    )


def sold_to_ask(comps: list[SoldComp]) -> SoldToAskStats:
    ratios = [r for c in comps if (r := c.sold_to_ask()) is not None]
    if not ratios:
        return SoldToAskStats(reduced_count=sum(1 for c in comps if c.was_reduced()))
    at_or_above = sum(1 for r in ratios if r >= 1.0)
    return SoldToAskStats(
        count=len(ratios),
        mean_ratio=_mean(ratios),
        median_ratio=_median(ratios),
        at_or_above_ask=at_or_above,
        share_at_or_above_ask=at_or_above / len(ratios),
        at_or_above_105=sum(1 for r in ratios if r >= 1.05),
        below_90=sum(1 for r in ratios if r < 0.90),
        max_ratio=max(ratios),
        min_ratio=min(ratios),
        reduced_count=sum(1 for c in comps if c.was_reduced()),
    )


def attribution_coverage(comps: list[SoldComp]) -> tuple[int, int]:
    """(attributed, total). An unattributable sale is reported, never guessed."""
    return sum(1 for c in comps if c.brokerage or c.agent), len(comps)


@dataclass
class Caveats:
    """Statements that must travel with the numbers (F4, F10)."""

    notes: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, sold: list[SoldComp], stats: SideStats) -> Caveats:
        notes: list[str] = []
        if any(c.lot_size_is_rounded for c in sold):
            notes.append(
                "Some lot sizes come from acreage rounded to 0.01 ac, worth about "
                f"+/-{ACREAGE_ROUNDING_ERROR:.0%} of $/sqft on a small lot."
            )
        if stats.missing_metric_count:
            notes.append(
                f"{stats.missing_metric_count} sale(s) had no usable size and are excluded "
                "from $/sqft statistics but kept in the dataset."
            )
        if stats.avg_ppsf and stats.median_ppsf and stats.median_ppsf > stats.avg_ppsf:
            notes.append(
                "Median $/sqft exceeds the mean: a few low-priced parcels are dragging the "
                "average. Prefer the median."
            )
        return cls(notes=notes)
