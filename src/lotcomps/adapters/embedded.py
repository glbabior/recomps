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

from lotcomps.model.comp import SQFT_PER_ACRE

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


TRANSFORMS = {
    "money": as_money,
    "date": as_date,
    "tag_date": as_tag_date,
    # Returns (sqft, derived_from_acres); `extract_records` unpacks it.
    "sqft": as_sqft,
    "text": lambda v: str(v).strip() if v not in (None, "") else None,
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
        )


@dataclass
class ExtractedRecords:
    rows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)


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
    return result


def read_lot_size(record: Any, path: str) -> tuple[float | None, bool]:
    """Convenience for the one field that returns two values."""
    return as_sqft(dig(record, path))
