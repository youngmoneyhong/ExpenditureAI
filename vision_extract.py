from __future__ import annotations

import base64
import mimetypes
import os
from enum import Enum
from pathlib import Path
from openai import OpenAI
from pydantic import BaseModel, Field

from validators import build_transaction_hash, parse_iso_date


class Source(str, Enum):
    UOB_TMRW = "UOB_TMRW"
    DBS_PAYLAH = "DBS_PAYLAH"
    DBS_BANK = "DBS_BANK"
    UNKNOWN = "UNKNOWN"


class TransactionType(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    REFUND = "refund"
    UNKNOWN = "unknown"


class MoneyFlow(str, Enum):
    OUTFLOW = "outflow"
    INFLOW = "inflow"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class ReimbursementType(str, Enum):
    FRIEND_REPAYMENT = "friend_repayment"
    MERCHANT_REFUND = "merchant_refund"
    CASHBACK = "cashback"
    SALARY = "salary"
    SELF_TRANSFER = "self_transfer"
    UNKNOWN = "unknown"


class ExtractedTransaction(BaseModel):
    date: str = Field(description="Transaction date in ISO format YYYY-MM-DD when possible.")
    source: Source = Field(description="Detected source app or account.")
    description: str = Field(description="Merchant, recipient, sender, or transaction description.")
    amount: float = Field(description="Negative for spend/outflow, positive for income/refund/inflow.")
    currency: str = Field(description="Currency code, usually SGD.")
    transaction_time: str = Field(description="Visible transaction time. Use an empty string if unavailable.")
    transaction_reference: str = Field(description="Visible transaction ID/reference. Use an empty string if unavailable.")
    money_flow: MoneyFlow = Field(description="Direction of money movement from the user's perspective.")
    transaction_type: TransactionType = Field(description="Best classification of the transaction direction/type.")
    category: str = Field(description="Optional spending category inferred from description. Use an empty string if unknown.")
    reimbursement_candidate: bool = Field(
        description=(
            "True if this appears to be a friend paying the user back for a shared/group bill, "
            "split expense, meal, ride, tickets, booking, or other fronted payment."
        )
    )
    reimbursement_type: ReimbursementType = Field(
        description="Classify incoming/reversal transactions so friend repayments are not mixed with refunds, salary, cashback, or self-transfers."
    )
    reimbursement_confidence: float = Field(
        ge=0,
        le=1,
        description="Confidence that the reimbursement classification is correct. Use 0 if not applicable.",
    )
    reimbursement_reason: str = Field(
        description="Short reason for the reimbursement flag. Use an empty string if not a reimbursement candidate."
    )
    linked_expense_hint: str = Field(
        description="If likely linked to a previous shared expense, provide a short hint like dinner, Grab, hotel, ticket, or merchant. Use an empty string if unknown."
    )
    confidence: float = Field(ge=0, le=1, description="Extraction confidence from 0 to 1.")
    raw_text: str = Field(description="Relevant OCR text used for this transaction. Use an empty string if unavailable.")


class TransactionExtraction(BaseModel):
    detected_source: Source
    statement_period: str = Field(description="Visible statement period or date range. Use an empty string if unavailable.")
    transactions: list[ExtractedTransaction]
    warnings: list[str] = Field(description="Extraction warnings. Use an empty list if there are no warnings.")


SYSTEM_PROMPT = """You extract personal finance transactions from Singapore banking app screenshots.

Return only transactions visible in the image. Do not invent missing rows.

Rules:
- Use ISO dates as YYYY-MM-DD. If the year is missing, infer the most likely year from visible context or today's date supplied by the user.
- UOB credit card purchases are expenses and must have negative amounts.
- DBS PayLah outgoing payments/transfers are negative. Incoming receipts/refunds are positive.
- Set money_flow from the user's perspective: outflow for spending/payments, inflow for money received/refunds, neutral only for non-money movements.
- Use DBS_PAYLAH only if the screenshot clearly shows PayLah branding, PayLah wallet, mobile number/name transfers, QR payments, or PayLah transaction language.
- Use DBS_BANK only for normal DBS/POSB deposit, savings, current, card, or account transactions.
- Use UOB_TMRW only for UOB TMRW app, UOB account, or UOB card transaction screens.
- Ignore UOB_TMRW transaction rows whose description starts with PAYMT THRU E-BANK.
- If source is unclear, use UNKNOWN and add a warning.
- Flag reimbursement_candidate=true only when the row is an incoming inflow from an individual and the description/reference suggests repayment, splitting, shared expense, food, ride, booking, tickets, hotel, cab, Grab, dinner, lunch, drinks, or settlement.
- Do not flag all incoming PayLah transfers as reimbursements. If it is only a name with no reference, set reimbursement_candidate=false unless there is strong context.
- Do not mark salary, bank interest, cashback, merchant refunds, card reversals, self-transfers, or wallet top-ups as friend reimbursements.
- Preserve merchant or recipient names in the description.
- Ignore available balance, credit limit, reward points, navigation labels, and totals unless they are clearly a transaction row.
- If unsure about a row, include it with a lower confidence and add a warning.
"""


def _image_to_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def extract_transactions_from_image(
    image_path: str | Path,
    *,
    model: str | None = None,
    today: str,
) -> TransactionExtraction:
    path = Path(image_path)
    client = OpenAI()
    model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    response = client.responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"Today's date is {today}. Extract all visible UOB TMRW, "
                            "UOB credit card, DBS PayLah, or DBS banking transactions "
                            f"from this screenshot named {path.name}."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": _image_to_data_url(path),
                    },
                ],
            },
        ],
        text_format=TransactionExtraction,
    )

    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("OpenAI returned no parsed transaction data.")

    return parsed


def extraction_to_rows(
    extraction: TransactionExtraction,
    *,
    filename: str,
    archive_path: str = "",
) -> list[dict]:
    rows: list[dict] = []
    for transaction in extraction.transactions:
        date_value = parse_iso_date(transaction.date)
        row = {
            "date": date_value,
            "source": transaction.source.value,
            "description": transaction.description,
            "amount_original": transaction.amount,
            "amount_parse_error": False,
            "amount": transaction.amount,
            "currency": transaction.currency or "SGD",
            "transaction_time": transaction.transaction_time,
            "transaction_reference": transaction.transaction_reference,
            "money_flow": transaction.money_flow.value,
            "transaction_type": transaction.transaction_type.value,
            "category": transaction.category,
            "reimbursement_candidate": transaction.reimbursement_candidate,
            "reimbursement_type": transaction.reimbursement_type.value,
            "reimbursement_confidence": transaction.reimbursement_confidence,
            "reimbursement_reason": transaction.reimbursement_reason,
            "linked_expense_hint": transaction.linked_expense_hint,
            "confidence": transaction.confidence,
            "image_filename": filename,
            "archive_path": archive_path,
            "raw_text": transaction.raw_text,
        }
        row["transaction_hash"] = build_transaction_hash(
            transaction_date=row["date"],
            source=row["source"],
            description=row["description"],
            amount=row["amount"],
            currency=row["currency"],
            money_flow=row["money_flow"],
            transaction_time=row["transaction_time"],
            transaction_reference=row["transaction_reference"],
        )
        rows.append(row)
    return rows
