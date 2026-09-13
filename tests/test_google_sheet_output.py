import unittest
import os
import re
import tempfile
import threading
import time
from copy import deepcopy
from unittest.mock import Mock, patch

import gspread
from app import (
    append_preview_metrics,
    apply_category_flow_rules,
    apply_existing_sheet_duplicate_check,
    apply_workflow_state,
    duplicate_comparisons,
    extract_all,
    guided_workflow_state,
    overlapping_screenshot_duplicate_mask,
    vision_worker_count,
)
from enrichment_agents import EnrichmentOutput, enrich_transactions
from review_memory import merchant_rule_records, save_review_memory, update_merchant_rules
from sheets import (
    SheetClient,
    _apply_default_column_widths,
    _annual_overview_rows,
    _checked_category_summary_formula,
    _format_year_summary,
    _period_from_date,
    _rows_for_worksheet,
    _summary_matrix_rows,
    worksheet_url,
)
from validators import CATEGORY_OPTIONS, GOOGLE_SHEET_COLUMNS, normalize_category, rows_to_dataframe
from vision_extract import TransactionExtraction


class FakeWorksheet:
    def __init__(self, title, rows=None):
        self.title = title
        self.id = abs(hash(title)) % 100000
        self.rows = deepcopy(rows) if rows is not None else [list(GOOGLE_SHEET_COLUMNS)]
        self.col_count = len(GOOGLE_SHEET_COLUMNS)
        self.row_count = 1000
        self.formats = []
        self.batch_formats = []
        self.updates = []
        self.cleared = False
        self.cleared_ranges = []
        self.frozen = None
        self.hidden_columns = []

    def get_all_values(self):
        return deepcopy(self.rows)

    def get(self, cell_range):
        match = re.fullmatch(r"([A-Z]+)(\d+):\1", cell_range)
        if not match:
            return self.rows
        column = gspread.utils.a1_to_rowcol(f"{match.group(1)}1")[1] - 1
        start_row = int(match.group(2)) - 1
        return [
            [row[column]] if column < len(row) else []
            for row in self.rows[start_row:]
        ]

    def format(self, cell_range, value):
        self.formats.append((cell_range, value))

    def batch_format(self, formats):
        self.batch_formats.extend(formats)

    def clear(self):
        self.cleared = True

    def batch_clear(self, ranges):
        self.cleared_ranges.extend(ranges)

    def freeze(self, rows=None, cols=None):
        self.frozen = (rows, cols)

    def update(self, cell_range, values, **_kwargs):
        self.updates.append((cell_range, deepcopy(values)))
        start_row, start_col = gspread.utils.a1_to_rowcol(cell_range.split(":")[0])
        for row_index, incoming in enumerate(values, start_row - 1):
            while len(self.rows) <= row_index:
                self.rows.append([])
            row = self.rows[row_index]
            row.extend([""] * max(0, start_col - 1 + len(incoming) - len(row)))
            row[start_col - 1:start_col - 1 + len(incoming)] = deepcopy(incoming)

    def columns_auto_resize(self, _start, _end):
        return None

    def add_cols(self, count):
        self.col_count += count

    def add_rows(self, count):
        self.row_count += count

    def hide_columns(self, start, end):
        self.hidden_columns.append((start, end))


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self.worksheets_by_title = {worksheet.title: worksheet for worksheet in worksheets}
        self.batch_updates = []
        self.conditional_formats = {}
        self.title = "2026"

    def add_worksheet(self, title, rows, cols):
        worksheet = FakeWorksheet(title, [])
        worksheet.row_count = int(rows)
        worksheet.col_count = int(cols)
        self.worksheets_by_title[title] = worksheet
        return worksheet

    def worksheet(self, title):
        if title not in self.worksheets_by_title:
            raise gspread.WorksheetNotFound(title)
        return self.worksheets_by_title[title]

    def worksheets(self):
        return list(self.worksheets_by_title.values())

    def batch_update(self, request):
        self.batch_updates.append(deepcopy(request))
        for item in request.get("requests", []):
            if "addConditionalFormatRule" in item:
                addition = item["addConditionalFormatRule"]
                rule = deepcopy(addition["rule"])
                sheet_id = rule["ranges"][0]["sheetId"]
                self.conditional_formats.setdefault(sheet_id, []).insert(addition["index"], rule)
            elif "deleteConditionalFormatRule" in item:
                deletion = item["deleteConditionalFormatRule"]
                self.conditional_formats[deletion["sheetId"]].pop(deletion["index"])

    def fetch_sheet_metadata(self, params=None):
        return {"sheets": [
            {"properties": {"sheetId": sheet_id}, "conditionalFormats": deepcopy(rules)}
            for sheet_id, rules in self.conditional_formats.items()
        ]}


