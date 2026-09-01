"""Quadrant classification and the by-area table (F11).

Two traps this module exists to avoid.

**The naming-grid trap.** Do not infer east/west from a street name. A town's
E/W naming grid splits at whichever street the surveyors chose, which is often
not the arterial you are using as your analytical divider. In the reference
market the grid splits at one avenue and the divider is another, so an address
literally labelled "E Chandler St" sits *west* of the divider. Classification reads
geocoded coordinates, never the address string.

**The straight-line trap.** Arterials curve. Classifying north/south against a
single latitude misplaces parcels at the ends of the market, where the real
street has drifted hundreds of metres from that constant. The divider is
therefore a piecewise-linear centerline of geocoded intersections, and a parcel
is compared against the latitude interpolated at *its own* longitude.

Outside the centerline's longitude span the endpoint latitude is held constant
rather than extrapolated: an extrapolated arterial diverges from reality fast,
and a held endpoint is wrong in a bounded, explainable way.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from itertools import pairwise

from recomps.config.profile import CompProfile
from recomps.model.comp import ActiveListing, Comp, Quadrant, SoldComp
from recomps.plugin.market import MarketGeometry

#: Parcels within this many degrees of a divider are flagged borderline.
#: ~0.0002 deg latitude is roughly 22 m, about the width of an arterial and its
#: verge, and comparable to the error of a street-interpolated geocode.
BORDERLINE_DEGREES = 0.0002


class UnconfiguredGeometry(RuntimeError):
    pass


def interpolate_divider_lat(geometry: MarketGeometry, lon: float) -> float:
    """Latitude of the N/S divider at `lon`."""
    points = sorted(geometry.ns_centerline, key=lambda w: w.lon)
    if not points:
        raise UnconfiguredGeometry("market has no N/S centerline waypoints")
    if len(points) == 1 or lon <= points[0].lon:
        return points[0].lat
    if lon >= points[-1].lon:
        return points[-1].lat
    for left, right in pairwise(points):
        if left.lon <= lon <= right.lon:
            span = right.lon - left.lon
            if span == 0:
                return left.lat
            t = (lon - left.lon) / span
            return left.lat + (right.lat - left.lat) * t
    return points[-1].lat  # pragma: no cover - unreachable given the guards above


@dataclass
class Classification:
    quadrant: Quadrant | None
    borderline: bool = False
    reason: str = ""


def classify(comp: Comp, geometry: MarketGeometry) -> Classification:
    """Assign a parcel to a quadrant from its geocoded position."""
    if comp.lat is None or comp.lon is None:
        return Classification(None, reason="not geocoded")
    if not geometry.is_configured():
        return Classification(None, reason="market geometry not configured")

    divider_lat = interpolate_divider_lat(geometry, comp.lon)
    ns = "N" if comp.lat >= divider_lat else "S"
    ew = "E" if comp.lon >= (geometry.ew_longitude or 0.0) else "W"

    borderline = (
        abs(comp.lat - divider_lat) < BORDERLINE_DEGREES
        or abs(comp.lon - (geometry.ew_longitude or 0.0)) < BORDERLINE_DEGREES
    )
    reason = ""
    if borderline:
        reason = (
            "on or beside a divider street; assigned by geocoded side "
            "(ties resolve north and east)"
        )
    return Classification(Quadrant(f"{ns}{ew}"), borderline=borderline, reason=reason)


def classify_all(comps: list[Comp], geometry: MarketGeometry) -> dict[str, Classification]:
    """Classify in place and return the per-address decisions for the run log."""
    decisions: dict[str, Classification] = {}
    for comp in comps:
        decision = classify(comp, geometry)
        if decision.quadrant is not None:
            comp.area = decision.quadrant
        elif comp.area is not None:
            # A parcel that arrived with an area already assigned keeps it. A
            # source or a plugin may know the answer for a parcel that cannot be
            # geocoded, and discarding that in favour of None would lose real
            # information to a failed lookup.
            decision = Classification(
                comp.area, reason=f"{decision.reason}; kept the area supplied by the source"
            )
        if decision.borderline:
            comp.notes.append(f"Borderline: {decision.reason}")
        decisions[comp.address] = decision
    return decisions


@dataclass
class AreaRow:
    """One quadrant's row in the by-area table.

    Median $/sqft and average size sit beside the average $/sqft on purpose. A
    quadrant can carry higher absolute prices *and* a lower average $/sqft
    simply because its parcels are larger, which makes a naive $/sqft comparison
    invert the real premium. Showing size next to rate makes the mix visible.
    """

    area: str
    sold_count: int = 0
    sold_avg_ppsf: float | None = None
    sold_median_ppsf: float | None = None
    sold_median_price: float | None = None
    sold_avg_size: float | None = None
    active_count: int = 0
    active_avg_ppsf: float | None = None

    def to_dict(self) -> dict:
        return {
            "area": self.area,
            "sold_count": self.sold_count,
            "sold_avg_ppsf": self.sold_avg_ppsf,
            "sold_median_ppsf": self.sold_median_ppsf,
            "sold_median_price": self.sold_median_price,
            "sold_avg_size": self.sold_avg_size,
            "active_count": self.active_count,
            "active_avg_ppsf": self.active_avg_ppsf,
        }


@dataclass
class AreaTable:
    rows: list[AreaRow] = field(default_factory=list)
    unclassified_sold: int = 0
    unclassified_active: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "unclassified_sold": self.unclassified_sold,
            "unclassified_active": self.unclassified_active,
            "notes": self.notes,
        }


def _row(area: str, sold: list[SoldComp], active: list[ActiveListing], denom: str) -> AreaRow:
    sold_ppsf = [v for c in sold if (v := c.price_per_sqft(denom)) is not None]
    sold_prices = [c.sold_price for c in sold if c.sold_price is not None]
    sizes = [v for c in sold if (v := c.metric_sqft(denom)) is not None]
    active_ppsf = [v for c in active if (v := c.price_per_sqft(denom)) is not None]
    return AreaRow(
        area=area,
        sold_count=len(sold),
        sold_avg_ppsf=statistics.fmean(sold_ppsf) if sold_ppsf else None,
        sold_median_ppsf=statistics.median(sold_ppsf) if sold_ppsf else None,
        sold_median_price=statistics.median(sold_prices) if sold_prices else None,
        sold_avg_size=statistics.fmean(sizes) if sizes else None,
        active_count=len(active),
        active_avg_ppsf=statistics.fmean(active_ppsf) if active_ppsf else None,
    )


def build_area_table(
    sold: list[SoldComp],
    active: list[ActiveListing],
    profile: CompProfile,
    geometry: MarketGeometry,
) -> AreaTable:
    denom = profile.metric.value
    table = AreaTable()
    for quadrant in Quadrant:
        table.rows.append(
            _row(
                quadrant.value,
                [c for c in sold if c.area is quadrant],
                [c for c in active if c.area is quadrant],
                denom,
            )
        )
    # East/west aggregates make the size-mix distortion legible at a glance.
    for side, members in (("East of divider", ("NE", "SE")), ("West of divider", ("NW", "SW"))):
        table.rows.append(
            _row(
                side,
                [c for c in sold if c.area and c.area.value in members],
                [c for c in active if c.area and c.area.value in members],
                denom,
            )
        )
    table.unclassified_sold = sum(1 for c in sold if c.area is None)
    table.unclassified_active = sum(1 for c in active if c.area is None)
    if geometry.ns_divider_name or geometry.ew_divider_name:
        table.notes.append(
            f"Quadrants are relative to {geometry.ns_divider_name or 'the N/S divider'} "
            f"(north/south) and {geometry.ew_divider_name or 'the E/W divider'} (east/west). "
            "Assigned from geocoded coordinates; street-name directionals are not used."
        )
    if table.unclassified_sold or table.unclassified_active:
        table.notes.append(
            f"{table.unclassified_sold} sold and {table.unclassified_active} active parcels "
            "could not be geocoded and are absent from the quadrant rows."
        )
    return table
