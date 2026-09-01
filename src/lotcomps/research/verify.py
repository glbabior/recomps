"""The verification pass.

Nothing extracted from a page enters the dataset without being checked against
what is already known about that sale. This is the single most important guard
in the research layer, because every failure it catches produces output that
looks entirely reasonable.

The failures, all of them observed rather than imagined:

**The stale attribution.** A property page lists an agent -- for a transaction
from 1998. The name is real, the agent is real, the page is real, and the
attribution is worthless.

**The wrong property.** A search returns a page for a similar address on a
similar street. Its price and date are plausible for the market and wrong for
this parcel.

**The invented figure.** An extractor filling a required field with something
that fits rather than reporting that the page did not say. Cheap to produce,
indistinguishable from data.

The rule for matching a claim to a known sale is deliberately lopsided:

**Price is the primary key.** Sale prices are distinctive, exactly recorded,
and agree across sources.

**Dates are a tolerance, never an equality.** Recording date and close of
escrow routinely differ by a day or two, and one source in the reference data
was eighteen days out. Requiring dates to match discards good attributions;
requiring them merely to be *close* keeps them while still rejecting a sale
from another year.

A claim that fails is not silently dropped. It is recorded with its reason, so
a run that rejected fifteen of forty attributions says so rather than quietly
reporting fewer comps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from lotcomps.model.address import AddressKey
from lotcomps.model.comp import SoldComp

#: How far a claimed sale date may sit from the known one. Three days covers
#: the routine recording-versus-escrow drift.
DEFAULT_DATE_TOLERANCE = timedelta(days=3)
#: A wider tolerance for sources known to publish recording dates. Still far
#: inside the gap to a different transaction on the same parcel.
LOOSE_DATE_TOLERANCE = timedelta(days=21)
#: Prices are quoted to the dollar but occasionally rounded to the nearest
#: hundred by a source. Anything wider than this is a different sale.
PRICE_TOLERANCE = 0.005  # half a percent


@dataclass
class Claim:
    """A fact an extractor produced, and the evidence offered for it."""

    address: str
    field_name: str
    value: object
    source_url: str = ""
    claimed_price: float | None = None
    claimed_date: date | None = None

    def describe(self) -> str:
        return f"{self.address}: {self.field_name}={self.value!r} from {self.source_url}"


@dataclass
class Verdict:
    accepted: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.accepted


@dataclass
class VerificationLog:
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.accepted)} accepted, {len(self.rejected)} rejected, "
            f"{len(self.unverifiable)} could not be checked"
        )


#: MLS records use filler names where there is no real counterparty, and they
#: read exactly like people. "Out Of Area Out Of Area" appeared as a buyer agent
#: on a live page during development; left alone it would have earned its own
#: row in the agent performance table and confused dual-agency detection.
_PLACEHOLDER_NAMES = frozenset(
    {
        "out of area", "out of area out of area", "outofarea",
        "non member", "nonmember", "non-member", "non member non member",
        "public record", "not available", "unavailable", "unknown",
        "none", "n/a", "na", "tbd", "agent", "listing agent", "seller",
        "buyer", "owner", "no agent", "not applicable", "withheld",
        ".", "-", "--", "—",
    }
)


def clean_name(value: str | None) -> str | None:
    """Return a real name, or None if it is a placeholder.

    Treating a filler value as an agent is worse than having no agent: it
    invents a person, gives them closings, and ranks them.
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    normalized = text.lower().strip(" .,")
    if normalized in _PLACEHOLDER_NAMES:
        return None
    # "Out Of Area Out Of Area" style doubling of a placeholder.
    halves = normalized.split()
    midpoint = len(halves) // 2
    if (
        len(halves) % 2 == 0
        and halves[:midpoint] == halves[midpoint:]
        and " ".join(halves[:midpoint]) in _PLACEHOLDER_NAMES
    ):
        return None
    return text


def prices_match(known: float | None, claimed: float | None) -> bool:
    if known is None or claimed is None:
        return False
    if known == 0:
        return claimed == 0
    return abs(known - claimed) / abs(known) <= PRICE_TOLERANCE


def dates_match(
    known: date | None, claimed: date | None, tolerance: timedelta = DEFAULT_DATE_TOLERANCE
) -> bool:
    if known is None or claimed is None:
        return False
    return abs((known - claimed).days) <= tolerance.days


