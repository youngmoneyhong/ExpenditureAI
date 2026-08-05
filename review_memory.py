from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from validators import CATEGORY_MIGRATIONS, CATEGORY_OPTIONS, normalize_amount, normalize_bool


APP_DIR = Path(__file__).resolve().parent
DEFAULT_MEMORY_PATH = APP_DIR / "review_memory.json"


def memory_path() -> Path:
    path_value = os.getenv("REVIEW_MEMORY_FILE", str(DEFAULT_MEMORY_PATH))
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return path


def review_memory_category_records() -> list[dict[str, str]]:
    memory = _load_memory()
    records = []
    for item in memory.get("categories", {}).values():
        category = str(item.get("category", "")).strip()
        description = str(item.get("description", "")).strip()
        if category not in CATEGORY_OPTIONS or not description:
            continue
        records.append(
            {
                "description": description,
                "category": category,
                "source": str(item.get("source", "")).strip(),
                "money_flow": str(item.get("money_flow", "")).strip(),
            }
        )
    return records


def merchant_rule_records() -> list[dict[str, object]]:
    memory = _load_memory()
    records = []
    for rule_key, item in memory.get("categories", {}).items():
        category = str(item.get("category", "")).strip()
        description = str(item.get("description", "")).strip()
        if category not in CATEGORY_OPTIONS or not description:
            continue
        records.append(
            {
                "rule_key": rule_key,
                "merchant": description,
                "source": str(item.get("source", "")).strip(),
                "flow": str(item.get("money_flow", "")).strip(),
                "category": category,
                "uses": int(item.get("count", 0) or 0),
                "updated_at": str(item.get("updated_at", "")).strip(),
            }
        )
    return sorted(
        records,
        key=lambda item: (str(item["merchant"]).lower(), str(item["source"]).lower()),
    )


def update_merchant_rules(records: list[dict[str, object]]) -> int:
    memory = _load_memory()
    categories = memory.setdefault("categories", {})
    updated = 0
    now = datetime.now().isoformat(timespec="seconds")

    for record in records:
        rule_key = str(record.get("rule_key", "")).strip()
        if not rule_key or rule_key not in categories:
            continue
        if normalize_bool(record.get("forget", False)):
            categories.pop(rule_key, None)
            updated += 1
            continue

        category = str(record.get("category", "")).strip()
        if category not in CATEGORY_OPTIONS:
            continue
        item = categories[rule_key]
        if str(item.get("category", "")).strip() == category:
            continue
        item["category"] = category
        item["updated_at"] = now
        updated += 1

    if updated:
        memory["updated_at"] = now
        _save_memory(memory)
    return updated