class GoogleSheetOutputTests(unittest.TestCase):
    def test_forced_duplicate_check_does_not_reuse_a_stale_miss(self):
        class SessionState(dict):
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__

        dataframe = apply_workflow_state(rows_to_dataframe([{
            "date": "2026-09-11",
            "source": "DBS_BANK",
            "description": "ACCOUNTANT-GENERAL REF123",
            "amount": "5000.00",
            "money_flow": "inflow",
            "category": "Net Salary",
            "confidence": 0.99,
        }]))
        sheet = Mock()
        sheet.existing_duplicate_matches_by_period.side_effect = [
            {},
            {0: {
                "spreadsheet_id": "year-2026",
                "worksheet": "September",
                "worksheet_id": 123,
                "row": 9,
                "transaction": {
                    "date": "2026-09-11",
                    "description": "ACCOUNTANT-GENERAL REF123",
                    "amount": "5000.00",
                    "category": "Net Salary",
                    "money_flow": "inflow",
                },
            }},
        ]

        with (
            patch("app.SheetClient.from_env", return_value=sheet),
            patch("app.st.session_state", SessionState()),
        ):
            first = apply_existing_sheet_duplicate_check(dataframe)
            refreshed = apply_existing_sheet_duplicate_check(first, force_refresh=True)

        self.assertEqual(sheet.existing_duplicate_matches_by_period.call_count, 2)
        self.assertFalse(refreshed.at[0, "include_in_append"])
        self.assertEqual(refreshed.at[0, "status"], "needs_review")
        self.assertEqual(refreshed.at[0, "matched_worksheet"], "September")
        self.assertEqual(refreshed.at[0, "matched_row"], "9")

    def test_manual_structure_refresh_migrates_categories_and_rebuilds_summary(self):
        client = object.__new__(SheetClient)
        client.config = type("Config", (), {"drive_folder_id": "folder"})()
        month = FakeWorksheet(
            "June",
            [
                list(GOOGLE_SHEET_COLUMNS),
                ["Yes", "2026-06-01", "DBS", "Equity / ETF Contributions"],
                ["Yes", "2026-06-02", "DBS", "Money Market Fund Contributions"],
            ],
        )
        year = FakeSpreadsheet([month])
        year.id = "year-2026"
        unrelated = FakeSpreadsheet([FakeWorksheet("Data")])
        unrelated.title = "Notes"
        unrelated.id = "notes"
        client.client = type(
            "Client",
            (),
            {"open_by_key": lambda _self, key: {"year-2026": year, "notes": unrelated}[key]},
        )()
        client._list_spreadsheets_in_folder = lambda: ["year-2026", "notes"]

        audits = client.refresh_existing_workbook_structures()

        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].year, "2026")
        self.assertEqual(audits[0].month_tabs, 1)
        self.assertEqual(audits[0].migrated_categories, 2)
        self.assertEqual(month.rows[1][3], "ETF Contributions")
        self.assertEqual(month.rows[2][3], "Other Investment Contributions")
        self.assertEqual(year.worksheet("Summary").updates[0][0], "A1:C47")

    def test_extra_ledger_columns_are_hidden_with_zero_based_bounds(self):
        client = object.__new__(SheetClient)
        worksheet = FakeWorksheet("June")
        worksheet.col_count = len(GOOGLE_SHEET_COLUMNS) + 1

        client._hide_internal_columns(worksheet)

        self.assertEqual(worksheet.hidden_columns, [(len(GOOGLE_SHEET_COLUMNS), 13)])

    def test_google_sheet_tab_url_targets_the_appended_worksheet(self):
        self.assertEqual(
            worksheet_url("spreadsheet-123", 456),
            "https://docs.google.com/spreadsheets/d/spreadsheet-123/edit#gid=456",
        )

    def test_monthly_ledger_places_category_before_description(self):
        self.assertEqual(
            GOOGLE_SHEET_COLUMNS[:5],
            ["check", "date", "source", "category", "description"],
        )

    def test_export_rows_are_limited_to_columns_a_through_l(self):
        worksheet = FakeWorksheet("June")
        dataframe = rows_to_dataframe(
            [
                {
                    "check": "No",
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "Cafe",
                    "amount": "-12.50",
                    "money_flow": "outflow",
                    "category": "Food",
                    "transaction_hash": "internal-only",
                    "reimbursement_for": "row 7",
                }
            ]
        )

        rows = _rows_for_worksheet(worksheet, dataframe)

        self.assertEqual(len(rows[0]), 12)
        self.assertEqual(rows[0][3], "Food")
        self.assertNotIn("internal-only", rows[0])
        self.assertNotIn("row 7", rows[0])

    def test_summary_formula_uses_standalone_reimbursements(self):
        formula = _checked_category_summary_formula(FakeWorksheet("June"))

        self.assertIn("Offsets Received", formula)
        self.assertNotIn('"Carousell Sales"', formula)
        self.assertIn('"Cashbacks & Refunds"', formula)
        self.assertIn('"Reimbursements"', formula)
        self.assertNotIn("reimbursement_for_category", formula)
        self.assertNotIn("reimbursement_candidate", formula)
        self.assertEqual(formula.count("("), formula.count(")"))

    def test_summary_formula_expands_to_the_worksheet_row_capacity(self):
        worksheet = FakeWorksheet("June")
        worksheet.row_count = 1600

        formula = _checked_category_summary_formula(worksheet)

        self.assertIn("$A$2:$A$1600", formula)
        self.assertIn("$H$2:$H$1600", formula)

    def test_new_month_is_added_as_a_distinct_summary_table(self):
        client = object.__new__(SheetClient)
        spreadsheet = FakeSpreadsheet([FakeWorksheet("June"), FakeWorksheet("July")])

        formula = client._year_summary_formula(spreadsheet)

        self.assertIn('{"June Summary","",""}', formula)
        self.assertIn('{"July Summary","",""}', formula)

    def test_future_month_is_added_as_a_summary_column(self):
        matrix, _ = _summary_matrix_rows(
            [FakeWorksheet("August"), FakeWorksheet("September")],
            "2026",
        )

        self.assertEqual(matrix[0], ["Category", "Aug 2026", "Sep 2026", "Year Total"])

    def test_future_years_route_to_their_own_workbooks(self):
        self.assertEqual(_period_from_date("2027-01-03"), (2027, "January"))
        self.assertEqual(_period_from_date("2028-12-31"), (2028, "December"))

    def test_summary_matrix_delegates_to_financial_statement(self):
        from financial_statement import statement_matrix

        worksheets = [FakeWorksheet("June"), FakeWorksheet("Jul"), FakeWorksheet("Aug")]
        statuses = {"June": 2, "Jul": 3, "Aug": 4}
        matrix, layout = _summary_matrix_rows(
            worksheets, "2026", salary_status_rows=statuses,
        )

        self.assertEqual(matrix[0], ["Category", "Jun 2026", "Jul 2026", "Aug 2026", "Year Total"])
        self.assertEqual((matrix, layout), statement_matrix(worksheets, "2026", salary_status_rows=statuses))

    def test_summary_write_replaces_legacy_content_with_one_matrix(self):
        client = object.__new__(SheetClient)
        spreadsheet = FakeSpreadsheet([FakeWorksheet("June"), FakeWorksheet("Jul")])
        spreadsheet.title = "2026"
        legacy_rows = [["Category", "Jun 2026", "Jul 2026", "Aug 2026", "Year Total"]]
        legacy_rows.extend([[""] * 5 for _ in range(26)])
        legacy_rows[26][0] = "Net Spend (a + b + c)"
        summary = FakeWorksheet("Summary", legacy_rows)

        client._write_year_summary(summary, spreadsheet)

        ranges = [cell_range for cell_range, _ in summary.updates]
        self.assertFalse(summary.cleared)
        self.assertEqual(ranges[0], "A1:D47")
        self.assertEqual(summary.frozen, (1, 1))
        self.assertEqual(summary.cleared_ranges, ["E1:E27"])
        formatted = {}
        for item in summary.batch_formats:
            formatted.setdefault(item["range"], {}).update(item["format"])
        self.assertEqual(formatted["A2:D2"]["backgroundColor"]["blue"], 0.43)
        self.assertEqual(formatted["A30:D30"]["backgroundColor"], formatted["A29:D29"]["backgroundColor"])
        self.assertEqual(formatted["A37:D37"]["backgroundColor"]["green"], 0.49)
        self.assertEqual(formatted["A37"]["backgroundColor"]["blue"], 0.96)
        self.assertEqual(formatted["A47:D47"]["backgroundColor"]["green"], 0.49)
        self.assertEqual(formatted["A47"]["backgroundColor"]["blue"], 0.96)

    def test_default_column_widths_are_150_then_100_pixels(self):
        spreadsheet = FakeSpreadsheet([])
        worksheet = FakeWorksheet("June")

        _apply_default_column_widths(spreadsheet, worksheet, 12)

        requests = spreadsheet.batch_updates[0]["requests"]
        self.assertEqual(requests[0]["updateDimensionProperties"]["properties"]["pixelSize"], 150)
        self.assertEqual(requests[1]["updateDimensionProperties"]["properties"]["pixelSize"], 100)
        self.assertEqual(requests[1]["updateDimensionProperties"]["range"]["endIndex"], 12)

    def test_summary_total_row_has_a_distinct_color(self):
        worksheet = FakeWorksheet(
            "Summary",
            [
                ["June Summary", "", ""],
                ["Metric", "Amount ($)", ""],
                ["Total Spend", "20", ""],
                ["Offsets Received", "5", ""],
                ["Net Spend", "15", ""],
                ["", "", ""],
                ["Category", "Spend ($)", "%"],
                ["Food", "20", "100%"],
                ["Total", "20", "100%"],
            ],
        )

        _format_year_summary(worksheet)

        formatted = {item["range"]: item["format"] for item in worksheet.batch_formats}
        self.assertEqual(formatted["A1:C1"]["backgroundColor"]["blue"], 0.43)
        self.assertEqual(formatted["A9:C9"]["backgroundColor"]["green"], 0.49)


