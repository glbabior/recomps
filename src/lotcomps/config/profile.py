"""Comp profiles (F0).

The kind of property being comped is configuration, not a hardcoded assumption.
In the original hand-run process the "vacant land" assumption hid in four
places, and each one is now an explicit field here:

  1. how a listing is recognized as the target type  -> `identification`
  2. the $/sqft denominator                          -> `metric`
  3. the exclusion rules                             -> `exclusions`
  4. the similar-comp bracket                        -> `similar_bracket`

Everything downstream (stats, valuation, workbook columns and their formulas)
reads those fields rather than assuming land.

Scope honesty: `vacant_land` is the validated reference path -- all golden
numbers come from a real land dataset. The improved-property profiles are wired
end to end but ship as documented-experimental; their identification heuristics
have not been validated against a live run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class PropertyType(StrEnum):
    VACANT_LAND = "vacant_land"
    SINGLE_FAMILY = "single_family"
    CONDO_TOWNHOME = "condo_townhome"
    MULTI_FAMILY = "multi_family"

    @property
    def is_improved(self) -> bool:
        return self is not PropertyType.VACANT_LAND


#: Which profiles are validated against real data vs. architecturally supported.
VALIDATED_TYPES = frozenset({PropertyType.VACANT_LAND})


class Denominator(StrEnum):
    """Field name on a Comp used as the $/sqft denominator."""

    LOT_SQFT = "lot_sqft"
    LIVING_SQFT = "living_sqft"


@dataclass
class Identification:
    """How to recognize the target property type on an aggregator (F0, F1).

    For vacant land the empirically reliable rule is the *absence* of bed/bath
    counts on a listing card. Improved types invert that and additionally accept
    the source's own type labels.
    """

    #: True  -> a listing with NO bed/bath counts is the target type (land).
    #: False -> a listing WITH bed/bath counts is the target type (improved).
    requires_absent_bed_bath: bool = True
    #: Source-side type labels/filters that positively identify the type.
    type_labels: list[str] = field(default_factory=lambda: ["LOT", "LAND"])
    #: A sale including a habitable structure is excluded outright (F1, land).
    exclude_habitable_structure: bool = True

    def matches(self, *, has_bed_bath: bool, label: str | None = None) -> bool:
        if label and any(t.lower() in label.lower() for t in self.type_labels):
            return True
        return (not has_bed_bath) if self.requires_absent_bed_bath else has_bed_bath


@dataclass
class SimilarBracket:
    """The subject-comparison bracket (F0, F5).

    Keys on whichever attribute the profile treats as size. `explicit_range`
    pins the bracket to fixed bounds when a market has a conventional one -- the
    reference land market uses 5,000-8,000 sqft for a 6,450 sqft subject rather
    than a bare percentage.
    """

    attribute: Denominator = Denominator.LOT_SQFT
    tolerance: float = 0.25  # +/- fraction of the subject's size
    explicit_range: tuple[float, float] | None = None
    match_bed_count: bool = False

    def bounds(self, subject_size: float) -> tuple[float, float]:
        if self.explicit_range is not None:
            return self.explicit_range
        return (subject_size * (1 - self.tolerance), subject_size * (1 + self.tolerance))


@dataclass
class Exclusions:
    """Exclusion and outlier policy (F3).

    Excluded rows leave the dataset. Outliers *stay* in the dataset and are only
    filtered for the reported "core" view -- the spec is explicit that extremes
    are kept in the data and excluded from a secondary view, not dropped.
    """

    max_lot_sqft: float | None = 43_560.0  # 1 acre; raw-acreage / non-residential
    min_lot_sqft: float | None = None
    #: Normalized address keys the market plugin excludes by name.
    explicit_address_keys: list[str] = field(default_factory=list)
    #: "Core view" bounds on $/sqft -- reporting only, never removal.
    core_min_ppsf: float | None = 30.0
    core_max_ppsf: float | None = 150.0


@dataclass
class Subject:
    """The property being valued in F5."""

    lot_sqft: float | None = None
    living_sqft: float | None = None
    beds: float | None = None
    baths: float | None = None
    year_built: int | None = None
    label: str = "Your property"

    def size_for(self, denominator: Denominator) -> float | None:
        return getattr(self, denominator.value, None)


@dataclass
class CompProfile:
    """A named, reusable parameterization of the whole pipeline."""

    name: str
    property_type: PropertyType = PropertyType.VACANT_LAND
    metric: Denominator = Denominator.LOT_SQFT
    identification: Identification = field(default_factory=Identification)
    similar_bracket: SimilarBracket = field(default_factory=SimilarBracket)
    exclusions: Exclusions = field(default_factory=Exclusions)
    subject: Subject = field(default_factory=Subject)
    #: Extra per-comp fields collected for this type, in workbook column order.
    attributes: list[str] = field(default_factory=list)
    window_days: int = 90

    # -- derived -----------------------------------------------------------
    @property
    def is_experimental(self) -> bool:
        return self.property_type not in VALIDATED_TYPES

    @property
    def metric_label(self) -> str:
        return "Lot (sq ft)" if self.metric is Denominator.LOT_SQFT else "Living Area (sq ft)"

    @property
    def secondary_size_label(self) -> str | None:
        """Improved profiles keep lot size as a secondary column (F0, S8)."""
        return "Lot (sq ft)" if self.metric is Denominator.LIVING_SQFT else None

    def subject_size(self) -> float | None:
        return self.subject.size_for(self.metric)

    def validate(self) -> list[str]:
        """Return human-readable problems; an empty list means usable."""
        problems: list[str] = []
        if not self.name:
            problems.append("profile needs a name")
        if self.property_type.is_improved and self.metric is Denominator.LOT_SQFT:
            problems.append(
                "improved property types should price on living-area sq ft, not lot sq ft"
            )
        if not self.property_type.is_improved and self.metric is Denominator.LIVING_SQFT:
            problems.append("vacant land has no living area; metric must be lot_sqft")
        if self.subject_size() is None:
            problems.append(
                f"subject {self.metric.value} is required to value the subject property (F5)"
            )
        if self.window_days <= 0:
            problems.append("window_days must be positive")
        lo, hi = self.exclusions.core_min_ppsf, self.exclusions.core_max_ppsf
        if lo is not None and hi is not None and lo >= hi:
            problems.append("core view lower bound must be below the upper bound")
        return problems

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["property_type"] = self.property_type.value
        d["metric"] = self.metric.value
        d["similar_bracket"]["attribute"] = self.similar_bracket.attribute.value
        if self.similar_bracket.explicit_range is not None:
            d["similar_bracket"]["explicit_range"] = list(self.similar_bracket.explicit_range)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CompProfile:
        d = dict(d)
        bracket = dict(d.pop("similar_bracket", {}) or {})
        if "attribute" in bracket:
            bracket["attribute"] = Denominator(bracket["attribute"])
        rng = bracket.get("explicit_range")
        if rng is not None:
            bracket["explicit_range"] = (float(rng[0]), float(rng[1]))
        return cls(
            name=d.get("name", "default"),
            property_type=PropertyType(d.get("property_type", "vacant_land")),
            metric=Denominator(d.get("metric", "lot_sqft")),
            identification=Identification(**(d.get("identification") or {})),
            similar_bracket=SimilarBracket(**bracket),
            exclusions=Exclusions(**(d.get("exclusions") or {})),
            subject=Subject(**(d.get("subject") or {})),
            attributes=list(d.get("attributes") or []),
            window_days=int(d.get("window_days", 90)),
        )


# ---------------------------------------------------------------------------
# Built-in starting points. A market plugin or the `configure` wizard overrides
# the subject and any market-specific bounds.
# ---------------------------------------------------------------------------


def vacant_land_profile(name: str = "empty-lots") -> CompProfile:
    """The validated reference profile."""
    return CompProfile(
        name=name,
        property_type=PropertyType.VACANT_LAND,
        metric=Denominator.LOT_SQFT,
        identification=Identification(
            requires_absent_bed_bath=True,
            type_labels=["LOT", "LAND"],
            exclude_habitable_structure=True,
        ),
        similar_bracket=SimilarBracket(attribute=Denominator.LOT_SQFT, tolerance=0.25),
        exclusions=Exclusions(),
        attributes=[],
    )


def improved_profile(
    name: str, property_type: PropertyType = PropertyType.SINGLE_FAMILY
) -> CompProfile:
    """EXPERIMENTAL (F0): wired end to end, heuristics not validated live."""
    return CompProfile(
        name=name,
        property_type=property_type,
        metric=Denominator.LIVING_SQFT,
        identification=Identification(
            requires_absent_bed_bath=False,
            type_labels=["SINGLE_FAMILY", "HOUSE", "SFR"],
            exclude_habitable_structure=False,
        ),
        similar_bracket=SimilarBracket(attribute=Denominator.LIVING_SQFT, tolerance=0.25),
        # The 1-acre rule and the $30/$150 bounds are land numbers; improved
        # types need their own. Left open rather than guessed.
        exclusions=Exclusions(max_lot_sqft=None, core_min_ppsf=None, core_max_ppsf=None),
        attributes=["beds", "baths", "year_built"],
    )


BUILTIN_PROFILES = {
    "empty-lots": vacant_land_profile,
}
