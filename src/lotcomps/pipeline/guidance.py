"""Pricing guidance (F6).

Three strategies derived fresh from each run, plus a walk-away floor. These are
decision aids for a conversation with an agent. They are not an appraisal, and
every renderer that emits them must say so.

The one non-obvious mechanic is strategy A. Buyers search in price bands, so a
list price a thousand dollars under a band edge is seen by everyone searching
the band below it. Pricing at $499,000 rather than $505,000 does not cost six
thousand dollars of value -- in a market where under-priced listings get bid up
(F9), it buys the competition that recovers it.

Expected sale ranges come from the run's own sold-to-ask distribution rather
than fixed percentages, and they widen or narrow with the market accordingly.
They are guidance, not regression-tested figures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lotcomps.pipeline.stats import SoldToAskStats
from lotcomps.pipeline.valuation import Valuation

#: Price-band granularity used by consumer search filters below $1M.
DEFAULT_SEARCH_BAND = 50_000
#: How far under a band edge to sit.
UNDERCUT = 1_000
#: Strategy C premium over the primary anchor.
ANCHOR_PREMIUM = 0.055
#: The floor sits this far below the conservative (all-sold median) basis.
FLOOR_DISCOUNT = 0.05

DISCLAIMER = (
    "Comps-based guidance, not an appraisal. The author is not a licensed appraiser. "
    "Validate with a local agent before listing."
)


def _band_floor(value: float, band: int = DEFAULT_SEARCH_BAND) -> float:
    """Largest search-band edge at or below `value`."""
    return (int(value) // band) * band


def _just_under_ten_k(value: float) -> float:
    """Round down to a $10K edge and sit $1,000 under it."""
    return (int(value) // 10_000) * 10_000 - UNDERCUT


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class Strategy:
    key: str
    label: str
    list_price: float | None
    expected_low: float | None
    expected_high: float | None
    tradeoff: str
    recommended: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "list_price": self.list_price,
            "expected_low": self.expected_low,
            "expected_high": self.expected_high,
            "tradeoff": self.tradeoff,
            "recommended": self.recommended,
        }


@dataclass
class Guidance:
    strategies: list[Strategy] = field(default_factory=list)
    floor: float | None = None
    floor_basis: str = ""
    disclaimer: str = DISCLAIMER
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strategies": [s.to_dict() for s in self.strategies],
            "floor": self.floor,
            "floor_basis": self.floor_basis,
            "disclaimer": self.disclaimer,
            "warnings": self.warnings,
        }


def _expected_range(
    list_price: float, ratios: list[float], *, aggressive: bool
) -> tuple[float | None, float | None]:
    """Project a sale range for `list_price` from the sold-to-ask distribution.

    A list price set below market competes for the upper half of the observed
    ratio distribution; one set at or above market draws from the lower half,
    because the same market that bids up bargains grinds down optimistic asks.
    """
    if not ratios:
        return None, None
    lo_q, hi_q = (0.45, 0.90) if aggressive else (0.15, 0.60)
    lo, hi = _percentile(ratios, lo_q), _percentile(ratios, hi_q)
    if lo is None or hi is None:
        return None, None
    return round(list_price * lo, -3), round(list_price * hi, -3)


def build_guidance(
    valuation: Valuation, sold_to_ask: SoldToAskStats, ratios: list[float]
) -> Guidance:
    primary = valuation.primary
    conservative = valuation.by_key("all_sold_median")
    anchor = primary.value if primary else None

    if anchor is None:
        return Guidance(floor_basis="unavailable: no primary valuation")

    compete = _band_floor(anchor) - UNDERCUT
    at_market = _just_under_ten_k(anchor)
    high = _just_under_ten_k(anchor * (1 + ANCHOR_PREMIUM))

    pace = ""
    if sold_to_ask.count:
        pace = f" Observed sold/ask ran {sold_to_ask.min_ratio:.0%}-{sold_to_ask.max_ratio:.0%}."

    compete_lo, compete_hi = _expected_range(compete, ratios, aggressive=True)
    at_market_lo, at_market_hi = _expected_range(at_market, ratios, aggressive=False)
    high_lo, high_hi = _expected_range(high, ratios, aggressive=False)

    strategies = [
        Strategy(
            key="compete",
            label="A. Price to compete (recommended)",
            list_price=compete,
            expected_low=compete_lo,
            expected_high=compete_hi,
            tradeoff=(
                f"Sits just under the ${_band_floor(anchor):,.0f} search cutoff, so it reaches "
                "every buyer shopping the band below. Positioned to draw multiple offers and "
                "bid up toward or above market value. Fastest sale."
            ),
            recommended=True,
        ),
        Strategy(
            key="at_market",
            label="B. Price at market",
            list_price=at_market,
            expected_low=at_market_lo,
            expected_high=at_market_hi,
            tradeoff=(
                "Straightforward ask near the best-estimate value. Moderate market time."
                + pace
            ),
        ),
        Strategy(
            key="anchor_high",
            label="C. Anchor high",
            list_price=high,
            expected_low=high_lo,
            expected_high=high_hi,
            tradeoff=(
                f"About {ANCHOR_PREMIUM:.0%} over the anchor. Tests the ceiling, and risks a "
                "longer market time and a price cut -- reduced listings in this dataset closed "
                "below even their reduced ask."
            ),
        ),
    ]

    floor = None
    basis = "no conservative basis available"
    if conservative and conservative.value:
        floor = round(conservative.value * (1 - FLOOR_DISCOUNT), -3)
        basis = f"{FLOOR_DISCOUNT:.0%} below the all-sold-median basis"

    warnings: list[str] = []
    # Strategy A drops to the next search-band edge below the anchor. When the
    # anchor sits just above an edge that drop is nearly a whole band, and the
    # ask can land under the walk-away floor -- an ask you would refuse to
    # accept. Worth saying out loud rather than quietly presenting both.
    if floor is not None and compete < floor:
        warnings.append(
            f"Strategy A (${compete:,.0f}) sits below the walk-away floor "
            f"(${floor:,.0f}) because the anchor falls just above a "
            f"${DEFAULT_SEARCH_BAND:,.0f} band edge. Either treat that list price as an "
            "invitation to bid rather than a price you would accept, or use strategy B."
        )
    return Guidance(strategies=strategies, floor=floor, floor_basis=basis, warnings=warnings)
