from __future__ import annotations

import calendar
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gspread
import pandas as pd
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from validators import (
    CATEGORY_MIGRATIONS,
    CATEGORY_OPTIONS,
    EXPENSE_OFFSET_INFLOW_CATEGORIES,
    GOOGLE_SHEET_COLUMNS,
    INFLOW_CATEGORY_OPTIONS,
    OUTFLOW_CATEGORY_OPTIONS,
    normalize_amount,
    normalize_bool,
)


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

APP_DIR = Path(__file__).resolve().parent
OBSOLETE_SHEET_COLUMNS = ["transaction_time"]
SUMMARY_WORKSHEET_TITLE = "Summary"
CATEGORY_VALIDATION_WORKSHEET_TITLE = "_Category Validation"


@dataclass
class SheetConfig:
    spreadsheet_id: str = ""
    worksheet_name: str = "Transactions"
    drive_folder_id: str = ""
    service_account_file: str | None = None
    service_account_json: str | None = None

    @classmethod
    def from_env(cls) -> "SheetConfig":
        return cls(
            spreadsheet_id=os.getenv("GOOGLE_SHEET_ID", ""),
            worksheet_name=os.getenv("GOOGLE_WORKSHEET_NAME", "Transactions"),
            drive_folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID", ""),
            service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"),
            service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),
        )


@dataclass
class AppendAudit:
    year: str
    month: str
    spreadsheet_id: str
    worksheet_title: str
    attempted: int
    appended: int
    skipped_duplicates: int
    verified: bool
    missing_hashes: list[str]
    created_spreadsheet: bool = False
    created_worksheet: bool = False


def _resolve_service_account_path(path_value: str | None) -> Path:
    if not path_value:
        raise ValueError("Configure GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON.")

    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    path = path.resolve()

    if not path.exists():
        raise FileNotFoundError(
            "Google service account file was not found. "
            f"Expected it at: {path}. "
            "Download the service account JSON from Google Cloud, put it in this folder, "
            "and make sure GOOGLE_SERVICE_ACCOUNT_FILE in .env matches the filename."
        )
    return path


