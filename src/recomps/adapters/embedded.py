"""Reading a page's own structured data instead of its rendered text.

Most listing sites are single-page applications that ship their search results
as JSON inside the HTML and render them with JavaScript afterwards. A fetcher
that reads only the rendered text sees whatever the server bothered to
pre-render -- often a handful of cards -- and reports that as the whole result
set. The JSON block underneath usually holds all of it.

Reading that block directly is better than reading the page three ways over:

* **Complete.** The full result set rather than the pre-rendered slice.
* **Exact.** Prices and dates are parsed, not interpreted. A parser cannot
  misread $550,000 as $55,000; a language model can, and would do it fluently.
* **Free.** No tokens, no latency, no per-page cost.
* **Richer.** These payloads routinely carry coordinates and canonical URLs
  that never appear in the visible page at all.

Nothing here knows about any particular website. The block to look for, where
the records live inside it, and how each field maps are all configuration, so a
market plugin can describe a site without any site-specific code being
published. The AI-driven extractor is the fallback for pages that genuinely
have no structured data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from recomps.model.comp import SQFT_PER_ACRE

#: Script blocks that conventionally carry page data rather than code.
DATA_SCRIPT_PATTERN = re.compile(
    r"<script[^>]*\bid=[\"'](?P<id>[^\"']+)[\"'][^>]*>(?P<body>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_LD_JSON_PATTERN = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)

_MONEY = re.compile(r"[-+]?[\d,]*\.?\d+")
_DATE_LIKE = re.compile(r"^[A-Za-z]{3,9}\.?\s+\d{1,2},\s*\d{4}$")
_DIMENSION = re.compile(r"([\d,.]+)\s*(acre|acres|ac|sq\s*ft|sqft|square feet)", re.IGNORECASE)


class EmbeddedDataMissing(LookupError):
    pass


def find_embedded_json(html: str, script_id: str) -> Any | None:
    """The parsed contents of a `<script id="...">` data block."""
    for match in DATA_SCRIPT_PATTERN.finditer(html):
        if match.group("id") == script_id:
            try:
                return json.loads(match.group("body"))
            except json.JSONDecodeError:
                return None
    return None


def find_ld_json(html: str) -> list[Any]:
    """Every schema.org block on the page. A common fallback carrier."""
    blocks: list[Any] = []
    for match in _LD_JSON_PATTERN.finditer(html):
        try:
            blocks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return blocks


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path, tolerating a missing link anywhere along it.

    Supports list indexes (``a.0.b``) because these payloads mix the two
    freely. A path that does not resolve returns None rather than raising --
    a source that stopped publishing a field should degrade the run, not end it.
    """
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


# ---------------------------------------------------------------------------
# Field transforms
# ---------------------------------------------------------------------------