def verify_claim(
    claim: Claim,
    comp: SoldComp,
    *,
    date_tolerance: timedelta = DEFAULT_DATE_TOLERANCE,
    require_date: bool = False,
) -> Verdict:
    """Whether a claim is about the sale we think it is about.

    Price carries the decision. A matching price with a date a few days off is
    the same sale recorded twice; a matching date with a different price is two
    different transactions.
    """
    if claim.claimed_price is None and claim.claimed_date is None:
        return Verdict(False, "no evidence offered: neither a price nor a date")

    if claim.claimed_price is not None:
        if not prices_match(comp.sold_price, claim.claimed_price):
            return Verdict(
                False,
                f"price {claim.claimed_price:,.0f} does not match the known "
                f"{comp.sold_price:,.0f}" if comp.sold_price else "no known price to check",
            )
        # Price agrees. A wildly different date still means a different event
        # on the same parcel -- an earlier sale, or a listing rather than a sale.
        if (
            claim.claimed_date is not None
            and comp.sold_date is not None
            and not dates_match(comp.sold_date, claim.claimed_date, LOOSE_DATE_TOLERANCE)
        ):
            return Verdict(
                False,
                f"price matches but the date {claim.claimed_date} is far from the "
                f"known {comp.sold_date}; probably a different transaction",
            )
        return Verdict(True)

    # Date only. Much weaker, so it must be tight, and it is only enough when
    # the caller says a price was unavailable rather than merely unread.
    if not require_date:
        return Verdict(False, "no price offered, and a date alone is not enough")
    if dates_match(comp.sold_date, claim.claimed_date, date_tolerance):
        return Verdict(True, "matched on date alone; weaker evidence")
    return Verdict(
        False, f"date {claim.claimed_date} does not match the known {comp.sold_date}"
    )


def apply_claims(
    comps: list[SoldComp],
    claims: list[Claim],
    *,
    identification: object = None,
    date_tolerance: timedelta = DEFAULT_DATE_TOLERANCE,
) -> VerificationLog:
    """Write verified claims onto their comps, and log everything else.

    A claim only ever *fills* a field. It never overwrites one that a more
    trusted source already supplied, because the first source to answer is the
    aggregate listing itself and a per-property page is more likely to be
    describing something else.
    """
    log = VerificationLog()
    keep_unit = bool(getattr(identification, "unit_suffix_is_significant", False))
    by_key: dict[AddressKey, SoldComp] = {
        AddressKey.of(c.address, keep_unit=keep_unit): c for c in comps
    }

    for claim in claims:
        comp = by_key.get(AddressKey.of(claim.address, keep_unit=keep_unit))
        if comp is None:
            log.unverifiable.append(
                f"{claim.describe()} -- no comp with that address in this run"
            )
            continue
        value = claim.value
        if claim.field_name in ("agent", "brokerage", "buyer_agent", "buyer_brokerage"):
            value = clean_name(value if isinstance(value, str) else None)
            if value is None and claim.value:
                log.rejected.append(
                    f"{claim.describe()} -- placeholder name, not a real party"
                )
                continue
        if value in (None, "", "-", "—"):
            continue

        verdict = verify_claim(claim, comp, date_tolerance=date_tolerance)
        if not verdict:
            log.rejected.append(f"{claim.describe()} -- {verdict.reason}")
            continue
        if getattr(comp, claim.field_name, None) is not None:
            continue  # already known from a more direct source
        setattr(comp, claim.field_name, value)
        if claim.source_url and claim.source_url not in comp.sources:
            comp.sources.append(claim.source_url)
        log.accepted.append(claim.describe())
    return log


def flag_anomalous_rows(comps: list[SoldComp]) -> list[str]:
    """Report parcels that appear to have sold more than once in the window.

    One source in the reference data emitted an interim "sold" row for a parcel
    months before its real close -- a recording artifact. Two sales of one
    parcel inside a ninety-day window is nearly always that rather than a genuine
    flip, so it is surfaced for a human to judge rather than silently resolved.
    """
    seen: dict[str, SoldComp] = {}
    notes: list[str] = []
    for comp in comps:
        key = str(AddressKey.of(comp.address, keep_unit=False))
        previous = seen.get(key)
        if previous is None:
            seen[key] = comp
            continue
        # Two rows for one parcel are usually the same sale listed twice -- once
        # bare and once with an MLS lot suffix, occasionally a dollar apart.
        # Deduplication collapses those silently, so reporting them as anomalies
        # is noise that trains the reader to skip the anomaly list. Only a
        # genuinely different price or a genuinely different date is worth a
        # human's attention.
        same_price = prices_match(previous.sold_price, comp.sold_price)
        same_date = dates_match(previous.sold_date, comp.sold_date)
        if same_price and same_date:
            continue
        notes.append(
            f"{comp.address} appears to have sold twice in this window "
            f"({previous.sold_date} at {previous.sold_price:,.0f}; "
            f"{comp.sold_date} at {comp.sold_price:,.0f}). Probably a recording "
            "artifact -- check which row is real."
        )
    return notes