class SheetClient:
    def __init__(self, config: SheetConfig):
        if not config.spreadsheet_id and not config.drive_folder_id:
            raise ValueError("Configure GOOGLE_DRIVE_FOLDER_ID or GOOGLE_SHEET_ID.")
        if not config.service_account_file and not config.service_account_json:
            raise ValueError(
                "Configure GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON."
            )

        if config.service_account_json:
            info = json.loads(config.service_account_json)
            credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            service_account_path = _resolve_service_account_path(
                config.service_account_file
            )
            credentials = Credentials.from_service_account_file(
                service_account_path, scopes=SCOPES
            )

        self.config = config
        self.client = gspread.authorize(credentials)
        self.drive = build("drive", "v3", credentials=credentials)
        self.spreadsheet = None
        self.worksheet = None

        if config.spreadsheet_id and not config.drive_folder_id:
            self.spreadsheet = self.client.open_by_key(config.spreadsheet_id)
            self.worksheet, _ = self._get_or_create_worksheet(
                self.spreadsheet, config.worksheet_name
            )

    @classmethod
    def from_env(cls) -> "SheetClient":
        return cls(SheetConfig.from_env())

    def _get_or_create_worksheet(self, spreadsheet, title: str):
        if title == SUMMARY_WORKSHEET_TITLE:
            return self._get_or_create_summary_worksheet(spreadsheet), False

        created = False
        try:
            worksheet = spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = self._create_or_reuse_default_worksheet(spreadsheet, title)
            created = True

        values = worksheet.get_all_values()
        if not values:
            _ensure_min_columns(worksheet, len(GOOGLE_SHEET_COLUMNS))
            worksheet.append_row(GOOGLE_SHEET_COLUMNS, value_input_option="USER_ENTERED")
        else:
            values = self._remove_obsolete_headers(worksheet, values)
            self._ensure_headers(spreadsheet, worksheet, values[0])
        self._hide_internal_columns(worksheet)
        self._apply_dropdowns(spreadsheet, worksheet)
        self._apply_transaction_table_format(worksheet, spreadsheet=spreadsheet)
        return worksheet, created

    def _get_or_create_summary_worksheet(self, spreadsheet):
        try:
            return spreadsheet.worksheet(SUMMARY_WORKSHEET_TITLE)
        except gspread.WorksheetNotFound:
            return spreadsheet.add_worksheet(
                title=SUMMARY_WORKSHEET_TITLE,
                rows=1000,
                cols=6,
            )

    def _create_or_reuse_default_worksheet(self, spreadsheet, title: str):
        worksheets = spreadsheet.worksheets()
        if len(worksheets) == 1 and worksheets[0].title == "Sheet1":
            worksheet = worksheets[0]
            if not worksheet.get_all_values():
                worksheet.update_title(title)
                return worksheet

        return spreadsheet.add_worksheet(
            title=title, rows=1000, cols=len(GOOGLE_SHEET_COLUMNS)
        )

    def _ensure_headers(self, spreadsheet, worksheet, existing_headers: list[str]) -> None:
        headers = list(existing_headers)
        headers = self._insert_missing_ordered_columns(worksheet, headers)
        headers = self._move_misordered_columns(spreadsheet, worksheet, headers)

        changed = False
        for column in GOOGLE_SHEET_COLUMNS:
            if column not in headers:
                headers.append(column)
                changed = True

        if changed:
            _ensure_min_columns(worksheet, len(headers))
            worksheet.update("1:1", [headers], value_input_option="USER_ENTERED")

    def _insert_missing_ordered_columns(self, worksheet, headers: list[str]) -> list[str]:
        for target_index, column in enumerate(GOOGLE_SHEET_COLUMNS):
            if column in headers:
                continue

            remaining_headers = headers[target_index:]
            should_insert = any(header in GOOGLE_SHEET_COLUMNS for header in remaining_headers)
            if not should_insert:
                continue

            _ensure_min_columns(worksheet, len(headers) + 1)
            worksheet.insert_cols(
                [[column]],
                col=target_index + 1,
                value_input_option="USER_ENTERED",
            )
            headers.insert(target_index, column)

        return headers

    def _move_misordered_columns(self, spreadsheet, worksheet, headers: list[str]) -> list[str]:
        for target_index, column in enumerate(GOOGLE_SHEET_COLUMNS):
            if column not in headers:
                continue

            current_index = headers.index(column)
            if current_index == target_index:
                continue

            spreadsheet.batch_update(
                {
                    "requests": [
                        {
                            "moveDimension": {
                                "source": {
                                    "sheetId": worksheet.id,
                                    "dimension": "COLUMNS",
                                    "startIndex": current_index,
                                    "endIndex": current_index + 1,
                                },
                                "destinationIndex": target_index,
                            }
                        }
                    ]
                }
            )
            moved = headers.pop(current_index)
            headers.insert(target_index, moved)

        return headers

    def _hide_internal_columns(self, worksheet) -> None:
        if worksheet.col_count > len(GOOGLE_SHEET_COLUMNS):
            worksheet.hide_columns(len(GOOGLE_SHEET_COLUMNS) + 1, worksheet.col_count)

    def _remove_obsolete_headers(self, worksheet, values: list[list[str]]) -> list[list[str]]:
        if not values:
            return values

        headers = list(values[0])
        deleted = False
        for column in OBSOLETE_SHEET_COLUMNS:
            while column in headers:
                column_index = headers.index(column) + 1
                worksheet.delete_columns(column_index)
                headers.pop(column_index - 1)
                deleted = True

        return worksheet.get_all_values() if deleted else values

    def _apply_transaction_table_format(self, worksheet, *, spreadsheet=None) -> None:
        headers = _worksheet_headers(worksheet)
        if not headers:
            return

        end_col = _column_letter(len(GOOGLE_SHEET_COLUMNS))
        worksheet.format(
            f"A:{end_col}",
            {"textFormat": {"fontSize": 12}},
        )
        worksheet.format(
            f"A1:{end_col}1",
            {
                "textFormat": {"bold": True, "fontSize": 12},
                "backgroundColor": {"red": 0.93, "green": 0.95, "blue": 0.98},
            },
        )
        worksheet.freeze(rows=1)
        if spreadsheet is not None:
            _apply_default_column_widths(
                spreadsheet,
                worksheet,
                len(GOOGLE_SHEET_COLUMNS),
                transaction_layout=True,
            )

    def _apply_dropdowns(self, spreadsheet, worksheet) -> None:
        headers = _worksheet_headers(worksheet)
        requests = []

        if "category" in headers and "money_flow" in headers:
            category_index = headers.index("category")
            requests.append(
                _dropdown_request(
                    worksheet_id=worksheet.id,
                    column_index=category_index,
                    options=CATEGORY_OPTIONS,
                    message=(
                        "Inflow: Carousell Sales, Cashbacks & Refunds, Reimbursement, "
                        "or GVs & Prize Award. Outflow: an expense category."
                    ),
                )
            )

        if "check" in headers:
            check_index = headers.index("check")
            requests.append(
                _dropdown_request(
                    worksheet_id=worksheet.id,
                    column_index=check_index,
                    options=["Yes", "No"],
                    message="Tally checked? Choose Yes or No.",
                )
            )

        if "money_flow" in headers:
            flow_index = headers.index("money_flow")
            requests.append(
                _dropdown_request(
                    worksheet_id=worksheet.id,
                    column_index=flow_index,
                    options=["outflow", "inflow", "neutral"],
                    message="Choose money flow.",
                )
            )

        if not requests:
            return
        spreadsheet.batch_update({"requests": requests})

    def _ensure_category_validation_worksheet(self, spreadsheet):
        try:
            worksheet = spreadsheet.worksheet(CATEGORY_VALIDATION_WORKSHEET_TITLE)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=CATEGORY_VALIDATION_WORKSHEET_TITLE,
                rows=max(len(OUTFLOW_CATEGORY_OPTIONS), len(INFLOW_CATEGORY_OPTIONS)) + 1,
                cols=3,
            )

        values = [["Outflow", "Inflow", "Neutral"]]
        height = max(len(OUTFLOW_CATEGORY_OPTIONS), len(INFLOW_CATEGORY_OPTIONS))
        values.extend(
            [
                [
                    OUTFLOW_CATEGORY_OPTIONS[index] if index < len(OUTFLOW_CATEGORY_OPTIONS) else "",
                    INFLOW_CATEGORY_OPTIONS[index] if index < len(INFLOW_CATEGORY_OPTIONS) else "",
                    "Transfer" if index == 0 else "",
                ]
                for index in range(height)
            ]
        )
        worksheet.update(
            f"A1:C{len(values)}",
            values,
            value_input_option="RAW",
        )
        spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": worksheet.id, "hidden": True},
                            "fields": "hidden",
                        }
                    }
                ]
            }
        )
        return worksheet

    def existing_duplicate_keys(self) -> set[str]:
        if self.worksheet is None:
            return set()
        return _duplicate_keys_from_worksheet(self.worksheet)

    def existing_duplicate_mask_by_period(self, dataframe: pd.DataFrame) -> pd.Series:
        mask = pd.Series(False, index=dataframe.index)
        if dataframe.empty:
            return mask

        if not self.config.drive_folder_id:
            existing_keys = self.existing_duplicate_keys()
            incoming_keys = dataframe.apply(_transaction_duplicate_key, axis=1)
            return incoming_keys.isin(existing_keys)

        cache: dict[tuple[str, str], set[str]] = {}
        for index, row in dataframe.iterrows():
            try:
                year, month = _period_from_date(row.get("date", ""))
            except ValueError:
                continue

            period = (str(year), month)
            if period not in cache:
                cache[period] = self._existing_duplicate_keys_for_period(
                    year=str(year),
                    month=month,
                )

            key = _transaction_duplicate_key(row)
            if key and key in cache[period]:
                mask.at[index] = True

        return mask

    def _existing_duplicate_keys_for_period(self, *, year: str, month: str) -> set[str]:
        spreadsheet_id = self._find_spreadsheet_in_folder(year)
        if not spreadsheet_id:
            return set()

        spreadsheet = self.client.open_by_key(spreadsheet_id)
        try:
            worksheet = spreadsheet.worksheet(month)
        except gspread.WorksheetNotFound:
            return set()
        return _duplicate_keys_from_worksheet(worksheet)

    def category_memory_records(self, limit: int = 750) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []

        if self.config.drive_folder_id:
            for spreadsheet_id in self._list_spreadsheets_in_folder():
                spreadsheet = self.client.open_by_key(spreadsheet_id)
                for worksheet in spreadsheet.worksheets():
                    if worksheet.title in {
                        SUMMARY_WORKSHEET_TITLE,
                        CATEGORY_VALIDATION_WORKSHEET_TITLE,
                    }:
                        continue
                    records.extend(_category_records_from_worksheet(worksheet))
                    if len(records) >= limit:
                        return records[:limit]
            return records[:limit]

        if self.worksheet is not None:
            return _category_records_from_worksheet(self.worksheet)[:limit]

        return []

    def append_transactions(self, dataframe: pd.DataFrame) -> int:
        if self.worksheet is None:
            raise ValueError("Single-sheet append requires GOOGLE_SHEET_ID without GOOGLE_DRIVE_FOLDER_ID.")
        if dataframe.empty:
            return 0

        _append_transaction_rows(self.worksheet, dataframe)
        return len(dataframe)

    def append_transactions_by_period(
        self,
        dataframe: pd.DataFrame,
        *,
        skip_duplicates: bool = True,
    ) -> list[AppendAudit]:
        if dataframe.empty:
            return []
        if not self.config.drive_folder_id:
            return [self._append_legacy_with_audit(dataframe, skip_duplicates=skip_duplicates)]

        audits: list[AppendAudit] = []
        working = dataframe.copy()
        working["_period"] = working["date"].apply(_period_from_date)

        period_groups = working.groupby("_period", sort=False)
        for (year, month), group in sorted(
            period_groups,
            key=lambda item: (
                item[0][0],
                list(calendar.month_name).index(item[0][1]),
            ),
        ):
            group = group.drop(columns=["_period"])
            spreadsheet, created_spreadsheet = self._get_or_create_year_spreadsheet(str(year))
            worksheet, created_worksheet = self._get_or_create_worksheet(spreadsheet, month)
            audit = self._append_group_with_audit(
                worksheet=worksheet,
                dataframe=group,
                skip_duplicates=skip_duplicates,
                year=str(year),
                month=month,
                spreadsheet_id=spreadsheet.id,
                created_spreadsheet=created_spreadsheet,
                created_worksheet=created_worksheet,
            )
            audits.append(audit)

        for year in sorted({audit.year for audit in audits if audit.year != "single"}):
            spreadsheet, _ = self._get_or_create_year_spreadsheet(year)
            self.refresh_year_summary(spreadsheet)

        return audits

    def refresh_year_summary(self, spreadsheet) -> None:
        summary = self._get_or_create_summary_worksheet(spreadsheet)
        for month_worksheet in self._month_worksheets(spreadsheet):
            month_values = month_worksheet.get_all_values()
            if month_values:
                self._ensure_headers(spreadsheet, month_worksheet, month_values[0])
            self._hide_internal_columns(month_worksheet)
            self._migrate_category_labels(month_worksheet)
            self._normalize_month_ledger_values(month_worksheet)
            self._apply_dropdowns(spreadsheet, month_worksheet)
            self._apply_transaction_table_format(month_worksheet, spreadsheet=spreadsheet)
        self._write_year_summary(summary, spreadsheet)

    def _migrate_category_labels(self, worksheet) -> int:
        headers = _worksheet_headers(worksheet)
        if "category" not in headers:
            return 0

        category_column = _column_letter(headers.index("category") + 1)
        values = worksheet.get(f"{category_column}2:{category_column}")
        replacements = [
            [CATEGORY_MIGRATIONS.get(str(value[0]).strip(), str(value[0]).strip())] if value else [""]
            for value in values
        ]
        if replacements == values:
            return 0
        worksheet.update(
            f"{category_column}2:{category_column}{len(replacements) + 1}",
            replacements,
            value_input_option="USER_ENTERED",
        )
        return sum(before != after for before, after in zip(values, replacements))

    def _normalize_month_ledger_values(self, worksheet) -> int:
        """Make direct Sheet edits obey the same flow/category rules as the app."""
        headers = _worksheet_headers(worksheet)
        required = {"amount", "money_flow", "category"}
        if not required.issubset(headers):
            return 0

        values = worksheet.get(f"A2:{_column_letter(len(headers))}")
        if not values:
            return 0

        amount_index = headers.index("amount")
        flow_index = headers.index("money_flow")
        category_index = headers.index("category")
        normalized_amounts = []
        normalized_categories = []
        changes = 0
        for row in values:
            padded = row + [""] * (len(headers) - len(row))
            flow = str(padded[flow_index]).strip().lower()
            category = str(padded[category_index]).strip()
            amount = normalize_amount(padded[amount_index])
            normalized_amount = -abs(amount) if flow == "outflow" else abs(amount) if flow == "inflow" else amount
            if flow == "inflow" and category not in INFLOW_CATEGORY_OPTIONS:
                category = "Reimbursement"
            elif flow == "outflow" and category not in OUTFLOW_CATEGORY_OPTIONS:
                category = "Others"
            normalized_amounts.append([normalized_amount])
            normalized_categories.append([category])
            current_amount = normalize_amount(padded[amount_index])
            changes += int(
                current_amount != normalized_amount
                or str(padded[category_index]).strip() != category
            )

        if not changes:
            return 0
        amount_column = _column_letter(amount_index + 1)
        category_column = _column_letter(category_index + 1)
        end_row = len(values) + 1
        worksheet.update(
            f"{amount_column}2:{amount_column}{end_row}",
            normalized_amounts,
            value_input_option="USER_ENTERED",
        )
        worksheet.update(
            f"{category_column}2:{category_column}{end_row}",
            normalized_categories,
            value_input_option="USER_ENTERED",
        )
        return changes

    def _month_worksheets(self, spreadsheet) -> list:
        worksheets = [
            worksheet
            for worksheet in spreadsheet.worksheets()
            if _month_number_from_title(worksheet.title) is not None
        ]
        return sorted(worksheets, key=lambda worksheet: _month_number_from_title(worksheet.title))

    def _year_summary_formula(self, spreadsheet) -> str:
        chunks: list[str] = []
        for worksheet in self._month_worksheets(spreadsheet):
            formula = _checked_category_summary_formula(worksheet)
            if not formula:
                continue

            chunks.extend(
                [
                    f'{{"{worksheet.title} Summary","",""}}',
                    formula.removeprefix("="),
                    '{"","",""}',
                    '{"","",""}',
                ]
            )

        if not chunks:
            return ""

        return "=VSTACK(" + ",".join(chunks[:-2]) + ")"

    def _write_year_summary(
        self,
        worksheet,
        spreadsheet,
    ) -> None:
        month_worksheets = self._month_worksheets(spreadsheet)
        year = _summary_year_label(spreadsheet.title)
        matrix, layout = _summary_matrix_rows(month_worksheets, year)
        matrix_width = len(matrix[0])
        matrix_end_col = _column_letter(matrix_width)
        matrix_end_row = len(matrix)
        _ensure_min_columns(worksheet, matrix_width)

        # Write the replacement before removing old content so a failed refresh never blanks Summary.
        worksheet.update(
            f"A1:{matrix_end_col}{matrix_end_row}",
            matrix,
            value_input_option="USER_ENTERED",
        )
        worksheet.update(
            f"A1:{matrix_end_col}1",
            [matrix[0]],
            value_input_option="RAW",
        )
        worksheet.batch_clear(
            [
                f"A{matrix_end_row + 1}:ZZ1000",
                f"{_column_letter(matrix_width + 1)}1:ZZ{matrix_end_row}",
            ]
        )
        worksheet.freeze(rows=1, cols=1)
        worksheet.format(
            f"A1:{matrix_end_col}1",
            {
                "backgroundColor": {"red": 0.12, "green": 0.25, "blue": 0.43},
                "textFormat": {
                    "bold": True,
                    "fontSize": 12,
                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                },
            },
        )
        worksheet.format(
            f"A2:{matrix_end_col}{matrix_end_row}",
            {"textFormat": {"bold": False, "fontSize": 12}},
        )
        worksheet.format(
            f"A{layout['gross_spend_row']}:{matrix_end_col}{layout['gross_spend_row']}",
            {
                "backgroundColor": {"red": 0.93, "green": 0.95, "blue": 0.98},
                "textFormat": {"bold": True, "fontSize": 12},
            },
        )
        for summary_row, color in (
            (layout["variable_spend_row"], {"red": 0.93, "green": 0.96, "blue": 0.98}),
            (layout["fixed_spend_row"], {"red": 0.94, "green": 0.94, "blue": 0.94}),
        ):
            worksheet.format(
                f"A{summary_row}:{matrix_end_col}{summary_row}",
                {
                    "backgroundColor": color,
                    "textFormat": {"bold": False, "fontSize": 12},
                },
            )
        for fixed_cost_category in ("Insurance", "Subscriptions", "Income Tax"):
            fixed_cost_row = next(
                row_index
                for row_index, row in enumerate(matrix, start=1)
                if row[0] == fixed_cost_category
            )
            worksheet.format(
                f"A{fixed_cost_row}:{matrix_end_col}{fixed_cost_row}",
                {
                    "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                    "textFormat": {"bold": False, "fontSize": 12},
                },
            )
        worksheet.format(
            f"A{layout['first_offset_row']}:{matrix_end_col}{layout['last_offset_row']}",
            {
                "backgroundColor": {"red": 0.88, "green": 0.95, "blue": 0.90},
                "textFormat": {"bold": False, "fontSize": 12},
            },
        )
        worksheet.format(
            f"A{layout['total_offset_row']}:{matrix_end_col}{layout['total_offset_row']}",
            {
                "backgroundColor": {"red": 0.82, "green": 0.94, "blue": 0.91},
                "textFormat": {"bold": True, "fontSize": 12},
            },
        )
        worksheet.format(
            f"A{layout['net_spend_row']}:{matrix_end_col}{layout['net_spend_row']}",
            {
                "backgroundColor": {"red": 0.10, "green": 0.49, "blue": 0.40},
                "textFormat": {
                    "bold": True,
                    "fontSize": 12,
                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                },
            },
        )
        worksheet.format(
            f"B:{matrix_end_col}",
            {"numberFormat": {"type": "NUMBER", "pattern": "$#,##0.00"}},
        )
        _apply_default_column_widths(
            spreadsheet,
            worksheet,
            matrix_width,
            first_column_width=200,
        )

    def _append_legacy_with_audit(
        self,
        dataframe: pd.DataFrame,
        *,
        skip_duplicates: bool,
    ) -> AppendAudit:
        if self.spreadsheet is None or self.worksheet is None:
            raise ValueError("Legacy append requires GOOGLE_SHEET_ID.")
        return self._append_group_with_audit(
            worksheet=self.worksheet,
            dataframe=dataframe,
            skip_duplicates=skip_duplicates,
            year="single",
            month=self.config.worksheet_name,
            spreadsheet_id=self.spreadsheet.id,
            created_spreadsheet=False,
            created_worksheet=False,
        )

    def _append_group_with_audit(
        self,
        *,
        worksheet,
        dataframe: pd.DataFrame,
        skip_duplicates: bool,
        year: str,
        month: str,
        spreadsheet_id: str,
        created_spreadsheet: bool,
        created_worksheet: bool,
    ) -> AppendAudit:
        attempted = len(dataframe)
        existing_transaction_keys = _duplicate_keys_from_worksheet(worksheet)
        to_append = dataframe.copy()
        skipped = 0

        if skip_duplicates:
            incoming_keys = to_append.apply(_transaction_duplicate_key, axis=1)
            duplicate_in_upload = incoming_keys.duplicated(keep="first") & incoming_keys.ne("")
            duplicate_override = to_append.get("duplicate_override", False)
            if not isinstance(duplicate_override, pd.Series):
                duplicate_override = pd.Series(False, index=to_append.index)
            duplicate_override = duplicate_override.apply(normalize_bool)
            is_new = duplicate_override | (
                ~incoming_keys.isin(existing_transaction_keys)
                & ~duplicate_in_upload
            )
            skipped = int((~is_new).sum())
            to_append = to_append[is_new].copy()

        if not to_append.empty:
            self._apply_transaction_table_format(worksheet)
            before_next_row = _next_transaction_row(worksheet)
            _append_transaction_rows(worksheet, to_append)
            after_next_row = _next_transaction_row(worksheet)
            appended_row_delta = after_next_row - before_next_row
        else:
            appended_row_delta = 0

        expected_keys = {
            _transaction_duplicate_key(row)
            for _, row in to_append.iterrows()
            if _transaction_duplicate_key(row)
        }
        actual_keys = _duplicate_keys_from_worksheet(worksheet)
        missing_hashes = sorted(expected_keys - actual_keys)
        if appended_row_delta < len(to_append):
            missing_hashes.append(
                f"row_count_shortfall_expected_{len(to_append)}_got_{appended_row_delta}"
            )

        return AppendAudit(
            year=year,
            month=month,
            spreadsheet_id=spreadsheet_id,
            worksheet_title=worksheet.title,
            attempted=attempted,
            appended=len(to_append),
            skipped_duplicates=skipped,
            verified=not missing_hashes,
            missing_hashes=missing_hashes,
            created_spreadsheet=created_spreadsheet,
            created_worksheet=created_worksheet,
        )

    def _get_or_create_year_spreadsheet(self, year: str):
        existing_id = self._find_spreadsheet_in_folder(year)
        if existing_id:
            return self.client.open_by_key(existing_id), False

        spreadsheet = self.client.create(year)
        self.drive.files().update(
            fileId=spreadsheet.id,
            addParents=self.config.drive_folder_id,
            removeParents="root",
            fields="id, parents",
        ).execute()
        return spreadsheet, True

    def _find_spreadsheet_in_folder(self, name: str) -> str | None:
        escaped_name = name.replace("'", "\\'")
        escaped_folder = self.config.drive_folder_id.replace("'", "\\'")
        query = (
            "mimeType='application/vnd.google-apps.spreadsheet' "
            f"and name='{escaped_name}' "
            f"and '{escaped_folder}' in parents "
            "and trashed=false"
        )
        response = self.drive.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=10,
        ).execute()
        files = response.get("files", [])
        if len(files) > 1:
            ids = ", ".join(file["id"] for file in files)
            raise ValueError(
                f"Found multiple Google Sheets named {name} in the Drive folder. "
                f"Please rename/remove duplicates before appending. IDs: {ids}"
            )
        return files[0]["id"] if files else None

    def _list_spreadsheets_in_folder(self) -> list[str]:
        escaped_folder = self.config.drive_folder_id.replace("'", "\\'")
        query = (
            "mimeType='application/vnd.google-apps.spreadsheet' "
            f"and '{escaped_folder}' in parents "
            "and trashed=false"
        )
        response = self.drive.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=100,
            orderBy="name desc",
        ).execute()
        return [file["id"] for file in response.get("files", [])]


