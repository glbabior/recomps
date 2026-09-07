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

from recomps.config.profile import CompProfile
from recomps.model.address import AddressKey
from recomps.model.comp import ActiveListing, Comp, SoldComp


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
    #: Rows that were the same property listed twice by a source.
    duplicates: list[str] = field(default_factory=list)
    #: Addresses carrying more than one genuine sale. Reported, because it looks
    #: exactly like a duplicate and must not be quietly treated as one.
    distinct_at_one_address: list[str] = field(default_factory=list)


#: Two rows priced within this of each other are the same transaction listed
#: twice. Observed duplicates have landed a single dollar apart; genuinely
#: different parcels at one address were hundreds of thousands apart.
SAME_PRICE_TOLERANCE = 0.005
#: Sizes differing by more than this are different pieces of land, whatever the
#: address says.
SAME_SIZE_TOLERANCE = 0.05


def _completeness(comp: Comp) -> int:
    """How many of the fields the analysis needs this row actually carries."""
    return sum(
        1
        for v in (
            comp.lot_sqft, comp.living_sqft, comp.brokerage, comp.agent,
            getattr(comp, "final_list_price", None), getattr(comp, "sold_date", None),
            comp.lat,
        )
        if v is not None
    )


def _close(a: float | None, b: float | None, tolerance: float) -> bool | None:
    """Whether two figures agree, or None when one of them is missing."""
    if a is None or b is None:
        return None
    if a == b:
        return True
    largest = max(abs(a), abs(b))
    return largest > 0 and abs(a - b) / largest <= tolerance


def _same_sale(a: SoldComp, b: SoldComp) -> bool:
    """Whether two rows at one address describe one transaction.

    Price decides, because that is the field sources agree on -- recording date
    and close of escrow routinely differ, and one observed source was 18 days
    out. Size breaks the tie when price is unknown. When neither can be
    compared the rows are treated as the same sale, which preserves the
    original behaviour for the MLS-suffix case this was written for.
    """
    priced = _close(a.sold_price, b.sold_price, SAME_PRICE_TOLERANCE)
    if priced is False:
        return False
    return _close(a.lot_sqft, b.lot_sqft, SAME_SIZE_TOLERANCE) is not False


def _exclusion_reason(comp: Comp, profile: CompProfile) -> str | None:
    ex = profile.exclusions
    if str(comp.key_for(profile.identification)) in set(ex.explicit_address_keys):
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
    identification = profile.identification

    # Deduplicate the sold side before anything else. A source can list one
    # sale twice -- once bare and once with an MLS lot suffix -- and counting
    # it twice would shift every statistic downstream.
    #
    # But a shared street address is not proof of a shared sale. Two genuinely
    # different parcels can carry one address (observed: a 3.9-acre and a
    # 5.14-acre parcel at the same address, listed at different prices), and
    # collapsing those deletes a real sale from every statistic without a
    # trace. So the address selects candidates and the *price* decides, which
    # is the same rule the verification pass uses.
    groups: dict[AddressKey, list[SoldComp]] = {}
    for comp in sold:
        groups.setdefault(comp.key_for(identification), []).append(comp)

    deduped: list[SoldComp] = []
    for members in groups.values():
        kept: list[SoldComp] = []
        for comp in members:
            twin = next((k for k in kept if _same_sale(k, comp)), None)
            if twin is None:
                kept.append(comp)
                continue
            if _completeness(comp) > _completeness(twin):
                kept[kept.index(twin)] = comp
            result.duplicates.append(comp.address)
        if len(kept) > 1:
            result.distinct_at_one_address.append(
                f"{kept[0].address}: {len(kept)} distinct sales share this address"
            )
        deduped.extend(kept)
    sold = deduped

    sold_keys = set(groups)
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
        if listing.key_for(identification) in sold_keys:
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
