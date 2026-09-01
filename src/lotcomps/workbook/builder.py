"""Workbook generation (S8).

Design rule for the whole module: **the workbook holds formulas, not answers.**
Every statistic on Summary is an Excel expression over the comp sheets, so an
owner who deletes a bad comp or corrects a lot size watches the valuation move.
A workbook of precomputed numbers would be a screenshot; this one is a model.

That has two consequences the code has to respect.

*No literal cell references.* Column letters come from the schema (see
`lotcomps.workbook.schema`) and row numbers are recorded as sheets are written,
never counted by hand. Insert a row anywhere and the references still resolve.

*openpyxl writes no cached values.* A file full of formulas and no results
renders blank in Google Sheets, macOS Quick Look, and most preview panes, which
is exactly where a portfolio reader will open it first. Setting
``fullCalcOnLoad`` makes the first real spreadsheet that opens it compute and
store the values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from lotcomps.model.comp import NOT_FOUND
from lotcomps.pipeline.run import RunResult
from lotcomps.workbook import styles as st
from lotcomps.workbook.schema import (
    FMT_INT,
    FMT_MONEY,
    FMT_MONEY_CENTS,
    FMT_PERCENT,
    SheetSchema,
    active_schema,
    sold_schema,
)

FIRST_DATA_ROW = 2


@dataclass
class SheetRefs:
    """Where things landed, so other sheets can point at them by name."""

    rows: dict[str, int] = field(default_factory=dict)

    def __setitem__(self, key: str, row: int) -> None:
        self.rows[key] = row

    def __getitem__(self, key: str) -> int:
        return self.rows[key]

    def cell(self, key: str, column: str = "B") -> str:
        return f"{column}{self.rows[key]}"


def _quote(sheet_name: str) -> str:
    return f"'{sheet_name}'"


def _col_range(sheet: str, schema: SheetSchema, key: str, last_row: int) -> str:
    """An absolute cross-sheet range for one schema column."""
    last_row = max(last_row, FIRST_DATA_ROW)
    return f"{_quote(sheet)}!{schema.range(key, FIRST_DATA_ROW, last_row, absolute=True)}"


# ---------------------------------------------------------------------------
# Comp sheets
# ---------------------------------------------------------------------------


def _write_comp_sheet(
    ws: Worksheet, schema: SheetSchema, records: list, source_note: str
) -> int:
    for column in schema.columns:
        cell = ws.cell(row=1, column=schema.index(column.key), value=column.header)
        st.style_header(cell)
        ws.column_dimensions[schema.letter(column.key)].width = column.width
    ws.freeze_panes = "A2"

    row = FIRST_DATA_ROW
    for record in records:
        for column in schema.columns:
            cell = ws.cell(row=row, column=schema.index(column.key))
            if column.is_formula:
                cell.value = schema.render(column.formula, row)
            else:
                value = getattr(record, column.attr, None)
                if value is None or value == "":
                    # An honest gap, never an imputed value (S7).
                    cell.value = NOT_FOUND
                    cell.alignment = st.CENTER
                elif hasattr(value, "value"):  # Enum, e.g. Quadrant
                    cell.value = value.value
                elif isinstance(value, date):
                    cell.value = value
                else:
                    cell.value = value
            cell.font = st.BODY
            if cell.value != NOT_FOUND:
                cell.number_format = column.number_format
        row += 1

    last = row - 1
    note = ws.cell(row=row + 1, column=1, value=source_note)
    note.font = st.NOTE
    return last


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _write_summary(
    ws: Worksheet,
    result: RunResult,
    sold: SheetSchema,
    active: SheetSchema,
    sold_last: int,
    active_last: int,
    changes: list[tuple[str, str]] | None,
) -> SheetRefs:
    refs = SheetRefs()
    profile = result.profile
    S, A = "Sold Comps", "Active Listings"

    def sold_range(key: str) -> str:
        return _col_range(S, sold, key, sold_last)

    def active_range(key: str) -> str:
        return _col_range(A, active, key, active_last)

    ws.column_dimensions["A"].width = 38
    for letter in ("B", "C", "D", "E", "F", "G"):
        ws.column_dimensions[letter].width = 16

    ws["A1"] = f"{result.market_description or result.market_name} - Price per Square Foot"
    ws["A1"].font = st.TITLE
    ws["A2"] = (
        f"Profile: {profile.name} ({profile.property_type.value}). "
        f"Window {result.window_start} to {result.window_end}. "
        f"Pulled {result.pull_date}. Source: {result.researcher or 'unknown'}."
    )
    ws["A2"].font = st.SUBTITLE

    row = 4
    ws.cell(row=row, column=2, value=f"Sold ({result.window_start} - {result.window_end})").font = (
        st.BODY_BOLD
    )
    ws.cell(row=row, column=3, value="Active listings").font = st.BODY_BOLD

    stat_rows = [
        ("count", "Number of properties", f"=COUNTA({sold_range('address')})",
         f"=COUNTA({active_range('address')})", FMT_INT),
        ("avg_ppsf", "Average $/sq ft", f"=AVERAGE({sold_range('ppsf')})",
         f"=AVERAGE({active_range('ppsf')})", FMT_MONEY_CENTS),
        ("median_ppsf", "Median $/sq ft", f"=MEDIAN({sold_range('ppsf')})",
         f"=MEDIAN({active_range('ppsf')})", FMT_MONEY_CENTS),
        ("avg_price", "Average price", f"=AVERAGE({sold_range('price')})",
         f"=AVERAGE({active_range('price')})", FMT_MONEY),
        ("median_price", "Median price", f"=MEDIAN({sold_range('price')})",
         f"=MEDIAN({active_range('price')})", FMT_MONEY),
        ("avg_size", f"Average {profile.metric_label.lower()}",
         f"=AVERAGE({sold_range('metric')})", f"=AVERAGE({active_range('metric')})", FMT_INT),
        ("min_ppsf", "Min $/sq ft", f"=MIN({sold_range('ppsf')})",
         f"=MIN({active_range('ppsf')})", FMT_MONEY_CENTS),
        ("max_ppsf", "Max $/sq ft", f"=MAX({sold_range('ppsf')})",
         f"=MAX({active_range('ppsf')})", FMT_MONEY_CENTS),
        ("avg_ratio", "Average sold-to-ask ratio",
         f"=AVERAGE({sold_range('sold_to_ask')})", None, FMT_PERCENT),
        ("share_at_ask", "Sold at or above final ask",
         f'=COUNTIF({sold_range("sold_to_ask")},">=1")/COUNTA({sold_range("sold_to_ask")})',
         None, FMT_PERCENT),
    ]
    for key, label, sold_formula, active_formula, fmt in stat_rows:
        row += 1
        refs[key] = row
        ws.cell(row=row, column=1, value=label).font = st.BODY
        c = ws.cell(row=row, column=2, value=sold_formula)
        c.font, c.number_format = st.BODY, fmt
        if active_formula:
            c = ws.cell(row=row, column=3, value=active_formula)
            c.font, c.number_format = st.BODY, fmt

    # -- subject valuation block ------------------------------------------
    row += 2
    ws.cell(row=row, column=1, value=f"{profile.subject.label} - Estimated Value").font = st.SECTION

    row += 1
    refs["subject_size"] = row
    ws.cell(row=row, column=1, value=f"{profile.metric_label} (editable)").font = st.BODY
    size_cell = ws.cell(row=row, column=2, value=profile.subject_size())
    st.style_editable(size_cell, FMT_INT)

    bracket_low, bracket_high = None, None
    if result.valuation:
        bracket_low, bracket_high = result.valuation.bracket_low, result.valuation.bracket_high

    row += 1
    refs["bracket_low"] = row
    ws.cell(row=row, column=1, value="Similar-size bracket (editable low / high)").font = st.BODY
    st.style_editable(ws.cell(row=row, column=2, value=bracket_low), FMT_INT)
    st.style_editable(ws.cell(row=row, column=3, value=bracket_high), FMT_INT)

    lo = f"$B${refs['bracket_low']}"
    hi = f"$C${refs['bracket_low']}"
    bracket_attr = profile.similar_bracket.attribute.value
    # The bracket keys on whichever attribute the profile names, which is not
    # always the $/sqft denominator, so resolve it rather than reusing 'metric'.
    bracket_key = "metric" if bracket_attr == profile.metric.value else "lot_sqft"
    if not sold.has(bracket_key):
        bracket_key = "metric"
    size_range = sold_range(bracket_key)
    ppsf_range = sold_range("ppsf")
    # SUMPRODUCT rather than COUNTIFS/AVERAGEIFS: it predates 2007, and it lets
    # the bracket recompute live when the owner edits the bounds above.
    membership = f"({size_range}>={lo})*({size_range}<={hi})"

    row += 1
    refs["bracket_count"] = row
    ws.cell(row=row, column=1, value="Comps in bracket").font = st.BODY
    c = ws.cell(row=row, column=2, value=f"=SUMPRODUCT({membership})")
    c.font, c.number_format = st.BODY, FMT_INT

    row += 1
    refs["bracket_ppsf"] = row
    ws.cell(row=row, column=1, value="Bracket average $/sq ft").font = st.BODY
    c = ws.cell(
        row=row,
        column=2,
        value=f"=IF(B{refs['bracket_count']}=0,\"\",SUMPRODUCT({membership}*{ppsf_range})"
        f"/B{refs['bracket_count']})",
    )
    c.font, c.number_format = st.BODY, FMT_MONEY_CENTS

    size_ref = f"$B${refs['subject_size']}"
    basis_rows = [
        ("value_similar", "Estimate - similar-size sold average (primary)",
         f"=B{refs['bracket_ppsf']}*{size_ref}", True),
        ("value_median", "Estimate - all-sold median $/sq ft",
         f"=B{refs['median_ppsf']}*{size_ref}", False),
        ("value_avg", "Estimate - all-sold average $/sq ft",
         f"=B{refs['avg_ppsf']}*{size_ref}", False),
        ("value_active", "Estimate - active-listing median $/sq ft (asking)",
         f"=C{refs['median_ppsf']}*{size_ref}", False),
    ]
    for key, label, formula, is_primary in basis_rows:
        row += 1
        refs[key] = row
        label_cell = ws.cell(row=row, column=1, value=label)
        value_cell = ws.cell(row=row, column=2, value=formula)
        value_cell.number_format = FMT_MONEY
        if is_primary:
            label_cell.font = value_cell.font = st.PRIMARY
            label_cell.fill = value_cell.fill = st.PRIMARY_FILL
        else:
            label_cell.font = value_cell.font = st.BODY

    # -- by-area table -----------------------------------------------------
    row += 2
    ws.cell(row=row, column=1, value="By area").font = st.SECTION
    row += 1
    headers = [
        "Area", "Sold", "Sold avg $/sqft", "Sold median $/sqft", "Sold median price",
        f"Avg {profile.metric_label.lower()}", "Active", "Active avg $/sqft",
    ]
    for i, header in enumerate(headers, start=1):
        st.style_header(ws.cell(row=row, column=i, value=header))
    for area_row in result.area_table.rows:
        row += 1
        values = [
            area_row.area, area_row.sold_count, area_row.sold_avg_ppsf,
            area_row.sold_median_ppsf, area_row.sold_median_price, area_row.sold_avg_size,
            area_row.active_count, area_row.active_avg_ppsf,
        ]
        formats = [None, FMT_INT, FMT_MONEY_CENTS, FMT_MONEY_CENTS, FMT_MONEY, FMT_INT,
                   FMT_INT, FMT_MONEY_CENTS]
        for i, (value, fmt) in enumerate(zip(values, formats, strict=True), start=1):
            cell = ws.cell(row=row, column=i, value=value if value is not None else NOT_FOUND)
            cell.font = st.BODY
            if fmt and value is not None:
                cell.number_format = fmt

    # -- what changed ------------------------------------------------------
    if changes:
        row += 2
        ws.cell(row=row, column=1, value="What changed since the last run").font = st.SECTION
        for label, detail in changes:
            row += 1
            ws.cell(row=row, column=1, value=label).font = st.BODY_BOLD
            cell = ws.cell(row=row, column=2, value=detail)
            cell.font, cell.alignment = st.BODY, st.WRAP

    # -- notes -------------------------------------------------------------
    row += 2
    ws.cell(row=row, column=1, value="Notes").font = st.SECTION
    notes = list(result.caveats) + list(result.area_table.notes) + list(result.warnings)
    notes.append(
        "Blue text on yellow is editable. Every statistic above is a live formula over the "
        "comp sheets, so corrections propagate."
    )
    notes.append(
        "Market research, not an appraisal. The author is not a licensed appraiser."
    )
    for note in notes:
        row += 1
        cell = ws.cell(row=row, column=1, value=f"- {note}")
        cell.font, cell.alignment = st.NOTE, st.WRAP
    return refs


# ---------------------------------------------------------------------------
# Pricing guidance
# ---------------------------------------------------------------------------


def _write_guidance(ws: Worksheet, result: RunResult, summary: SheetRefs) -> None:
    guidance = result.guidance
    ws.column_dimensions["A"].width = 34
    for letter, width in (("B", 16), ("C", 26), ("D", 62)):
        ws.column_dimensions[letter].width = width

    ws["A1"] = f"Pricing Guidance - {result.profile.subject.label}"
    ws["A1"].font = st.TITLE
    ws["A2"] = guidance.disclaimer if guidance else ""
    ws["A2"].font = st.SUBTITLE

    row = 4
    ws.cell(row=row, column=1, value="Market value anchors").font = st.SECTION
    for label, key in (
        ("Best estimate (similar-size sold comps)", "value_similar"),
        ("Conservative (all-sold median $/sqft)", "value_median"),
    ):
        row += 1
        ws.cell(row=row, column=1, value=label).font = st.BODY
        cell = ws.cell(row=row, column=2, value=f"=Summary!B{summary[key]}")
        cell.font, cell.number_format = st.BODY, FMT_MONEY

    row += 2
    ws.cell(row=row, column=1, value="Listing strategies").font = st.SECTION
    row += 1
    for i, header in enumerate(
        ["Strategy", "List price", "Expected sale range", "Trade-off"], start=1
    ):
        st.style_header(ws.cell(row=row, column=i, value=header))

    for strategy in (guidance.strategies if guidance else []):
        row += 1
        label = ws.cell(row=row, column=1, value=strategy.label)
        label.font = st.BODY_BOLD if strategy.recommended else st.BODY
        label.alignment = st.WRAP
        # The list price is the owner's decision, so it is an input, not output.
        st.style_editable(ws.cell(row=row, column=2, value=strategy.list_price), FMT_MONEY)
        span = (
            f"${strategy.expected_low:,.0f} - ${strategy.expected_high:,.0f}"
            if strategy.expected_low and strategy.expected_high
            else NOT_FOUND
        )
        ws.cell(row=row, column=3, value=span).font = st.BODY
        cell = ws.cell(row=row, column=4, value=strategy.tradeoff)
        cell.font, cell.alignment = st.BODY, st.WRAP
        ws.row_dimensions[row].height = 42

    row += 2
    ws.cell(row=row, column=1, value="Floor / walk-away reference").font = st.SECTION
    row += 1
    ws.cell(row=row, column=1, value=guidance.floor_basis if guidance else "").font = st.BODY
    cell = ws.cell(row=row, column=2, value=f"=ROUND(Summary!B{summary['value_median']}*0.95,-3)")
    cell.font, cell.number_format = st.BODY_BOLD, FMT_MONEY

    row += 2
    ws.cell(row=row, column=1, value="Notes").font = st.SECTION
    for note in [
        (
            "Expected sale ranges come from this run's own sold-to-ask distribution, not "
            "fixed percentages, so they widen and narrow with the market."
        ),
        (
            "Strategy A sits just under a search-band edge on purpose: buyers filter by "
            "price band, and reaching the band below costs less than it appears to."
        ),
        guidance.disclaimer if guidance else "",
    ]:
        if not note:
            continue
        row += 1
        cell = ws.cell(row=row, column=1, value=f"- {note}")
        cell.font, cell.alignment = st.NOTE, st.WRAP


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def _write_agents(
    ws: Worksheet, result: RunResult, sold: SheetSchema, sold_last: int
) -> None:
    analysis = result.agent_analysis
    S = "Sold Comps"
    agent_range = _col_range(S, sold, "agent", sold_last)
    ratio_range = _col_range(S, sold, "sold_to_ask", sold_last)
    brokerage_range = _col_range(S, sold, "brokerage", sold_last)

    ws.column_dimensions["A"].width = 34
    for letter in ("B", "C", "D", "E"):
        ws.column_dimensions[letter].width = 18

    ws["A1"] = "Who is closing sales here - listing side"
    ws["A1"].font = st.TITLE
    ws["A2"] = (
        f"{analysis.attributed} of {analysis.total} sales attributed. "
        "Volume is a signal, not an endorsement - interview two or three."
    )
    ws["A2"].font = st.SUBTITLE

    row = 4
    ws.cell(row=row, column=1, value="Brokerage families").font = st.SECTION
    row += 1
    for i, header in enumerate(["Brokerage family", "Closings", "Share"], start=1):
        st.style_header(ws.cell(row=row, column=i, value=header))
    for brokerage in analysis.brokerages:
        row += 1
        ws.cell(row=row, column=1, value=brokerage.family).font = st.BODY
        # COUNTIF with a wildcard keeps the tally live: correct a brokerage name
        # on the comp sheet and the count follows.
        cell = ws.cell(
            row=row,
            column=2,
            value=f'=COUNTIF({brokerage_range},"{_wildcard(brokerage.family)}")',
        )
        cell.font, cell.number_format = st.BODY, FMT_INT
        cell = ws.cell(row=row, column=3, value=brokerage.share)
        cell.font, cell.number_format = st.BODY, FMT_PERCENT

    row += 2
    ws.cell(row=row, column=1, value="Agent performance").font = st.SECTION
    row += 1
    for i, header in enumerate(
        ["Agent", "Brokerage", "Closings", "Avg sold / ask", "Pattern"], start=1
    ):
        st.style_header(ws.cell(row=row, column=i, value=header))

    for agent in analysis.agents:
        row += 1
        fill = None
        if agent.flag == "shortlist":
            fill = st.SHORTLIST_FILL
        elif agent.flag == "caution":
            fill = st.CAUTION_FILL
        name = ws.cell(row=row, column=1, value=agent.agent)
        broker = ws.cell(row=row, column=2, value=agent.brokerage or NOT_FOUND)
        closings = ws.cell(
            row=row, column=3, value=f'=COUNTIF({agent_range},"{agent.agent}")'
        )
        closings.number_format = FMT_INT
        ratio = ws.cell(
            row=row,
            column=4,
            value=f'=IF(C{row}=0,"",AVERAGEIF({agent_range},"{agent.agent}",{ratio_range}))',
        )
        ratio.number_format = FMT_PERCENT
        pattern = ws.cell(
            row=row,
            column=5,
            value=(agent.flag or "") + (" - dual agency" if agent.dual_agency else ""),
        )
        for cell in (name, broker, closings, ratio, pattern):
            cell.font = st.BODY
            if fill:
                cell.fill = fill

    row += 2
    ws.cell(row=row, column=1, value="Notes").font = st.SECTION
    for note in analysis.caveats:
        row += 1
        cell = ws.cell(row=row, column=1, value=f"- {note}")
        cell.font, cell.alignment = st.NOTE, st.WRAP


def _wildcard(family: str) -> str:
    """A COUNTIF pattern that matches a brokerage family's trading names."""
    return f"{family.split(' ')[0]}*" if family else "*"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_workbook(result: RunResult, changes: list[tuple[str, str]] | None = None) -> Workbook:
    wb = Workbook()
    # Without this the file carries formulas and no values, and renders blank in
    # every viewer that does not compute (Google Sheets, Quick Look, previews).
    wb.calculation.fullCalcOnLoad = True

    sold = sold_schema(result.profile)
    active = active_schema(result.profile)

    summary_ws = wb.active
    summary_ws.title = "Summary"
    guidance_ws = wb.create_sheet("Pricing Guidance")
    sold_ws = wb.create_sheet("Sold Comps")
    active_ws = wb.create_sheet("Active Listings")
    agents_ws = wb.create_sheet("Agents")

    pulled = f"Pulled {result.pull_date} from {result.researcher or 'unknown source'}."
    sold_last = _write_comp_sheet(
        sold_ws, sold, result.sold,
        f"{pulled} Window {result.window_start} to {result.window_end}. "
        f'"{NOT_FOUND}" means the value was searched for and not found - never estimated.',
    )
    active_last = _write_comp_sheet(
        active_ws, active, result.active,
        f"{pulled} Active listings reflect asking prices, not transactions.",
    )

    refs = _write_summary(
        summary_ws, result, sold, active, sold_last, active_last, changes
    )
    _write_guidance(guidance_ws, result, refs)
    _write_agents(agents_ws, result, sold, sold_last)
    return wb


def write_workbook(
    result: RunResult, path: str | Path, changes: list[tuple[str, str]] | None = None
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(result, changes).save(target)
    return target