def _period_from_date(value: object) -> tuple[int, str]:
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Cannot route row with invalid date: {text}") from exc
    return parsed.year, calendar.month_name[parsed.month]


def _duplicate_keys_from_worksheet(worksheet) -> set[str]:
    keys = set()
    for row in _transaction_records_from_worksheet(worksheet):
        key = _transaction_duplicate_key(row)
        if key:
            keys.add(key)
    return keys


def _transaction_duplicate_key(row: dict | pd.Series) -> str:
    date = str(row.get("date", "")).strip()
    description = " ".join(str(row.get("description", "")).upper().split())
    source = str(row.get("source", "")).strip().upper()
    currency = str(row.get("currency", "SGD") or "SGD").strip().upper()
    flow = str(row.get("money_flow", "")).strip().lower()
    amount = abs(normalize_amount(row.get("amount", 0)))
    if not date or not description:
        return ""
    return "|".join([date, source, description, flow, f"{amount:.2f}", currency])


def _summary_row_end(worksheet) -> int:
    return max(int(getattr(worksheet, "row_count", 1000)), 1000)


def _month_number_from_title(title: object) -> int | None:
    parts = str(title or "").strip().split()
    if len(parts) not in {1, 2} or (len(parts) == 2 and not re.fullmatch(r"\d{4}", parts[1])):
        return None

    name = parts[0].casefold()
    for month_number in range(1, 13):
        if name in {calendar.month_name[month_number].casefold(), calendar.month_abbr[month_number].casefold()}:
            return month_number
    return None


