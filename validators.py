from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

import pandas as pd


INCOME_CATEGORIES = [
    "Net Salary",
    "Bonus / AVC / Other Employment Income",
    "Carousell Sales",
    "Prize Awards/Government Vouchers",
    "Gifts received",
]

VARIABLE_EXPENSE_CATEGORIES = [
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
    "Admin & Fees",
    "Others",
]

FIXED_EXPENSE_CATEGORIES = [
    "Parent Allowance",
    "Insurance",
    "Subscriptions",
    "Income Tax",
    "Bills / Recurring Commitments",
]

ALLOCATION_CATEGORIES = [
    "ETF Contributions",
    "Equity Contributions",
    "Crypto Contributions",
    "Commodities Contributions",
    "Dedicated Cash Savings",
    "Other Investment Contributions",
]

OFFSET_CATEGORIES = ["Reimbursements", "Cashbacks & Refunds"]
# Retain ambiguous legacy classifications without guessing their economic meaning.
REVIEW_CATEGORIES = ["GVs & Prize Award", "Income", "Funding", "Investments", "Family"]
INFLOW_CATEGORY_OPTIONS = INCOME_CATEGORIES + OFFSET_CATEGORIES + REVIEW_CATEGORIES[:3]
OUTFLOW_CATEGORY_OPTIONS = VARIABLE_EXPENSE_CATEGORIES + FIXED_EXPENSE_CATEGORIES + ALLOCATION_CATEGORIES
CATEGORY_OPTIONS = INCOME_CATEGORIES + VARIABLE_EXPENSE_CATEGORIES + FIXED_EXPENSE_CATEGORIES + OFFSET_CATEGORIES + ALLOCATION_CATEGORIES + ["Transfer"] + REVIEW_CATEGORIES

CATEGORY_MIGRATIONS = {
    "Groceries": "Others",
    "Gifts & Charity": "Gifts",
    "Auto & Parking": "Others",
    "Business": "Others",
    "Fuel": "Others",
    "Kids": "Others",
    "Loans": "Others",
    "Pets": "Others",
    "Cash Withdrawal": "Others",
    "Rental": "Others",
    "Cash & Cheque": "Others",
    "Taxes": "Income Tax",
    "Refund": "Cashbacks & Refunds",
    "Cashbacks": "Cashbacks & Refunds",
    "Fees": "Admin & Fees",
    "Reimbursement": "Reimbursements",
    "Bills": "Bills / Recurring Commitments",
    "Prize Awards / Other Cash Income": "Prize Awards/Government Vouchers",
    "Equity / ETF Contributions": "ETF Contributions",
    "Money Market Fund Contributions": "Other Investment Contributions",
}


SHEET_COLUMNS = [
    "check",
    "date",
    "source",
    "description",
    "amount_original",
    "amount_parse_error",
    "amount_missing",
    "allocation_confirmed",
    "amount",
    "currency",
    "transaction_reference",
    "money_flow",
    "transaction_type",
    "category",
    "category_confidence",
    "category_reason",
    "reimbursement_candidate",
    "reimbursement_type",
    "reimbursement_confidence",
    "reimbursement_reason",
    "linked_expense_hint",
    "reimbursement_for",
    "reimbursement_for_category",
    "anomaly_flag",
    "anomaly_severity",
    "anomaly_reason",
    "insight_note",
    "status",
    "include_in_append",
    "duplicate_override",
    "ignore_reason",
    "review_note",
    "confidence",
    "image_filename",
    "archive_path",
    "screenshot_id",
    "image_region",
    "extraction_confidence",
    "matched_workbook_id",
    "matched_worksheet",
    "matched_worksheet_id",
    "matched_row",
    "matched_sheet_data",
    "decision_source",
    "workflow_state",
    "transaction_hash",
    "raw_text",
]

