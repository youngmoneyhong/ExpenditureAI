from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from enum import Enum

import pandas as pd
from openai import OpenAI
from pydantic import BaseModel, Field

from validators import CATEGORY_OPTIONS, normalize_bool


class Category(str, Enum):
    FOOD = "Food"
    TAXI = "Taxi"
    PUBLIC_TRANSPORT = "Public Transport"
    SHOPPING = "Shopping"
    GIFTS = "Gifts"
    ENTERTAINMENT = "Entertainment"
    OTHERS = "Others"
    BILLS = "Bills"
    EDUCATION = "Education"
    ADMIN_AND_FEES = "Admin & Fees"
    HEALTH = "Health"
    INSURANCE = "Insurance"
    PERSONAL_CARE = "Personal Care"
    SUBSCRIPTIONS = "Subscriptions"
    INCOME_TAX = "Income Tax"
    TRAVEL = "Travel"
    TRANSFER = "Transfer"
    CAROUSELL_SALES = "Carousell Sales"
    CASHBACKS_AND_REFUNDS = "Cashbacks & Refunds"
    REIMBURSEMENT = "Reimbursement"
    GVS_AND_PRIZE_AWARD = "GVs & Prize Award"


class AnomalySeverity(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CategoryDecision(BaseModel):
    transaction_hash: str
    category: Category
    confidence: float = Field(ge=0, le=1)
    reason: str


class CategoryAgentOutput(BaseModel):
    decisions: list[CategoryDecision]


class AnomalyDecision(BaseModel):
    transaction_hash: str
    anomaly_flag: bool
    severity: AnomalySeverity
    reason: str


class AnomalyAgentOutput(BaseModel):
    decisions: list[AnomalyDecision]


class InsightDecision(BaseModel):
    transaction_hash: str
    note: str


class InsightAgentOutput(BaseModel):
    overview: str
    notes: list[InsightDecision]


class EnrichmentOutput(BaseModel):
    category_decisions: list[CategoryDecision] = Field(default_factory=list)
    anomaly_decisions: list[AnomalyDecision] = Field(default_factory=list)
    insight_overview: str = ""
    insight_notes: list[InsightDecision] = Field(default_factory=list)


CATEGORY_PROMPT = f"""You are a category agent for a Singapore personal expense tracker.

Classify each transaction into exactly one allowed category:
{", ".join(CATEGORY_OPTIONS)}

Rules and examples:
- Inflow transactions can only use Reimbursement, Carousell Sales, Cashbacks & Refunds, or GVs & Prize Award.
- Outflow transactions should use spending categories such as Food, Taxi, Public Transport, Shopping, Bills, Subscriptions, Income Tax, Travel, Others, etc.
- FAIRPRICE, NTUC, COLD STORAGE, SHENG SIONG -> Groceries
- Restaurants, cafes, bars, hawker food, GRABFOOD, FOODPANDA, DELIVEROO -> Food
- GRAB rides, GOJEK, CDG, taxi -> Taxi
- MRT, BUS, SimplyGo, TransitLink -> Public Transport
- DBS PAYLAH personal transfers, wallet top-ups, self transfers -> Transfer
- NETFLIX, SPOTIFY, APPLE.COM subscriptions, GOOGLE storage -> Subscriptions
- Incoming friend payback for a shared bill -> Reimbursement
- Carousell buyer payments or marketplace sale proceeds -> Carousell Sales
- Merchant reversal/refund -> Cashbacks & Refunds
- Gift voucher, prize, or award received -> GVs & Prize Award
- If unsure, use Others with low confidence.
"""


ANOMALY_PROMPT = """You are an anomaly detection agent for a personal expense tracker.

Flag only rows that deserve human attention. Look for:
- unusually large amount compared with the uploaded batch
- positive/negative sign that conflicts with money_flow or transaction_type
- zero amount
- unknown source
- category that appears inconsistent with description
- likely duplicate within the batch
- possible reimbursement/refund that may offset another expense

Do not flag routine groceries, food, transport, or subscriptions unless something looks off.
"""


INSIGHT_PROMPT = """You are an insight agent for a personal expense tracker.

Give short, practical observations for this uploaded batch. Also attach short per-row notes only when useful.
Focus on spending patterns, reimbursements, large items, subscriptions, and things the user should review.
"""


def enrich_transactions(
    dataframe: pd.DataFrame,
    *,
    model: str | None = None,
    run_category_agent: bool = True,
    run_anomaly_agent: bool = True,
    run_insight_agent: bool = True,
    category_memory: list[dict] | None = None,
) -> tuple[pd.DataFrame, str]:
    if dataframe.empty:
        return dataframe, ""

    model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    enriched = dataframe.copy()
    category_memory = category_memory or []
    if run_category_agent and category_memory:
        _apply_category_memory(enriched, category_memory)

    try:
        output = _run_agent(
            model=model,
            system_prompt=_enrichment_prompt(
                category_memory=category_memory,
                run_category_agent=run_category_agent,
                run_anomaly_agent=run_anomaly_agent,
                run_insight_agent=run_insight_agent,
            ),
            rows=_compact_rows(enriched),
            output_type=EnrichmentOutput,
        )
        insight_overview = output.insight_overview if run_insight_agent else ""

        if run_category_agent:
            _apply_category_output(
                enriched,
                CategoryAgentOutput(decisions=output.category_decisions),
            )
            if category_memory:
                _apply_category_memory(enriched, category_memory)

        if run_anomaly_agent:
            _apply_anomaly_output(
                enriched,
                AnomalyAgentOutput(decisions=output.anomaly_decisions),
            )

        if run_insight_agent:
            _apply_insight_output(
                enriched,
                InsightAgentOutput(
                    overview=output.insight_overview,
                    notes=output.insight_notes,
                ),
            )
    except Exception as exc:
        insight_overview = f"Enrichment failed: {exc}"
    return enriched, insight_overview


def _enrichment_prompt(
    *,
    category_memory: list[dict],
    run_category_agent: bool,
    run_anomaly_agent: bool,
    run_insight_agent: bool,
) -> str:
    sections = [
        "You are the combined enrichment agent for a Singapore personal expense tracker.",
        "Return only the requested structured output. Leave every disabled section empty.",
    ]
    if run_category_agent:
        sections.append(_category_prompt_with_memory(category_memory))
    if run_anomaly_agent:
        sections.append(ANOMALY_PROMPT)
    if run_insight_agent:
        sections.append(INSIGHT_PROMPT)
    return "\n\n".join(sections)


def _run_agent(model: str, system_prompt: str, rows: list[dict], output_type):
    client = OpenAI()
    response = client.responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Analyze these extracted transaction rows:\n"
                        + json.dumps(rows, ensure_ascii=True),
                    }
                ],
            },
        ],
        text_format=output_type,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("OpenAI returned no parsed enrichment output.")
    return parsed


