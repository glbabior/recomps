"""Comparable-sale and active-listing records.

One record type carries both land and improved-property fields. The active
CompProfile decides which fields are *required*, which are collected, and which
one is the $/sqft denominator (F0) - the record itself stays profile-agnostic so
a snapshot written under one profile is still readable under another.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from lotcomps.model.address import AddressKey

SQFT_PER_ACRE = 43_560.0

#: Rendered for any value the research layer could not find. Never imputed (S7).
NOT_FOUND = "—"  # em dash


class Quadrant(StrEnum):
    NE = "NE"
    NW = "NW"
    SE = "SE"
    SW = "SW"


def acres_to_sqft(acres: float) -> float:
    """Convert acreage to square feet (F1).

    Aggregators that publish acreage rounded to 0.01 ac introduce roughly +/-4%
    of $/sqft error on a small lot; callers should carry `lot_size_is_rounded`
    so F4 can raise the caveat rather than silently presenting false precision.
    """
    return acres * SQFT_PER_ACRE


@dataclass
class Comp:
    """Fields shared by sold comps and active listings."""

    address: str
    lot_sqft: float | None = None
    living_sqft: float | None = None
    beds: float | None = None
    baths: float | None = None
    year_built: int | None = None
    condition: str | None = None  # optional; see F0 note on post-fire stock
    brokerage: str | None = None
    agent: str | None = None
    area: Quadrant | None = None
    lat: float | None = None
    lon: float | None = None
    lot_size_is_rounded: bool = False
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> AddressKey:
        return AddressKey.of(self.address)

    def metric_sqft(self, denominator: str) -> float | None:
        """Return the $/sqft denominator named by the active profile."""
        return getattr(self, denominator, None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.area is not None:
            d["area"] = self.area.value
        return d


@dataclass
class SoldComp(Comp):
    sold_date: date | None = None
    sold_price: float | None = None
    final_list_price: float | None = None
    original_list_price: float | None = None
    #: Buying-side agent, where a source attributes one. Only needed to detect
    #: dual agency (F10); absent on most aggregate pages.
    buyer_agent: str | None = None

    def price_per_sqft(self, denominator: str) -> float | None:
        m = self.metric_sqft(denominator)
        if not m or not self.sold_price:
            return None
        return self.sold_price / m

    def sold_to_ask(self) -> float | None:
        """Sold divided by *final* list price (F9)."""
        if not self.sold_price or not self.final_list_price:
            return None
        return self.sold_price / self.final_list_price

    def was_reduced(self) -> bool:
        if self.final_list_price is None or self.original_list_price is None:
            return False
        return self.original_list_price > self.final_list_price

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["sold_date"] = self.sold_date.isoformat() if self.sold_date else None
        return d


@dataclass
class ActiveListing(Comp):
    list_price: float | None = None
    listed_date: date | None = None

    def price_per_sqft(self, denominator: str) -> float | None:
        m = self.metric_sqft(denominator)
        if not m or not self.list_price:
            return None
        return self.list_price / m

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["listed_date"] = self.listed_date.isoformat() if self.listed_date else None
        return d