# The application keeps the full schema for extraction, review, and local memory.
# Google Sheets is intentionally a concise, user-facing ledger. Keep category
# beside source so the two most common manual corrections sit before description.
GOOGLE_SHEET_COLUMNS = [
    "check",
    "date",
    "source",
    "category",
    "description",
    "amount_original",
    "amount_parse_error",
    "amount",
    "currency",
    "transaction_reference",
    "money_flow",
    "transaction_type",
]

# Income is not an expense offset. Reporting uses positive offsets and subtracts D.
EXPENSE_OFFSET_INFLOW_CATEGORIES = set(OFFSET_CATEGORIES)


def parse_iso_date(value: str) -> str:
    value = str(value).strip()
    if not value:
        return ""

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass

    # Keep model output visible to the user if it could not be normalized.
    return value


def normalize_amount(value: object) -> float:
    amount, _ = normalize_amount_with_error(value)
    return amount


def normalize_amount_with_error(value: object) -> tuple[float, bool]:
    if value is None:
        return 0.0, False
    text = str(value).strip().replace("$", "").replace(",", "")
    try:
        number = Decimal(text)
        return (float(number), False) if number.is_finite() else (0.0, True)
    except (InvalidOperation, ValueError):
        return 0.0, bool(str(value).strip())


def build_transaction_hash(
    *,
    transaction_date: str,
    source: str,
    description: str,
    amount: object,
    currency: str,
    money_flow: str = "",
    transaction_time: str = "",
    transaction_reference: str = "",
) -> str:
    basis = "|".join(
        [
            parse_iso_date(transaction_date),
            str(transaction_time).strip(),
            source.strip().upper(),
            " ".join(description.upper().split()),
            str(money_flow).strip().lower(),
            f"{abs(normalize_amount(amount)):.2f}",
            currency.strip().upper() or "SGD",
            str(transaction_reference).strip().upper(),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def rows_to_dataframe(rows: Iterable[dict]) -> pd.DataFrame:
    normalized = []
    for row in rows:
        item = {column: row.get(column, "") for column in SHEET_COLUMNS}
        item["check"] = str(item["check"] or "No").strip() or "No"
        item["date"] = parse_iso_date(item["date"])
        item["amount_missing"] = normalize_bool(item["amount_missing"]) or item["amount"] is None or str(item["amount"]).strip() == ""
        item["allocation_confirmed"] = normalize_bool(item["allocation_confirmed"])
        item["amount_original"] = str(
            item["amount_original"] if item["amount_original"] != "" else item["amount"]
        )
        item["amount"], amount_parse_error = normalize_amount_with_error(item["amount"])
        item["amount_parse_error"] = amount_parse_error or normalize_bool(item["amount_parse_error"])
        item["currency"] = str(item["currency"] or "SGD").upper()
        item["category"] = normalize_category(item["category"])
        item["reimbursement_candidate"] = normalize_bool(item["reimbursement_candidate"])
        item["anomaly_flag"] = normalize_bool(item["anomaly_flag"])
        item["include_in_append"] = normalize_bool(
            item["include_in_append"] if item["include_in_append"] != "" else True
        )
        item["duplicate_override"] = normalize_bool(item["duplicate_override"])
        item["category_confidence"] = normalize_float(item["category_confidence"])
        item["reimbursement_confidence"] = normalize_float(item["reimbursement_confidence"])
        item["confidence"] = normalize_float(item["confidence"])
        item["transaction_hash"] = build_transaction_hash(
            transaction_date=item["date"],
            source=item["source"],
            description=item["description"],
            amount=item["amount"],
            currency=item["currency"],
            money_flow=item["money_flow"],
            transaction_reference=item["transaction_reference"],
        )
        normalized.append(item)

    return pd.DataFrame(normalized, columns=SHEET_COLUMNS)


def normalize_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y"}


def normalize_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_category(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "Others"

    lookup = {category.upper(): category for category in CATEGORY_OPTIONS}
    migrated = {source.upper(): target for source, target in CATEGORY_MIGRATIONS.items()}
    return migrated.get(text.upper(), lookup.get(text.upper(), "Others"))


def today_iso() -> str:
    return date.today().isoformat()
