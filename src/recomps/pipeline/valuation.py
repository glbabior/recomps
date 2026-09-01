"""Subject valuation (F5).

Four bases, deliberately reported side by side rather than blended:

  1. *similar-size sold average $/sqft* -- the primary anchor,
  2. all-sold median $/sqft,
  3. all-sold average $/sqft,
  4. active-listing median $/sqft (asking-based context, not evidence of value).

The similar-size bracket leads because smaller parcels transact at a higher
$/sqft than larger ones, so a market-wide figure understates a small subject.
The bracket is what corrects for that, and the profile decides which attribute
it keys on (F0).

Every basis multiplies an *unrounded* $/sqft by the subject size. Rounding the
rate first moves the answer by tens of dollars.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from recomps.config.profile import CompProfile
from recomps.model.comp import ActiveListing, SoldComp


@dataclass
class ValuationBasis:
    key: str
    label: str
    ppsf: float | None
    value: float | None
    sample_size: int
    is_primary: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "ppsf": self.ppsf,
            "value": self.value,
            "sample_size": self.sample_size,
            "is_primary": self.is_primary,
            "note": self.note,
        }


@dataclass
class Valuation:
    subject_size: float | None
    bracket_low: float | None
    bracket_high: float | None
    bracket_count: int
    bases: list[ValuationBasis] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def primary(self) -> ValuationBasis | None:
        return next((b for b in self.bases if b.is_primary), None)

    def by_key(self, key: str) -> ValuationBasis | None:
        return next((b for b in self.bases if b.key == key), None)

    def to_dict(self) -> dict:
        return {
            "subject_size": self.subject_size,
            "bracket_low": self.bracket_low,
            "bracket_high": self.bracket_high,
            "bracket_count": self.bracket_count,
            "bases": [b.to_dict() for b in self.bases],
            "warnings": self.warnings,
        }


def similar_size_comps(
    comps: list[SoldComp], profile: CompProfile
) -> tuple[list[SoldComp], float | None, float | None]:
    """Select the subject-comparison bracket (F0 `similar_bracket`, F5)."""
    subject_size = profile.subject.size_for(profile.similar_bracket.attribute)
    if subject_size is None:
        return [], None, None
    low, high = profile.similar_bracket.bounds(subject_size)
    attribute = profile.similar_bracket.attribute.value
    selected = []
    for comp in comps:
        size = comp.metric_sqft(attribute)
        if size is None or not (low <= size <= high):
            continue
        if (
            profile.similar_bracket.match_bed_count
            and profile.subject.beds is not None
            and comp.beds != profile.subject.beds
        ):
            continue
        selected.append(comp)
    return selected, low, high


def value_subject(
    sold: list[SoldComp], active: list[ActiveListing], profile: CompProfile
) -> Valuation:
    denominator = profile.metric.value
    subject_size = profile.subject_size()
    bracket, low, high = similar_size_comps(sold, profile)

    def rate(values: list[float], fn) -> float | None:
        return fn(values) if values else None

    bracket_ppsf = [v for c in bracket if (v := c.price_per_sqft(denominator)) is not None]
    all_ppsf = [v for c in sold if (v := c.price_per_sqft(denominator)) is not None]
    active_ppsf = [v for c in active if (v := c.price_per_sqft(denominator)) is not None]

    specs = [
        (
            "similar_size_avg",
            f"Similar-size sold average $/sqft ({len(bracket_ppsf)} comps)",
            rate(bracket_ppsf, statistics.fmean),
            len(bracket_ppsf),
            True,
            "Primary anchor: controls for the size premium smaller parcels carry.",
        ),
        (
            "all_sold_median",
            "All-sold median $/sqft",
            rate(all_ppsf, statistics.median),
            len(all_ppsf),
            False,
            "Conservative market-wide basis; the floor reference is derived from this.",
        ),
        (
            "all_sold_avg",
            "All-sold average $/sqft",
            rate(all_ppsf, statistics.fmean),
            len(all_ppsf),
            False,
            "Mean is skewed by parcel quality; shown for completeness.",
        ),
        (
            "active_median",
            "Active-listing median $/sqft",
            rate(active_ppsf, statistics.median),
            len(active_ppsf),
            False,
            "What sellers are asking, not what buyers paid. Context only.",
        ),
    ]

    bases = [
        ValuationBasis(
            key=key,
            label=label,
            ppsf=ppsf,
            value=(ppsf * subject_size) if (ppsf is not None and subject_size) else None,
            sample_size=n,
            is_primary=primary,
            note=note,
        )
        for key, label, ppsf, n, primary, note in specs
    ]

    warnings: list[str] = []
    if subject_size is None:
        warnings.append("No subject size configured; run `recomps configure` to set one.")
    if len(bracket_ppsf) < 5:
        warnings.append(
            f"Only {len(bracket_ppsf)} comps fall in the similar-size bracket; the primary "
            "anchor is thin. Widen the bracket tolerance or the window."
        )
    return Valuation(
        subject_size=subject_size,
        bracket_low=low,
        bracket_high=high,
        bracket_count=len(bracket_ppsf),
        bases=bases,
        warnings=warnings,
    )