def _summary_year_label(spreadsheet_title: object) -> str:
    title = str(spreadsheet_title or "").strip()
    return title if re.fullmatch(r"\d{4}", title) else "Year"


def _summary_month_label(worksheet, year: str) -> str:
    month_number = _month_number_from_title(worksheet.title)
    if month_number is None:
        return str(worksheet.title)
    return f"{calendar.month_abbr[month_number]} {year}"


def _offset_categories_formula() -> str:
    categories = ",".join(
        f'"{category}"' for category in sorted(EXPENSE_OFFSET_INFLOW_CATEGORIES)
    )
    return "{" + categories + "}"


def _monthly_net_spend_formula(worksheet, *, headers: list[str] | None = None) -> str:
    headers = headers or _worksheet_headers(worksheet)
    required = ["check", "amount", "category", "money_flow"]
    if not all(column in headers for column in required):
        return "=0"

    title = _quote_sheet_title(worksheet.title)
    check_col = _column_letter(headers.index("check") + 1)
    amount_col = _column_letter(headers.index("amount") + 1)
    category_col = _column_letter(headers.index("category") + 1)
    flow_col = _column_letter(headers.index("money_flow") + 1)
    row_end = _summary_row_end(worksheet)
    amount_range = f"{title}!${amount_col}$2:${amount_col}${row_end}"
    check_range = f"{title}!${check_col}$2:${check_col}${row_end}"
    flow_range = f"{title}!${flow_col}$2:${flow_col}${row_end}"
    category_range = f"{title}!${category_col}$2:${category_col}${row_end}"
    gross_spend = (
        f"IFERROR(SUMPRODUCT(ABS({amount_range}),--({check_range}=\"Yes\"),"
        f"--({flow_range}=\"outflow\"),--({category_range}<>\"Transfer\")),0)"
    )
    offsets = "+".join(
        f"IFERROR(SUMPRODUCT(ABS({amount_range}),--({check_range}=\"Yes\"),"
        f"--({flow_range}=\"inflow\"),--({category_range}=\"{category}\")),0)"
        for category in sorted(EXPENSE_OFFSET_INFLOW_CATEGORIES)
    )
    return f"={gross_spend}-({offsets})"


