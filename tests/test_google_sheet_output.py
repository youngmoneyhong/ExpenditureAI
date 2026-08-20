import unittest
import os
import tempfile
import threading
import time
from unittest.mock import patch

import gspread
from app import (
    append_preview_metrics,
    apply_category_flow_rules,
    apply_workflow_state,
    apply_workflow_state,
    duplicate_comparisons,
    extract_all,
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
        self.rows = rows or [list(GOOGLE_SHEET_COLUMNS)]
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
        return self.rows

    def get(self, _range):
        return self.rows

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
        self.updates.append((cell_range, values))

    def columns_auto_resize(self, _start, _end):
        return None

    def add_cols(self, count):
        self.col_count += count

    def hide_columns(self, start, end):
        self.hidden_columns.append((start, end))


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self.worksheets_by_title = {worksheet.title: worksheet for worksheet in worksheets}
        self.batch_updates = []

    def worksheet(self, title):
        if title not in self.worksheets_by_title:
            raise gspread.WorksheetNotFound(title)
        return self.worksheets_by_title[title]

    def worksheets(self):
        return list(self.worksheets_by_title.values())

    def batch_update(self, request):
        self.batch_updates.append(request)


class GoogleSheetOutputTests(unittest.TestCase):
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
        self.assertIn('"Carousell Sales"', formula)
        self.assertIn('"Cashbacks & Refunds"', formula)
        self.assertIn('"Reimbursement"', formula)
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

    def test_summary_matrix_has_month_year_columns_and_net_spend(self):
        matrix, layout = _summary_matrix_rows(
            [FakeWorksheet("June"), FakeWorksheet("Jul"), FakeWorksheet("Aug")],
            "2026",
        )

        self.assertEqual(matrix[0], ["Category", "Jun 2026", "Jul 2026", "Aug 2026", "Year Total"])
        self.assertEqual(matrix[1][0], "Net Spend (a + b + c)")
        self.assertEqual(
            [row[0] for row in matrix],
            [
                "Category",
                "Net Spend (a + b + c)",
                "Food",
                "Public Transport",
                "Taxi",
                "Shopping",
                "Gifts",
                "Entertainment",
                "Travel",
                "Health",
                "Personal Care",
                "Education",
                "Bills",
                "Admin & Fees",
                "Others",
                "Variable Spend (a)",
                "Insurance",
                "Subscriptions",
                "Income Tax",
                "Fixed Spend (b)",
                "Gross Spend (a + b)",
                "Carousell Sales",
                "Cashbacks & Refunds",
                "Reimbursement",
                "GVs & Prize Award",
                "Total Offset (c)",
                "Net Spend (a + b + c)",
            ],
        )
        carousell_row = next(row for row in matrix if row[0] == "Carousell Sales")
        self.assertTrue(carousell_row[1].startswith("=-IFERROR"))
        self.assertEqual(matrix[layout["gross_spend_row"] - 1][0], "Gross Spend (a + b)")
        self.assertEqual(matrix[layout["total_offset_row"] - 1][0], "Total Offset (c)")
        self.assertEqual(matrix[layout["net_spend_row"] - 1][0], "Net Spend (a + b + c)")
        self.assertEqual(matrix[layout["bottom_net_spend_row"] - 1][0], "Net Spend (a + b + c)")
        self.assertIn("=SUM(B3:B15)", matrix[layout["variable_spend_row"] - 1][1])
        self.assertIn("=SUM(B17:B19)", matrix[layout["fixed_spend_row"] - 1][1])
        self.assertIn("SUMPRODUCT", matrix[layout["net_spend_row"] - 1][1])
        self.assertNotIn("LOWER(", matrix[layout["net_spend_row"] - 1][1])
        self.assertEqual(matrix[layout["bottom_net_spend_row"] - 1][1], "=B21+B26")

    def test_summary_write_replaces_legacy_content_with_one_matrix(self):
        client = object.__new__(SheetClient)
        spreadsheet = FakeSpreadsheet([FakeWorksheet("June"), FakeWorksheet("Jul")])
        spreadsheet.title = "2026"
        summary = FakeWorksheet("Summary")

        client._write_year_summary(summary, spreadsheet)

        ranges = [cell_range for cell_range, _ in summary.updates]
        self.assertFalse(summary.cleared)
        self.assertEqual(ranges[0], "A1:D27")
        self.assertEqual(summary.frozen, (1, 1))
        self.assertIn("A28:ZZ1000", summary.cleared_ranges)
        self.assertIn("E1:ZZ27", summary.cleared_ranges)
        formatted = dict(summary.formats)
        self.assertEqual(formatted["A3:D16"]["backgroundColor"]["blue"], 0.98)
        self.assertEqual(formatted["A17:D20"]["backgroundColor"]["red"], 0.94)
        self.assertEqual(formatted["A21:D21"]["backgroundColor"]["blue"], 0.82)
        self.assertEqual(formatted["A27:D27"]["backgroundColor"]["green"], 0.49)

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
        self.assertEqual(result.at[0, "category"], "Reimbursement")
        self.assertEqual(result.at[0, "reimbursement_for"], "")
        self.assertEqual(result.at[0, "reimbursement_for_category"], "")
        self.assertEqual(preview["offsets"], 18.0)
        self.assertEqual(preview["net_spend"], -18.0)

    def test_carousell_sales_and_refunds_offset_net_spend(self):
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

        self.assertEqual(preview["offsets"], 35.0)
        self.assertEqual(preview["net_spend"], -35.0)

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
            "Family",
            "Fuel",
            "Groceries",
            "Kids",
            "Loans",
            "Pets",
            "Cash Withdrawal",
            "Rental",
            "Investments",
        }

        self.assertEqual(normalize_category("Gifts & Charity"), "Gifts")
        self.assertEqual(normalize_category("Gifts"), "Gifts")
        for category in removed:
            self.assertEqual(normalize_category(category), "Others")
            self.assertNotIn(category, CATEGORY_OPTIONS)


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
        self.assertEqual(result.at[1, "category"], "Reimbursement")


if __name__ == "__main__":
    unittest.main()
