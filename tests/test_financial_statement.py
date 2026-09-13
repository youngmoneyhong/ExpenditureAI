"""Offline cash-statement reconciliation, formula, and review regressions."""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import unittest
from unittest.mock import patch

from app import append_preview_metrics, apply_workflow_state
from financial_statement import (
    AMOUNT_FORMAT,
    calculate_statement,
    statement_category_formula,
    statement_issues,
    statement_matrix,
    style_statement,
)
from sheets import SheetClient
from validators import GOOGLE_SHEET_COLUMNS, rows_to_dataframe
from test_google_sheet_output import FakeSpreadsheet, FakeWorksheet


def transaction(category, amount, money_flow="outflow", **overrides):
    return {
        "check": "Yes", "date": "2026-06-04", "source": "DBS_BANK",
        "description": category, "category": category, "amount": amount,
        "currency": "SGD", "money_flow": money_flow, "transaction_type": "unknown",
        "confidence": 0.99, **overrides,
    }


def example_records():
    return [
        transaction("Net Salary", Decimal("5000"), "inflow"),
        transaction("Food", Decimal("1000")),
        transaction("Parent Allowance", Decimal("500")),
        transaction("Reimbursements", Decimal("100"), "inflow"),
        transaction("ETF Contributions", Decimal("2000"),
                    transaction_type="contribution"),
    ]