def _monthly_category_formula(
    worksheet,
    category: str,
    *,
    headers: list[str] | None = None,
) -> str:
    headers = headers or _worksheet_headers(worksheet)
    required = ["check", "amount", "category", "money_flow"]
    if not all(column in headers for column in required):
        return "=0"

    title = _quote_sheet_title(worksheet.title)
    check_col = _column_letter(headers.index("check") + 1)
    amount_col = _column_letter(headers.index("amount") + 1)
    category_col = _column_letter(headers.index("category") + 1)
    flow_col = _column_letter(headers.index("money_flow") + 1)
    row_end = _summary_row_end(worksheet)
    flow = "inflow" if category in INFLOW_CATEGORY_OPTIONS else "outflow"
    sign = "-" if category in EXPENSE_OFFSET_INFLOW_CATEGORIES else ""
    category_value = category.replace('"', '""')
    amount_range = f"{title}!${amount_col}$2:${amount_col}${row_end}"
    check_range = f"{title}!${check_col}$2:${check_col}${row_end}"
    flow_range = f"{title}!${flow_col}$2:${flow_col}${row_end}"
    category_range = f"{title}!${category_col}$2:${category_col}${row_end}"
    return (
        f"={sign}IFERROR(SUMPRODUCT(ABS({amount_range}),--({check_range}=\"Yes\"),"
        f"--({flow_range}=\"{flow}\"),--({category_range}=\"{category_value}\")),0)"
    )


def _monthly_gross_spend_formula(worksheet, *, headers: list[str] | None = None) -> str:
    headers = headers or _worksheet_headers(worksheet)
    required = ["check", "amount", "category", "money_flow"]
    if not all(column in headers for column in required):
        return "=0"

    title = _quote_sheet_title(worksheet.title)
    check_col = _column_letter(headers.index("check") + 1)
    amount_col = _column_letter(headers.index("amount") + 1)
    category_col = _column_letter(headers.index("category") + 1)
    flow_col = _column_letter(headers.index("money_flow") + 1)
    row_end = _summary_row_end(worksheet)
    amount_range = f"{title}!${amount_col}$2:${amount_col}${row_end}"
    check_range = f"{title}!${check_col}$2:${check_col}${row_end}"
    flow_range = f"{title}!${flow_col}$2:${flow_col}${row_end}"
    category_range = f"{title}!${category_col}$2:${category_col}${row_end}"
    return (
        f"=IFERROR(SUMPRODUCT(ABS({amount_range}),--({check_range}=\"Yes\"),"
        f"--({flow_range}=\"outflow\"),--({category_range}<>\"Transfer\")),0)"
    )


