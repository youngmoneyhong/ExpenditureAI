"""Back up, verify, and optionally refresh an existing annual workbook.

Run with the project's Python: scripts/verify_statement.py YEAR [--fixture] [--apply].
Fixtures use temporary sheets, removed in finally; existing transactions are read-only.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from financial_statement import calculate_statement, column_letter, month_number, statement_matrix, statement_issues
from sheets import SheetClient
from validators import GOOGLE_SHEET_COLUMNS


def snapshot(spreadsheet):
    sheets = {}
    for worksheet in spreadsheet.worksheets():
        sheets[worksheet.title] = {
            "id": worksheet.id,
            "formulas": worksheet.get_all_values(value_render_option="FORMULA"),
            "values": worksheet.get_all_values(),
        }
    return {"id": spreadsheet.id, "title": spreadsheet.title,
            "metadata": spreadsheet.fetch_sheet_metadata(), "sheets": sheets}


def records(values):
    return [dict(zip(values[0], row)) for row in values[1:]] if values else []


def reconcile(summary, months, year, layout):
    data = summary.get_all_values(value_render_option="UNFORMATTED_VALUE")
    expected_months = []
    for index, month in enumerate(months, 1):
        calculated = calculate_statement(records(month.get_all_values()), int(year), month_number(month.title))
        expected_months.append(calculated)
        for row in layout["detail_rows"] + layout["subtotal_rows"]:
            key = next((key for key, value in layout.items() if isinstance(value, int) and value == row
                        and key != "year_total_col" and not key.endswith(("_first_detail", "_last_detail"))), None)
            key = key or data[row - 1][0].strip()
            actual = data[row - 1][index] if len(data[row - 1]) > index else ""
            expected = calculated[key]
            assert_equal(actual, expected, f"{month.title} {key}")
    for row in layout["detail_rows"] + layout["subtotal_rows"]:
        key = next((key for key, value in layout.items() if isinstance(value, int) and value == row
                    and key != "year_total_col" and not key.endswith(("_first_detail", "_last_detail"))), None)
        key = key or data[row - 1][0].strip()
        values = [month[key] for month in expected_months]
        expected = None if not values or any(value is None for value in values) else sum(values)
        actual = data[row - 1][-1] if len(data[row - 1]) == len(months) + 2 else ""
        assert_equal(actual, expected, f"Year Total {key}")
    return [{key: None if month[key] is None else str(month[key]) for key in ("income_total", "gross", "offset_total", "net_expenditure", "operating", "allocation_total", "final")} for month in expected_months]


def assert_equal(actual, expected, label):
    if expected is None:
        assert actual == "", f"{label}: expected missing, got {actual!r}"
    else:
        assert isinstance(actual, (int, float)), f"{label}: expected {expected}, got {actual!r}"
        assert abs(Decimal(str(actual)) - expected) < Decimal("0.005"), f"{label}: {actual} != {expected}"


def verify_fixture(spreadsheet):
    owned = []
    try:
        # The explicit non-production year also prevents a concurrent refresh using this tab.
        source = spreadsheet.add_worksheet(title="January 2099", rows=100, cols=12)
        owned.append(source)
        result = spreadsheet.add_worksheet(title="_Statement Test " + uuid4().hex[:6], rows=100, cols=3)
        owned.append(result)
        rows = []
        for category, amount, flow, kind in [
            ("Net Salary", 5000, "inflow", "income"),
            ("Food", -1000, "outflow", "expense"),
            ("Parent Allowance", -500, "outflow", "expense"),
            ("Reimbursement", -100, "inflow", "refund"),
            ("ETF Contributions", -2000, "outflow", "contribution"),
        ]:
            record = dict(check="Yes", date="2099-01-05", source="TEST ONLY", category=category,
                          description="TEST ONLY - user-provided validation fixture", amount=amount,
                          currency="SGD", money_flow=flow, transaction_type=kind)
            rows.append([record.get(key, "") for key in GOOGLE_SHEET_COLUMNS])
        source.update(values=[GOOGLE_SHEET_COLUMNS, *rows], range_name="A1:L6", value_input_option="USER_ENTERED")
        matrix, layout = statement_matrix([source], "2099")
        result.update(values=matrix, range_name=f"A1:C{len(matrix)}", value_input_option="USER_ENTERED")
        totals = reconcile(result, [source], "2099", layout)
        assert totals[0]["net_expenditure"] == "1400"
        assert totals[0]["operating"] == "3600"
        assert totals[0]["final"] == "1600"
        print("Google Sheets formula fixture passed: net expenditure 1400, operating 3600, final 1600.")
    finally:
        for worksheet in reversed(owned):
            spreadsheet.del_worksheet(worksheet)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("year")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    client = SheetClient.from_env()
    identifier = client._find_spreadsheet_in_folder(args.year)
    if not identifier:
        raise ValueError("Existing annual workbook not found; no workbook was created.")
    spreadsheet = client.client.open_by_key(identifier)
    baseline = snapshot(spreadsheet)
    folder = ROOT / "backups"
    folder.mkdir(exist_ok=True)
    path = folder / f"statement-{args.year}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(baseline, ensure_ascii=True), encoding="utf-8")
    print(f"Backup saved: {path.name}")
    months = client._month_worksheets(spreadsheet)
    issues = {ws.title: statement_issues(records(baseline["sheets"][ws.title]["values"]), args.year, month_number(ws.title)) for ws in months}
    print("Review issue counts:", {month: len(rows) for month, rows in issues.items()})
    if args.fixture:
        verify_fixture(spreadsheet)
    if args.apply:
        client.refresh_year_summary(spreadsheet)
        matrix, layout = statement_matrix(months, args.year)
        totals = reconcile(spreadsheet.worksheet("Summary"), months, args.year, layout)
        print("Reconciled all monthly and annual detail/subtotal formulas:", dict(zip([ws.title for ws in months], totals)))
    for month in months:
        assert month.get_all_values(value_render_option="FORMULA") == baseline["sheets"][month.title]["formulas"], f"Ledger changed: {month.title}"
    print("Verified monthly values, formulas, headers and row order preserved.")


if __name__ == "__main__":
    main()
