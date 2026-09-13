"""Cash-based statement formulas. The source ledger is never rewritten here."""

from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from gspread.utils import rowcol_to_a1

from validators import (
    ALLOCATION_CATEGORIES, CATEGORY_MIGRATIONS, FIXED_EXPENSE_CATEGORIES,
    INCOME_CATEGORIES, OFFSET_CATEGORIES, REVIEW_CATEGORIES,
    VARIABLE_EXPENSE_CATEGORIES, normalize_bool, normalize_category,
)

SALARY_REVIEW_SHEET = "Statement Review"
AMOUNT_FORMAT = '"S$"#,##0.00;[Red]("S$"#,##0.00);"S$"0.00'
STATEMENT_CATEGORIES = (INCOME_CATEGORIES + VARIABLE_EXPENSE_CATEGORIES
                        + FIXED_EXPENSE_CATEGORIES + OFFSET_CATEGORIES + ALLOCATION_CATEGORIES)


def column_letter(column: int) -> str:
    return rowcol_to_a1(1, column)[:-1]


def month_number(title: str) -> int:
    name = title.split()[0].lower()
    for month in range(1, 13):
        if name in (calendar.month_name[month].lower(), calendar.month_abbr[month].lower()):
            return month
    raise ValueError(f"Not a month worksheet: {title}")


def _aliases(category: str) -> list[str]:
    return [category] + [old for old, new in CATEGORY_MIGRATIONS.items() if new == category]


def statement_category_formula(worksheet, category: str, *, headers=None, year=None) -> str:
    """Sum recorded cash magnitudes, with missing/invalid inputs left blank, not zero."""
    if headers is None:
        values = worksheet.get_all_values()
        headers = values[0] if values else []
    required = {"check", "amount", "category", "money_flow", "date", "currency"}
    if category in ALLOCATION_CATEGORIES:
        required.add("transaction_type")
    if not required.issubset(headers):
        return '=""'
    title = "'" + worksheet.title.replace("'", "''") + "'"
    end = max(int(worksheet.row_count), 2)

    def cells(name):
        col = column_letter(headers.index(name) + 1)
        return f"{title}!${col}$2:${col}${end}"

    amount, dates = cells("amount"), cells("date")
    flow = "inflow" if category in INCOME_CATEGORIES + OFFSET_CATEGORIES else "outflow"
    aliases = "+".join(
        f'--(UPPER(TO_TEXT({cells("category")}))="{alias.upper()}")'
        for alias in _aliases(category)
    )
    selected = f'--(UPPER(TO_TEXT({cells("check")}))="YES")*({aliases})'
    period = f'IFERROR(TEXT(IF(ISNUMBER({dates}),{dates},DATEVALUE({dates})),"yyyy-mm"),"")'
    target = f"{int(year):04d}-{month_number(worksheet.title):02d}" if year else None
    period_match = f'--({period}="{target}")' if target else f'--({period}<>"")'
    bad_date = f'SUMPRODUCT({selected},--({period}=""))'
    selected_month = f"{selected}*{period_match}"
    valid = (
        f'--ISNUMBER({amount})*--({amount}<>"")'
        f'*--(LOWER(TO_TEXT({cells("money_flow")}))="{flow}")'
        f'*--(UPPER(TO_TEXT({cells("currency")}))="SGD")'
    )
    if "amount_parse_error" in headers:
        valid += f'*--(LOWER(TO_TEXT({cells("amount_parse_error")}))<>"true")'
    if category in ALLOCATION_CATEGORIES:
        valid += f'*--(LOWER(TO_TEXT({cells("transaction_type")}))="contribution")'
    # IFERROR applies only to individual raw values, never to a failed total.
    invalid = f"SUMPRODUCT({selected_month},1-IFERROR({valid},0))"
    total = f"SUMPRODUCT({selected_month},IFERROR(ABS({amount}),0))"
    return f'=IF({bad_date}+{invalid}>0,"",{total})'


def _complete_sum(cells: str, count: int) -> str:
    return f'=IF(COUNT({cells})={count},SUM({cells}),"")'