def _summary_matrix_rows(month_worksheets: list, year: str) -> tuple[list[list[str]], dict[str, int]]:
    fixed_categories = ["Insurance", "Subscriptions", "Income Tax"]
    spending_categories = [
        category
        for category in CATEGORY_OPTIONS
        if category not in INFLOW_CATEGORY_OPTIONS and category != "Transfer"
    ]
    variable_categories = [
        category for category in spending_categories if category not in fixed_categories
    ]
    offset_categories = [
        "Carousell Sales",
        "Cashbacks & Refunds",
        "Reimbursement",
        "GVs & Prize Award",
    ]
    month_headers = [_summary_month_label(worksheet, year) for worksheet in month_worksheets]
    matrix = [["Category", *month_headers, "Year Total"]]
    matrix.append(["Net Spend (a + b + c)", *([""] * len(month_worksheets)), ""])

    headers_by_worksheet = {
        worksheet.title: _worksheet_headers(worksheet) for worksheet in month_worksheets
    }
    for category in variable_categories:
        matrix.append(
            [category]
            + [
                _monthly_category_formula(
                    worksheet,
                    category,
                    headers=headers_by_worksheet[worksheet.title],
                )
                for worksheet in month_worksheets
            ]
            + [""]
        )
    matrix.append(["Variable Spend (a)", *([""] * len(month_worksheets)), ""])
    for category in fixed_categories:
        matrix.append(
            [category]
            + [
                _monthly_category_formula(
                    worksheet,
                    category,
                    headers=headers_by_worksheet[worksheet.title],
                )
                for worksheet in month_worksheets
            ]
            + [""]
        )
    matrix.append(["Fixed Spend (b)", *([""] * len(month_worksheets)), ""])
    matrix.append(["Gross Spend (a + b)", *([""] * len(month_worksheets)), ""])
    for category in offset_categories:
        matrix.append(
            [category]
            + [
                _monthly_category_formula(
                    worksheet,
                    category,
                    headers=headers_by_worksheet[worksheet.title],
                )
                for worksheet in month_worksheets
            ]
            + [""]
        )
    matrix.append(["Total Offset (c)", *([""] * len(month_worksheets)), ""])

    net_spend_row = 2
    first_variable_row = 3
    last_variable_row = first_variable_row + len(variable_categories) - 1
    variable_spend_row = last_variable_row + 1
    first_fixed_row = variable_spend_row + 1
    last_fixed_row = first_fixed_row + len(fixed_categories) - 1
    fixed_spend_row = last_fixed_row + 1
    gross_spend_row = fixed_spend_row + 1
    first_offset_row = gross_spend_row + 1
    last_offset_row = first_offset_row + len(offset_categories) - 1
    total_offset_row = last_offset_row + 1
    first_month_col = 2
    last_month_col = first_month_col + len(month_worksheets) - 1
    year_total_col = first_month_col + len(month_worksheets)

    for row_index in (
        list(range(first_variable_row, last_variable_row + 1))
        + list(range(first_fixed_row, last_fixed_row + 1))
        + list(range(first_offset_row, last_offset_row + 1))
    ):
        matrix[row_index - 1][-1] = _summary_row_total_formula(
            row_index,
            first_month_col,
            last_month_col,
        )
    for row_index in (variable_spend_row, fixed_spend_row):
        matrix[row_index - 1][-1] = _summary_row_total_formula(
            row_index,
            first_month_col,
            last_month_col,
        )

    for month_index in range(len(month_worksheets)):
        worksheet = month_worksheets[month_index]
        headers = headers_by_worksheet[worksheet.title]
        matrix[net_spend_row - 1][month_index + 1] = _monthly_net_spend_formula(
            worksheet,
            headers=headers,
        )
        column = _column_letter(first_month_col + month_index)
        matrix[variable_spend_row - 1][month_index + 1] = (
            f"=SUM({column}{first_variable_row}:{column}{last_variable_row})"
        )
        matrix[fixed_spend_row - 1][month_index + 1] = (
            f"=SUM({column}{first_fixed_row}:{column}{last_fixed_row})"
        )
        matrix[gross_spend_row - 1][month_index + 1] = (
            f"={column}{variable_spend_row}+{column}{fixed_spend_row}"
        )
        matrix[total_offset_row - 1][month_index + 1] = (
            f"=SUM({column}{first_offset_row}:{column}{last_offset_row})"
        )

    matrix[gross_spend_row - 1][-1] = _summary_row_total_formula(
        gross_spend_row,
        first_month_col,
        last_month_col,
    )
    matrix[net_spend_row - 1][-1] = _summary_row_total_formula(
        net_spend_row,
        first_month_col,
        last_month_col,
    )
    matrix[total_offset_row - 1][-1] = _summary_row_total_formula(
        total_offset_row,
        first_month_col,
        last_month_col,
    )
    return matrix, {
        "variable_spend_row": variable_spend_row,
        "fixed_spend_row": fixed_spend_row,
        "gross_spend_row": gross_spend_row,
        "first_offset_row": first_offset_row,
        "last_offset_row": last_offset_row,
        "total_offset_row": total_offset_row,
        "net_spend_row": net_spend_row,
        "year_total_col": year_total_col,
    }


def _summary_row_total_formula(row: int, first_month_col: int, last_month_col: int) -> str:
    if last_month_col < first_month_col:
        return "=0"
    return f"=SUM({_column_letter(first_month_col)}{row}:{_column_letter(last_month_col)}{row})"


def _annual_overview_rows(month_worksheets: list) -> tuple[list[list[str]], list[list[str]]]:
    months = [worksheet.title for worksheet in month_worksheets]
    monthly_rows = [["Month", "Net Spend ($)"]]
    monthly_rows.extend(
        [[worksheet.title, _monthly_net_spend_formula(worksheet)] for worksheet in month_worksheets]
    )
    categories = [
        category
        for category in CATEGORY_OPTIONS
        if category not in INFLOW_CATEGORY_OPTIONS and category != "Transfer"
    ]
    category_rows = [["Category"] + months]
    for category in categories:
        category_rows.append(
            [category]
            + [
                _monthly_category_formula(worksheet, category)
                for worksheet in month_worksheets
            ]
        )
    total_row = ["Total"]
    for month_index in range(len(month_worksheets)):
        column = _column_letter(5 + month_index)
        total_row.append(f"=SUM({column}3:{column}{len(category_rows)})")
    category_rows.append(total_row)
    return monthly_rows, category_rows