class ReimbursementWorkflowTests(unittest.TestCase):
    def test_reimbursement_can_append_without_an_expense_link(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_PAYLAH",
                    "description": "Friend paid back lunch",
                    "amount": "18.00",
                    "money_flow": "inflow",
                    "category": "Reimbursement",
                    "reimbursement_candidate": True,
                    "reimbursement_type": "friend_repayment",
                    "confidence": 0.95,
                }
            ]
        )

        result = apply_workflow_state(dataframe)
        preview = append_preview_metrics(result)

        self.assertTrue(result.at[0, "include_in_append"])
        self.assertEqual(result.at[0, "status"], "ready")
        self.assertEqual(result.at[0, "category"], "Reimbursements")
        self.assertEqual(result.at[0, "reimbursement_for"], "")
        self.assertEqual(result.at[0, "reimbursement_for_category"], "")
        self.assertEqual(preview["offsets"], 18.0)
        self.assertEqual(preview["net_spend"], -18.0)

    def test_carousell_sales_are_income_and_only_refunds_offset_net_spend(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "Sold a desk",
                    "amount": "25.00",
                    "money_flow": "inflow",
                    "category": "Carousell Sales",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-06-05",
                    "source": "DBS_BANK",
                    "description": "Merchant refund",
                    "amount": "10.00",
                    "money_flow": "inflow",
                    "category": "Cashbacks & Refunds",
                    "confidence": 0.95,
                },
            ]
        )

        preview = append_preview_metrics(apply_workflow_state(dataframe))

        self.assertEqual(preview["income"], 25.0)
        self.assertEqual(preview["offsets"], 10.0)
        self.assertEqual(preview["net_spend"], -10.0)
        self.assertEqual(preview["operating"], 35.0)

    def test_dbs_bank_paylah_top_up_stays_ignored(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "TOP-UP TO PAYLAH!",
                    "amount": "-20.00",
                    "money_flow": "outflow",
                    "category": "Transfer",
                }
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertFalse(result.at[0, "include_in_append"])
        self.assertEqual(result.at[0, "status"], "ignored")

    def test_paylah_wallet_top_up_is_ignored_but_recipient_payment_stays_included(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-08-23",
                    "source": "UNKNOWN",
                    "description": "Top up my wallet",
                    "amount": "8.00",
                    "money_flow": "inflow",
                    "category": "Others",
                },
                {
                    "date": "2026-08-23",
                    "source": "DBS_PAYLAH",
                    "description": "amanda",
                    "amount": "-8.00",
                    "money_flow": "outflow",
                    "category": "Entertainment",
                },
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertFalse(result.at[0, "include_in_append"])
        self.assertEqual(result.at[0, "status"], "ignored")
        self.assertEqual(result.at[0, "category"], "Transfer")
        self.assertTrue(result.at[1, "include_in_append"])
        self.assertEqual(result.at[1, "money_flow"], "outflow")
        self.assertEqual(result.at[1, "amount"], -8.0)

    def test_uob_ebanking_payment_stays_ignored(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "UOB_TMRW",
                    "description": "PAYMT THRU E-BANK CARD PAYMENT",
                    "amount": "-20.00",
                    "money_flow": "outflow",
                    "category": "Bills",
                }
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertFalse(result.at[0, "include_in_append"])
        self.assertEqual(result.at[0, "status"], "ignored")
        self.assertEqual(result.at[0, "ignore_reason"], "UOB e-banking payment")

    def test_uob_credit_card_bill_payment_stays_ignored_without_hiding_carousell_sale(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-08-20",
                    "source": "DBS_BANK",
                    "description": "UOB:4006822041885495:!BANK Transfer CCRD 17869791140383620086 Other transfers FAST / PayNow Transfer ICT",
                    "amount": "-550.00",
                    "money_flow": "outflow",
                    "category": "Others",
                },
                {
                    "date": "2026-08-20",
                    "source": "DBS_BANK",
                    "description": "Carousell P NjwF 20260820SCBLSG22BRT0119722 CSDB FAST / PayNow Transfer ICT",
                    "amount": "35.00",
                    "money_flow": "inflow",
                    "category": "Carousell Sales",
                },
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertFalse(result.at[0, "include_in_append"])
        self.assertEqual(result.at[0, "status"], "ignored")
        self.assertEqual(result.at[0, "ignore_reason"], "UOB credit-card bill payment")
        self.assertTrue(result.at[1, "include_in_append"])
        self.assertEqual(result.at[1, "category"], "Carousell Sales")

    def test_same_topup_words_from_another_bank_are_not_auto_ignored(self):
        dataframe = rows_to_dataframe([{
            "date": "2026-06-04", "source": "UOB_TMRW",
            "description": "TOP UP MY WALLET", "amount": "-8.00",
            "money_flow": "outflow", "category": "Others", "confidence": .95,
        }])
        result = apply_workflow_state(dataframe)
        self.assertTrue(result.at[0, "include_in_append"])
        self.assertNotEqual(result.at[0, "status"], "ignored")

    def test_known_merchants_are_classified_deterministically(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "BUS/MRT SINGAPORE",
                    "amount": "1.09",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-06-25",
                    "source": "DBS_BANK",
                    "description": "ACCOUNTANT-GENERAL",
                    "amount": "-5000.00",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-06-30",
                    "source": "DBS_BANK",
                    "description": "IBG GOV GOV",
                    "amount": "-200.00",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-07-01",
                    "source": "DBS_BANK",
                    "description": "MINDEF SAF",
                    "amount": "-100.00",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-07-02",
                    "source": "DBS_BANK",
                    "description": "INTERACTIVE BROKERS PART",
                    "amount": "1200.00",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-07-03",
                    "source": "DBS_BANK",
                    "description": "TIGER BROKERS SINGAPORE PTE LTD",
                    "amount": "800.00",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-07-04",
                    "source": "DBS_BANK",
                    "description": "MOOMOO SG",
                    "amount": "600.00",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertEqual(result.at[0, "category"], "Public Transport")
        self.assertEqual(result.at[0, "money_flow"], "outflow")
        self.assertEqual(result.at[0, "amount"], -1.09)
        self.assertEqual(result.at[1, "category"], "Net Salary")
        self.assertEqual(result.at[1, "money_flow"], "inflow")
        self.assertEqual(result.at[1, "amount"], 5000.0)
        self.assertEqual(result.at[2, "category"], "Prize Awards/Government Vouchers")
        self.assertEqual(result.at[2, "money_flow"], "inflow")
        self.assertEqual(result.at[2, "amount"], 200.0)
        self.assertEqual(result.at[3, "category"], "Prize Awards/Government Vouchers")
        self.assertEqual(result.at[3, "money_flow"], "inflow")
        self.assertEqual(result.at[3, "amount"], 100.0)
        self.assertEqual(result.at[4, "category"], "ETF Contributions")
        self.assertEqual(result.at[4, "money_flow"], "outflow")
        self.assertEqual(result.at[4, "amount"], -1200.0)
        self.assertEqual(result.at[4, "status"], "needs_review")
        self.assertFalse(result.at[4, "include_in_append"])
        self.assertFalse(result.at[4, "allocation_confirmed"])
        for row in [5, 6]:
            self.assertEqual(result.at[row, "category"], "Equity Contributions")
            self.assertEqual(result.at[row, "money_flow"], "outflow")
            self.assertLess(result.at[row, "amount"], 0)
            self.assertEqual(result.at[row, "status"], "needs_review")
            self.assertFalse(result.at[row, "include_in_append"])
            self.assertFalse(result.at[row, "allocation_confirmed"])
        self.assertTrue(result.loc[:3, "include_in_append"].all())


class OverlappingScreenshotDuplicateTests(unittest.TestCase):
    def test_flags_ocr_variants_of_the_same_transaction(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "PAYNOW TO ALICE TAN",
                    "amount": "-23.50",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "PAYNOW ALICE TAN",
                    "amount": "-23.50",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "PAYNOW TO ALICETAN",
                    "amount": "-23.50",
                    "money_flow": "outflow",
                    "category": "Others",
                    "confidence": 0.95,
                },
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertTrue(overlapping_screenshot_duplicate_mask(dataframe).at[1])
        self.assertTrue(overlapping_screenshot_duplicate_mask(dataframe).at[2])
        self.assertTrue(result.at[0, "include_in_append"])
        self.assertFalse(result.at[1, "include_in_append"])
        self.assertFalse(result.at[2, "include_in_append"])
        self.assertIn("overlapping screenshots", result.at[1, "review_note"])

        comparisons = duplicate_comparisons(dataframe)
        self.assertEqual(len(comparisons), 2)
        self.assertEqual(comparisons[0]["reference_index"], 0)
        self.assertEqual(comparisons[0]["duplicate_index"], 1)

    def test_does_not_merge_distinct_same_amount_purchases(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "GRAB TAXI",
                    "amount": "-18.00",
                    "money_flow": "outflow",
                    "category": "Transport",
                    "confidence": 0.95,
                },
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "GRAB FOOD",
                    "amount": "-18.00",
                    "money_flow": "outflow",
                    "category": "Food",
                    "confidence": 0.95,
                },
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertFalse(overlapping_screenshot_duplicate_mask(dataframe).any())
        self.assertTrue(result["include_in_append"].all())

    def test_different_references_keep_identical_payments_distinct(self):
        common = {
            "date": "2026-06-04", "source": "DBS_PAYLAH", "description": "AMANDA",
            "amount": "-8.00", "money_flow": "outflow", "category": "Food", "confidence": .95,
        }
        dataframe = rows_to_dataframe([
            {**common, "transaction_reference": "PAY-001"},
            {**common, "transaction_reference": "PAY-002"},
        ])
        result = apply_workflow_state(dataframe)
        self.assertTrue(result["include_in_append"].all())
        self.assertEqual(duplicate_comparisons(dataframe), [])

    def test_possible_two_leg_allocations_require_a_separate_decision(self):
        dataframe = rows_to_dataframe([
            {"date": "2026-06-04", "source": "DBS_BANK", "description": "TRANSFER TO BROKER",
             "amount": "-2000", "currency": "SGD", "money_flow": "outflow",
             "category": "ETF Contributions", "allocation_confirmed": True, "confidence": .95},
            {"date": "2026-06-05", "source": "BROKER", "description": "BUY ETF",
             "amount": "-2000", "currency": "SGD", "money_flow": "outflow",
             "category": "ETF Contributions", "allocation_confirmed": True, "confidence": .95},
        ])
        result = apply_workflow_state(dataframe)
        self.assertTrue(result.at[0, "include_in_append"])
        self.assertFalse(result.at[1, "include_in_append"])
        self.assertIn("Duplicate-looking", result.at[1, "review_note"])
        self.assertIn("second leg", duplicate_comparisons(dataframe)[0]["reason"])