class StatementCalculationTests(unittest.TestCase):
    def test_reference_statement_uses_decimal_and_separates_allocations(self):
        records = example_records()
        original = deepcopy(records)
        totals = calculate_statement(records, 2026, 6)
        expected = {
            "income_total": "5000", "variable_total": "1000", "fixed_total": "500",
            "gross": "1500", "offset_total": "100", "net_expenditure": "1400",
            "operating": "3600", "allocation_total": "2000", "final": "1600",
        }
        for key, amount in expected.items():
            with self.subTest(key=key):
                self.assertIsInstance(totals[key], Decimal)
                self.assertEqual(totals[key], Decimal(amount))
        self.assertEqual(records, original)

    def test_allocations_change_only_allocation_and_final_totals(self):
        before = calculate_statement(example_records()[:-1], 2026, 6)
        after = calculate_statement(example_records(), 2026, 6)
        for key in ["income_total", "variable_total", "fixed_total", "gross",
                    "offset_total", "net_expenditure", "operating"]:
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before["final"] - after["final"], Decimal("2000"))

    def test_legacy_negative_offsets_have_the_same_positive_interpretation(self):
        for category in ["Reimbursements", "Reimbursement", "Cashbacks & Refunds"]:
            positive = [transaction(category, Decimal("100"), "inflow")]
            negative = [transaction(category, Decimal("-100"), "inflow")]
            with self.subTest(category=category):
                a = calculate_statement(positive, 2026, 6, salary_confirmed_zero=True)
                b = calculate_statement(negative, 2026, 6, salary_confirmed_zero=True)
                self.assertEqual(a, b)
                self.assertEqual(b["offset_total"], Decimal("100"))
                self.assertEqual(b["net_expenditure"], Decimal("-100"))

    def test_carousell_is_income_and_not_an_expense_offset(self):
        totals = calculate_statement(
            example_records() + [transaction("Carousell Sales", Decimal("250"), "inflow")],
            2026, 6,
        )
        self.assertEqual(totals["income_total"], Decimal("5250"))
        self.assertEqual(totals["offset_total"], Decimal("100"))
        self.assertEqual(totals["net_expenditure"], Decimal("1400"))
        self.assertEqual(totals["operating"], Decimal("3850"))
        self.assertEqual(totals["final"], Decimal("1850"))

    def test_no_salary_is_zero_without_a_review_tab(self):
        records = example_records()[1:]
        totals = calculate_statement(records, 2026, 6)
        self.assertEqual(totals["Net Salary"], Decimal("0"))
        self.assertEqual(totals["net_expenditure"], Decimal("1400"))
        self.assertEqual(totals["operating"], Decimal("-1400"))
        self.assertEqual(totals["final"], Decimal("-3400"))

    def test_credited_salary_takes_priority_over_confirmed_zero(self):
        self.assertEqual(calculate_statement(example_records(), 2026, 6),
                         calculate_statement(example_records(), 2026, 6, salary_confirmed_zero=True))

    def test_checked_unconfirmed_allocation_invalidates_final_not_operating(self):
        for kind in [None, "", "unknown", "purchase", "transfer"]:
            records = example_records()
            records[-1]["transaction_type"] = kind
            with self.subTest(kind=kind):
                totals = calculate_statement(records, 2026, 6)
                self.assertIsNone(totals["ETF Contributions"])
                self.assertIsNone(totals["allocation_total"])
                self.assertIsNone(totals["final"])
                self.assertEqual(totals["gross"], Decimal("1500"))
                self.assertEqual(totals["operating"], Decimal("3600"))
                self.assertIn("Confirm fresh allocation", statement_issues(records, "2026", 6)[0][3])

    def test_wrong_period_and_unchecked_invalid_records_do_not_poison_totals(self):
        excluded = [
            transaction("Food", None, date="2027-06-04"),
            transaction("Net Salary", Decimal("9000"), "inflow", date="2025-06-04"),
            transaction("Food", Decimal("999"), date="2026-07-01"),
            transaction("Food", None, check="No", date="invalid"),
        ]
        self.assertEqual(calculate_statement(example_records() + excluded, 2026, 6),
                         calculate_statement(example_records(), 2026, 6))
        self.assertEqual(len(statement_issues(excluded, "2026", 6)), 3)

    def test_missing_and_invalid_amounts_propagate_but_real_zero_does_not(self):
        for amount in [None, "", "not a number", "NaN", "Infinity"]:
            records = example_records() + [transaction("Food", amount)]
            with self.subTest(amount=amount):
                totals = calculate_statement(records, 2026, 6)
                for key in ["Food", "variable_total", "gross", "net_expenditure", "operating", "final"]:
                    self.assertIsNone(totals[key], key)
                self.assertEqual(totals["fixed_total"], Decimal("500"))
                self.assertEqual(totals["allocation_total"], Decimal("2000"))
        self.assertEqual(calculate_statement(example_records() + [transaction("Food", Decimal("0"))], 2026, 6),
                         calculate_statement(example_records(), 2026, 6))

    def test_parse_error_zero_is_not_a_recorded_zero(self):
        totals = calculate_statement([transaction("Food", 0, amount_parse_error=True)], 2026, 6)
        self.assertIsNone(totals["Food"])

    def test_invalid_date_currency_and_flow_leave_affected_totals_missing(self):
        for override in [{"date": ""}, {"date": "2026-02-30"}, {"currency": "USD"},
                         {"currency": ""}, {"money_flow": "inflow"}]:
            with self.subTest(override=override):
                records = [transaction("Food", Decimal("10"), **override)]
                self.assertIsNone(calculate_statement(records, 2026, 6)["gross"])
                self.assertEqual(len(statement_issues(records, "2026", 6)), 1)

    def test_aliases_are_counted_in_canonical_sections(self):
        records = [transaction("Bills", Decimal("75")),
                   transaction("Reimbursement", Decimal("10"), "inflow")]
        totals = calculate_statement(records, 2026, 6, salary_confirmed_zero=True)
        self.assertEqual(totals["Bills / Recurring Commitments"], Decimal("75"))
        self.assertEqual(totals["fixed_total"], Decimal("75"))
        self.assertEqual(totals["variable_total"], Decimal("0"))
        self.assertEqual(totals["Reimbursements"], Decimal("10"))
        self.assertEqual(totals["net_expenditure"], Decimal("65"))

    def test_noncash_and_funding_are_excluded_and_flagged(self):
        excluded = [transaction("GVs & Prize Award", Decimal("300"), "inflow"),
                    transaction("Funding", Decimal("5000"), "inflow")]
        self.assertEqual(calculate_statement(example_records() + excluded, 2026, 6),
                         calculate_statement(example_records(), 2026, 6))
        issues = statement_issues(excluded, "2026", 6)
        self.assertEqual([row[:3] for row in issues],
                         [[2, "2026-06-04", "GVs & Prize Award"], [3, "2026-06-04", "Funding"]])
        for issue in issues:
            self.assertIn("Clarify cash income", issue[3])

    def test_decimal_cents_do_not_accumulate_binary_rounding_error(self):
        totals = calculate_statement([transaction("Food", Decimal("0.10")),
                                      transaction("Food", Decimal("0.20"))],
                                     2026, 6, salary_confirmed_zero=True)
        self.assertEqual(totals["gross"], Decimal("0.30"))
        self.assertEqual(totals["final"], Decimal("-0.30"))