def _checked_category_summary_formula(worksheet) -> str:
    headers = _worksheet_headers(worksheet)
    required = ["check", "amount", "category", "money_flow"]
    if not all(column in headers for column in required):
        return ""

    title = _quote_sheet_title(worksheet.title)
    check_col = _column_letter(headers.index("check") + 1)
    amount_col = _column_letter(headers.index("amount") + 1)
    category_col = _column_letter(headers.index("category") + 1)
    flow_col = _column_letter(headers.index("money_flow") + 1)
    row_end = _summary_row_end(worksheet)
    offset_categories = _offset_categories_formula()[1:-1]
    checked_spend_filter = ",".join(
        [
            f"{title}!${check_col}$2:${check_col}${row_end}=\"Yes\"",
            f"{title}!${amount_col}$2:${amount_col}${row_end}<>0",
            f"LOWER({title}!${flow_col}$2:${flow_col}${row_end})=\"outflow\"",
            f"{title}!${category_col}$2:${category_col}${row_end}<>\"Transfer\"",
        ]
    )
    checked_offset_filter = ",".join(
        [
            f"{title}!${check_col}$2:${check_col}${row_end}=\"Yes\"",
            f"{title}!${amount_col}$2:${amount_col}${row_end}<>0",
            f"LOWER({title}!${flow_col}$2:${flow_col}${row_end})=\"inflow\"",
            f"ISNUMBER(MATCH({title}!${category_col}$2:${category_col}${row_end},{{{offset_categories}}},0))",
        ]
    )
    checked_outflow_count = (
        f"COUNTIFS({title}!${check_col}$2:${check_col}${row_end},\"Yes\","
        f"{title}!${amount_col}$2:${amount_col}${row_end},\"<>0\","
        f"{title}!${flow_col}$2:${flow_col}${row_end},\"outflow\","
        f"{title}!${category_col}$2:${category_col}${row_end},\"<>Transfer\")"
    )
    checked_inflow_counts = [
        (
            f"COUNTIFS({title}!${check_col}$2:${check_col}${row_end},\"Yes\","
            f"{title}!${amount_col}$2:${amount_col}${row_end},\"<>0\","
            f"{title}!${flow_col}$2:${flow_col}${row_end},\"inflow\","
            f"{title}!${category_col}$2:${category_col}${row_end},\"{category}\")"
        )
        for category in sorted(EXPENSE_OFFSET_INFLOW_CATEGORIES)
    ]
    has_checked_activity = "+".join([checked_outflow_count, *checked_inflow_counts])
    checked_activity_filter = ",".join(
        [
            f"{title}!${check_col}$2:${check_col}${row_end}=\"Yes\"",
            f"{title}!${amount_col}$2:${amount_col}${row_end}<>0",
            f"((LOWER({title}!${flow_col}$2:${flow_col}${row_end})=\"outflow\")*({title}!${category_col}$2:${category_col}${row_end}<>\"Transfer\"))+((LOWER({title}!${flow_col}$2:${flow_col}${row_end})=\"inflow\")*ISNUMBER(MATCH({title}!${category_col}$2:${category_col}${row_end},{{{offset_categories}}},0)))",
        ]
    )

    return (
        "=IF("
        f"{has_checked_activity}=0,"
        "{\"Metric\",\"Amount ($)\",\"\";\"Total Spend\",0,\"\";\"Offsets Received\",0,\"\";\"Net Spend\",0,\"\";\"\",\"\",\"\";\"Category\",\"Net Impact ($)\",\"% of Gross Spend\";\"Total\",0,0},"
        "LET("
        f"spend,IFERROR(SUM(ABS(FILTER({title}!${amount_col}$2:${amount_col}${row_end},{checked_spend_filter}))),0),"
        f"offsets,IFERROR(SUM(ABS(FILTER({title}!${amount_col}$2:${amount_col}${row_end},{checked_offset_filter}))),0),"
        f"categories,FILTER({title}!${category_col}$2:${category_col}${row_end},{checked_activity_filter}),"
        f"amounts,FILTER(IF(LOWER({title}!${flow_col}$2:${flow_col}${row_end})=\"inflow\",-ABS({title}!${amount_col}$2:${amount_col}${row_end}),ABS({title}!${amount_col}$2:${amount_col}${row_end})),{checked_activity_filter}),"
        "summary,QUERY({categories,amounts},\"select Col1, sum(Col2) group by Col1 order by sum(Col2) desc label Col1 '', sum(Col2) ''\",0),"
        "total,SUM(INDEX(summary,,2)),"
        "VSTACK({\"Metric\",\"Amount ($)\",\"\"},{\"Total Spend\",spend,\"\"},{\"Offsets Received\",offsets,\"\"},{\"Net Spend\",spend-offsets,\"\"},{\"\",\"\",\"\"},{\"Category\",\"Net Impact ($)\",\"% of Gross Spend\"},HSTACK(INDEX(summary,,1),INDEX(summary,,2),ARRAYFORMULA(IF(spend=0,0,INDEX(summary,,2)/spend))),{\"Total\",total,IF(spend=0,0,total/spend)})"
        ")"
        ")"
    )


def _quote_sheet_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _checked_category_totals(worksheet) -> list[tuple[str, float]]:
    totals: dict[str, float] = {}
    for row in _transaction_records_from_worksheet(worksheet):
        if not _is_checked_yes(row.get("check", "")):
            continue

        category = str(row.get("category", "")).strip() or "Uncategorized"
        amount = normalize_amount(row.get("amount", 0))
        money_flow = str(row.get("money_flow", "")).strip().lower()
        if money_flow == "outflow":
            value = abs(amount)
        elif money_flow == "inflow":
            value = -abs(amount)
        else:
            continue

        if value == 0:
            continue
        totals[category] = totals.get(category, 0.0) + value

    return sorted(totals.items(), key=lambda item: (-item[1], item[0].lower()))


def _is_checked_yes(value: object) -> bool:
    return str(value or "").strip().lower() in {"yes", "y", "true", "1"}


def _format_year_summary(worksheet, *, start_row: int = 1) -> None:
    values = worksheet.get(f"A{start_row}:C{start_row + 299}")
    formats = [
        {
            "range": f"A{start_row}:C",
            "format": {
                "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                "textFormat": {"bold": False, "italic": False, "fontSize": 12},
            },
        }
    ]

    for row_index, row in enumerate(values, start=start_row):
        label = str(row[0]).strip() if row else ""
        if not label:
            continue
        row_range = f"A{row_index}:C{row_index}"
        if label.endswith("Summary"):
            formats.append(
                {
                    "range": row_range,
                    "format": {
                        "backgroundColor": {"red": 0.12, "green": 0.25, "blue": 0.43},
                        "textFormat": {
                            "bold": True,
                            "fontSize": 12,
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        },
                    },
                }
            )
        elif label in {"Metric", "Category"}:
            formats.append(
                {
                    "range": row_range,
                    "format": {
                        "backgroundColor": {"red": 0.86, "green": 0.92, "blue": 0.97},
                        "textFormat": {"bold": True, "fontSize": 12},
                    },
                }
            )
        elif label == "Net Spend":
            formats.append(
                {
                    "range": row_range,
                    "format": {
                        "backgroundColor": {"red": 0.82, "green": 0.94, "blue": 0.91},
                        "textFormat": {"bold": True, "fontSize": 12},
                    },
                }
            )
        elif label == "Total":
            formats.append(
                {
                    "range": row_range,
                    "format": {
                        "backgroundColor": {"red": 0.10, "green": 0.49, "blue": 0.40},
                        "textFormat": {
                            "bold": True,
                            "fontSize": 12,
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        },
                    },
                }
            )

    worksheet.batch_format(formats)


def _ensure_min_columns(worksheet, required_columns: int) -> None:
    if worksheet.col_count < required_columns:
        worksheet.add_cols(required_columns - worksheet.col_count)


def _apply_default_column_widths(
    spreadsheet,
    worksheet,
    column_count: int,
    *,
    transaction_layout: bool = False,
    first_column_width: int = 150,
) -> None:
    if column_count < 1:
        return

    requests = [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": worksheet.id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": first_column_width},
                "fields": "pixelSize",
            }
        }
    ]
    if column_count > 1:
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": 1,
                        "endIndex": column_count,
                    },
                    "properties": {"pixelSize": 100},
                    "fields": "pixelSize",
                }
            }
        )
    if transaction_layout:
        # Category is column D and Description is column E in the user ledger.
        requests.extend(
            [
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "COLUMNS",
                            "startIndex": 3,
                            "endIndex": 4,
                        },
                        "properties": {"pixelSize": 160},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "COLUMNS",
                            "startIndex": 4,
                            "endIndex": 5,
                        },
                        "properties": {"pixelSize": 300},
                        "fields": "pixelSize",
                    }
                },
            ]
        )
    spreadsheet.batch_update({"requests": requests})