def statement_integrity_formula(worksheet, year: str, *, headers=None) -> str:
    """Count checked rows that cannot safely belong to this monthly statement."""
    if headers is None:
        values = worksheet.get_all_values()
        headers = values[0] if values else []
    required = {"check", "date", "category"}
    if not required.issubset(headers):
        return "1"
    title = "'" + worksheet.title.replace("'", "''") + "'"
    end = max(int(worksheet.row_count), 2)

    def cells(name):
        col = column_letter(headers.index(name) + 1)
        return f"{title}!${col}$2:${col}${end}"

    checked = f'--(UPPER(TO_TEXT({cells("check")}))="YES")'
    dates = cells("date")
    period = f'IFERROR(TEXT(IF(ISNUMBER({dates}),{dates},DATEVALUE({dates})),"yyyy-mm"),"")'
    target = f"{int(year):04d}-{month_number(worksheet.title):02d}"
    accepted = STATEMENT_CATEGORIES + ["Transfer"]
    accepted.extend(old for old, new in CATEGORY_MIGRATIONS.items() if new in accepted)
    accepted_values = ",".join(f'"{value.upper()}"' for value in dict.fromkeys(accepted))
    category = f'UPPER(TO_TEXT({cells("category")}))'
    excluded_category = f'--ISNA(MATCH({category},{{{accepted_values}}},0))'
    wrong_period = f'--({period}<>"{target}")'
    return f"SUMPRODUCT({checked},--(({wrong_period}+{excluded_category})>0))"


def statement_matrix(month_worksheets: list, year: str, *, salary_status_rows=None):
    year = str(year)
    if not re.fullmatch(r"\d{4}", year):
        raise ValueError("A four-digit workbook year is required for a statement.")
    worksheets = sorted(month_worksheets, key=lambda ws: month_number(ws.title))
    if len({month_number(ws.title) for ws in worksheets}) != len(worksheets):
        raise ValueError("Multiple ledger tabs represent the same month; reconcile them first.")
    for ws in worksheets:
        explicit_year = re.search(r"\b(\d{4})\b", ws.title)
        if explicit_year and explicit_year[1] != year:
            raise ValueError(f"{ws.title} does not belong to {year}.")
    width = len(worksheets) + 2
    matrix = [["Category", *[f"{calendar.month_abbr[month_number(ws.title)]} {year}" for ws in worksheets], "Year Total"]]
    layout = {"section_rows": [], "detail_rows": [], "subtotal_rows": []}
    headers = {}
    for ws in worksheets:
        values = ws.get_all_values()
        headers[ws.title] = values[0] if values else []
    integrity = {
        ws.title: statement_integrity_formula(ws, year, headers=headers[ws.title])
        for ws in worksheets
    }

    def section(label):
        matrix.append([label] + [""] * (width - 1))
        layout["section_rows"].append(len(matrix))

    def detail(category):
        row = len(matrix) + 1
        values = []
        for ws in worksheets:
            formula = statement_category_formula(ws, category, headers=headers[ws.title], year=year)
            values.append(formula)
        last_month = column_letter(width - 1)
        total = _complete_sum(f"B{row}:{last_month}{row}", len(worksheets)) if worksheets else '=""'
        matrix.append(["  " + category, *values, total])
        layout["detail_rows"].append(row)

    def subtotal(label, key, first=None, last=None, expression=None, dependencies=None):
        row = len(matrix) + 1
        values = []
        for index in range(len(worksheets)):
            col = column_letter(index + 2)
            if expression:
                refs = ",".join(f"{col}{layout[name]}" for name in dependencies)
                expr = expression.format(**{name: f"{col}{layout[name]}" for name in dependencies})
                formula = f'=IF(COUNT({refs})={len(dependencies)},{expr},"")'
            else:
                formula = _complete_sum(f"{col}{first}:{col}{last}", last - first + 1)
            guard = integrity[worksheets[index].title]
            values.append(f'=IF(({guard})>0,"",{formula[1:]})')
        total = _complete_sum(f"B{row}:{column_letter(width-1)}{row}", len(worksheets)) if worksheets else '=""'
        matrix.append([label, *values, total])
        layout[key] = row
        layout["subtotal_rows"].append(row)

    def group(label, categories, total, key):
        section(label)
        first = len(matrix) + 1
        for category in categories:
            detail(category)
        layout[f"{key}_first_detail"] = first
        layout[f"{key}_last_detail"] = len(matrix)
        subtotal(total, key, first, len(matrix))

    group("INCOME", INCOME_CATEGORIES, "Total Income (A)", "income_total")
    group("VARIABLE EXPENDITURE", VARIABLE_EXPENSE_CATEGORIES, "Total Variable Expenditure (B)", "variable_total")
    group("FIXED EXPENDITURE", FIXED_EXPENSE_CATEGORIES, "Total Fixed Expenditure (C)", "fixed_total")
    subtotal("Gross Expenditure (B + C)", "gross", expression="{variable_total}+{fixed_total}", dependencies=["variable_total", "fixed_total"])
    group("EXPENSE OFFSETS", OFFSET_CATEGORIES, "Total Expense Offsets (D)", "offset_total")
    subtotal("Net Expenditure (B + C - D)", "net_expenditure", expression="{gross}-{offset_total}", dependencies=["gross", "offset_total"])
    section("OPERATING RESULT")
    subtotal("Operating Surplus / (Deficit) = A - B - C + D", "operating", expression="{income_total}-{net_expenditure}", dependencies=["income_total", "net_expenditure"])
    group("INVESTMENT & SAVINGS ALLOCATION", ALLOCATION_CATEGORIES, "Total Investment & Savings Allocation (E)", "allocation_total")
    section("FINAL RESULT")
    subtotal("Net Surplus / (Deficit) After Allocations = A - B - C + D - E", "final", expression="{operating}-{allocation_total}", dependencies=["operating", "allocation_total"])
    layout["year_total_col"] = width
    return matrix, layout


