from __future__ import annotations

import calendar
import json
import math
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
    ALLOCATION_CATEGORIES,
    CATEGORY_MIGRATIONS,
    FIXED_EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    OFFSET_CATEGORIES,
    VARIABLE_EXPENSE_CATEGORIES,
    CATEGORY_OPTIONS,
    EXPENSE_OFFSET_INFLOW_CATEGORIES,
    GOOGLE_SHEET_COLUMNS,
    INFLOW_CATEGORY_OPTIONS,
    OUTFLOW_CATEGORY_OPTIONS,
    normalize_amount,
    normalize_amount_with_error,
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
STATEMENT_REVIEW_WORKSHEET_TITLE = "Statement Review"
STATEMENT_ISSUES_WORKSHEET_TITLE = "Statement Issues"
SALARY_STATUS_OPTIONS = ["Pending", "Confirmed zero", "Historical zero"]
LEGACY_CATEGORY_ALIASES = {"Bills": "Bills / Recurring Commitments", "Reimbursement": "Reimbursements"}


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
    worksheet_id: int
    attempted: int
    appended: int
    skipped_duplicates: int
    verified: bool
    missing_hashes: list[str]
    created_spreadsheet: bool = False
    created_worksheet: bool = False


@dataclass
class WorkbookRefreshAudit:
    year: str
    spreadsheet_id: str
    summary_worksheet_id: int
    month_tabs: int
    migrated_categories: int


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

    def _get_or_create_append_worksheet(self, spreadsheet, title: str):
        """Use existing month tabs without repeating expensive structure styling."""
        try:
            return spreadsheet.worksheet(title), False
        except gspread.WorksheetNotFound:
            return self._get_or_create_worksheet(spreadsheet, title)

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
            # gspread column ranges are zero-based and end-exclusive.
            worksheet.hide_columns(len(GOOGLE_SHEET_COLUMNS), worksheet.col_count)

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
                    options=list(dict.fromkeys([
                        *CATEGORY_OPTIONS,
                        *LEGACY_CATEGORY_ALIASES.keys(),
                        "Transfer",
                    ])),
                    message=(
                        "Choose a category. Inflow and outflow compatibility is checked "
                        "before append."
                    ),
                )
            )

        if "amount" in headers and "money_flow" in headers:
            requests.append(_amount_sign_validation_request(
                worksheet_id=worksheet.id,
                amount_column_index=headers.index("amount"),
                flow_column_index=headers.index("money_flow"),
            ))

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
        outflow_options = list(dict.fromkeys([
            *OUTFLOW_CATEGORY_OPTIONS,
            *[old for old, new in LEGACY_CATEGORY_ALIASES.items() if new in OUTFLOW_CATEGORY_OPTIONS],
        ]))
        inflow_options = list(dict.fromkeys([
            *INFLOW_CATEGORY_OPTIONS,
            *[old for old, new in LEGACY_CATEGORY_ALIASES.items() if new in INFLOW_CATEGORY_OPTIONS],
        ]))
        try:
            worksheet = spreadsheet.worksheet(CATEGORY_VALIDATION_WORKSHEET_TITLE)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=CATEGORY_VALIDATION_WORKSHEET_TITLE,
                rows=max(len(outflow_options), len(inflow_options)) + 1,
                cols=3,
            )
        _ensure_min_rows(
            worksheet, len(CATEGORY_OPTIONS) + len(LEGACY_CATEGORY_ALIASES) + 1
        )

        values = [["Outflow", "Inflow", "Neutral"]]
        height = max(len(outflow_options), len(inflow_options))
        values.extend(
            [
                [
                    outflow_options[index] if index < len(outflow_options) else "",
                    inflow_options[index] if index < len(inflow_options) else "",
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
            allocation_key = _allocation_collision_key(row)
            if (key and key in cache[period]) or (
                allocation_key and allocation_key in cache[period]
            ):
                mask.at[index] = True

        return mask

    def existing_duplicate_matches_by_period(self, dataframe: pd.DataFrame) -> dict:
        """Return matched ledger evidence keyed by incoming DataFrame index."""
        matches = {}
        if dataframe.empty or not self.config.drive_folder_id:
            return matches
        cache = {}
        for index, incoming in dataframe.iterrows():
            try:
                year, month = _period_from_date(incoming.get("date", ""))
            except ValueError:
                continue
            period = (str(year), month)
            if period not in cache:
                spreadsheet_id = self._find_spreadsheet_in_folder(str(year))
                if not spreadsheet_id:
                    cache[period] = ("", None, [])
                else:
                    spreadsheet = self.client.open_by_key(spreadsheet_id)
                    try:
                        worksheet = spreadsheet.worksheet(month)
                        cache[period] = (
                            spreadsheet_id,
                            worksheet.id,
                            _transaction_records_with_rows(worksheet),
                        )
                    except gspread.WorksheetNotFound:
                        cache[period] = (spreadsheet_id, None, [])
            spreadsheet_id, worksheet_id, candidates = cache[period]
            incoming_key = _transaction_duplicate_key(incoming)
            allocation_key = _allocation_collision_key(incoming)
            for row_number, candidate in candidates:
                if (
                    incoming_key and incoming_key == _transaction_duplicate_key(candidate)
                ) or (
                    allocation_key and allocation_key == _allocation_collision_key(candidate)
                ):
                    matches[index] = {
                        "spreadsheet_id": spreadsheet_id,
                        "worksheet": month,
                        "worksheet_id": worksheet_id,
                        "row": row_number,
                        "transaction": candidate,
                    }
                    break
        return matches

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
                        STATEMENT_REVIEW_WORKSHEET_TITLE,
                        STATEMENT_ISSUES_WORKSHEET_TITLE,
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
        _validate_append_transactions(dataframe)
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
            worksheet, created_worksheet = self._get_or_create_append_worksheet(
                spreadsheet, month
            )
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

        years_requiring_structure_refresh = {
            audit.year
            for audit in audits
            if audit.year != "single"
            and (audit.created_spreadsheet or audit.created_worksheet)
        }
        for year in sorted(years_requiring_structure_refresh):
            spreadsheet, _ = self._get_or_create_year_spreadsheet(year)
            self.refresh_year_summary(spreadsheet)

        return audits

    def refresh_year_summary(self, spreadsheet) -> None:
        month_worksheets = self._month_worksheets(spreadsheet)
        summary = self._get_or_create_summary_worksheet(spreadsheet)
        for month_worksheet in month_worksheets:
            self._apply_dropdowns(spreadsheet, month_worksheet)
            self._apply_transaction_table_format(month_worksheet, spreadsheet=spreadsheet)
        self._write_year_summary(summary, spreadsheet)

    def refresh_existing_workbook_structures(self) -> list[WorkbookRefreshAudit]:
        """Refresh existing year workbooks without changing transaction amounts or rows."""
        if self.config.drive_folder_id:
            spreadsheets = [
                self.client.open_by_key(spreadsheet_id)
                for spreadsheet_id in self._list_spreadsheets_in_folder()
            ]
        elif self.spreadsheet is not None:
            spreadsheets = [self.spreadsheet]
        else:
            return []

        audits = []
        for spreadsheet in spreadsheets:
            title = str(getattr(spreadsheet, "title", "")).strip()
            if self.config.drive_folder_id and not re.fullmatch(r"\d{4}", title):
                continue
            month_worksheets = self._month_worksheets(spreadsheet)
            if not month_worksheets:
                continue

            migrated = sum(
                self._migrate_category_labels(worksheet)
                for worksheet in month_worksheets
            )
            self.refresh_year_summary(spreadsheet)
            summary = self._get_or_create_summary_worksheet(spreadsheet)
            audits.append(
                WorkbookRefreshAudit(
                    year=title or "Workbook",
                    spreadsheet_id=spreadsheet.id,
                    summary_worksheet_id=summary.id,
                    month_tabs=len(month_worksheets),
                    migrated_categories=migrated,
                )
            )
        return audits

    def _write_statement_issues(self, spreadsheet, month_worksheets) -> None:
        from financial_statement import statement_issues, style_review_sheet

        year = _summary_year_label(spreadsheet.title)
        matrix = [["Month", "Ledger Row", "Date", "Category", "Issue"]]
        for month in month_worksheets:
            values = month.get_all_values()
            if not values:
                continue
            # Keep blank/invalid-date rows so the audit retains actual ledger row numbers.
            records = [
                {header: value for header, value in zip(values[0], row) if header}
                for row in values[1:]
            ]
            matrix.extend(
                [month.title, *issue]
                for issue in statement_issues(records, year, _month_number_from_title(month.title))
            )
        try:
            audit = spreadsheet.worksheet(STATEMENT_ISSUES_WORKSHEET_TITLE)
        except gspread.WorksheetNotFound:
            audit = spreadsheet.add_worksheet(
                title=STATEMENT_ISSUES_WORKSHEET_TITLE, rows=100, cols=8
            )
        old = audit.get_all_values()
        if old and any(old[0]) and old[0][:5] != matrix[0]:
            raise ValueError("Statement Issues has an unrecognized header; preserve it before migration.")
        old_height = 0
        if old and len(old[0]) >= 8 and old[0][6] == "Generated rows":
            old_height = int(old[0][7])
            if not 1 <= old_height <= audit.row_count:
                raise ValueError("Statement Issues has an invalid generated footprint.")
        elif old and any(any(row[6:8]) for row in old):
            raise ValueError("Statement Issues G:H contains notes; relocate them before migration.")
        _ensure_min_columns(audit, 8)
        _ensure_min_rows(audit, len(matrix))
        audit.update(f"A1:E{len(matrix)}", matrix, value_input_option="RAW")
        if old_height > len(matrix):
            audit.batch_clear([f"A{len(matrix) + 1}:E{old_height}"])
        audit.update("G1:H1", [["Generated rows", len(matrix)]], value_input_option="RAW")
        style_review_sheet(audit, spreadsheet, [120, 90, 120, 260, 560])

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
        """Deprecated: preserve ledger values, blanks, formulas and classifications."""
        return 0

    def _month_worksheets(self, spreadsheet) -> list:
        year = _summary_year_label(getattr(spreadsheet, "title", ""))
        months = {}
        for worksheet in spreadsheet.worksheets():
            month = _month_number_from_title(worksheet.title)
            if month is None:
                continue
            parts = worksheet.title.strip().split()
            if len(parts) == 2 and year != "Year" and parts[1] != year:
                continue
            if month in months:
                raise ValueError(
                    f"Duplicate month tabs: {months[month].title!r} and {worksheet.title!r}."
                )
            months[month] = worksheet
        return [months[month] for month in sorted(months)]

    def _ensure_statement_review(self, spreadsheet, month_worksheets, year: str) -> dict[str, int]:
        from financial_statement import style_review_sheet
        try:
            review = spreadsheet.worksheet(STATEMENT_REVIEW_WORKSHEET_TITLE)
        except gspread.WorksheetNotFound:
            review = spreadsheet.add_worksheet(
                title=STATEMENT_REVIEW_WORKSHEET_TITLE, rows=100, cols=5
            )
        _ensure_min_columns(review, 5)
        values = review.get_all_values()
        header = values[0] if values else []
        if header[:2] not in ([], ["Month", "Salary Status"]):
            raise ValueError("Statement Review must have headers Month and Salary Status.")
        first_migration = len(header) < 5 or not str(header[4]).strip()
        cutoff = datetime.now().date().replace(day=1)
        if first_migration:
            if any(header[3:5]):
                raise ValueError("Statement Review D1:E1 contains metadata; inspect before migration.")
            review.update("A1:B1", [["Month", "Salary Status"]], value_input_option="RAW")
            review.update(
                "D1:E1", [["Migration cutoff", cutoff.isoformat()]], value_input_option="RAW"
            )
        status_rows = {}
        for row_number, row in enumerate(values[1:], start=2):
            if row and str(row[0]).strip():
                title = str(row[0]).strip()
                if title in status_rows:
                    raise ValueError(f"Duplicate Statement Review month: {title!r}.")
                status_rows[title] = row_number
        next_row = max(2, len(values) + 1)
        additions = []
        for month in month_worksheets:
            if month.title in status_rows:
                continue
            parts = month.title.strip().split()
            month_year = parts[1] if len(parts) == 2 else year
            historical = (
                first_migration
                and month_year.isdigit()
                and (int(month_year), _month_number_from_title(month.title))
                < (cutoff.year, cutoff.month)
            )
            status_rows[month.title] = next_row + len(additions)
            additions.append([month.title, "Historical zero" if historical else "Pending"])
        if additions:
            _ensure_min_rows(review, next_row + len(additions) - 1)
            review.update(
                f"A{next_row}:B{next_row + len(additions) - 1}",
                additions, value_input_option="RAW",
            )
        request = _dropdown_request(
            worksheet_id=review.id, column_index=1, options=SALARY_STATUS_OPTIONS,
            message="Pending requires salary review; choose a zero status only after review.",
        )
        request["setDataValidation"]["rule"]["strict"] = True
        spreadsheet.batch_update({"requests": [request]})
        style_review_sheet(review, spreadsheet, [140, 170, 350, 170, 140])
        return {month.title: status_rows[month.title] for month in month_worksheets}

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

    def _write_year_summary(self, worksheet, spreadsheet) -> None:
        from financial_statement import style_statement

        month_worksheets = self._month_worksheets(spreadsheet)
        year = _summary_year_label(spreadsheet.title)
        matrix, layout = _summary_matrix_rows(month_worksheets, year)
        old_values = worksheet.get_all_values()
        old_height, old_width = _generated_summary_footprint(old_values, matrix, layout)
        matrix_width = len(matrix[0])
        matrix_end_col = _column_letter(matrix_width)
        matrix_end_row = len(matrix)
        for row_index, row in enumerate(old_values[:matrix_end_row]):
            for col_index, value in enumerate(row[:matrix_width]):
                if value != "" and (row_index >= old_height or col_index >= old_width):
                    raise ValueError(
                        "Summary contains notes outside its generated table. Move those notes "
                        "below the new statement before refreshing; nothing was overwritten."
                    )
        _ensure_min_columns(worksheet, matrix_width)
        _ensure_min_rows(worksheet, matrix_end_row)

        # Only the generated footprint is owned here; overlapping notes need a migration snapshot.
        worksheet.update(
            f"A1:{matrix_end_col}{matrix_end_row}", matrix,
            value_input_option="USER_ENTERED",
        )
        worksheet.update(
            f"A1:{matrix_end_col}1", [matrix[0]], value_input_option="RAW",
        )
        clear_ranges = []
        if old_height > matrix_end_row:
            clear_ranges.append(
                f"A{matrix_end_row + 1}:{_column_letter(old_width)}{old_height}"
            )
        if old_width > matrix_width:
            clear_ranges.append(
                f"{_column_letter(matrix_width + 1)}1:"
                f"{_column_letter(old_width)}{min(old_height, matrix_end_row)}"
            )
        if clear_ranges:
            worksheet.batch_clear(clear_ranges)
        _remove_statement_deficit_rules(worksheet, spreadsheet, layout)
        style_statement(worksheet, spreadsheet, matrix, layout)

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
        _validate_append_transactions(dataframe)
        attempted = len(dataframe)
        existing_transaction_keys = _duplicate_keys_from_worksheet(worksheet)
        to_append = dataframe.copy()
        skipped = 0

        if skip_duplicates:
            incoming_keys = to_append.apply(_transaction_duplicate_key, axis=1)
            allocation_keys = to_append.apply(_allocation_collision_key, axis=1)
            duplicate_in_upload = (
                (incoming_keys.duplicated(keep="first") & incoming_keys.ne(""))
                | (allocation_keys.duplicated(keep="first") & allocation_keys.ne(""))
            )
            duplicate_override = to_append.get("duplicate_override", False)
            if not isinstance(duplicate_override, pd.Series):
                duplicate_override = pd.Series(False, index=to_append.index)
            duplicate_override = duplicate_override.apply(normalize_bool)
            is_new = duplicate_override | (
                ~incoming_keys.isin(existing_transaction_keys)
                & ~allocation_keys.isin(existing_transaction_keys)
                & ~duplicate_in_upload
            )
            skipped = int((~is_new).sum())
            to_append = to_append[is_new].copy()

        if not to_append.empty:
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
            worksheet_id=worksheet.id,
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


def worksheet_url(spreadsheet_id: str, worksheet_id: int) -> str:
    """Build a direct Google Sheets URL for one worksheet tab."""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={worksheet_id}"


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
        allocation_key = _allocation_collision_key(row)
        if allocation_key:
            keys.add(allocation_key)
    return keys


def _transaction_duplicate_key(row: dict | pd.Series) -> str:
    date = str(row.get("date", "")).strip()
    description = " ".join(str(row.get("description", "")).upper().split())
    source = str(row.get("source", "")).strip().upper()
    currency = str(row.get("currency", "SGD") or "SGD").strip().upper()
    flow = str(row.get("money_flow", "")).strip().lower()
    amount = abs(normalize_amount(row.get("amount", 0)))
    reference = str(row.get("transaction_reference", "")).strip().upper()
    if not date or not description:
        return ""
    key = "|".join([date, source, description, flow, f"{amount:.2f}", currency])
    return f"{key}|REF:{reference}" if reference else key


def _allocation_collision_key(row: dict | pd.Series) -> str:
    category = str(row.get("category", "")).strip()
    category = LEGACY_CATEGORY_ALIASES.get(category, category)
    if (
        category not in ALLOCATION_CATEGORIES
        and str(row.get("transaction_type", "")).strip() != "contribution"
    ):
        return ""
    date = str(row.get("date", "")).strip()
    amount = abs(normalize_amount(row.get("amount", 0)))
    currency = str(row.get("currency", "SGD") or "SGD").strip().upper()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or amount <= 0:
        return ""
    return f"allocation|{date[:7]}|{amount:.2f}|{currency}"


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
    headers = headers if headers is not None else _worksheet_headers(worksheet)
    gross = _monthly_gross_spend_formula(worksheet, headers=headers).removeprefix("=")
    offsets = "+".join(
        _monthly_category_formula(worksheet, category, headers=headers).removeprefix("=")
        for category in OFFSET_CATEGORIES
    )
    return f"={gross}-({offsets})"


def _monthly_category_formula(
    worksheet, category: str, *, headers: list[str] | None = None, year=None,
) -> str:
    from financial_statement import statement_category_formula

    return statement_category_formula(worksheet, category, headers=headers, year=year)


def _monthly_gross_spend_formula(worksheet, *, headers: list[str] | None = None) -> str:
    headers = headers if headers is not None else _worksheet_headers(worksheet)
    terms = [
        _monthly_category_formula(worksheet, category, headers=headers).removeprefix("=")
        for category in VARIABLE_EXPENSE_CATEGORIES + FIXED_EXPENSE_CATEGORIES
    ]
    return "=" + "+".join(terms)


def _summary_matrix_rows(
    month_worksheets: list, year: str, *, salary_status_rows: dict[str, int] | None = None,
) -> tuple[list[list[str]], dict[str, int | list[int]]]:
    from financial_statement import statement_matrix

    return statement_matrix(month_worksheets, year, salary_status_rows=salary_status_rows)


def _generated_summary_footprint(values, matrix, layout) -> tuple[int, int]:
    if not values or not values[0] or "Year Total" not in values[0]:
        return 0, 0
    width = values[0].index("Year Total") + 1
    if values[0][0] == matrix[0][0] and len(values) > 1 and values[1][:1] == ["INCOME"]:
        final_label = matrix[layout["final"] - 1][0]
        for row_number, row in enumerate(values[:len(matrix)], start=1):
            if row and row[0] == final_label:
                return row_number, width
        return 0, 0
    if values[0][0] == "Category":
        # Legacy generated matrices ended at row 27; notes below are not ours.
        for row_number, row in enumerate(values[2:27], start=3):
            if row and row[0] == "Net Spend (a + b + c)":
                return row_number, width
        return min(27, len(values)), width
    if values[0][0] == matrix[0][0]:
        final_label = matrix[layout["final"] - 1][0]
        for row_number, row in enumerate(values[:len(matrix)], start=1):
            if row and row[0] == final_label:
                return row_number, width
    return 0, 0


def _remove_statement_deficit_rules(worksheet, spreadsheet, layout) -> None:
    metadata = spreadsheet.fetch_sheet_metadata(
        params={"fields": "sheets(properties(sheetId),conditionalFormats)"}
    )
    expected = {
        "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
        "format": {
            "backgroundColor": {"red": 1, "green": .9, "blue": .9},
            "textFormat": {
                "foregroundColor": {"red": .7, "green": .08, "blue": .08}, "bold": True,
            },
        },
    }
    requests = []
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != worksheet.id:
            continue
        for index, rule in enumerate(sheet.get("conditionalFormats", [])):
            ranges = rule.get("ranges", [])
            rows = {layout["operating"], layout["final"]}
            actual = rule.get("booleanRule", {})
            actual_format = actual.get("format", {})
            expected_format = expected["format"]
            # Sheets quantizes RGB to 8-bit and adds ColorStyle mirrors on readback.
            colors_match = all(
                abs(actual_color.get(channel, 0) - expected_color.get(channel, 0)) <= 1 / 255 + 1e-6
                for actual_color, expected_color in (
                    (actual_format.get("backgroundColor", {}), expected_format["backgroundColor"]),
                    (actual_format.get("textFormat", {}).get("foregroundColor", {}), expected_format["textFormat"]["foregroundColor"]),
                )
                for channel in ("red", "green", "blue")
            )
            if (actual.get("condition") == expected["condition"] and colors_match
                    and actual_format.get("textFormat", {}).get("bold") is True and len(ranges) == 2
                    and {area.get("endRowIndex") for area in ranges} == rows
                    and all(area.get("sheetId") == worksheet.id
                            and area.get("startRowIndex") == area.get("endRowIndex") - 1
                            and area.get("startColumnIndex") == 1
                            and area.get("endColumnIndex", 0) >= 2 for area in ranges)):
                requests.append({"deleteConditionalFormatRule": {"sheetId": worksheet.id, "index": index}})
    if requests:
        spreadsheet.batch_update({"requests": list(reversed(requests))})

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


def _ensure_min_rows(worksheet, required_rows: int) -> None:
    if worksheet.row_count < required_rows:
        worksheet.add_rows(required_rows - worksheet.row_count)


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
    validation_end = len(CATEGORY_OPTIONS) + len(LEGACY_CATEGORY_ALIASES) + 1
    range_formula = (
        f"=INDIRECT(\"'{title}'!\"&IF(${flow_column}2=\"inflow\","
        f"\"$B$2:$B${validation_end}\","
        f"IF(${flow_column}2=\"outflow\","
        f"\"$A$2:$A${validation_end}\",\"$C$2\")))"
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
        f'=OR(${amount_column}2="",AND(ISNUMBER(${amount_column}2),OR('
        f'AND(${flow_column}2="inflow",${amount_column}2>0),'
        f'AND(${flow_column}2="outflow",${amount_column}2<0),'
        f'${flow_column}2="neutral")))'
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
                "inputMessage": "Numeric amounts: inflow positive, outflow negative; neutral transfers are excluded.",
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
                    "Inflow: income or expense offsets. Outflow: spending or confirmed "
                    "contributions. Neutral: Transfer."
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
    return [record for _, record in _transaction_records_with_rows(worksheet)]


def _transaction_records_with_rows(worksheet) -> list[tuple[int, dict[str, str]]]:
    headers = _worksheet_headers(worksheet)
    if not headers:
        return []

    date_index = headers.index("date") if "date" in headers else 0
    end_col = _column_letter(len(headers))
    values = worksheet.get(f"A1:{end_col}")
    if len(values) <= 1:
        return []

    records = []
    for row_number, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        if not str(padded[date_index]).strip():
            continue
        records.append((row_number, dict(zip(headers, padded[: len(headers)]))))
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


def _validate_append_transactions(dataframe: pd.DataFrame) -> None:
    for index, row in dataframe.iterrows():
        raw_amount = row.get("amount", "")
        missing = pd.isna(raw_amount) or str(raw_amount).strip() == ""
        amount, parse_error = normalize_amount_with_error(raw_amount)
        if (missing or parse_error or not math.isfinite(amount) or normalize_bool(row.get("amount_missing", False))
                or normalize_bool(row.get("amount_parse_error", False))):
            raise ValueError(f"Row {index}: a valid, explicit amount is required.")
        category = str(row.get("category", "")).strip()
        category = LEGACY_CATEGORY_ALIASES.get(category, category)
        if str(row.get("currency", "")).strip().upper() != "SGD":
            raise ValueError(f"Row {index}: a verified SGD amount is required; no exchange rate is assumed.")
        flow = str(row.get("money_flow", "")).strip().lower()
        kind = str(row.get("transaction_type", "")).strip().lower()
        if flow == "neutral" and category == "Transfer":
            continue
        spending = VARIABLE_EXPENSE_CATEGORIES + FIXED_EXPENSE_CATEGORIES
        if category in INCOME_CATEGORIES + OFFSET_CATEGORIES:
            valid = flow == "inflow" and amount > 0
        elif category in spending + ALLOCATION_CATEGORIES:
            valid = flow == "outflow" and amount < 0
        else:
            valid = False
        if not valid:
            raise ValueError(f"Row {index}: unsupported category/flow or amount sign.")
        if category in ALLOCATION_CATEGORIES and kind != "contribution":
            raise ValueError(f"Row {index}: allocations require transaction_type='contribution'.")


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
    _validate_append_transactions(dataframe)
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
