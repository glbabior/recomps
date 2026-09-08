"""The bracket ladder (F13): how the estimate moves as "similar size" widens.

Every valuation here rests on a bandwidth choice. The primary basis prices the
subject against sales near its own size, and "near" is a number somebody picked.
On one real run the estimate moved nearly ten percent between a tight reading of
that word and the pinned one -- which is a large answer to a question the output
never asked out loud.

So this reports the whole curve rather than one point on it. Nested ranges
centred on the subject, from tight to the entire market, each with its own
sample size and rate. Two things become visible that a single figure hides:

* **The trade-off.** A tighter range is more relevant and thinner; a wider one
  is better evidenced and more diluted. Reading the count down the column is
  reading the price of relevance.
* **The limit.** The widest rung is every sale in the market, and it is exactly
  the all-sold basis reported elsewhere. Widening the bracket to everything does
  not produce a better similar-size estimate; it produces the market average,
  which is a different claim.

The rungs are *nested*, not the disjoint quantile bands of `size_bands`. Those
answer "does size move the rate here"; this answers "how sure is my number".

Values are shown from both the median and the mean because the two bases the
engine already reports disagree, and the ladder should reconcile with them
rather than introduce a third convention: at the widest rung the median column
is the all-sold-median basis and the mean column is the all-sold-average basis,
to the cent.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from recomps.config.profile import CompProfile
from recomps.model.comp import SoldComp

#: Half-widths to report, as a fraction of the subject's size. The profile's own
#: bracket is inserted among these at its true width.
RUNGS = (0.10, 0.15, 0.25, 0.40, 0.60, 1.00)
#: Below this a median is an anecdote. The row is still shown, because the point
#: of the ladder is watching the sample grow, but it is marked.
THIN = 3


@dataclass
class LadderRung:
    label: str
    low: float | None
    high: float | None
    count: int
    median_ppsf: float | None = None
    avg_ppsf: float | None = None
    value_from_median: float | None = None
    value_from_avg: float | None = None
    #: The rung the headline figures actually use.
    is_profile_bracket: bool = False
    #: The whole market: the limiting case, not a similar-size reading.
    is_all_sold: bool = False

    @property
    def is_thin(self) -> bool:
        return self.count < THIN

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "low": self.low,
            "high": self.high,
            "count": self.count,
            "median_ppsf": self.median_ppsf,
            "avg_ppsf": self.avg_ppsf,
            "value_from_median": self.value_from_median,
            "value_from_avg": self.value_from_avg,
            "is_profile_bracket": self.is_profile_bracket,
            "is_all_sold": self.is_all_sold,
            "is_thin": self.is_thin,
        }


@dataclass
class BracketLadder:
    rows: list[LadderRung] = field(default_factory=list)
    #: How far the estimate travels across the rungs that are not too thin to
    #: read, as a fraction of the rung the headline uses.
    spread_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "spread_pct": self.spread_pct,
            "notes": self.notes,
        }


def _rung(
    label: str,
    comps: list[SoldComp],
    denominator: str,
    value_size: float,
    low: float | None,
    high: float | None,
) -> LadderRung:
    rates = [v for c in comps if (v := c.price_per_sqft(denominator)) is not None]
    median = statistics.median(rates) if rates else None
    mean = statistics.fmean(rates) if rates else None
    return LadderRung(
        label=label,
        low=low,
        high=high,
        count=len(comps),
        median_ppsf=median,
        avg_ppsf=mean,
        # Unrounded rates, as everywhere: rounding to the displayed cents moves
        # the answer by tens of dollars.
        value_from_median=median * value_size if median is not None else None,
        value_from_avg=mean * value_size if mean is not None else None,
    )


def build_bracket_ladder(sold: list[SoldComp], profile: CompProfile) -> BracketLadder:
    """Build the widening-bracket table for a run (F13)."""
    ladder = BracketLadder()
    bracket = profile.similar_bracket
    attribute = bracket.attribute.value
    denominator = profile.metric.value
    # Two different sizes, deliberately, exactly as `valuation.py` keeps them
    # apart: the bracket's attribute decides which comps a rung *contains*,
    # and the profile's metric is what a rate is multiplied by. They are the
    # same number for land, and are not for an improved property priced on
    # living area while bracketed on lot size -- where using one for both
    # multiplies a $/living-sqft rate by a lot size and reports a valuation
    # several times too high, as a clean number beside a correct one.
    subject_size = profile.subject.size_for(bracket.attribute)
    value_size = profile.subject_size()
    if subject_size is None or value_size is None:
        ladder.notes.append(
            "No subject size, so there is no centre to widen a bracket around."
        )
        return ladder

    sized = [c for c in sold if c.metric_sqft(attribute) is not None]

    def within(low: float, high: float) -> list[SoldComp]:
        return [c for c in sized if low <= c.metric_sqft(attribute) <= high]

    profile_low, profile_high = bracket.bounds(subject_size)
    profile_width = (
        max(abs(profile_high - subject_size), abs(subject_size - profile_low))
        / subject_size
    )

    candidates: list[tuple[float, float, float, str, bool]] = []
    for fraction in RUNGS:
        low, high = subject_size * (1 - fraction), subject_size * (1 + fraction)
        candidates.append((fraction, low, high, f"+/-{fraction:.0%}", False))
    # The profile's own bracket sits at its true width, whether it came from a
    # tolerance or from pinned bounds. A tolerance that coincides with a rung
    # replaces it rather than doubling it.
    candidates = [c for c in candidates if abs(c[0] - profile_width) > 0.005]
    candidates.append(
        (profile_width, profile_low, profile_high, "this search", True)
    )
    candidates.sort(key=lambda c: c[0])

    for _, low, high, name, is_profile in candidates:
        rung = _rung(
            f"{name}  ({low:,.0f}-{high:,.0f} sqft)",
            within(low, high),
            denominator,
            value_size,
            low,
            high,
        )
        rung.is_profile_bracket = is_profile
        ladder.rows.append(rung)

    everything = _rung(
        f"every sale ({len(sold)})", sold, denominator, value_size, None, None
    )
    everything.is_all_sold = True
    ladder.rows.append(everything)

    _describe(ladder, profile)
    return ladder


def _describe(ladder: BracketLadder, profile: CompProfile) -> None:
    """Say what the bandwidth choice is worth, in money."""
    readable = [
        r
        for r in ladder.rows
        if not r.is_thin and not r.is_all_sold and r.value_from_avg is not None
    ]
    anchor = next((r for r in ladder.rows if r.is_profile_bracket), None)
    if len(readable) < 2 or anchor is None or not anchor.value_from_avg:
        return
    values = [r.value_from_avg for r in readable]
    low, high = min(values), max(values)
    ladder.spread_pct = (high - low) / anchor.value_from_avg * 100.0
    ladder.notes.append(
        f"Across the rungs thick enough to read, the estimate runs ${low:,.0f} to "
        f"${high:,.0f} -- a spread of {ladder.spread_pct:.0f}% of the figure this "
        'search reports. That is the cost of the word "similar", and it is not '
        "error: each rung is a correct answer to a slightly different question."
    )
    unit = "living area" if profile.metric.value == "living_sqft" else "lot"
    ladder.notes.append(
        "The widest rung is the whole market, not a better bracket. Widening until "
        f"every sale is included stops estimating a {unit} like yours and starts "
        "reporting the market average, which is the all-sold basis reported above."
    )