def _compact_rows(dataframe: pd.DataFrame) -> list[dict]:
    fields = [
        "transaction_hash",
        "date",
        "source",
        "description",
        "amount",
        "currency",
        "money_flow",
        "transaction_type",
        "category",
        "reimbursement_candidate",
        "reimbursement_type",
        "linked_expense_hint",
    ]
    available = [field for field in fields if field in dataframe.columns]
    return dataframe[available].fillna("").to_dict(orient="records")


def _category_prompt_with_memory(category_memory: list[dict]) -> str:
    examples = _top_memory_examples(category_memory)
    if not examples:
        return CATEGORY_PROMPT

    example_lines = [
        f"- {example.get('money_flow', 'unknown')} | {example['description']} -> {example['category']}"
        for example in examples[:40]
    ]
    return (
        CATEGORY_PROMPT
        + "\n\nUser-specific memory from previously saved Google Sheet rows. "
        + "Prefer these patterns over generic examples when they match:\n"
        + "\n".join(example_lines)
    )


def _apply_category_memory(dataframe: pd.DataFrame, category_memory: list[dict]) -> None:
    exact_memory, examples = _build_category_memory(category_memory)
    if not exact_memory and not examples:
        return

    for column in ["category", "category_reason"]:
        if column not in dataframe:
            dataframe[column] = ""
        dataframe[column] = dataframe[column].astype(object)
    if "category_confidence" not in dataframe:
        dataframe["category_confidence"] = 0.0
    dataframe["category_confidence"] = pd.to_numeric(
        dataframe["category_confidence"], errors="coerce"
    ).fillna(0.0).astype(float)

    for index, row in dataframe.iterrows():
        if _should_skip_memory(row):
            continue

        description_key = _memory_key(row.get("description", ""))
        flow = str(row.get("money_flow", "")).strip().lower()
        if not description_key:
            continue

        exact = exact_memory.get(f"{flow}|{description_key}")
        if exact:
            category, count = exact
            dataframe.at[index, "category"] = category
            dataframe.at[index, "category_confidence"] = 0.98 if count >= 2 else 0.92
            dataframe.at[index, "category_reason"] = (
                f"Matched your previous category for this description ({count} saved row(s))."
            )
            continue

        best = _best_fuzzy_memory_match(description_key, flow, examples)
        if best is None:
            continue

        category, score, matched_description = best
        dataframe.at[index, "category"] = category
        dataframe.at[index, "category_confidence"] = round(min(score, 0.93), 2)
        dataframe.at[index, "category_reason"] = (
            f"Matched your previous similar category: {matched_description}."
        )