def _dropdown_request(
    *,
    worksheet_id: int,
    column_index: int,
    options: list[str],
    message: str,
) -> dict:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": 1,
                "startColumnIndex": column_index,
                "endColumnIndex": column_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": option}
                        for option in options
                    ],
                },
                "inputMessage": message,
                "strict": False,
                "showCustomUi": True,
            },
        }
    }


def _dependent_category_dropdown_request(
    *,
    worksheet_id: int,
    column_index: int,
    flow_column_index: int,
    validation_worksheet_title: str,
) -> dict:
    flow_column = _column_letter(flow_column_index + 1)
    title = validation_worksheet_title.replace("'", "''")
    range_formula = (
        f"=INDIRECT(\"'{title}'!\"&IF(${flow_column}2=\"inflow\","
        f"\"$B$2:$B${len(INFLOW_CATEGORY_OPTIONS) + 1}\","
        f"IF(${flow_column}2=\"outflow\","
        f"\"$A$2:$A${len(OUTFLOW_CATEGORY_OPTIONS) + 1}\",\"$C$2\")))"
    )
    return {
        "setDataValidation": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": 1,
                "startColumnIndex": column_index,
                "endColumnIndex": column_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_RANGE",
                    "values": [{"userEnteredValue": range_formula}],
                },
                "inputMessage": "Choose a category allowed by the selected flow.",
                "strict": True,
                "showCustomUi": True,
            },
        }
    }


def _amount_sign_validation_request(
    *,
    worksheet_id: int,
    amount_column_index: int,
    flow_column_index: int,
) -> dict:
    amount_column = _column_letter(amount_column_index + 1)
    flow_column = _column_letter(flow_column_index + 1)
    formula = (
        f'=OR(${amount_column}2="",${flow_column}2="",'
        f'AND(${flow_column}2="inflow",${amount_column}2>0),'
        f'AND(${flow_column}2="outflow",${amount_column}2<0),'
        f'AND(${flow_column}2="neutral",${amount_column}2=0))'
    )
    return {
        "setDataValidation": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": 1,
                "startColumnIndex": amount_column_index,
                "endColumnIndex": amount_column_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "CUSTOM_FORMULA",
                    "values": [{"userEnteredValue": formula}],
                },
                "inputMessage": "Inflow amounts must be positive; outflow amounts must be negative.",
                "strict": True,
                "showCustomUi": True,
            },
        }
    }


def _flow_sensitive_category_request(
    *,
    worksheet_id: int,
    column_index: int,
    flow_column_index: int,
) -> dict:
    category_column = _column_letter(column_index + 1)
    flow_column = _column_letter(flow_column_index + 1)
    inflow_test = "OR(" + ",".join(
        f'${category_column}2="{category}"' for category in INFLOW_CATEGORY_OPTIONS
    ) + ")"
    outflow_test = "OR(" + ",".join(
        f'${category_column}2="{category}"' for category in OUTFLOW_CATEGORY_OPTIONS
    ) + ")"
    formula = (
        f'=OR(AND(${flow_column}2="inflow",{inflow_test}),'
        f'AND(${flow_column}2="outflow",{outflow_test}),'
        f'AND(${flow_column}2="neutral",${category_column}2="Transfer"))'
    )
    return {
        "setDataValidation": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": 1,
                "startColumnIndex": column_index,
                "endColumnIndex": column_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "CUSTOM_FORMULA",
                    "values": [{"userEnteredValue": formula}],
                },
                "inputMessage": (
                    "Inflow: Carousell Sales, Cashbacks, or Reimbursement. "
                    "Outflow: an expense category. Neutral: Transfer."
                ),
                "strict": True,
                "showCustomUi": True,
            },
        }
    }


def _worksheet_headers(worksheet) -> list[str]:
    values = worksheet.get_all_values()
    if not values:
        return GOOGLE_SHEET_COLUMNS
    return values[0][: len(GOOGLE_SHEET_COLUMNS)]


def _transaction_records_from_worksheet(worksheet) -> list[dict[str, str]]:
    headers = _worksheet_headers(worksheet)
    if not headers:
        return []

    date_index = headers.index("date") if "date" in headers else 0
    end_col = _column_letter(len(headers))
    values = worksheet.get(f"A1:{end_col}")
    if len(values) <= 1:
        return []

    records = []
    for row in values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        if not str(padded[date_index]).strip():
            continue
        records.append(dict(zip(headers, padded[: len(headers)])))
    return records


def _category_records_from_worksheet(worksheet) -> list[dict[str, str]]:
    records = []
    for row in _transaction_records_from_worksheet(worksheet):
        if not _is_checked_yes(row.get("check", "")):
            continue
        description = str(row.get("description", "")).strip()
        category = str(row.get("category", "")).strip()
        if not description or category not in CATEGORY_OPTIONS:
            continue
        records.append(
            {
                "description": description,
                "category": category,
                "source": str(row.get("source", "")).strip(),
                "money_flow": str(row.get("money_flow", "")).strip(),
            }
        )
    return records


def _rows_for_worksheet(worksheet, dataframe: pd.DataFrame) -> list[list[str]]:
    headers = _worksheet_headers(worksheet)
    if not headers:
        headers = GOOGLE_SHEET_COLUMNS
    export_dataframe = dataframe.fillna("").copy()
    if "check" in headers:
        if "check" not in export_dataframe:
            export_dataframe["check"] = "No"
        else:
            export_dataframe["check"] = export_dataframe["check"].apply(
                lambda value: str(value or "No").strip() or "No"
            )
    return [
        [_sheet_cell_value(row.get(header, "")) for header in headers]
        for row in export_dataframe.to_dict(orient="records")
    ]


def _sheet_cell_value(value):
    if pd.isna(value):
        return ""
    if hasattr(value, "item"):
        value = value.item()
    return value


def _append_transaction_rows(worksheet, dataframe: pd.DataFrame) -> None:
    rows = _rows_for_worksheet(worksheet, dataframe)
    if not rows:
        return
    next_row = _next_transaction_row(worksheet)
    end_col = _column_letter(len(_worksheet_headers(worksheet)))
    worksheet.update(
        f"A{next_row}:{end_col}{next_row + len(rows) - 1}",
        rows,
        value_input_option="USER_ENTERED",
    )


def _next_transaction_row(worksheet) -> int:
    headers = _worksheet_headers(worksheet)
    date_column = headers.index("date") + 1 if "date" in headers else 1
    date_values = worksheet.col_values(date_column)
    return max(2, len(date_values) + 1)


def _column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _cell(column_index: int, row_index: int) -> str:
    return f"{_column_letter(column_index)}{row_index}"
