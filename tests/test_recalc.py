"""Verify the workbook's formulas actually compute.

Two levels, because the strong check needs a spreadsheet engine that is not
installed everywhere:

*Always* — the formula strings are checked structurally, and every cross-sheet
reference is checked to point at a sheet and range that exist. A typo'd sheet
name produces `#REF!` for a user and nothing at all for a formula-string test
that does not resolve it.

*When LibreOffice is available* — the workbook is recalculated headlessly and
every computed cell is checked for an error value. CI installs it; a laptop
usually has not, and the test skips rather than pretending.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date

import pytest
from openpyxl import load_workbook

from lotcomps.markets.demoville import MARKET
from lotcomps.pipeline.run import run_pipeline
from lotcomps.research.fixture import FixtureResearcher
from lotcomps.workbook.builder import build_workbook

AS_OF = date(2026, 8, 31)
ERROR_VALUES = {"#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#N/A", "#NULL!", "#NUM!"}

SHEET_REF = re.compile(r"'([^']+)'!\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)")
LOCAL_REF = re.compile(r"(?<![A-Z0-9'!$])\$?([A-Z]{1,2})\$?(\d+)(?![0-9(])")


@pytest.fixture(scope="module")
def workbook_path(tmp_path_factory):
    result = run_pipeline(
        MARKET, MARKET.profiles()["demo-lots"], FixtureResearcher(), window_end=AS_OF
    )
    path = tmp_path_factory.mktemp("recalc") / "comps.xlsx"
    build_workbook(result).save(path)
    return path


def _formula_cells(wb):
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    yield ws, cell


def test_every_cross_sheet_reference_resolves(workbook_path):
    """A typo'd sheet name is invisible until someone opens the file."""
    wb = load_workbook(workbook_path)
    names = set(wb.sheetnames)
    problems = []
    for ws, cell in _formula_cells(wb):
        for sheet, _, first, _, last in SHEET_REF.findall(cell.value):
            if sheet not in names:
                problems.append(f"{ws.title}!{cell.coordinate}: unknown sheet {sheet!r}")
            elif int(last) < int(first):
                problems.append(f"{ws.title}!{cell.coordinate}: inverted range in {cell.value}")
    assert not problems, "\n".join(problems)


def test_local_references_point_at_populated_cells(workbook_path):
    """Catches an off-by-one in the row bookkeeping the Summary sheet does."""
    wb = load_workbook(workbook_path)
    problems = []
    for ws, cell in _formula_cells(wb):
        body = SHEET_REF.sub(" ", cell.value)
        for column, row in LOCAL_REF.findall(body):
            target = ws[f"{column}{row}"]
            if target.value is None:
                problems.append(
                    f"{ws.title}!{cell.coordinate} ({cell.value}) points at empty "
                    f"{column}{row}"
                )
    assert not problems, "\n".join(problems)


def test_no_formula_divides_by_a_possibly_empty_cell(workbook_path):
    """Every division either guards with IF or divides by a data column."""
    wb = load_workbook(workbook_path)
    for ws, cell in _formula_cells(wb):
        if "/" not in cell.value:
            continue
        assert "IF(" in cell.value or "!" in cell.value or re.search(
            r"/\$?[A-Z]{1,2}\$?\d+", cell.value
        ), f"{ws.title}!{cell.coordinate}: unguarded division in {cell.value}"


@pytest.mark.skipif(
    shutil.which("soffice") is None and shutil.which("libreoffice") is None,
    reason="LibreOffice not installed; CI runs this check",
)
def test_libreoffice_recalculates_without_errors(workbook_path, tmp_path):
    """The strong check: a real spreadsheet engine computes every formula."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    subprocess.run(
        [soffice, "--headless", "--convert-to", "xlsx:Calc MS Excel 2007 XML",
         "--outdir", str(tmp_path), str(workbook_path)],
        check=True, capture_output=True, timeout=300,
    )
    recalculated = tmp_path / workbook_path.name
    assert recalculated.exists(), "LibreOffice produced no output"

    wb = load_workbook(recalculated, data_only=True)
    errors = [
        f"{ws.title}!{cell.coordinate} = {cell.value}"
        for ws in wb.worksheets
        for row in ws.iter_rows()
        for cell in row
        if isinstance(cell.value, str) and cell.value in ERROR_VALUES
    ]
    assert not errors, "formula errors after recalculation:\n" + "\n".join(errors)

    summary = wb["Summary"]
    computed = [
        c.value for row in summary.iter_rows() for c in row
        if isinstance(c.value, (int, float))
    ]
    assert len(computed) > 10, "Summary computed almost nothing; formulas may not have run"