class MerchantRuleTests(unittest.TestCase):
    def test_can_edit_and_forget_a_merchant_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_file = os.path.join(temp_dir, "review_memory.json")
            with patch.dict(os.environ, {"REVIEW_MEMORY_FILE": memory_file}):
                dataframe = rows_to_dataframe(
                    [
                        {
                            "date": "2026-06-04",
                            "source": "DBS_BANK",
                            "description": "Friendly Cafe Pte Ltd",
                            "amount": "-12.00",
                            "money_flow": "outflow",
                            "category": "Food",
                            "include_in_append": True,
                        }
                    ]
                )
                save_review_memory(dataframe)
                rules = merchant_rule_records()

                self.assertEqual(len(rules), 1)
                edited = [{**rules[0], "category": "Shopping", "forget": False}]
                self.assertEqual(update_merchant_rules(edited), 1)
                self.assertEqual(merchant_rule_records()[0]["category"], "Shopping")

                forgotten = [{**merchant_rule_records()[0], "forget": True}]
                self.assertEqual(update_merchant_rules(forgotten), 1)
                self.assertEqual(merchant_rule_records(), [])


class PerformanceWorkflowTests(unittest.TestCase):
    def test_failed_screenshot_does_not_discard_successful_results(self):
        class UploadedFile:
            def __init__(self, name):
                self.name = name

            def seek(self, _offset):
                return None

            def getbuffer(self):
                return b"test image"

        successful = TransactionExtraction.model_validate({
            "detected_source": "DBS_BANK", "statement_period": "",
            "transactions": [], "warnings": [],
        })
        uploads = [UploadedFile("good.png"), UploadedFile("bad.png")]
        with (
            patch("app.extract_transactions_from_image", side_effect=[successful, RuntimeError("unreadable")]),
            patch("app.st.progress"),
        ):
            result = extract_all(
                uploads, "gpt-4.1-mini", archive_screenshots=False,
                save_raw_text=False, run_category_agent=False,
                run_anomaly_agent=False, run_insight_agent=False,
            )
        self.assertTrue(result.empty)
        statuses = [state["status"] for state in __import__("app").st.session_state.screenshot_states.values()]
        self.assertEqual(sorted(statuses), ["Failed", "Ready"])

    def test_guided_states_are_derived_without_replacing_rule_status(self):
        dataframe = rows_to_dataframe([
            {"date": "2026-09-01", "source": "DBS_BANK", "description": "A", "amount": -1, "money_flow": "outflow", "category": "Food", "status": "ready"},
            {"date": "2026-09-02", "source": "DBS_BANK", "description": "B", "amount": -1, "money_flow": "outflow", "category": "Food", "status": "needs_review", "include_in_append": False},
            {"date": "2026-09-03", "source": "DBS_BANK", "description": "C", "amount": 0, "money_flow": "neutral", "category": "Transfer", "status": "ignored", "include_in_append": False},
        ])
        self.assertEqual(guided_workflow_state(dataframe).tolist(), ["ready", "needs_attention", "excluded"])

    def test_vision_concurrency_is_bounded_and_configurable(self):
        with patch.dict(os.environ, {"VISION_CONCURRENCY": "4"}):
            self.assertEqual(vision_worker_count(8), 4)
        with patch.dict(os.environ, {"VISION_CONCURRENCY": "invalid"}):
            self.assertEqual(vision_worker_count(8), 3)
        with patch.dict(os.environ, {"VISION_CONCURRENCY": "0"}):
            self.assertEqual(vision_worker_count(2), 1)

    def test_screenshots_are_extracted_in_parallel(self):
        class UploadedFile:
            def __init__(self, name):
                self.name = name

            def seek(self, _offset):
                return None

            def getbuffer(self):
                return b"test image"

        active = 0
        peak_active = 0
        lock = threading.Lock()

        def delayed_extraction(*_args, **_kwargs):
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return TransactionExtraction.model_validate(
                {
                    "detected_source": "DBS_BANK",
                    "statement_period": "",
                    "transactions": [],
                    "warnings": [],
                }
            )

        uploads = [UploadedFile(f"statement-{index}.png") for index in range(4)]
        with (
            patch.dict(os.environ, {"VISION_CONCURRENCY": "3"}),
            patch("app.extract_transactions_from_image", side_effect=delayed_extraction),
            patch("app.st.progress"),
        ):
            result = extract_all(
                uploads,
                "gpt-4.1-mini",
                archive_screenshots=False,
                save_raw_text=False,
                run_category_agent=False,
                run_anomaly_agent=False,
                run_insight_agent=False,
            )

        self.assertTrue(result.empty)
        self.assertGreaterEqual(peak_active, 2)

    def test_enrichment_uses_one_combined_model_request(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "Friendly Cafe",
                    "amount": "-12.00",
                    "money_flow": "outflow",
                    "category": "Others",
                    "transaction_hash": "txn-1",
                }
            ]
        )
        transaction_hash = dataframe.at[0, "transaction_hash"]
        output = EnrichmentOutput.model_validate(
            {
                "category_decisions": [
                    {
                        "transaction_hash": transaction_hash,
                        "category": "Food",
                        "confidence": 0.98,
                        "reason": "Cafe merchant.",
                    }
                ],
                "anomaly_decisions": [
                    {
                        "transaction_hash": transaction_hash,
                        "anomaly_flag": False,
                        "severity": "none",
                        "reason": "Routine purchase.",
                    }
                ],
                "insight_overview": "One routine food purchase.",
                "insight_notes": [{"transaction_hash": transaction_hash, "note": "Food spend."}],
            }
        )

        with patch("enrichment_agents._run_agent", return_value=output) as run_agent:
            enriched, overview = enrich_transactions(dataframe)

        self.assertEqual(run_agent.call_count, 1)
        self.assertEqual(enriched.at[0, "category"], "Food")
        self.assertFalse(enriched.at[0, "anomaly_flag"])
        self.assertEqual(enriched.at[0, "insight_note"], "Food spend.")
        self.assertEqual(overview, "One routine food purchase.")