def style_statement(worksheet, spreadsheet, matrix, layout):
    end_col = column_letter(len(matrix[0]))
    end_row = len(matrix)
    navy = {"red": .12, "green": .25, "blue": .43}
    teal = {"red": .10, "green": .49, "blue": .40}
    white = {"red": 1, "green": 1, "blue": 1}
    light_blue = {"red": .88, "green": .93, "blue": .96}
    neutral = {"red": .97, "green": .98, "blue": .98}
    divider = {"red": .72, "green": .77, "blue": .82}
    formats = [
        {"range": f"A1:{end_col}{end_row}", "format": {
            "backgroundColor": white, "textFormat": {"fontSize": 12, "bold": False, "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
            "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
        {"range": f"B2:{end_col}{end_row}", "format": {"numberFormat": {"type": "NUMBER", "pattern": AMOUNT_FORMAT}, "horizontalAlignment": "RIGHT"}},
    ]
    for row in [1, *layout["section_rows"]]:
        formats.append({"range": f"A{row}:{end_col}{row}", "format": {"backgroundColor": navy, "textFormat": {"bold": True, "foregroundColor": white}}})
    for key in ["income_total", "variable_total", "fixed_total", "offset_total", "allocation_total"]:
        for row in range(layout[f"{key}_first_detail"], layout[f"{key}_last_detail"] + 1):
            color = white if (row - layout[f"{key}_first_detail"]) % 2 == 0 else neutral
            formats.append({"range": f"A{row}:{end_col}{row}", "format": {"backgroundColor": color}})
    for row in layout["subtotal_rows"]:
        formats.append({"range": f"A{row}:{end_col}{row}", "format": {
            "backgroundColor": light_blue, "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": navy},
            "borders": {"top": {"style": "SOLID", "color": divider}}}})
    for row in [layout["operating"], layout["final"]]:
        formats.append({"range": f"A{row}:{end_col}{row}", "format": {"backgroundColor": teal, "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": white}}})
        formats.append({"range": f"A{row}", "format": {
            "backgroundColor": light_blue,
            "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": navy},
        }})
    formats.append({"range": f"{end_col}1:{end_col}{end_row}", "format": {
        "borders": {"left": {"style": "SOLID_MEDIUM", "color": divider}},
    }})
    for row in layout["detail_rows"]:
        formats.append({"range": f"{end_col}{row}", "format": {"textFormat": {
            "bold": True, "fontSize": 12, "foregroundColor": navy,
        }}})
    formats.append({"range": f"A{layout['final']}:{end_col}{layout['final']}", "format": {
        "borders": {"top": {"style": "SOLID_MEDIUM", "color": teal}},
    }})
    worksheet.batch_format(formats)
    worksheet.freeze(rows=1, cols=1)
    requests = []
    for dimension, start, end, size in [("COLUMNS", 0, 1, 480), ("COLUMNS", 1, len(matrix[0]), 145), ("ROWS", 0, end_row, 27)]:
        requests.append({"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": dimension, "startIndex": start, "endIndex": end}, "properties": {"pixelSize": size}, "fields": "pixelSize"}})
    for row in [1, *layout["section_rows"]]:
        requests.append({"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": row-1, "endIndex": row}, "properties": {"pixelSize": 30 if row == 1 else 28}, "fields": "pixelSize"}})
    for row in [layout["operating"], layout["final"]]:
        requests.append({"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": row-1, "endIndex": row}, "properties": {"pixelSize": 38}, "fields": "pixelSize"}})
    # Red deficits stay legible on the two emphasized teal result rows.
    requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{"sheetId": worksheet.id, "startRowIndex": row-1, "endRowIndex": row, "startColumnIndex": 1, "endColumnIndex": len(matrix[0])} for row in [layout["operating"], layout["final"]]],
        "booleanRule": {"condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]}, "format": {"backgroundColor": {"red": 1, "green": .9, "blue": .9}, "textFormat": {"foregroundColor": {"red": .7, "green": .08, "blue": .08}, "bold": True}}}}}})
    spreadsheet.batch_update({"requests": requests})


def style_review_sheet(worksheet, spreadsheet, widths):
    end = column_letter(len(widths))
    worksheet.batch_format([
        {"range": f"A:{end}", "format": {"textFormat": {"fontFamily": "Arial", "fontSize": 11}, "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
        {"range": f"A1:{end}1", "format": {"backgroundColor": {"red": .12, "green": .25, "blue": .43}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
    ])
    worksheet.freeze(rows=1)
    spreadsheet.batch_update({"requests": [
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index+1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}}
        for index, width in enumerate(widths)
    ]})


def record_amount(row):
    value = row.get("amount", "")
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", ""))
        if not amount.is_finite() or normalize_bool(row.get("amount_parse_error")):
            return None
        return abs(amount)
    except (InvalidOperation, ValueError):
        return None


def statement_issues(records, year: str, month: int):
    """Explain omissions without guessing or changing historical classifications."""
    issues = []
    for number, row in enumerate(records, 2):
        if str(row.get("check", "")).lower() != "yes":
            continue
        category = normalize_category(row.get("category", ""))
        reasons = []
        try:
            paid = datetime.strptime(str(row.get("date", "")), "%Y-%m-%d")
            if (paid.year, paid.month) != (int(year), month):
                reasons.append("Date is outside this tab's month/year; excluded here")
        except ValueError:
            reasons.append("Missing or invalid payment date")
        if record_amount(row) is None:
            reasons.append("Missing or invalid amount")
        if str(row.get("currency", "")).upper() != "SGD":
            reasons.append("SGD amount required; no exchange rate assumed")
        if category in REVIEW_CATEGORIES or category not in STATEMENT_CATEGORIES + ["Transfer"]:
            reasons.append("Clarify cash income, noncash voucher, funding movement, or expense")
        if category in ALLOCATION_CATEGORIES and row.get("transaction_type") != "contribution":
            reasons.append("Confirm fresh allocation once, not a purchase using existing portfolio cash")
        expected = "inflow" if category in INCOME_CATEGORIES + OFFSET_CATEGORIES else "outflow"
        if category in STATEMENT_CATEGORIES and row.get("money_flow") != expected:
            reasons.append(f"Category requires {expected}")
        if category == "Reimbursements" and re.search(r"\b(SALARY|PAYROLL|BONUS|CPF)\b", str(row.get("description", "")), re.I):
            reasons.append("Check legacy reimbursement: possible employment or CPF entry")
        if reasons:
            issues.append([number, str(row.get("date", "")), str(row.get("category", "")), "; ".join(reasons)])
    return issues


def calculate_statement(records, year: int, month: int, *, salary_confirmed_zero=True):
    """Independent Decimal reconciliation of recorded transactions, not formula evaluation."""
    totals = {category: Decimal("0") for category in STATEMENT_CATEGORIES}
    for row in records:
        if str(row.get("check", "")).lower() != "yes":
            continue
        category = normalize_category(row.get("category", ""))
        if category not in totals:
            continue
        try:
            paid = datetime.strptime(str(row.get("date", "")), "%Y-%m-%d")
        except ValueError:
            totals[category] = None
            continue
        if (paid.year, paid.month) != (year, month):
            continue
        amount = record_amount(row)
        flow = "inflow" if category in INCOME_CATEGORIES + OFFSET_CATEGORIES else "outflow"
        if (row.get("money_flow") != flow or str(row.get("currency", "")).upper() != "SGD"
                or (category in ALLOCATION_CATEGORIES and row.get("transaction_type") != "contribution")):
            amount = None
        if amount is None:
            totals[category] = None
        elif totals[category] is not None:
            totals[category] += amount
    if totals["Net Salary"] == 0 and not salary_confirmed_zero:
        totals["Net Salary"] = None

    def total(keys):
        return None if any(totals[key] is None for key in keys) else sum((totals[key] for key in keys), Decimal("0"))

    totals.update(income_total=total(INCOME_CATEGORIES), variable_total=total(VARIABLE_EXPENSE_CATEGORIES),
                  fixed_total=total(FIXED_EXPENSE_CATEGORIES), offset_total=total(OFFSET_CATEGORIES),
                  allocation_total=total(ALLOCATION_CATEGORIES))
    for key, positive, negative in [
        ("gross", ["variable_total", "fixed_total"], []),
        ("net_expenditure", ["gross"], ["offset_total"]),
        ("operating", ["income_total"], ["net_expenditure"]),
        ("final", ["operating"], ["allocation_total"]),
    ]:
        totals[key] = None if any(totals[k] is None for k in positive + negative) else sum(totals[k] for k in positive) - sum(totals[k] for k in negative)
    return totals