def as_money(value: Any) -> float | None:
    """"$550,000" or "$550K" -> 550000.0"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    multiplier = 1.0
    if text.upper().endswith("K"):
        multiplier, text = 1_000.0, text[:-1]
    elif text.upper().endswith("M"):
        multiplier, text = 1_000_000.0, text[:-1]
    match = _MONEY.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group()) * multiplier
    except ValueError:
        return None


def as_date(value: Any):
    """"AUG 28, 2026" or "2026-08-28" -> a date."""
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def as_tag_date(value: Any):
    """Find the date among a list of display tags.

    Sites label a card with a status tag and a date tag in one list. Which
    position they occupy is not stable, so the date is identified by shape.
    """
    if not isinstance(value, list):
        return as_date(value)
    for tag in value:
        text = tag.get("formattedName") if isinstance(tag, dict) else tag
        if isinstance(text, str) and _DATE_LIKE.match(text.strip()):
            parsed = as_date(text.strip().title())
            if parsed:
                return parsed
    return None


def as_sqft(value: Any) -> tuple[float | None, bool]:
    """"0.27 acres" -> (11761.2, True). Returns (sqft, was_derived_from_acres).

    The flag matters: acreage published to two decimals carries about four
    percent of error on a small parcel, and the statistics have to say so
    rather than present a derived figure as though it were measured.
    """
    if value is None:
        return None, False
    if isinstance(value, (int, float)):
        return float(value), False
    match = _DIMENSION.search(str(value))
    if not match:
        return None, False
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None, False
    unit = match.group(2).lower().replace(" ", "")
    if unit.startswith("ac"):
        return amount * SQFT_PER_ACRE, True
    return amount, False


#: Licence-number labels that trail an agent name in attribution strings.
LICENCE_LABEL = re.compile(
    r"\b(?:DRE|BRE|CalBRE|Lic(?:ense)?)\b", re.IGNORECASE
)


def _split_attribution(value: Any) -> tuple[str | None, str | None]:
    """Pull an agent and a brokerage out of one attribution string.

    Indexes pack both into a single field: "Jane Roe DRE # 01234567, Some
    Brokerage". The obvious rule -- split on the comma -- is wrong, because a
    brokerage is often incorporated: "Sender Realty, Inc." would yield an agent
    called "Sender Realty" and a brokerage called "Inc.".

    So the licence label decides. A name is only claimed where the string
    actually identifies a licensee; everything else is a brokerage listing
    itself, which is a real and common state rather than a parse failure. On
    one observed index only six of thirty-four listings named a person at all.
    """
    if value in (None, ""):
        return None, None
    text = " ".join(str(value).split())
    if not text:
        return None, None
    match = LICENCE_LABEL.search(text)
    if match is None:
        return None, text
    name = text[: match.start()].strip(" .,#")
    remainder = text[match.end():]
    _, _, brokerage = remainder.partition(",")
    return (name or None), (brokerage.strip() or None)


TRANSFORMS = {
    "money": as_money,
    "date": as_date,
    "tag_date": as_tag_date,
    # Returns (sqft, derived_from_acres); `extract_records` unpacks it.
    "sqft": as_sqft,
    "text": lambda v: str(v).strip() if v not in (None, "") else None,
    "attribution_agent": lambda v: _split_attribution(v)[0],
    "attribution_brokerage": lambda v: _split_attribution(v)[1],
    "float": lambda v: float(v) if isinstance(v, (int, float, str)) and str(v).strip() else None,
    "raw": lambda v: v,
}


@dataclass
class FieldSpec:
    """Where one field lives in the payload, and how to read it."""

    path: str
    transform: str = "raw"

    def read(self, record: Any) -> Any:
        value = dig(record, self.path)
        fn = TRANSFORMS.get(self.transform)
        if fn is None:
            return value
        try:
            return fn(value)
        except Exception:
            return None


@dataclass
class EmbeddedSpec:
    """A market plugin's description of one site's embedded payload."""

    script_id: str = "__NEXT_DATA__"
    #: Dotted path from the payload root to the list of records.
    records_path: str = ""
    #: Field name -> where to find it. Names match Comp attributes.
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    #: Path to a per-record property URL, made absolute against this base.
    url_base: str = ""
    #: Dotted path to the site's own count of results for this search. When a
    #: plugin declares it, the extraction is checked against it -- see
    #: `_check_count`. One observed index claimed 166 properties and surfaced
    #: nine, which reads as a quiet market rather than a broken fetch.
    total_path: str = ""

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> EmbeddedSpec:
        fields = {}
        for name, spec in (raw.get("fields") or {}).items():
            if isinstance(spec, str):
                fields[name] = FieldSpec(path=spec)
            else:
                fields[name] = FieldSpec(
                    path=spec.get("path", ""), transform=spec.get("transform", "raw")
                )
        return cls(
            script_id=raw.get("script_id", "__NEXT_DATA__"),
            records_path=raw.get("records_path", raw.get("data_path", "")),
            fields=fields,
            url_base=raw.get("url_base", ""),
            total_path=raw.get("total_path", ""),
        )


@dataclass
class ExtractedRecords:
    rows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: What the page said it holds, when it says so at all.
    claimed_total: int | None = None

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def is_short(self) -> bool:
        """Whether the page claims more results than were extracted.

        A page that renders a subset of its own result set is the most
        expensive failure this adapter can have, because it does not look like
        a failure: the run completes, the workbook builds, and the market
        simply appears smaller than it is.
        """
        return self.claimed_total is not None and self.count < self.claimed_total


def extract_records(html: str, spec: EmbeddedSpec) -> ExtractedRecords:
    """Pull every record out of a page's embedded payload."""
    result = ExtractedRecords()
    payload = find_embedded_json(html, spec.script_id)
    if payload is None:
        result.problems.append(
            f"no readable <script id={spec.script_id!r}> block on the page"
        )
        return result

    records = dig(payload, spec.records_path) if spec.records_path else payload
    if not isinstance(records, list):
        result.problems.append(
            f"{spec.records_path!r} is not a list of records "
            f"(found {type(records).__name__})"
        )
        return result

    for index, record in enumerate(records):
        row: dict[str, Any] = {}
        for name, field_spec in spec.fields.items():
            value = field_spec.read(record)
            if name == "lot_sqft" and isinstance(value, tuple):
                row["lot_sqft"], row["lot_size_is_rounded"] = value
            else:
                row[name] = value
        if spec.url_base and row.get("source_url"):
            path = str(row["source_url"])
            if path.startswith("/"):
                row["source_url"] = spec.url_base.rstrip("/") + path
        if not row.get("address"):
            result.problems.append(f"record {index} has no address; skipped")
            continue
        result.rows.append(row)

    _check_count(result, payload, spec)
    return result


def _check_count(result: ExtractedRecords, payload: Any, spec: EmbeddedSpec) -> None:
    """Compare what came out against what the page says it holds.

    The cheapest guard there is against the worst failure this adapter has: a
    page that serves a subset of its own result set. One index claimed 166
    properties and surfaced nine, and nothing about the run looked wrong -- the
    workbook built, the statistics computed, and the market simply appeared
    smaller than it is. A source that agrees with itself has earned a little
    trust; one that does not has to say so out loud.
    """
    if not spec.total_path:
        return
    claimed = dig(payload, spec.total_path)
    if isinstance(claimed, str):
        digits = "".join(c for c in claimed if c.isdigit())
        claimed = int(digits) if digits else None
    if not isinstance(claimed, int) or isinstance(claimed, bool):
        result.problems.append(
            f"{spec.total_path!r} did not yield a usable count, so the extraction "
            "could not be checked against the page's own total"
        )
        return
    result.claimed_total = claimed
    if result.is_short:
        result.problems.append(
            f"the page claims {claimed} results but {result.count} were extracted. "
            "Treat this page as incomplete rather than the market as small."
        )


def read_lot_size(record: Any, path: str) -> tuple[float | None, bool]:
    """Convenience for the one field that returns two values."""
    return as_sqft(dig(record, path))
