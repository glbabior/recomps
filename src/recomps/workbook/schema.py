"""Column schemas for the comp sheets.

The workbook is built from live Excel formulas, and a formula names its operands
by column letter. Under a land profile the sold-comp sheet computes ``=C2/D2``;
under a single-family profile three attribute columns appear and the same
calculation becomes ``=C2/D2`` over different columns entirely. Any literal
letter written into this codebase is a bug waiting for the first non-land run.

So no formula anywhere names a letter. Columns are declared with semantic keys,
the schema resolves a key to whatever letter it landed on, and formulas are
templates over those keys::

    Column(key="ppsf", formula="=IF(ISNUMBER({metric}{row}),...)")

That indirection is the whole reason a profile can reshape the output instead of
merely relabelling it.

Formula vocabulary is deliberately pre-2007: COUNT/COUNTA/COUNTIF, AVERAGE,
AVERAGEIF, MEDIAN, MIN/MAX, ROUND. No XLOOKUP, no FILTER, no dynamic arrays --
the output has to open cleanly in old Excel and in the viewers people actually
have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recomps.config.profile import CompProfile, Denominator

# Number formats, kept in one place so the sheets stay visually consistent.
FMT_MONEY = '"$"#,##0'
FMT_MONEY_CENTS = '"$"#,##0.00'
FMT_INT = "#,##0"
FMT_PERCENT = "0.0%"
FMT_DATE = "yyyy-mm-dd"
FMT_TEXT = "@"
FMT_DECIMAL = "0.00"


@dataclass
class Column:
    """One column of a comp sheet."""

    key: str
    header: str
    #: Attribute read off the comp record, when the column holds a value.
    attr: str | None = None
    #: Formula template over column keys and ``{row}``, when it holds a formula.
    formula: str | None = None
    number_format: str = FMT_TEXT
    width: float = 14.0

    @property
    def is_formula(self) -> bool:
        return self.formula is not None


@dataclass
class SheetSchema:
    """An ordered set of columns that can resolve keys to letters."""

    name: str
    columns: list[Column] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_key = {c.key: i for i, c in enumerate(self.columns)}
        if len(self._by_key) != len(self.columns):
            raise ValueError(f"{self.name}: duplicate column keys")

    def index(self, key: str) -> int:
        """1-based column index."""
        try:
            return self._by_key[key] + 1
        except KeyError:
            raise KeyError(f"{self.name}: no column {key!r}") from None

    def letter(self, key: str) -> str:
        from openpyxl.utils import get_column_letter

        return get_column_letter(self.index(key))

    def has(self, key: str) -> bool:
        return key in self._by_key

    def render(self, template: str, row: int | str) -> str:
        """Resolve a formula template's column keys and row placeholder."""
        values = {c.key: self.letter(c.key) for c in self.columns}
        values["row"] = row
        return template.format(**values)

    def range(self, key: str, first_row: int, last_row: int, *, absolute: bool = False) -> str:
        """A1-style range for one column, e.g. ``D2:D47``."""
        letter = self.letter(key)
        dollar = "$" if absolute else ""
        return f"{dollar}{letter}{dollar}{first_row}:{dollar}{letter}{dollar}{last_row}"


_ATTRIBUTE_COLUMNS: dict[str, Column] = {
    "beds": Column("beds", "Beds", attr="beds", number_format=FMT_DECIMAL, width=8),
    "baths": Column("baths", "Baths", attr="baths", number_format=FMT_DECIMAL, width=8),
    "year_built": Column(
        "year_built", "Year Built", attr="year_built", number_format="0", width=11
    ),
    "condition": Column("condition", "Condition", attr="condition", width=16),
}


def _metric_columns(profile: CompProfile) -> list[Column]:
    """The size column, the $/sqft column, profile attributes, and (for improved
    profiles) lot size retained as a secondary measure."""
    metric_attr = profile.metric.value
    columns = [
        Column(
            "metric",
            profile.metric_label,
            attr=metric_attr,
            number_format=FMT_INT,
            width=15,
        ),
        Column(
            "ppsf",
            "$ / sq ft",
            # Guarded, because "not found" renders as an em dash and dividing by
            # it yields #VALUE! -- which then poisons every aggregate over this
            # column: the average, the median, the min, the max and the core
            # view all come back as errors from one sale with no published size.
            # A blank is the honest result for a rate that cannot be computed,
            # and the statistics skip it rather than break on it.
            formula='=IF(ISNUMBER({metric}{row}),{price}{row}/{metric}{row},"")',
            number_format=FMT_MONEY_CENTS,
            width=12,
        ),
    ]
    columns += [_ATTRIBUTE_COLUMNS[a] for a in profile.attributes if a in _ATTRIBUTE_COLUMNS]
    if profile.metric is Denominator.LIVING_SQFT:
        columns.append(
            Column("lot_sqft", "Lot (sq ft)", attr="lot_sqft", number_format=FMT_INT, width=13)
        )
    return columns


def sold_schema(profile: CompProfile) -> SheetSchema:
    """Sold Comps columns (S8), adapted to the active profile."""
    return SheetSchema(
        "Sold Comps",
        [
            Column("address", "Address", attr="address", width=30),
            Column("sold_date", "Sold Date", attr="sold_date", number_format=FMT_DATE, width=12),
            Column("price", "Sold Price", attr="sold_price", number_format=FMT_MONEY, width=14),
            *_metric_columns(profile),
            Column("brokerage", "Listing Brokerage", attr="brokerage", width=30),
            Column("agent", "Agent (where shown)", attr="agent", width=22),
            Column(
                "final_list",
                "Final List Price",
                attr="final_list_price",
                number_format=FMT_MONEY,
                width=15,
            ),
            Column(
                "original_list",
                "Original List Price",
                attr="original_list_price",
                number_format=FMT_MONEY,
                width=16,
            ),
            Column(
                "sold_to_ask",
                "Sold / Ask",
                # Same guard, same reason: a sale with no published asking price
                # would return #VALUE! into the average-ratio and share-at-ask
                # cells on the Summary. No ask, no ratio -- not an error.
                formula=(
                    '=IF(ISNUMBER({final_list}{row}),'
                    '{price}{row}/{final_list}{row},"")'
                ),
                number_format=FMT_PERCENT,
                width=11,
            ),
            Column("area", "Area", attr="area", width=8),
        ],
    )


def active_schema(profile: CompProfile) -> SheetSchema:
    """Active Listings columns (S8), adapted to the active profile."""
    return SheetSchema(
        "Active Listings",
        [
            Column("address", "Address", attr="address", width=30),
            Column("price", "List Price", attr="list_price", number_format=FMT_MONEY, width=14),
            *_metric_columns(profile),
            Column("brokerage", "Listing Brokerage", attr="brokerage", width=30),
            Column("agent", "Agent", attr="agent", width=22),
            Column("area", "Area", attr="area", width=8),
        ],
    )