def apply_review_memory(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    memory = _load_memory()
    if not memory.get("categories") and not memory.get("decisions"):
        return dataframe

    working = dataframe.copy()
    categories = memory.get("categories", {})
    decisions = memory.get("decisions", {})
    for column in ["category", "category_reason", "ignore_reason", "review_note"]:
        if column not in working:
            working[column] = ""
        working[column] = working[column].astype(object)
    if "category_confidence" not in working:
        working["category_confidence"] = 0.0
    working["category_confidence"] = pd.to_numeric(
        working["category_confidence"], errors="coerce"
    ).fillna(0.0).astype(float)

    for index, row in working.iterrows():
        merchant_key = _merchant_key(row)
        transaction_key = _transaction_key(row)

        category_memory = categories.get(merchant_key)
        if category_memory and not _should_skip_category_memory(row):
            category = str(category_memory.get("category", "")).strip()
            if category in CATEGORY_OPTIONS:
                working.at[index, "category"] = category
                working.at[index, "category_confidence"] = max(
                    float(row.get("category_confidence") or 0),
                    0.94,
                )
                working.at[index, "category_reason"] = (
                    "Matched your local review memory."
                )

        decision = decisions.get(transaction_key)
        if not decision:
            continue

        action = str(decision.get("action", "")).strip()
        if action == "exclude":
            working.at[index, "include_in_append"] = False
            working.at[index, "status"] = "ignored"
            working.at[index, "ignore_reason"] = str(
                decision.get("reason") or "Matched local review memory"
            )
            working.at[index, "review_note"] = "Matched local review memory: excluded."
        if normalize_bool(decision.get("reimbursement_candidate", False)):
            working.at[index, "reimbursement_candidate"] = True
            reimbursement_type = str(decision.get("reimbursement_type", "")).strip()
            if reimbursement_type:
                working.at[index, "reimbursement_type"] = reimbursement_type

    return working


def save_review_memory(dataframe: pd.DataFrame) -> int:
    if dataframe.empty:
        return 0

    memory = _load_memory()
    categories = memory.setdefault("categories", {})
    decisions = memory.setdefault("decisions", {})
    now = datetime.now().isoformat(timespec="seconds")
    saved = 0

    for _, row in dataframe.fillna("").iterrows():
        description = str(row.get("description", "")).strip()
        category = str(row.get("category", "")).strip()
        if not description:
            continue

        merchant_key = _merchant_key(row)
        transaction_key = _transaction_key(row)

        if category in CATEGORY_OPTIONS and not _is_system_ignored(row):
            previous = categories.get(merchant_key, {})
            categories[merchant_key] = {
                "description": description,
                "source": str(row.get("source", "")).strip(),
                "money_flow": str(row.get("money_flow", "")).strip(),
                "category": category,
                "count": int(previous.get("count", 0)) + 1,
                "updated_at": now,
            }
            saved += 1

        include = normalize_bool(row.get("include_in_append", False))
        reimbursement_candidate = normalize_bool(row.get("reimbursement_candidate", False))

        decision = None
        if not include and not _is_system_ignored(row) and _looks_like_duplicate_review(row):
            reason = str(row.get("ignore_reason") or row.get("review_note") or "User excluded row.")
            reason = "User unticked this duplicate-looking row before append."
            decision = {
                "action": "exclude",
                "reason": reason,
            }

        if reimbursement_candidate:
            decision = decision or {"action": "reviewed"}
            decision["reimbursement_candidate"] = True
            decision["reimbursement_type"] = str(row.get("reimbursement_type", "")).strip()

        if decision:
            decision.update(
                {
                    "description": description,
                    "source": str(row.get("source", "")).strip(),
                    "amount": normalize_amount(row.get("amount", 0)),
                    "money_flow": str(row.get("money_flow", "")).strip(),
                    "updated_at": now,
                }
            )
            decisions[transaction_key] = decision
            saved += 1

    memory["updated_at"] = now
    _save_memory(memory)
    return saved


def _load_memory() -> dict:
    path = memory_path()
    if not path.exists():
        return {"version": 1, "categories": {}, "decisions": {}}

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "categories": {}, "decisions": {}}

    if not isinstance(data, dict):
        return {"version": 1, "categories": {}, "decisions": {}}
    data.setdefault("version", 1)
    data.setdefault("categories", {})
    data.setdefault("decisions", {})
    changed = False
    for item in data["categories"].values():
        category = str(item.get("category", "")).strip()
        migrated = CATEGORY_MIGRATIONS.get(category)
        if migrated:
            item["category"] = migrated
            changed = True
    if changed:
        _save_memory(data)
    return data


def _save_memory(memory: dict) -> None:
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(memory, file, indent=2, ensure_ascii=True, sort_keys=True)
    temp_path.replace(path)


def _merchant_key(row: pd.Series) -> str:
    source = str(row.get("source", "")).strip().upper()
    flow = str(row.get("money_flow", "")).strip().lower()
    description = _normalized_description(row.get("description", ""))
    return f"{source}|{flow}|{description}"


def _transaction_key(row: pd.Series) -> str:
    date = str(row.get("date", "")).strip()
    source = str(row.get("source", "")).strip().upper()
    description = _normalized_description(row.get("description", ""))
    amount = normalize_amount(row.get("amount", 0))
    flow = str(row.get("money_flow", "")).strip().lower()
    return f"{date}|{source}|{description}|{abs(amount):.2f}|{flow}"


def _normalized_description(value: object) -> str:
    text = str(value or "").upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\b(PTE|LTD|PRIVATE|LIMITED|SG|SINGAPORE)\b", " ", text)
    return " ".join(text.split())


def _should_skip_category_memory(row: pd.Series) -> bool:
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


def _is_system_ignored(row: pd.Series) -> bool:
    reason = str(row.get("ignore_reason", "")).strip().lower()
    review_note = str(row.get("review_note", "")).strip().lower()
    return "paylah wallet top-up" in reason or "paylah wallet top-up" in review_note


def _looks_like_duplicate_review(row: pd.Series) -> bool:
    status = str(row.get("status", "")).strip().lower()
    review_note = str(row.get("review_note", "")).strip().lower()
    ignore_reason = str(row.get("ignore_reason", "")).strip().lower()
    return (
        status == "duplicate"
        or "duplicate" in review_note
        or "duplicate" in ignore_reason
    )
