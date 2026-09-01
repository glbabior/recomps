"""Canonical address keys.

The spec's pipeline cross-checks two sources for the same sale (F1) and moves a
listing from active to sold when it closes (F3). Both need a stable identity for
a property that survives the fact that every aggregator writes an address
slightly differently: "0 N Ironwood Ave", "0 North Ironwood Avenue",
"0 N. Ironwood Ave.".

This is deliberately a *normalizer*, not a geocoder or a USPS validator. It
collapses the formatting differences we actually observe between sources and
stops there; anything more ambitious would need a real address service and would
fail closed in offline/fixture mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Street-type abbreviations, collapsed to a single canonical token.
_SUFFIXES = {
    "street": "st", "st": "st",
    "avenue": "ave", "av": "ave", "ave": "ave",
    "road": "rd", "rd": "rd",
    "drive": "dr", "dr": "dr",
    "boulevard": "blvd", "blvd": "blvd",
    "lane": "ln", "ln": "ln",
    "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl",
    "terrace": "ter", "ter": "ter",
    "way": "way",
    "circle": "cir", "cir": "cir",
    "trail": "trl", "trl": "trl",
    "canyon": "canyon", "cyn": "canyon",
}

# Directional prefixes/suffixes.
_DIRECTIONS = {
    "north": "n", "n": "n",
    "south": "s", "s": "s",
    "east": "e", "e": "e",
    "west": "w", "w": "w",
    "northeast": "ne", "ne": "ne",
    "northwest": "nw", "nw": "nw",
    "southeast": "se", "se": "se",
    "southwest": "sw", "sw": "sw",
}

# A trailing "#n" means two opposite things depending on the property type, and
# getting it wrong corrupts the dataset in one of two ways.
#
# On a condo it is the home's identity: "410 Bellweather Rd #2" and "#3" are
# different properties, and stripping the suffix merges a whole building into
# one address.
#
# On vacant land it is an artifact. MLS records append a lot or listing number
# to parcel addresses -- "125 W Thistle St #18", "607 Larkspur St #34" -- and
# the same sale appears both with and without it, sometimes as two rows on one
# page. Keeping the suffix there double-counts sales and breaks the join that
# attaches attribution to a comp.
#
# So the caller decides, and the comp profile tells it which case it is: see
# `Identification.unit_suffix_is_significant`.
_UNIT_RE = re.compile(r"(?:\b(?:apt|apartment|unit|ste|suite)\b\.?|#)\s*([\w-]+)", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[.,]")
_WS_RE = re.compile(r"\s+")


def normalize_address(raw: str, keep_unit: bool = True) -> str:
    """Return a canonical lowercase token string for `raw`.

    Drops city/state/ZIP tails and punctuation; canonicalizes directionals and
    street types. An un-numbered parcel ("0 Larkspur Vista Rd") keeps its
    leading 0, which is what the sources use as the house number.

    `keep_unit` decides whether a trailing "#n" is part of the identity -- true
    for a condo, false for a parcel, where it is an MLS listing number. See the
    note on `_UNIT_RE`.
    """
    if not raw:
        return ""
    s = raw.strip().lower()
    # Pull the unit out before trimming the tail: sources write it on either
    # side of the comma ("Rd #2, Demoville" and "Rd, Apt 2, Demoville"), and a
    # naive comma split loses it in the second form.
    unit = ""
    match = _UNIT_RE.search(s)
    if match:
        unit = f"#{match.group(1)}"
        s = s[: match.start()]  # the street address ends where the unit begins
    # Trim a trailing ", city, ST zip" tail if present.
    s = s.split(",")[0]
    s = _PUNCT_RE.sub(" ", s)
    tokens = [t for t in _WS_RE.split(s) if t]
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if tok in _DIRECTIONS and (i == 0 or i == 1 or i == len(tokens) - 1):
            out.append(_DIRECTIONS[tok])
        elif tok in _SUFFIXES and i == len(tokens) - 1:
            out.append(_SUFFIXES[tok])
        else:
            out.append(tok)
    if unit and keep_unit:
        out.append(unit)
    return " ".join(out)


@dataclass(frozen=True, order=True)
class AddressKey:
    """Hashable identity for a parcel, used to dedupe and to reconcile sources."""

    key: str

    @classmethod
    def of(cls, raw: str, keep_unit: bool = True) -> AddressKey:
        return cls(normalize_address(raw, keep_unit=keep_unit))

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key
