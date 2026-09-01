"""Exclusions, reconciliation, and the core view (F3).

Two distinct operations that are easy to conflate:

*Exclusion* removes a parcel from the dataset because it is not the kind of
property being comped -- raw acreage, non-residential, or a plugin's explicit
list. Excluded rows never reach the workbook.

*Core view* is a reporting filter. Extreme $/sqft values stay in the dataset and
in the sheets; a secondary set of statistics is computed with them filtered out
so the headline is not dragged by a single anomaly. Nothing is deleted.

A third operation, *reconciliation*, moves a listing from active to sold when it
turns out to have closed -- aggregator "active" indexes lag closings by days.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lotcomps.config.profile import CompProfile
from lotcomps.model.comp import ActiveListing, Comp, SoldComp


@dataclass
class ExclusionRecord:
    """Why one parcel left the dataset. Surfaced in the run log, never silent."""

    address: str
    reason: str


@dataclass
class FilterResult:
    sold: list[SoldComp] = field(default_factory=list)
    active: list[ActiveListing] = field(default_factory=list)
    excluded: list[ExclusionRecord] = field(default_factory=list)
    #: Comps kept in the dataset but missing the $/sqft denominator, so they
    #: cannot contribute to $/sqft statistics (F1). Logged, not dropped.
    missing_metric: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)


def _exclusion_reason(comp: Comp, profile: CompProfile) -> str | None:
    ex = profile.exclusions
    if str(comp.key) in set(ex.explicit_address_keys):
        return "on the market's explicit exclusion list"
    lot = comp.lot_sqft
    if lot is not None:
        if ex.max_lot_sqft is not None and lot > ex.max_lot_sqft:
            return f"lot {lot:,.0f} sqft exceeds the {ex.max_lot_sqft:,.0f} sqft cap"
        if ex.min_lot_sqft is not None and lot < ex.min_lot_sqft:
            return f"lot {lot:,.0f} sqft is below the {ex.min_lot_sqft:,.0f} sqft floor"
    return None


def apply_filters(
    sold: list[SoldComp],
    active: list[ActiveListing],
    profile: CompProfile,
) -> FilterResult:
    """Apply F3 to a freshly collected dataset."""
    result = FilterResult()
    denominator = profile.metric.value

    sold_keys = {c.key for c in sold}
    for comp in sold:
        reason = _exclusion_reason(comp, profile)
        if reason:
            result.excluded.append(ExclusionRecord(comp.address, reason))
            continue
        if comp.metric_sqft(denominator) is None:
            result.missing_metric.append(comp.address)
        result.sold.append(comp)

    for listing in active:
        # A listing that already appears in the sold set has closed (F3).
        if listing.key in sold_keys:
            result.reconciled.append(listing.address)
            continue
        reason = _exclusion_reason(listing, profile)
        if reason:
            result.excluded.append(ExclusionRecord(listing.address, reason))
            continue
        if listing.metric_sqft(denominator) is None:
            result.missing_metric.append(listing.address)
        result.active.append(listing)

    return result


def in_core_view(ppsf: float | None, profile: CompProfile) -> bool:
    """Whether a $/sqft value falls inside the market's core bounds."""
    if ppsf is None:
        return False
    ex = profile.exclusions
    if ex.core_min_ppsf is not None and ppsf < ex.core_min_ppsf:
        return False
    return not (ex.core_max_ppsf is not None and ppsf > ex.core_max_ppsf)