class CategoryMigrationTests(unittest.TestCase):
    def test_removed_categories_migrate_to_others_and_gifts_is_renamed(self):
        removed = {
            "Auto & Parking",
            "Business",
            "Cash & Cheque",
            "Fuel",
            "Groceries",
            "Kids",
            "Loans",
            "Pets",
            "Cash Withdrawal",
            "Rental",
        }

        self.assertEqual(normalize_category("Gifts & Charity"), "Gifts")
        self.assertEqual(normalize_category("Gifts"), "Gifts")
        for category in removed:
            self.assertEqual(normalize_category(category), "Others")
            self.assertNotIn(category, CATEGORY_OPTIONS)

    def test_ambiguous_legacy_categories_are_retained_for_review(self):
        for category in ["Family", "Investments", "GVs & Prize Award", "Income", "Funding"]:
            with self.subTest(category=category):
                self.assertEqual(normalize_category(category), category)
                self.assertIn(category, CATEGORY_OPTIONS)

    def test_unambiguous_statement_aliases_are_normalized(self):
        self.assertEqual(normalize_category("Bills"), "Bills / Recurring Commitments")
        self.assertEqual(normalize_category("Reimbursement"), "Reimbursements")


class FlowRulesTests(unittest.TestCase):
    def test_flow_changes_normalize_amount_sign_and_category(self):
        dataframe = rows_to_dataframe(
            [
                {
                    "date": "2026-06-04",
                    "source": "DBS_BANK",
                    "description": "Cafe",
                    "amount": "12.50",
                    "money_flow": "outflow",
                    "category": "Food",
                },
                {
                    "date": "2026-06-05",
                    "source": "DBS_BANK",
                    "description": "PayNow received",
                    "amount": "-20.00",
                    "money_flow": "inflow",
                    "category": "Food",
                },
            ]
        )

        result = apply_workflow_state(dataframe)

        self.assertEqual(result.at[0, "amount"], -12.5)
        self.assertEqual(result.at[1, "amount"], 20.0)
        self.assertEqual(result.at[1, "category"], "Food")
        self.assertEqual(result.at[1, "status"], "needs_review")
        self.assertFalse(result.at[1, "include_in_append"])


if __name__ == "__main__":
    unittest.main()