def _should_skip_memory(row: pd.Series) -> bool:
    if normalize_bool(row.get("reimbursement_candidate", False)):
        return True
    if str(row.get("money_flow", "")).lower() == "neutral":
        return True
    if str(row.get("reimbursement_type", "")).lower() in {
        "cashback",
        "salary",
        "friend_repayment",
        "merchant_refund",
        "self_transfer",
    }:
        return True
    return False


def _build_category_memory(
    category_memory: list[dict],
) -> tuple[dict[str, tuple[str, int]], list[dict[str, str]]]:
    by_key: dict[str, Counter] = defaultdict(Counter)
    display_by_key: dict[str, str] = {}
    for item in category_memory:
        description = str(item.get("description", "")).strip()
        category = str(item.get("category", "")).strip()
        flow = str(item.get("money_flow", "")).strip().lower()
        key = _memory_key(description)
        if not key or category not in CATEGORY_OPTIONS:
            continue
        keyed = f"{flow}|{key}"
        by_key[keyed][category] += 1
        display_by_key.setdefault(keyed, description)

    exact = {}
    examples = []
    for key, counts in by_key.items():
        category, count = counts.most_common(1)[0]
        exact[key] = (category, count)
        examples.append(
            {
                "key": key,
                "money_flow": key.split("|", 1)[0],
                "description_key": key.split("|", 1)[1] if "|" in key else key,
                "description": display_by_key.get(key, key),
                "category": category,
                "count": count,
            }
        )
    examples.sort(key=lambda item: item["count"], reverse=True)
    return exact, examples


def _top_memory_examples(category_memory: list[dict]) -> list[dict[str, str]]:
    _, examples = _build_category_memory(category_memory)
    return examples


def _best_fuzzy_memory_match(
    description_key: str,
    flow: str,
    examples: list[dict[str, str]],
) -> tuple[str, float, str] | None:
    best_score = 0.0
    best_example = None
    for example in examples[:250]:
        if str(example.get("money_flow", "")).strip().lower() != flow:
            continue
        memory_key = str(example.get("description_key") or example["key"])
        if not memory_key:
            continue
        score = SequenceMatcher(None, description_key, memory_key).ratio()
        if (
            len(description_key) >= 6
            and len(memory_key) >= 6
            and (description_key in memory_key or memory_key in description_key)
        ):
            score = max(score, 0.9)
        if score > best_score:
            best_score = score
            best_example = example

    if best_example is None or best_score < 0.88:
        return None
    return (
        str(best_example["category"]),
        float(best_score),
        str(best_example["description"]),
    )


def _memory_key(value: object) -> str:
    text = str(value or "").upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\b(PTE|LTD|PRIVATE|LIMITED|SG|SINGAPORE)\b", " ", text)
    return " ".join(text.split())


def _apply_category_output(dataframe: pd.DataFrame, output: CategoryAgentOutput) -> None:
    by_hash = {decision.transaction_hash: decision for decision in output.decisions}
    for index, row in dataframe.iterrows():
        decision = by_hash.get(str(row["transaction_hash"]))
        if decision is None:
            continue
        dataframe.at[index, "category"] = decision.category.value
        dataframe.at[index, "category_confidence"] = decision.confidence
        dataframe.at[index, "category_reason"] = decision.reason


def _apply_anomaly_output(dataframe: pd.DataFrame, output: AnomalyAgentOutput) -> None:
    by_hash = {decision.transaction_hash: decision for decision in output.decisions}
    for index, row in dataframe.iterrows():
        decision = by_hash.get(str(row["transaction_hash"]))
        if decision is None:
            continue
        dataframe.at[index, "anomaly_flag"] = decision.anomaly_flag
        dataframe.at[index, "anomaly_severity"] = decision.severity.value
        dataframe.at[index, "anomaly_reason"] = decision.reason


def _apply_insight_output(dataframe: pd.DataFrame, output: InsightAgentOutput) -> None:
    by_hash = {note.transaction_hash: note for note in output.notes}
    for index, row in dataframe.iterrows():
        note = by_hash.get(str(row["transaction_hash"]))
        if note is None:
            continue
        dataframe.at[index, "insight_note"] = note.note