class StatementFormulaTests(unittest.TestCase):
    def setUp(self):
        self.months = [FakeWorksheet("Jul"), FakeWorksheet("June")]
        self.matrix, self.layout = statement_matrix(
            self.months, "2026", salary_status_rows={"June": 2, "Jul": 3})

    def test_exact_rows_sections_and_layout(self):
        self.assertEqual(self.matrix[0], ["Category", "Jun 2026", "Jul 2026", "Year Total"])
        self.assertEqual([row[0] for row in self.matrix[1:]], [
            "INCOME", "  Net Salary", "  Bonus / AVC / Other Employment Income",
            "  Carousell Sales", "  Prize Awards/Government Vouchers", "  Gifts received", "Total Income (A)",
            "VARIABLE EXPENDITURE", "  Food", "  Public Transport", "  Taxi", "  Shopping",
            "  Gifts", "  Entertainment", "  Travel", "  Health", "  Personal Care",
            "  Education", "  Admin & Fees", "  Others", "Total Variable Expenditure (B)",
            "FIXED EXPENDITURE", "  Parent Allowance", "  Insurance", "  Subscriptions",
            "  Income Tax", "  Bills / Recurring Commitments", "Total Fixed Expenditure (C)",
            "Gross Expenditure (B + C)", "EXPENSE OFFSETS", "  Reimbursements",
            "  Cashbacks & Refunds", "Total Expense Offsets (D)", "Net Expenditure (B + C - D)",
            "OPERATING RESULT", "Operating Surplus / (Deficit) = A - B - C + D",
            "INVESTMENT & SAVINGS ALLOCATION", "  ETF Contributions",
            "  Equity Contributions", "  Crypto Contributions", "  Commodities Contributions",
            "  Dedicated Cash Savings", "  Other Investment Contributions",
            "Total Investment & Savings Allocation (E)",
            "FINAL RESULT", "Net Surplus / (Deficit) After Allocations = A - B - C + D - E",
        ])
        self.assertEqual(self.layout, {
            "section_rows": [2, 9, 23, 31, 36, 38, 46],
            "detail_rows": [3, 4, 5, 6, 7, *range(10, 22), *range(24, 29), 32, 33, *range(39, 45)],
            "subtotal_rows": [8, 22, 29, 30, 34, 35, 37, 45, 47],
            "income_total": 8, "variable_total": 22, "fixed_total": 29, "gross": 30,
            "offset_total": 34, "net_expenditure": 35, "operating": 37,
            "allocation_total": 45, "final": 47, "year_total_col": 4,
            "income_total_first_detail": 3, "income_total_last_detail": 7,
            "variable_total_first_detail": 10, "variable_total_last_detail": 21,
            "fixed_total_first_detail": 24, "fixed_total_last_detail": 28,
            "offset_total_first_detail": 32, "offset_total_last_detail": 33,
            "allocation_total_first_detail": 39, "allocation_total_last_detail": 44,
        })
        self.assertTrue(all(len(row) == 4 for row in self.matrix))
        for row in self.layout["section_rows"]:
            self.assertEqual(self.matrix[row - 1][1:], ["", "", ""])

    def test_exact_subtotal_references_propagate_missing_dependencies(self):
        expected = {
            8: '=IF(COUNT({c}3:{c}7)=5,SUM({c}3:{c}7),"")',
            22: '=IF(COUNT({c}10:{c}21)=12,SUM({c}10:{c}21),"")',
            29: '=IF(COUNT({c}24:{c}28)=5,SUM({c}24:{c}28),"")',
            30: '=IF(COUNT({c}22,{c}29)=2,{c}22+{c}29,"")',
            34: '=IF(COUNT({c}32:{c}33)=2,SUM({c}32:{c}33),"")',
            35: '=IF(COUNT({c}30,{c}34)=2,{c}30-{c}34,"")',
            37: '=IF(COUNT({c}8,{c}35)=2,{c}8-{c}35,"")',
            45: '=IF(COUNT({c}39:{c}44)=6,SUM({c}39:{c}44),"")',
            47: '=IF(COUNT({c}37,{c}45)=2,{c}37-{c}45,"")',
        }
        for row, formula in expected.items():
            for index, col in enumerate(["B", "C"], 1):
                actual = self.matrix[row - 1][index]
                self.assertIn(formula.format(c=col)[1:], actual)
                self.assertIn("SUMPRODUCT", actual)
        for row in self.layout["detail_rows"] + self.layout["subtotal_rows"]:
            self.assertEqual(self.matrix[row - 1][-1], f'=IF(COUNT(B{row}:C{row})=2,SUM(B{row}:C{row}),"")')

    def test_category_formula_has_date_year_currency_and_missing_guards(self):
        worksheet = FakeWorksheet("June")
        worksheet.row_count = 1600
        formula = statement_category_formula(worksheet, "Food", year="2026")
        for fragment in [
            "UPPER(TO_TEXT('June'!$A$2:$A$1600))=\"YES\"", "UPPER(TO_TEXT('June'!$D$2:$D$1600))=\"FOOD\"",
            "ISNUMBER('June'!$H$2:$H$1600)", "'June'!$H$2:$H$1600<>\"\"",
            "LOWER(TO_TEXT('June'!$K$2:$K$1600))=\"outflow\"", "UPPER(TO_TEXT('June'!$I$2:$I$1600))=\"SGD\"",
            "DATEVALUE('June'!$B$2:$B$1600)", '"yyyy-mm"', '="2026-06"',
            "LOWER(TO_TEXT('June'!$G$2:$G$1600))", "ABS('June'!$H$2:$H$1600)",
            '1-IFERROR(', '>0,"",SUMPRODUCT(',
        ]:
            self.assertIn(fragment, formula)
        self.assertFalse(formula.startswith("=IFERROR"))
        self.assertEqual(formula.count("("), formula.count(")"))

    def test_alias_and_allocation_formulas_use_the_right_columns(self):
        bills = self.matrix[27][1]
        reimbursement = self.matrix[31][1]
        allocation = self.matrix[38][1]
        self.assertIn('="BILLS"', bills)
        self.assertIn('="BILLS / RECURRING COMMITMENTS"', bills)
        self.assertIn('="REIMBURSEMENT"', reimbursement)
        self.assertIn('="REIMBURSEMENTS"', reimbursement)
        self.assertIn('="inflow"', reimbursement)
        self.assertIn("LOWER(TO_TEXT('June'!$L$2:$L$1000))=\"contribution\"", allocation)
        self.assertIn('="outflow"', allocation)
        self.assertNotIn('="contribution"', bills)
        self.assertIn('="inflow"', self.matrix[4][1])
        self.assertNotIn("Carousell", reimbursement)

    def test_salary_formulas_do_not_depend_on_a_review_tab(self):
        for index in (1, 2):
            formula = self.matrix[2][index]
            self.assertNotIn("Statement Review", formula)
            self.assertIn("SUMPRODUCT", formula)

    def test_required_headers_cannot_silently_return_zero(self):
        for name in ["check", "date", "amount", "category", "money_flow", "currency"]:
            headers = [column for column in GOOGLE_SHEET_COLUMNS if column != name]
            self.assertEqual(statement_category_formula(FakeWorksheet("June"), "Food", headers=headers, year="2026"), '=""')
        headers = [column for column in GOOGLE_SHEET_COLUMNS if column != "transaction_type"]
        self.assertEqual(statement_category_formula(FakeWorksheet("June"), "ETF Contributions", headers=headers, year="2026"), '=""')

    def test_reordered_headers_and_quoted_sheet_names(self):
        headers = ["amount", "currency", "date", "money_flow", "category", "check"]
        formula = statement_category_formula(FakeWorksheet("June O'Brien"), "Food", headers=headers, year="2026")
        self.assertIn("ABS('June O''Brien'!$A$2:$A$1000)", formula)
        self.assertIn("UPPER(TO_TEXT('June O''Brien'!$F$2:$F$1000))=\"YES\"", formula)

    def test_year_and_duplicate_month_guards(self):
        for year in ["Year", "26", "2026 notes", ""]:
            with self.subTest(year=year), self.assertRaises(ValueError):
                statement_matrix(self.months, year)
        for months in [[FakeWorksheet("June 2027")], [FakeWorksheet("June"), FakeWorksheet("Jun")]]:
            with self.assertRaises(ValueError):
                statement_matrix(months, "2026")

    def test_empty_workbook_has_blank_totals_not_invented_zeros(self):
        matrix, layout = statement_matrix([], "2026")
        self.assertEqual(matrix[0], ["Category", "Year Total"])
        for row in layout["detail_rows"] + layout["subtotal_rows"]:
            self.assertEqual(matrix[row - 1][1], '=""')

    def test_statement_style_covers_sections_totals_currency_and_deficits(self):
        worksheet = FakeWorksheet("Summary")
        spreadsheet = FakeSpreadsheet([])
        style_statement(worksheet, spreadsheet, self.matrix, self.layout)
        formats = {item["range"]: item["format"] for item in worksheet.batch_formats}
        self.assertEqual(formats["B2:D47"]["numberFormat"]["pattern"], AMOUNT_FORMAT)
        self.assertIn('[Red]("S$"', AMOUNT_FORMAT)
        self.assertEqual(worksheet.frozen, (1, 1))
        for row in [1, *self.layout["section_rows"]]:
            self.assertEqual(formats[f"A{row}:D{row}"]["backgroundColor"], {"red": .12, "green": .25, "blue": .43})
        for row in self.layout["subtotal_rows"]:
            matching = [item["format"] for item in worksheet.batch_formats if item["range"] == f"A{row}:D{row}"]
            self.assertTrue(any(item.get("textFormat", {}).get("bold") for item in matching))
        requests = spreadsheet.batch_updates[0]["requests"]
        condition = next(item["addConditionalFormatRule"]["rule"] for item in requests if "addConditionalFormatRule" in item)
        self.assertEqual(condition["booleanRule"]["condition"], {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]})
        self.assertEqual([item["startRowIndex"] for item in condition["ranges"]], [36, 46])


class StatementReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(SheetClient)

    def test_refresh_never_rewrites_monthly_amounts_categories_blanks_or_formulas(self):
        records = [transaction("Bills", ""), transaction("Reimbursement", "-100", "inflow"),
                   transaction("Family", "=25+25"), transaction("Food", "0")]
        rows = [list(GOOGLE_SHEET_COLUMNS)] + [[row.get(col, "") for col in GOOGLE_SHEET_COLUMNS] for row in records]
        month = FakeWorksheet("June", rows)
        spreadsheet = FakeSpreadsheet([month])
        before = month.get_all_values()
        with patch.object(self.client, "_migrate_category_labels") as migrate, patch.object(self.client, "_normalize_month_ledger_values") as normalize:
            self.client.refresh_year_summary(spreadsheet)
            self.client.refresh_year_summary(spreadsheet)
        migrate.assert_not_called()
        normalize.assert_not_called()
        self.assertEqual(month.get_all_values(), before)
        self.assertEqual(month.updates, [])
        self.assertEqual(month.cleared_ranges, [])
        self.assertFalse(month.cleared)
        self.assertEqual(spreadsheet.worksheet("Summary").updates[0][0], "A1:C47")
        for batch in spreadsheet.batch_updates:
            for request in batch["requests"]:
                self.assertIn(next(iter(request)), {"setDataValidation", "updateDimensionProperties", "updateSheetProperties", "addConditionalFormatRule", "deleteConditionalFormatRule"})

    def test_repeat_refresh_replaces_only_generated_deficit_formatting(self):
        summary = FakeWorksheet("Summary", [])
        spreadsheet = FakeSpreadsheet([FakeWorksheet("June"), summary])
        custom_rule = {
            "ranges": [{"sheetId": summary.id, "startRowIndex": 49, "endRowIndex": 50}],
            "booleanRule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "10"}]},
                            "format": {"textFormat": {"bold": True}}},
        }
        spreadsheet.conditional_formats[summary.id] = [deepcopy(custom_rule)]
        self.client.refresh_year_summary(spreadsheet)
        generated = spreadsheet.conditional_formats[summary.id][0]["booleanRule"]["format"]
        generated["backgroundColor"] = {"red": 1, "green": .8980392, "blue": .8980392}
        generated["backgroundColorStyle"] = {"rgbColor": generated["backgroundColor"]}
        generated["textFormat"]["foregroundColor"] = {"red": .69803923, "green": .078431375, "blue": .078431375}
        generated["textFormat"]["foregroundColorStyle"] = {"rgbColor": generated["textFormat"]["foregroundColor"]}
        self.client.refresh_year_summary(spreadsheet)
        rules = spreadsheet.conditional_formats[summary.id]
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[1], custom_rule)
        self.assertEqual(rules[0]["booleanRule"]["condition"]["type"], "NUMBER_LESS")

    def test_shrinking_summary_clears_only_previous_generated_columns(self):
        summary = FakeWorksheet("Summary", [])
        spreadsheet = FakeSpreadsheet([FakeWorksheet("June"), FakeWorksheet("July"), summary])
        self.client._write_year_summary(summary, spreadsheet)
        summary.update("F50", [["Personal note"]])
        del spreadsheet.worksheets_by_title["July"]
        self.client._write_year_summary(summary, spreadsheet)
        self.assertEqual(summary.cleared_ranges, ["D1:D47"])
        self.assertEqual(summary.rows[49][5], "Personal note")

    def test_expanding_summary_does_not_overwrite_personal_notes(self):
        rows = [["Category", "Jun 2026", "Year Total"], ["Net Spend (a + b + c)", "", ""]]
        rows.extend([[""] for _ in range(25)])
        rows.append(["Personal note"])
        summary = FakeWorksheet("Summary", rows)
        spreadsheet = FakeSpreadsheet([FakeWorksheet("June"), summary])
        before = summary.get_all_values()
        with self.assertRaisesRegex(ValueError, "notes"):
            self.client._write_year_summary(summary, spreadsheet)
        self.assertEqual(summary.get_all_values(), before)

    def test_initial_migration_and_review_statuses_persist_and_new_months_are_pending(self):
        months = [FakeWorksheet("June"), FakeWorksheet("September")]
        spreadsheet = FakeSpreadsheet(months)
        with patch("sheets.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 9)
            mapping = self.client._ensure_statement_review(spreadsheet, months, "2026")
        review = spreadsheet.worksheet("Statement Review")
        self.assertEqual(mapping, {"June": 2, "September": 3})
        self.assertEqual(review.rows[0], ["Month", "Salary Status", "", "Migration cutoff", "2026-09-01"])
        self.assertEqual(review.rows[1:], [["June", "Historical zero"], ["September", "Pending"]])
        review.update("B2:B3", [["Pending"], ["Confirmed zero"]])
        prior_updates = len(review.updates)
        months.append(FakeWorksheet("July"))
        with patch("sheets.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 9)
            mapping = self.client._ensure_statement_review(spreadsheet, months, "2026")
            self.client._ensure_statement_review(spreadsheet, months, "2026")
        self.assertEqual(review.rows[1:], [["June", "Pending"], ["September", "Confirmed zero"], ["July", "Pending"]])
        self.assertEqual(review.rows[0][4], "2026-09-01")
        self.assertEqual(mapping, {"June": 2, "September": 3, "July": 4})
        self.assertEqual(review.updates[prior_updates:], [("A4:B4", [["July", "Pending"]])])

    def test_new_future_month_starts_pending(self):
        spreadsheet = FakeSpreadsheet([FakeWorksheet("January")])
        with patch("sheets.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 9)
            self.client._ensure_statement_review(spreadsheet, spreadsheet.worksheets(), "2027")
        self.assertEqual(spreadsheet.worksheet("Statement Review").rows[1], ["January", "Pending"])

    def test_month_discovery_excludes_extra_year_and_nonledger_tabs(self):
        spreadsheet = FakeSpreadsheet([FakeWorksheet(title) for title in
                                       ["July", "June 2027", "Summary", "Statement Review", "June 2026"]])
        self.assertEqual([ws.title for ws in self.client._month_worksheets(spreadsheet)], ["June 2026", "July"])

    def test_duplicate_month_or_review_entries_require_reconciliation(self):
        with self.assertRaises(ValueError):
            self.client._month_worksheets(FakeSpreadsheet([FakeWorksheet("June"), FakeWorksheet("Jun")]))
        review = FakeWorksheet("Statement Review", [["Month", "Salary Status", "", "Migration cutoff", "2026-09-01"],
                                                    ["June", "Pending"], ["June", "Confirmed zero"]])
        with self.assertRaises(ValueError):
            self.client._ensure_statement_review(FakeSpreadsheet([review]), [FakeWorksheet("June")], "2026")

    def test_allocation_confirmation_checkbox_controls_append_and_contribution_type(self):
        for confirmed in [False, True]:
            records = example_records()
            records[-1]["allocation_confirmed"] = confirmed
            result = apply_workflow_state(rows_to_dataframe(records))
            with self.subTest(confirmed=confirmed):
                self.assertEqual(bool(result.at[4, "include_in_append"]), confirmed)
                self.assertEqual(result.at[4, "transaction_type"], "contribution" if confirmed else "unknown")
                self.assertEqual(result.at[4, "status"], "ready" if confirmed else "needs_review")
                preview = append_preview_metrics(result)
                self.assertEqual(preview["expense"], 1500)
                self.assertEqual(preview["operating"], 3600)
                self.assertEqual(preview["allocations"], 2000 if confirmed else 0)
                self.assertEqual(preview["final"], 1600 if confirmed else 3600)

    def test_missing_amount_is_not_reinterpreted_as_zero_in_review(self):
        for amount, missing in [(None, True), ("", True), ("0", False)]:
            result = apply_workflow_state(rows_to_dataframe([transaction("Food", amount)]))
            with self.subTest(amount=amount):
                self.assertEqual(bool(result.at[0, "amount_missing"]), missing)
                if missing:
                    self.assertFalse(result.at[0, "include_in_append"])
                    self.assertEqual(result.at[0, "status"], "needs_review")
                else:
                    self.assertEqual(result.at[0, "amount"], 0)

    def test_non_sgd_upload_is_blocked_until_user_enters_verified_sgd_amount(self):
        result = apply_workflow_state(rows_to_dataframe([
            transaction("Food", "-20", currency="USD")
        ]))
        self.assertFalse(result.at[0, "include_in_append"])
        self.assertEqual(result.at[0, "status"], "needs_review")
        self.assertIn("SGD", result.at[0, "review_note"])
        self.assertEqual(append_preview_metrics(result)["expense"], 0)


if __name__ == "__main__":
    unittest.main()
