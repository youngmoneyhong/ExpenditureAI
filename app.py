from __future__ import annotations

import os
import re
import shutil
import tempfile
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from batch_store import (
    AppendAuditRecord,
    BatchStatus,
    BatchStore,
    ScreenshotRecord,
    ScreenshotStatus,
    TransactionProvenance,
    TransactionRecord,
    TransactionStatus,
    screenshot_hash,
    transaction_fingerprint,
)
from enrichment_agents import enrich_transactions
from recommendation_service import RecommendationClient, RecommendationRequest
from review_memory import (
    apply_review_memory,
    memory_path,
    merchant_rule_records,
    review_memory_category_records,
    save_review_memory,
    update_merchant_rules,
)
from sheets import SheetClient, worksheet_url
from validators import (
    ALLOCATION_CATEGORIES,
    CATEGORY_OPTIONS,
    EXPENSE_OFFSET_INFLOW_CATEGORIES,
    FIXED_EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    INFLOW_CATEGORY_OPTIONS,
    OUTFLOW_CATEGORY_OPTIONS,
    VARIABLE_EXPENSE_CATEGORIES,
    normalize_amount,
    normalize_bool,
    rows_to_dataframe,
    today_iso,
)
from vision_extract import extract_transactions_from_image, extraction_to_rows


load_dotenv()
APP_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR = Path(os.getenv("SCREENSHOT_ARCHIVE_DIR", "screenshots"))
if not ARCHIVE_DIR.is_absolute():
    ARCHIVE_DIR = APP_DIR / ARCHIVE_DIR

st.set_page_config(
    page_title="ExpenditureAI",
    layout="wide",
)

st.markdown(
    """
    <style>
    html, body, [class*="st-"], .stMarkdown, .stText, label, p, span {
        font-size: 18px;
    }
    h1 {
        font-size: 36px;
    }
    h2, h3 {
        font-size: 25px;
    }
    input, textarea, button, select {
        font-size: 18px !important;
    }
    div[data-testid="stDataFrame"] div[role="grid"],
    div[data-testid="stDataEditor"] div[role="grid"] {
        font-size: 19px;
        line-height: 1.45;
    }
    div[data-testid="stDataFrame"] [role="columnheader"],
    div[data-testid="stDataEditor"] [role="columnheader"] {
        font-size: 18px;
        font-weight: 700;
    }
    div[data-testid="stMetricValue"] {
        font-size: 28px;
    }
    div[data-testid="stCaptionContainer"], .screenshot-caption {
        font-size: 15px;
    }
    .screenshot-strip {
        display: flex;
        gap: 12px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 8px 0 16px 0;
        scroll-snap-type: x proximity;
    }
    .screenshot-card {
        flex: 0 0 190px;
        scroll-snap-align: start;
    }
    .screenshot-card img {
        width: 100%;
        height: 220px;
        object-fit: contain;
        background: #f8fafc;
        border-radius: 6px;
    }
    div[data-testid="stFileUploader"] section {
        min-height: 180px;
        border: 2px dashed #7b93ad;
        background: #f7fafc;
    }
    .inbox-step {
        padding: 10px 12px;
        border-left: 4px solid #244a73;
        background: #f4f7f9;
        margin: 8px 0;
    }
    .screenshot-caption {
        margin-top: 6px;
        color: #94a3b8;
        text-align: center;
        word-break: break-word;
    }
    .workflow-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        margin: 22px 0 10px;
        padding: 16px 20px;
        color: #ffffff;
        background: #242535;
        border-top: 4px solid #ff806b;
        border-radius: 6px 6px 0 0;
    }
    .workflow-heading strong {
        color: #ffffff;
        font-size: 21px;
        letter-spacing: 0;
    }
    .workflow-heading span {
        color: #c9dedd;
        font-size: 14px;
    }
    div[data-testid="stSegmentedControl"] {
        padding: 12px;
        margin-bottom: 8px;
        background: #edf7f6;
        border: 1px solid #c8dcda;
        border-radius: 0 0 6px 6px;
    }
    div[data-testid="stSegmentedControl"] button {
        min-height: 54px;
        color: #242535 !important;
        background: #ffffff !important;
        border-color: #c8dcda !important;
        font-weight: 700 !important;
        letter-spacing: 0 !important;
    }
    div[data-testid="stSegmentedControl"] button[aria-pressed="true"] {
        color: #ffffff !important;
        background: #ff806b !important;
        border-color: #ff806b !important;
        box-shadow: 0 4px 12px rgba(36, 37, 53, 0.16);
    }
    div[data-testid="stSegmentedControl"] button:hover {
        border-color: #ff806b !important;
    }
    .workflow-context {
        margin: 0 0 18px;
        padding: 10px 14px;
        color: #334b50;
        background: #ffffff;
        border-left: 4px solid #77bdb6;
        border-radius: 0 4px 4px 0;
        font-size: 15px;
    }
    @media (max-width: 720px) {
        .workflow-heading {
            align-items: flex-start;
            flex-direction: column;
        }
        div[data-testid="stSegmentedControl"] button {
            min-height: 48px;
            padding-left: 7px !important;
            padding-right: 7px !important;
            font-size: 14px !important;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

REVIEW_COLUMNS = [
    "include_in_append",
    "status",
    "date",
    "source",
    "description",
    "amount_original",
    "amount_parse_error",
    "amount_missing",
    "amount",
    "currency",
    "money_flow",
    "category",
    "allocation_confirmed",
    "duplicate_override",
    "reimbursement_candidate",
    "reimbursement_type",
    "review_note",
]

ANOMALY_COLUMNS = [
    "date",
    "source",
    "description",
    "amount_original",
    "amount_parse_error",
    "amount",
    "category",
    "status",
    "anomaly_severity",
    "anomaly_reason",
    "review_note",
    "confidence",
    "transaction_hash",
]

IGNORED_COLUMNS = [
    "date",
    "source",
    "description",
    "amount",
    "category",
    "ignore_reason",
    "image_filename",
]

NON_REIMBURSEMENT_INFLOW_CATEGORIES = {
    *INCOME_CATEGORIES,
    "Cashbacks & Refunds",
    "GVs & Prize Award",
    "Income",
    "Funding",
}
REVIEW_REQUIRED_CATEGORIES = set(CATEGORY_OPTIONS) - set(
    INCOME_CATEGORIES + VARIABLE_EXPENSE_CATEGORIES + FIXED_EXPENSE_CATEGORIES
    + ALLOCATION_CATEGORIES + list(EXPENSE_OFFSET_INFLOW_CATEGORIES) + ["Transfer"]
)


def bool_series(dataframe: pd.DataFrame, column: str, default: bool = False) -> pd.Series:
    if dataframe.empty:
        return pd.Series(dtype=bool, index=dataframe.index)
    if column not in dataframe:
        return pd.Series(default, index=dataframe.index)
    return dataframe[column].apply(normalize_bool)


def upload_signature(uploaded_files) -> str:
    digest = hashlib.sha256()
    for uploaded_file in uploaded_files or []:
        digest.update(uploaded_file.name.encode("utf-8", errors="replace"))
        digest.update(uploaded_file.getvalue())
    return digest.hexdigest()


def guided_workflow_state(dataframe: pd.DataFrame) -> pd.Series:
    if dataframe.empty:
        return pd.Series(dtype=str, index=dataframe.index)
    status = dataframe["status"].fillna("").astype(str)
    state = pd.Series("ready", index=dataframe.index, dtype=str)
    state.loc[status.eq("needs_review")] = "needs_attention"
    state.loc[status.isin(["ignored", "duplicate"])] = "excluded"
    state.loc[
        bool_series(dataframe, "include_in_append")
        & dataframe.get("decision_source", pd.Series("", index=dataframe.index)).astype(str).eq("user")
    ] = "approved"
    return state


def batch_store() -> BatchStore:
    return BatchStore()


def json_safe_record(row: pd.Series) -> dict:
    record = {}
    for key, value in row.to_dict().items():
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            record[key] = None
        elif hasattr(value, "item"):
            record[key] = value.item()
        elif isinstance(value, (datetime, pd.Timestamp)):
            record[key] = value.isoformat()
        else:
            record[key] = value
    return record


def create_persistent_batch(uploaded_files) -> str:
    screenshots = [
        ScreenshotRecord(
            filename=item.name,
            content_hash=screenshot_hash(item.getvalue()),
        )
        for item in uploaded_files
    ]
    batch = batch_store().create_batch(screenshots)
    st.session_state.active_batch_id = batch.batch_id
    st.session_state.screenshot_ids = {
        screenshot.filename: screenshot.screenshot_id for screenshot in screenshots
    }
    st.session_state.batch_user_decisions = {}
    st.session_state.last_append_tab_links = []
    st.session_state.last_append_completed_batch_id = ""
    st.session_state.existing_sheet_match_cache = {}
    return batch.batch_id


def persist_batch_results(
    dataframe: pd.DataFrame,
    *,
    refresh_recommendations: bool = True,
) -> None:
    batch_id = st.session_state.get("active_batch_id")
    if not batch_id:
        return
    store = batch_store()
    batch = store.get(batch_id)
    state_by_name = st.session_state.get("screenshot_states", {})
    for screenshot in batch.screenshots:
        state = state_by_name.get(screenshot.filename, {})
        screenshot.status = ScreenshotStatus(str(state.get("status", "Ready")).lower().replace(" ", "_"))
        screenshot.error = str(state.get("error", ""))
    batch.transactions = []
    recommendations = dict(st.session_state.get("recommendations", {}))
    client = RecommendationClient() if refresh_recommendations else None
    merchant_history = merchant_rule_records() if refresh_recommendations else []
    screenshot_by_name = {item.filename: item for item in batch.screenshots}
    states = guided_workflow_state(dataframe)
    decisions = st.session_state.get("batch_user_decisions", {})
    for index, row in dataframe.iterrows():
        payload = json_safe_record(row)
        screenshot = screenshot_by_name.get(str(row.get("image_filename", "")))
        if screenshot is None:
            continue
        fingerprint = transaction_fingerprint(payload)
        recommendation_key = str(row.get("transaction_hash", fingerprint))
        recommendation_data = recommendations.get(recommendation_key, {})
        if refresh_recommendations:
            request = RecommendationRequest(
                screenshot_hash=screenshot.content_hash,
                transaction_fingerprint=fingerprint,
                transaction=payload,
                provenance={"screenshot_id": screenshot.screenshot_id},
                merchant_history=merchant_history,
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            )
            recommendation_data = client.recommend(request).model_dump()
            recommendations[recommendation_key] = recommendation_data
        batch.transactions.append(TransactionRecord(
            transaction_id=str(row.get("transaction_hash", "")) or fingerprint,
            fingerprint=fingerprint,
            status=TransactionStatus(states.loc[index]),
            data=payload,
            provenance=TransactionProvenance(
                screenshot_id=screenshot.screenshot_id,
                image_filename=screenshot.filename,
                image_region=(row.get("image_region") if isinstance(row.get("image_region"), dict) else None),
                extraction_confidence=float(row.get("confidence", 0) or 0),
                matched_workbook_id=str(row.get("matched_workbook_id", "") or ""),
                matched_worksheet=str(row.get("matched_worksheet", "") or ""),
                matched_row=(
                    int(float(row.get("matched_row")))
                    if str(row.get("matched_row", "")).strip() not in {"", "nan", "None"}
                    else None
                ),
                decision_source="deterministic",
            ),
            recommendation=recommendation_data or None,
            review_reasons=[str(row.get("review_note", ""))] if str(row.get("review_note", "")) else [],
            user_decision=decisions.get(str(row.get("transaction_hash", fingerprint))),
        ))
    st.session_state.recommendations = recommendations
    batch.status = BatchStatus.NEEDS_ATTENTION if (states == "needs_attention").any() else BatchStatus.READY
    store.save(batch)


def persist_append_completion(dataframe: pd.DataFrame, audits) -> None:
    batch_id = st.session_state.get("active_batch_id")
    if not batch_id:
        return
    store = batch_store()
    batch = store.get(batch_id)
    appended_hashes = set(dataframe.get("transaction_hash", pd.Series(dtype=str)).astype(str))
    appended_ids = []
    for transaction in batch.transactions:
        if transaction.transaction_id in appended_hashes:
            transaction.status = TransactionStatus.APPENDED
            appended_ids.append(transaction.transaction_id)
    audit = AppendAuditRecord(
        status="verified" if all(item.verified for item in audits) else "failed",
        completed_at=datetime.now().astimezone(),
        appended_transaction_ids=appended_ids,
        failed_transaction_ids=[] if all(item.verified for item in audits) else appended_ids,
        affected_destinations=[
            worksheet_url(item.spreadsheet_id, item.worksheet_id) for item in audits
        ],
    )
    batch.append_audits.append(audit)
    batch.status = BatchStatus.COMPLETE if all(item.verified for item in audits) else BatchStatus.FAILED
    store.save(batch)


def safe_filename(name: str) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix.lower() or ".png"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "screenshot"
    timestamp = datetime.now().strftime("%H%M%S_%f")
    return f"{timestamp}_{safe_stem}{suffix}"


def archive_uploaded_file(uploaded_file) -> Path:
    day_dir = ARCHIVE_DIR / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    archive_path = day_dir / safe_filename(uploaded_file.name)
    uploaded_file.seek(0)
    with archive_path.open("wb") as file:
        shutil.copyfileobj(uploaded_file, file)
    uploaded_file.seek(0)
    return archive_path


def display_archive_path(path: Path) -> str:
    try:
        return str(path.relative_to(APP_DIR))
    except ValueError:
        return str(path)


def uploaded_image_data_url(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix.lower().lstrip(".") or "png"
    mime_type = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    uploaded_file.seek(0)
    encoded = base64.b64encode(uploaded_file.getvalue()).decode("ascii")
    uploaded_file.seek(0)
    return f"data:image/{mime_type};base64,{encoded}"


def render_upload_preview(uploaded_files) -> None:
    cards = []
    for uploaded_file in uploaded_files:
        data_url = uploaded_image_data_url(uploaded_file)
        caption = uploaded_file.name
        cards.append(
            "<div class='screenshot-card'>"
            f"<img src='{data_url}' alt='{caption}'>"
            f"<div class='screenshot-caption'>{caption}</div>"
            "</div>"
        )
    st.markdown(
        "<div class='screenshot-strip'>" + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


def save_uploaded_file(uploaded_file) -> Path:
    suffix = Path(uploaded_file.name).suffix or ".png"
    uploaded_file.seek(0)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        uploaded_file.seek(0)
        return Path(tmp.name)


def vision_worker_count(file_count: int) -> int:
    """Keep parallel Vision requests quick without flooding the API."""
    try:
        configured = int(os.getenv("VISION_CONCURRENCY", "3"))
    except ValueError:
        configured = 3
    return min(file_count, max(1, min(configured, 4)))


def extract_all(
    uploaded_files,
    model: str,
    *,
    archive_screenshots: bool,
    save_raw_text: bool,
    run_category_agent: bool,
    run_anomaly_agent: bool,
    run_insight_agent: bool,
) -> pd.DataFrame:
    rows = []
    progress = st.progress(0)
    file_states = {}

    jobs = []
    for index, uploaded_file in enumerate(uploaded_files, start=1):
        archived_path = archive_uploaded_file(uploaded_file) if archive_screenshots else None
        path = save_uploaded_file(uploaded_file)
        jobs.append(
            {
                "index": index,
                "filename": uploaded_file.name,
                "path": path,
                "archive_path": display_archive_path(archived_path) if archived_path else "",
            }
        )
        file_states[uploaded_file.name] = {"status": "Queued", "error": ""}
    st.session_state.screenshot_states = file_states

    try:
        extractions = {}
        with ThreadPoolExecutor(max_workers=vision_worker_count(len(jobs))) as executor:
            futures = {}
            for job in jobs:
                file_states[job["filename"]]["status"] = "Reading"
                future = executor.submit(
                    extract_transactions_from_image,
                    job["path"],
                    model=model,
                    today=today_iso(),
                )
                futures[future] = job
            st.session_state.screenshot_states = file_states
            for completed, future in enumerate(as_completed(futures), start=1):
                job = futures[future]
                try:
                    extractions[job["index"]] = future.result()
                    file_states[job["filename"]]["status"] = "Checking history"
                except Exception as exc:
                    file_states[job["filename"]] = {
                        "status": "Failed",
                        "error": str(exc),
                    }
                progress.progress(completed / len(jobs))
                st.session_state.screenshot_states = file_states

        for job in jobs:
            extraction = extractions.get(job["index"])
            if extraction is None:
                continue
            extracted_rows = extraction_to_rows(
                extraction,
                filename=job["filename"],
                archive_path=job["archive_path"],
            )
            for extracted_row in extracted_rows:
                extracted_row["screenshot_id"] = st.session_state.get("screenshot_ids", {}).get(
                    job["filename"], ""
                )
                extracted_row["extraction_confidence"] = extracted_row.get("confidence", 0)
            rows.extend(extracted_rows)

            for warning in extraction.warnings:
                st.warning(f"{job['filename']}: {warning}")
            file_states[job["filename"]]["status"] = "Ready"
        st.session_state.screenshot_states = file_states
    finally:
        for job in jobs:
            job["path"].unlink(missing_ok=True)

    dataframe = rows_to_dataframe(rows)
    if not save_raw_text and not dataframe.empty:
        dataframe["raw_text"] = ""

    dataframe = apply_guarded_review_memory(dataframe)
    dataframe = apply_workflow_state(dataframe)

    enrichable = dataframe[bool_series(dataframe, "include_in_append")].copy()
    if not enrichable.empty and (run_category_agent or run_anomaly_agent or run_insight_agent):
        try:
            category_memory = load_category_memory() if run_category_agent else []
            enriched, insight_overview = enrich_transactions(
                enrichable,
                model=model,
                run_category_agent=run_category_agent,
                run_anomaly_agent=run_anomaly_agent,
                run_insight_agent=run_insight_agent,
                category_memory=category_memory,
            )
            dataframe.update(enriched)
            dataframe = apply_guarded_review_memory(dataframe)
            dataframe = apply_workflow_state(dataframe)
            st.session_state.insight_overview = insight_overview
        except Exception as exc:
            st.session_state.insight_overview = ""
            st.warning(
                "Extraction worked, but enrichment agents failed. "
                f"You can still review and append the extracted rows. Details: {exc}"
            )

    return dataframe


def filter_new_rows(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    sheet = SheetClient.from_env()
    existing_keys = sheet.existing_duplicate_keys()
    transaction_keys = review_duplicate_keys(dataframe)
    is_new = ~transaction_keys.isin(existing_keys)
    if "duplicate_override" in dataframe:
        is_new = is_new | bool_series(dataframe, "duplicate_override")
    return dataframe[is_new].copy(), int((~is_new).sum())


def apply_existing_sheet_duplicate_check(
    dataframe: pd.DataFrame,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    working = dataframe.copy()
    st.session_state.duplicate_check_error = ""
    try:
        cache_key = hashlib.sha256(
            "|".join(
                working.apply(
                    lambda row: f"{row.get('transaction_hash', '')}:{row.get('date', '')}",
                    axis=1,
                ).astype(str)
            ).encode("utf-8")
        ).hexdigest()
        cached = st.session_state.get("existing_sheet_match_cache", {})
        if cache_key in cached and not force_refresh:
            matches = cached[cache_key]
            evidence_lookup_available = True
        else:
            sheet = SheetClient.from_env()
            evidence_lookup_available = hasattr(sheet, "existing_duplicate_matches_by_period")
        if (force_refresh or cache_key not in cached) and evidence_lookup_available:
            matches = sheet.existing_duplicate_matches_by_period(working)
            st.session_state.existing_sheet_match_cache = {cache_key: matches}
        elif force_refresh or cache_key not in cached:
            matches = {}
        if matches:
            already_recorded = pd.Series(working.index.isin(matches), index=working.index)
            for index, match in matches.items():
                working.at[index, "matched_workbook_id"] = match.get("spreadsheet_id", "")
                working.at[index, "matched_worksheet"] = match.get("worksheet", "")
                working.at[index, "matched_worksheet_id"] = str(
                    match.get("worksheet_id", "") or ""
                )
                working.at[index, "matched_row"] = str(match.get("row", "") or "")
                working.at[index, "matched_sheet_data"] = json.dumps(
                    match.get("transaction", {}), ensure_ascii=True, default=str
                )
        elif evidence_lookup_available:
            already_recorded = pd.Series(False, index=working.index)
        else:
            already_recorded = sheet.existing_duplicate_mask_by_period(working)
    except Exception as exc:
        st.session_state.duplicate_check_error = str(exc)
        st.warning(
            "Past-append duplicate check is unavailable. Append is blocked so you do not accidentally double-record old rows."
        )
        return working

    if not already_recorded.any():
        return working

    duplicate_override = working["duplicate_override"].apply(normalize_bool)
    needs_prompt = (
        already_recorded
        & working["include_in_append"].apply(normalize_bool)
        & ~duplicate_override
        & ~working["status"].astype(str).isin(["ignored"])
    )
    working.loc[needs_prompt, "status"] = "needs_review"
    working.loc[needs_prompt, "include_in_append"] = False
    working.loc[needs_prompt, "review_note"] = (
        "Already recorded in Google Sheets. Tick append? and separate duplicate? only if you still want to append it."
    )

    override_mask = already_recorded & duplicate_override
    working.loc[override_mask, "review_note"] = (
        "Already recorded before, but marked as a separate duplicate for append."
    )

    return working


def load_category_memory() -> list[dict]:
    local_memory = review_memory_category_records()
    try:
        sheet = SheetClient.from_env()
        memory = sheet.category_memory_records() + local_memory
        if memory:
            st.caption(
                "Category memory loaded: "
                f"{len(memory)} saved row(s) "
                f"({len(local_memory)} from local review memory)."
            )
        return memory
    except Exception as exc:
        if local_memory:
            st.caption(
                "Google Sheets category memory unavailable; "
                f"using {len(local_memory)} local review-memory row(s). Details: {exc}"
            )
            return local_memory
        st.caption(f"Category memory unavailable for this run: {exc}")
        return []


def normalize_description(value: object) -> str:
    return " ".join(str(value or "").upper().split())


def apply_guarded_review_memory(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    working = apply_review_memory(dataframe)
    protected = (
        dataframe["money_flow"].astype(str).isin(["inflow", "neutral"])
        | dataframe["category"].astype(str).isin(
            set(ALLOCATION_CATEGORIES) | REVIEW_REQUIRED_CATEGORIES | {"Transfer"}
        )
        | dataframe["transaction_type"].astype(str).eq("contribution")
    )
    # Retain remembered duplicate exclusions, but never infer cash or capital approval.
    for column in ["category", "category_reason", "category_confidence",
                   "reimbursement_candidate", "reimbursement_type"]:
        if column in dataframe:
            working.loc[protected, column] = dataframe.loc[protected, column]
    for column in ["allocation_confirmed", "amount_missing"]:
        working[column] = bool_series(dataframe, column)
    working.loc[working["category"].ne(dataframe["category"]), "allocation_confirmed"] = False
    return working


def noncash_inflow_mask(dataframe: pd.DataFrame) -> pd.Series:
    descriptions = dataframe.get("description", pd.Series("", index=dataframe.index)).fillna("").astype(str)
    government_voucher = dataframe["category"].astype(str).eq("Prize Awards/Government Vouchers") & descriptions.str.contains(
        r"\b(?:government|govt|CDC|SG60|GST|climate)\b", case=False, regex=True
    )
    return dataframe["money_flow"].astype(str).eq("inflow") & descriptions.str.contains(
        r"\b(?:gift\s*(?:voucher|card)s?|vouchers?|reward points|non[ -]?cash)\b",
        case=False, regex=True,
    ) & ~government_voucher & ~(
        dataframe["category"].astype(str).eq("Cashbacks & Refunds")
        & descriptions.str.contains(r"\b(?:cash refund|refunded to|refund credited)\b", case=False, regex=True)
    )


def investment_proceeds_mask(dataframe: pd.DataFrame) -> pd.Series:
    descriptions = dataframe.get("description", pd.Series("", index=dataframe.index)).fillna("").astype(str)
    return dataframe["money_flow"].astype(str).eq("inflow") & descriptions.str.contains(
        r"\b(?:investment (?:sale|withdrawal)|(?:sale|sold|redemption) (?:of |proceeds.*)?(?:shares|stocks|etfs?|funds|crypto)|(?:brokerage|investment) withdrawal)\b",
        case=False, regex=True,
    )


def statement_review_reasons(dataframe: pd.DataFrame) -> dict[str, pd.Series]:
    amounts = pd.to_numeric(dataframe["amount"], errors="coerce")
    categories = dataframe["category"].astype(str)
    flows = dataframe["money_flow"].astype(str)
    allocations = categories.isin(ALLOCATION_CATEGORIES)
    return {
        "Enter the missing amount before append": (
            bool_series(dataframe, "amount_missing") | amounts.isna() | amounts.abs().eq(float("inf"))
        ),
        "Fix the unreadable amount before append": bool_series(dataframe, "amount_parse_error"),
        "Enter a verified SGD amount; no exchange rate is assumed": dataframe.get(
            "currency", pd.Series("", index=dataframe.index)
        ).astype(str).str.upper().ne("SGD"),
        "Confirm new external capital counted once, not both bank/brokerage legs or reinvestment": (
            allocations & ~bool_series(dataframe, "allocation_confirmed")
        ),
        "Allocation must be an outflow of new external capital": allocations & ~flows.eq("outflow"),
        "Choose an allocation category and confirm new external capital": (
            dataframe.get("transaction_type", pd.Series("", index=dataframe.index)).astype(str).eq("contribution")
            & ~allocations
        ),
        "Clarify the category; legacy labels and unknown inflows are excluded": (
            categories.isin(REVIEW_REQUIRED_CATEGORIES)
            | (flows.eq("inflow") & ~categories.isin(INCOME_CATEGORIES + list(EXPENSE_OFFSET_INFLOW_CATEGORIES)))
        ),
        "Noncash vouchers and awards are excluded from the cash statement": noncash_inflow_mask(dataframe),
        "Investment sale proceeds and withdrawals are neutral funding, not income": investment_proceeds_mask(dataframe),
        "Choose a valid money flow before append": ~flows.isin(["inflow", "outflow", "neutral"]),
    }


def is_paylah_wallet_topup(dataframe: pd.DataFrame) -> pd.Series:
    sources = dataframe["source"].astype(str).str.upper().str.strip()
    descriptions = dataframe["description"].apply(normalize_description)
    compact_descriptions = descriptions.str.replace(r"[^A-Z0-9]+", " ", regex=True)
    has_topup = compact_descriptions.str.contains(r"\bTOP\s*UP\b", regex=True)
    has_wallet_or_paylah = compact_descriptions.str.contains(
        r"\b(?:WALLET|PAYLAH)\b", regex=True
    )
    explicit_wallet_topup = descriptions.eq("TOP UP MY WALLET")
    paylah_wallet_topup = (
        sources.isin(["DBS_PAYLAH", "UNKNOWN"])
        & (
            explicit_wallet_topup
            | (has_topup & has_wallet_or_paylah)
        )
    )
    dbs_bank_paylah_topup = (
        sources.eq("DBS_BANK")
        & has_topup
        & compact_descriptions.str.contains(r"\bPAYLAH\b", regex=True)
    )
    return paylah_wallet_topup | dbs_bank_paylah_topup


def is_uob_ebanking_payment(dataframe: pd.DataFrame) -> pd.Series:
    sources = dataframe["source"].astype(str).str.upper().str.strip()
    descriptions = dataframe["description"].apply(normalize_description)
    return sources.eq("UOB_TMRW") & descriptions.str.startswith("PAYMT THRU E-BANK")


def is_uob_credit_card_bill_payment(dataframe: pd.DataFrame) -> pd.Series:
    """Identify bank transfers that settle a UOB credit-card bill, not new spending."""
    sources = dataframe["source"].astype(str).str.upper().str.strip()
    descriptions = dataframe["description"].apply(normalize_description)
    flows = dataframe["money_flow"].astype(str).str.lower().str.strip()
    amounts = pd.to_numeric(dataframe["amount"], errors="coerce").fillna(0)
    has_uob_credit_card_marker = descriptions.str.contains(
        r"(?:\bUOB\b.*\b(?:CCRD|CREDIT CARD)\b|\b(?:CCRD|CREDIT CARD)\b.*\bUOB\b)",
        regex=True,
    )
    is_outflow = flows.eq("outflow") | amounts.lt(0)
    return sources.isin(["DBS_BANK", "UOB_TMRW"]) & is_outflow & has_uob_credit_card_marker


def apply_known_merchant_category_rules(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Apply deterministic category rules for high-confidence recurring merchants."""
    if dataframe.empty:
        return dataframe

    working = dataframe.copy()
    descriptions = working["description"].apply(normalize_description)
    compact = descriptions.str.replace(r"[^A-Z0-9]+", " ", regex=True).str.strip()

    bus_mrt = compact.str.contains(r"\bBUS\s*/?\s*MRT\s+SINGAPORE\b", regex=True)
    accountant_general = compact.str.contains(r"\bACCOUNTANT\s+GENERAL\b", regex=True)
    ibg_gov = compact.str.contains(r"\bIBG\s+GOV\s+GOV\b", regex=True)
    mindef_saf = compact.str.contains(r"\bMINDEF\s+SAF\b", regex=True)
    interactive_brokers = compact.str.contains(
        r"\bINTERACTIVE\s+BROKERS\s+PART\b", regex=True
    )
    equity_brokers = compact.str.contains(
        r"\b(?:TIGER\s+BROKERS|MOOMOO)\b", regex=True
    )
    prize_award = ibg_gov | mindef_saf

    working.loc[bus_mrt, "category"] = "Public Transport"
    working.loc[bus_mrt, "money_flow"] = "outflow"
    working.loc[bus_mrt, "category_confidence"] = 0.99
    working.loc[bus_mrt, "category_reason"] = (
        "Matched deterministic merchant rule: BUS/MRT SINGAPORE -> Public Transport."
    )

    working.loc[accountant_general, "category"] = "Net Salary"
    working.loc[accountant_general, "money_flow"] = "inflow"
    working.loc[accountant_general, "transaction_type"] = "salary"
    working.loc[accountant_general, "reimbursement_candidate"] = False
    working.loc[accountant_general, "reimbursement_type"] = "salary"
    working.loc[accountant_general, "category_confidence"] = 0.99
    working.loc[accountant_general, "category_reason"] = (
        "Matched deterministic merchant rule: ACCOUNTANT-GENERAL -> Net Salary."
    )

    working.loc[prize_award, "category"] = "Prize Awards/Government Vouchers"
    working.loc[prize_award, "money_flow"] = "inflow"
    working.loc[prize_award, "transaction_type"] = "cash_income"
    working.loc[prize_award, "reimbursement_candidate"] = False
    working.loc[prize_award, "reimbursement_type"] = "unknown"
    working.loc[prize_award, "category_confidence"] = 0.99
    working.loc[prize_award, "category_reason"] = (
        "Matched deterministic merchant rule: government/SAF cash inflow -> Prize Awards/Government Vouchers."
    )

    working.loc[interactive_brokers, "category"] = "ETF Contributions"
    working.loc[interactive_brokers, "money_flow"] = "outflow"
    working.loc[interactive_brokers, "allocation_confirmed"] = False
    working.loc[interactive_brokers, "transaction_type"] = "unknown"
    working.loc[interactive_brokers, "category_confidence"] = 0.99
    working.loc[interactive_brokers, "category_reason"] = (
        "Matched deterministic merchant rule: INTERACTIVE BROKERS PART -> ETF Contributions."
    )

    working.loc[equity_brokers, "category"] = "Equity Contributions"
    working.loc[equity_brokers, "money_flow"] = "outflow"
    working.loc[equity_brokers, "allocation_confirmed"] = False
    working.loc[equity_brokers, "transaction_type"] = "unknown"
    working.loc[equity_brokers, "category_confidence"] = 0.99
    working.loc[equity_brokers, "category_reason"] = (
        "Matched deterministic merchant rule: Tiger Brokers/Moomoo -> Equity Contributions."
    )
    return working


def apply_category_flow_rules(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    working = dataframe.copy()
    inflow_mask = working["money_flow"].astype(str).eq("inflow")
    outflow_mask = working["money_flow"].astype(str).eq("outflow")
    working["category"] = working["category"].replace({
        "Reimbursement": "Reimbursements", "Bills": "Bills / Recurring Commitments"
    })
    category = working["category"].astype(str)

    invalid_inflow = inflow_mask & ~category.isin(INFLOW_CATEGORY_OPTIONS)
    working.loc[invalid_inflow, "status"] = "needs_review"
    working.loc[invalid_inflow, "include_in_append"] = False
    working.loc[invalid_inflow, "category_reason"] = (
        "Unknown inflow: clarify the cash category before append; no reimbursement assumed."
    )

    invalid_outflow = outflow_mask & ~category.isin(
        OUTFLOW_CATEGORY_OPTIONS + ["Transfer"] + list(REVIEW_REQUIRED_CATEGORIES)
    )
    working.loc[invalid_outflow, "category"] = "Others"
    working.loc[invalid_outflow, "category_reason"] = (
        "Adjusted because outflow rows use expense or allocation categories."
    )

    non_reimbursement_inflow = (
        inflow_mask
        & working["category"].astype(str).isin(NON_REIMBURSEMENT_INFLOW_CATEGORIES)
    )
    working.loc[non_reimbursement_inflow, "reimbursement_candidate"] = False
    working.loc[non_reimbursement_inflow, "reimbursement_for"] = ""
    working.loc[non_reimbursement_inflow, "reimbursement_for_category"] = ""

    working.loc[
        inflow_mask & working["category"].astype(str).eq("Reimbursements"),
        "reimbursement_candidate",
    ] = True
    working.loc[
        inflow_mask
        & working["category"].astype(str).eq("Reimbursements")
        & working["reimbursement_type"].astype(str).isin(["", "unknown"]),
        "reimbursement_type",
    ] = "friend_repayment"
    working.loc[
        inflow_mask & working["category"].astype(str).eq("Carousell Sales"),
        "reimbursement_type",
    ] = "unknown"
    working.loc[
        inflow_mask & working["category"].astype(str).eq("Cashbacks & Refunds"),
        "reimbursement_type",
    ] = "merchant_refund"

    return working


def amount_intensity_scale(dataframe: pd.DataFrame) -> float:
    if dataframe.empty or "amount" not in dataframe:
        return 1.0

    amounts = pd.to_numeric(dataframe["amount"], errors="coerce").abs()
    amounts = amounts[amounts > 0]
    if amounts.empty:
        return 1.0
    return max(float(amounts.quantile(0.90)), 1.0)


def flow_signal(value_flow: object, value_amount: object, scale: float) -> str:
    flow = str(value_flow or "").lower()
    amount = abs(normalize_amount(value_amount))
    if flow == "inflow":
        return f"IN +${amount:,.2f}"
    if flow == "outflow":
        return f"OUT -${amount:,.2f}"
    return ""


def highlight_review_rows(row: pd.Series, scale: float) -> list[str]:
    if review_row_needs_action(row):
        return [
            "background-color: rgba(234, 179, 8, 0.30); color: #fefce8;"
        ] * len(row)

    flow = str(row.get("money_flow", "")).lower()
    amount = abs(normalize_amount(row.get("amount", 0)))
    intensity = min(amount / max(scale, 1.0), 1.0)
    alpha = 0.12 + (0.26 * intensity)

    if flow == "inflow":
        color = f"rgba(22, 163, 74, {alpha:.2f})"
        text = "#ecfdf5"
    elif flow == "outflow":
        color = f"rgba(220, 38, 38, {alpha:.2f})"
        text = "#fff1f2"
    else:
        return [""] * len(row)

    return [f"background-color: {color}; color: {text};"] * len(row)


def review_row_needs_action(row: pd.Series) -> bool:
    status = str(row.get("status", "")).lower()
    if status == "needs_review":
        return True

    if normalize_bool(row.get("include_in_append", False)):
        if not is_valid_iso_date(row.get("date", "")):
            return True

    return False


def apply_workflow_state(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    working = rows_to_dataframe(dataframe.to_dict(orient="records"))
    previous_include = dataframe.get("include_in_append")
    if previous_include is not None:
        previous_include = previous_include.apply(normalize_bool)

    working["status"] = "ready"
    working["include_in_append"] = True
    working["duplicate_override"] = bool_series(working, "duplicate_override")
    working["amount_parse_error"] = bool_series(working, "amount_parse_error")
    working["amount_missing"] = bool_series(working, "amount_missing")
    working["allocation_confirmed"] = bool_series(working, "allocation_confirmed")
    working["ignore_reason"] = working["ignore_reason"].fillna("").astype(str)
    working["review_note"] = working["review_note"].fillna("").astype(str)
    memory_excluded_mask = (
        working["ignore_reason"].str.contains(
            "local review memory|user unticked this duplicate-looking row",
            case=False,
            na=False,
        )
        | working["review_note"].str.contains(
            "local review memory|user unticked this duplicate-looking row",
            case=False,
            na=False,
        )
    )
    already_recorded_mask = working["review_note"].str.contains(
        "already recorded",
        case=False,
        na=False,
    )

    working = apply_known_merchant_category_rules(working)

    paylah_topup_mask = is_paylah_wallet_topup(working)
    uob_ebanking_mask = is_uob_ebanking_payment(working)
    uob_credit_card_mask = is_uob_credit_card_bill_payment(working)
    ignored_transfer_mask = paylah_topup_mask | uob_ebanking_mask | uob_credit_card_mask
    working.loc[ignored_transfer_mask, "status"] = "ignored"
    working.loc[ignored_transfer_mask, "include_in_append"] = False
    working.loc[ignored_transfer_mask, "ignore_reason"] = "PayLah wallet top-up"
    working.loc[ignored_transfer_mask, "review_note"] = "Ignored by default: PayLah wallet top-up"
    working.loc[ignored_transfer_mask, "money_flow"] = "neutral"
    working.loc[ignored_transfer_mask, "transaction_type"] = "transfer"
    working.loc[ignored_transfer_mask, "reimbursement_type"] = "self_transfer"
    working.loc[ignored_transfer_mask, "reimbursement_candidate"] = False
    working.loc[ignored_transfer_mask, "reimbursement_for"] = ""
    working.loc[ignored_transfer_mask, "category"] = "Transfer"
    working.loc[uob_ebanking_mask, "ignore_reason"] = "UOB e-banking payment"
    working.loc[uob_ebanking_mask, "review_note"] = (
        "Ignored by default: UOB PAYMT THRU E-BANK"
    )
    working.loc[uob_credit_card_mask, "ignore_reason"] = "UOB credit-card bill payment"
    working.loc[uob_credit_card_mask, "review_note"] = (
        "Ignored by default: UOB credit-card bill payment transfer"
    )

    categories = working["category"].astype(str)
    funding = categories.eq("Funding") | investment_proceeds_mask(working)
    own_transfer = (
        categories.eq("Transfer")
        | working["reimbursement_type"].astype(str).eq("self_transfer")
        | working["transaction_type"].astype(str).eq("transfer")
    ) & ~categories.isin(ALLOCATION_CATEGORIES)
    noncash = noncash_inflow_mask(working)
    for mask, reason in [
        (own_transfer, "Own-account transfer, excluded from the cash statement"),
        (funding, "Neutral funding or investment proceeds, excluded from income"),
        (noncash, "Noncash voucher or award, excluded from the cash statement"),
    ]:
        mask = mask & ~ignored_transfer_mask
        working.loc[mask, "status"] = "ignored"
        working.loc[mask, "include_in_append"] = False
        working.loc[mask, "ignore_reason"] = reason
        working.loc[mask, "review_note"] = reason
        working.loc[mask, "money_flow"] = "neutral"
        working.loc[mask, "transaction_type"] = "transfer"
        working.loc[mask, "reimbursement_candidate"] = False
    working.loc[own_transfer & ~funding & ~noncash, "category"] = "Transfer"
    working.loc[funding, "category"] = "Funding"
    ignored_transfer_mask = ignored_transfer_mask | own_transfer | funding | noncash

    # A flow edit is authoritative: keep its amount sign correct automatically.
    amounts = pd.to_numeric(working["amount"], errors="coerce")
    amounts = amounts.mask(working["amount_missing"])
    working["amount"] = amounts
    flows = working["money_flow"].astype(str).str.lower()
    working.loc[flows.eq("outflow"), "amount"] = -amounts[flows.eq("outflow")].abs()
    working.loc[flows.eq("inflow"), "amount"] = amounts[flows.eq("inflow")].abs()

    non_inflow_mask = ~working["money_flow"].astype(str).eq("inflow") & ~ignored_transfer_mask
    working.loc[non_inflow_mask, "reimbursement_candidate"] = False
    working.loc[non_inflow_mask, "reimbursement_type"] = "unknown"
    working.loc[non_inflow_mask, "reimbursement_for"] = ""

    non_reimbursement_inflow_type = (
        working["money_flow"].astype(str).eq("inflow")
        & working["reimbursement_type"].astype(str).eq("cashback")
        & ~ignored_transfer_mask
    )
    working.loc[non_reimbursement_inflow_type, "reimbursement_candidate"] = False
    working.loc[non_reimbursement_inflow_type, "reimbursement_for"] = ""
    working.loc[non_reimbursement_inflow_type, "reimbursement_for_category"] = ""
    working.loc[
        non_reimbursement_inflow_type
        & working["reimbursement_type"].astype(str).eq("cashback"),
        "category",
    ] = "Cashbacks & Refunds"
    working = apply_category_flow_rules(working)

    paylah_repayment_mask = (
        working["source"].astype(str).eq("DBS_PAYLAH")
        & working["money_flow"].astype(str).eq("inflow")
        & ~ignored_transfer_mask
        & ~non_reimbursement_inflow_type
    )
    working.loc[
        paylah_repayment_mask
        & bool_series(working, "reimbursement_candidate")
        & working["reimbursement_type"].astype(str).isin(["", "unknown"]),
        "reimbursement_type",
    ] = "friend_repayment"
    working.loc[
        paylah_repayment_mask
        & bool_series(working, "reimbursement_candidate")
        & working["category"].astype(str).isin(["Transfer", "Others"]),
        "category",
    ] = "Reimbursements"
    standalone_reimbursement_mask = (
        working["money_flow"].astype(str).eq("inflow")
        & bool_series(working, "reimbursement_candidate")
        & ~working["reimbursement_type"].astype(str).isin(["cashback", "salary"])
        & ~working["category"].astype(str).isin(
            set(ALLOCATION_CATEGORIES) | REVIEW_REQUIRED_CATEGORIES | {"Transfer"}
        )
    )
    working.loc[standalone_reimbursement_mask, "category"] = "Reimbursements"
    working = apply_category_flow_rules(working)

    invalid_date_mask = working["date"].apply(lambda value: not is_valid_iso_date(value))
    working.loc[invalid_date_mask & ~ignored_transfer_mask, "status"] = "needs_review"
    working.loc[invalid_date_mask & ~ignored_transfer_mask, "include_in_append"] = False
    working.loc[invalid_date_mask & ~ignored_transfer_mask, "review_note"] = "Fix date before append"

    amount_parse_error_mask = bool_series(working, "amount_parse_error") & ~ignored_transfer_mask
    working.loc[amount_parse_error_mask, "status"] = "needs_review"
    working.loc[amount_parse_error_mask, "include_in_append"] = False
    working.loc[amount_parse_error_mask, "review_note"] = (
        "Fix amount before append; OCR produced an unreadable amount"
    )

    amounts = pd.to_numeric(working["amount"], errors="coerce").fillna(0)
    flows = working["money_flow"].astype(str).str.lower()
    sign_mismatch_mask = (
        (
            (flows.eq("outflow") & (amounts >= 0))
            | (flows.eq("inflow") & (amounts <= 0))
            | (flows.eq("neutral") & (amounts != 0))
        )
        & ~ignored_transfer_mask
        & ~amount_parse_error_mask
    )
    working.loc[sign_mismatch_mask, "status"] = "needs_review"
    working.loc[sign_mismatch_mask, "include_in_append"] = False
    working.loc[sign_mismatch_mask, "review_note"] = (
        "Fix amount sign or flow before append"
    )

    transaction_keys = review_duplicate_keys(working)
    overlap_duplicate_mask = overlapping_screenshot_duplicate_mask(working)
    duplicate_mask = (
        (
            working["transaction_hash"].duplicated(keep="first")
            | (transaction_keys.duplicated(keep="first") & transaction_keys.ne(""))
            | overlap_duplicate_mask
            | (
                allocation_collision_keys(working).duplicated(keep="first")
                & allocation_collision_keys(working).ne("")
            )
        )
        & ~ignored_transfer_mask
        & ~invalid_date_mask
        & ~amount_parse_error_mask
        & ~sign_mismatch_mask
    )
    duplicate_needs_review = duplicate_mask & ~bool_series(working, "duplicate_override")
    duplicate_needs_review = duplicate_needs_review & ~memory_excluded_mask
    working.loc[duplicate_needs_review, "status"] = "needs_review"
    working.loc[duplicate_needs_review, "include_in_append"] = False
    working.loc[duplicate_needs_review, "review_note"] = (
        "Duplicate-looking row, possibly from overlapping screenshots: leave append off, or tick separate duplicate? if this is truly another transaction"
    )
    duplicate_override_mask = duplicate_mask & bool_series(working, "duplicate_override")
    working.loc[duplicate_override_mask, "review_note"] = (
        "Marked as a separate duplicate; this row can be appended"
    )

    advisory_mask = (
        ~ignored_transfer_mask
        & ~invalid_date_mask
        & ~amount_parse_error_mask
        & ~sign_mismatch_mask
        & ~duplicate_needs_review
        & (
            bool_series(working, "anomaly_flag")
            | (working["confidence"] < 0.75)
            | working["source"].astype(str).eq("UNKNOWN")
            | (paylah_repayment_mask & ~bool_series(working, "reimbursement_candidate"))
        )
    )
    working.loc[advisory_mask, "status"] = "needs_review"
    unmarked_paylah_inflow = (
        paylah_repayment_mask
        & ~bool_series(working, "reimbursement_candidate")
        & working["review_note"].astype(str).str.strip().eq("")
    )
    working.loc[unmarked_paylah_inflow, "review_note"] = (
        "PayLah inflow: tick reimbursement only if this pays back an expense"
    )

    # Reimbursements are standalone inflows. Historical linkage fields are no
    # longer part of the workflow or exported ledger.
    working["reimbursement_for"] = ""
    working["reimbursement_for_category"] = ""
    working["linked_expense_hint"] = ""

    if previous_include is not None:
        manual_excluded = (
            previous_include.reindex(working.index, fill_value=True).eq(False)
            & ~ignored_transfer_mask
            & ~duplicate_needs_review
            & ~invalid_date_mask
            & ~already_recorded_mask
        )
        working.loc[manual_excluded, "include_in_append"] = False
        working.loc[manual_excluded, "status"] = "ignored"
        working.loc[manual_excluded, "ignore_reason"] = "Manually excluded"

    already_recorded_needs_prompt = (
        already_recorded_mask
        & ~working["duplicate_override"].apply(normalize_bool)
        & ~ignored_transfer_mask
        & ~invalid_date_mask
    )
    working.loc[already_recorded_needs_prompt, "include_in_append"] = False
    working.loc[already_recorded_needs_prompt, "status"] = "needs_review"
    working.loc[already_recorded_needs_prompt, "review_note"] = (
        "Already recorded in Google Sheets. Tick append? and separate duplicate? only if you still want to append it."
    )

    working.loc[memory_excluded_mask, "include_in_append"] = False
    working.loc[memory_excluded_mask, "status"] = "ignored"
    working.loc[memory_excluded_mask, "review_note"] = (
        "Matched local review memory: excluded."
    )

    allocations = working["category"].astype(str).isin(ALLOCATION_CATEGORIES)
    confirmed = allocations & bool_series(working, "allocation_confirmed")
    working.loc[allocations & ~confirmed, "transaction_type"] = "unknown"
    working.loc[confirmed & working["money_flow"].eq("outflow"), "transaction_type"] = "contribution"
    working.loc[~allocations, "allocation_confirmed"] = False
    # Reapply hard gates after memory, manual selection, and duplicate overrides.
    for reason, mask in statement_review_reasons(working).items():
        mask = mask & ~ignored_transfer_mask
        working.loc[mask, "include_in_append"] = False
        working.loc[mask, "status"] = "needs_review"
        working.loc[mask, "review_note"] = reason

    return working


def validation_summary(dataframe: pd.DataFrame) -> list[str]:
    if dataframe.empty:
        return []

    warnings = []
    included = dataframe[bool_series(dataframe, "include_in_append")].copy()
    if "confidence" in included and (included["confidence"] < 0.75).any():
        warnings.append(f"{int((included['confidence'] < 0.75).sum())} included row(s) have confidence below 0.75.")
    if "amount_parse_error" in included and bool_series(included, "amount_parse_error").any():
        warnings.append(
            f"{int(bool_series(included, 'amount_parse_error').sum())} included row(s) have amount parse errors."
        )
    if "amount" in included and (included["amount"] == 0).any():
        warnings.append(f"{int((included['amount'] == 0).sum())} included row(s) have zero amount.")
    if "source" in included and included["source"].astype(str).eq("UNKNOWN").any():
        warnings.append(f"{int(included['source'].astype(str).eq('UNKNOWN').sum())} included row(s) have unknown source.")
    if "reimbursement_candidate" in included and bool_series(included, "reimbursement_candidate").any():
        warnings.append(f"{int(bool_series(included, 'reimbursement_candidate').sum())} included row(s) are possible friend reimbursements.")
    if "anomaly_flag" in included and bool_series(included, "anomaly_flag").any():
        warnings.append(f"{int(bool_series(included, 'anomaly_flag').sum())} included row(s) were flagged by the anomaly agent.")

    return warnings


def anomaly_rows(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    mask = pd.Series(False, index=dataframe.index)
    if "anomaly_flag" in dataframe:
        mask = mask | bool_series(dataframe, "anomaly_flag")
    if "confidence" in dataframe:
        mask = mask | (dataframe["confidence"] < 0.75)
    if "date" in dataframe:
        mask = mask | dataframe["date"].apply(lambda value: not is_valid_iso_date(value))
    if "source" in dataframe:
        mask = mask | dataframe["source"].astype(str).eq("UNKNOWN")
    if "amount" in dataframe:
        mask = mask | (dataframe["amount"] == 0)
    if "status" in dataframe:
        mask = mask | dataframe["status"].astype(str).isin(["duplicate", "needs_review"])

    columns = [column for column in ANOMALY_COLUMNS if column in dataframe.columns]
    return dataframe.loc[mask, columns].copy()


def merge_review_edits(full_dataframe: pd.DataFrame, edited_review: pd.DataFrame) -> pd.DataFrame:
    merged = full_dataframe.copy()
    editable_columns = [
        column
        for column in REVIEW_COLUMNS
        if column in edited_review.columns and column in merged.columns
    ]

    edited_review = edited_review.copy()
    edited_review["_row_id"] = edited_review["_row_id"].astype(int)
    for _, row in edited_review.iterrows():
        row_id = int(row["_row_id"])
        previous_amount = pd.to_numeric(pd.Series([merged.at[row_id, "amount"]]), errors="coerce").iloc[0]
        edited_amount = pd.to_numeric(pd.Series([row.get("amount")]), errors="coerce").iloc[0]
        amount_changed = not (pd.isna(previous_amount) and pd.isna(edited_amount)) and (
            pd.isna(previous_amount) or pd.isna(edited_amount) or previous_amount != edited_amount
        )
        old_category = str(merged.at[row_id, "category"])
        for column in editable_columns:
            if column in {"amount_missing", "amount_parse_error", "amount_original", "status"}:
                continue
            merged.at[row_id, column] = row[column]
        if amount_changed:
            merged.at[row_id, "amount_missing"] = pd.isna(edited_amount)
            merged.at[row_id, "amount_parse_error"] = pd.isna(edited_amount) and pd.notna(row.get("amount"))
        if amount_changed or old_category != str(merged.at[row_id, "category"]):
            if normalize_bool(full_dataframe.loc[row_id].get("allocation_confirmed", False)):
                merged.at[row_id, "allocation_confirmed"] = False
            if str(merged.at[row_id, "transaction_type"]) == "contribution":
                merged.at[row_id, "transaction_type"] = "unknown"

    return apply_workflow_state(merged)


def blocking_issues(dataframe: pd.DataFrame) -> list[str]:
    issues = []
    if st.session_state.get("duplicate_check_error"):
        issues.append(
            "Past-append duplicate check failed. Fix Google Sheets/service-account access before appending."
        )

    included = dataframe[bool_series(dataframe, "include_in_append")].copy()
    if included.empty:
        issues.append("No included rows to append.")
        return issues

    for reason, mask in statement_review_reasons(included).items():
        if mask.any():
            issues.append(f"{reason} ({int(mask.sum())} included row(s)).")
    neutral = included["money_flow"].astype(str).eq("neutral") | included["category"].astype(str).eq("Transfer")
    if neutral.any():
        issues.append("Exclude neutral funding and own-account transfers before append.")

    invalid_dates = included["date"].apply(lambda value: not is_valid_iso_date(value))
    if invalid_dates.any():
        issues.append(
            f"Fix or exclude {int(invalid_dates.sum())} included row(s) with invalid/blank dates before append."
        )

    unknown_sources = included["source"].astype(str).eq("UNKNOWN")
    if unknown_sources.any():
        issues.append(
            f"Fix or exclude {int(unknown_sources.sum())} included row(s) with UNKNOWN source before append."
        )

    amount_parse_errors = bool_series(included, "amount_parse_error")
    if amount_parse_errors.any():
        issues.append(
            f"Fix {int(amount_parse_errors.sum())} included row(s) where the amount could not be parsed."
        )

    amounts = pd.to_numeric(included["amount"], errors="coerce").fillna(0)
    flows = included["money_flow"].astype(str).str.lower()
    wrong_signs = (
        (flows.eq("outflow") & (amounts >= 0))
        | (flows.eq("inflow") & (amounts <= 0))
        | (flows.eq("neutral") & (amounts != 0))
    )
    if wrong_signs.any():
        issues.append(
            f"Fix money-flow/sign mismatch for {int(wrong_signs.sum())} included row(s): outflow must be negative, inflow positive, neutral zero."
        )

    duplicate_hashes = included["transaction_hash"].duplicated(keep="first")
    transaction_keys = review_duplicate_keys(included)
    duplicate_keys = transaction_keys.duplicated(keep="first") & transaction_keys.ne("")
    duplicate_blockers = (duplicate_hashes | duplicate_keys) & ~included[
        "duplicate_override"
    ].apply(normalize_bool)
    if duplicate_blockers.any():
        issues.append(
            f"Untick duplicate-looking included row(s), or tick separate duplicate? if each one is genuinely separate."
        )

    return issues


def append_action_items(dataframe: pd.DataFrame) -> tuple[list[str], list[str]]:
    included = dataframe[bool_series(dataframe, "include_in_append")].copy()
    required: list[str] = []
    recommended: list[str] = []
    for reason, mask in statement_review_reasons(dataframe).items():
        if (mask & bool_series(dataframe, "include_in_append")).any():
            required.append(reason + ".")
        elif (mask & dataframe["status"].astype(str).eq("needs_review")).any():
            recommended.append(reason + ".")

    if st.session_state.get("duplicate_check_error"):
        required.append(
            "Past-append duplicate check failed. Check Google Sheets access, then rerun before appending."
        )

    already_recorded_rows = dataframe[
        dataframe["review_note"].astype(str).str.contains(
            "already recorded",
            case=False,
            na=False,
        )
        & ~dataframe["duplicate_override"].apply(normalize_bool)
    ]
    if not already_recorded_rows.empty:
        rows = _row_labels(already_recorded_rows)
        recommended.append(
            "Already recorded row(s) are unticked by default. "
            f"To append anyway, tick append? and separate duplicate?: {', '.join(rows)}."
        )

    if included.empty:
        return (
            ["Tick at least one row in the append? column."],
            recommended
            + ["Rows in Ignored and skipped rows will not be sent to Google Sheets."],
        )

    invalid_dates = included["date"].apply(lambda value: not is_valid_iso_date(value))
    if invalid_dates.any():
        rows = _row_labels(included[invalid_dates])
        required.append(
            f"Fix the date for {len(rows)} included row(s), or untick append?: {', '.join(rows)}."
        )

    unknown_sources = included["source"].astype(str).eq("UNKNOWN")
    if unknown_sources.any():
        rows = _row_labels(included[unknown_sources])
        required.append(
            f"Choose the source app for UNKNOWN row(s), or untick append?: {', '.join(rows)}."
        )

    amount_parse_errors = bool_series(included, "amount_parse_error")
    if amount_parse_errors.any():
        rows = _row_labels(included[amount_parse_errors])
        required.append(
            f"Fix amount text for {len(rows)} row(s), or untick append?: {', '.join(rows)}."
        )

    amounts = pd.to_numeric(included["amount"], errors="coerce").fillna(0)
    flows = included["money_flow"].astype(str).str.lower()
    wrong_signs = (
        (flows.eq("outflow") & (amounts >= 0))
        | (flows.eq("inflow") & (amounts <= 0))
        | (flows.eq("neutral") & (amounts != 0))
    )
    if wrong_signs.any():
        rows = _row_labels(included[wrong_signs])
        required.append(
            "Fix amount sign or flow. Outflow must be negative, inflow positive, neutral zero: "
            f"{', '.join(rows)}."
        )

    duplicate_hashes = included["transaction_hash"].duplicated(keep="first")
    transaction_keys = review_duplicate_keys(included)
    duplicate_keys = transaction_keys.duplicated(keep="first") & transaction_keys.ne("")
    duplicate_rows = included[
        (duplicate_hashes | duplicate_keys)
        & ~bool_series(included, "duplicate_override")
    ]
    if not duplicate_rows.empty:
        rows = _row_labels(duplicate_rows)
        required.append(
            f"Duplicate-looking row(s): untick append?, or tick separate duplicate? if it is truly another transaction: {', '.join(rows)}."
        )

    low_confidence = included["confidence"] < 0.75
    if low_confidence.any():
        rows = _row_labels(included[low_confidence])
        recommended.append(
            f"Check low-confidence row(s) for date, description, and amount: {', '.join(rows)}."
        )

    anomalies = bool_series(included, "anomaly_flag")
    if anomalies.any():
        rows = _row_labels(included[anomalies])
        recommended.append(
            f"Open Anomalies and sanity-check flagged row(s): {', '.join(rows)}."
        )

    if not required:
        recommended.insert(
            0,
            "Ready to append. Quickly scan date, amount sign, category, and reimbursement status before clicking the button.",
        )

    return required, recommended


def _row_labels(dataframe: pd.DataFrame, limit: int = 6) -> list[str]:
    labels = []
    for index, row in dataframe.head(limit).iterrows():
        description = str(row.get("description", "")).strip() or "no description"
        amount = row.get("amount", "")
        labels.append(f"row {index} ({description}, {amount})")
    remaining = len(dataframe) - len(labels)
    if remaining > 0:
        labels.append(f"+{remaining} more")
    return labels


def review_duplicate_keys(dataframe: pd.DataFrame) -> pd.Series:
    if dataframe.empty:
        return pd.Series(dtype=str)

    amounts = dataframe["amount"].apply(lambda value: abs(normalize_amount(value)))
    currencies = dataframe["currency"].apply(
        lambda value: str(value or "SGD").strip().upper()
    )
    flows = dataframe["money_flow"].apply(lambda value: str(value or "").strip().lower())
    keys = (
        dataframe["date"].astype(str).str.strip()
        + "|"
        + dataframe["source"].astype(str).str.upper().str.strip()
        + "|"
        + dataframe["description"].astype(str).str.upper().str.split().str.join(" ")
        + "|"
        + flows
        + "|"
        + amounts.map(lambda value: f"{value:.2f}")
        + "|"
        + currencies
    )
    references = dataframe.get(
        "transaction_reference", pd.Series("", index=dataframe.index)
    ).astype(str).str.upper().str.strip()
    keys = keys + references.where(references.eq(""), "|REF:" + references)
    has_required_fields = (
        dataframe["date"].astype(str).str.strip().ne("")
        & dataframe["description"].astype(str).str.strip().ne("")
    )
    return keys.where(has_required_fields, "")


def allocation_collision_keys(dataframe: pd.DataFrame) -> pd.Series:
    """Conservative monthly key for possible bank/brokerage legs of one allocation."""
    if dataframe.empty:
        return pd.Series(dtype=str)
    categories = dataframe["category"].astype(str)
    kinds = dataframe.get("transaction_type", pd.Series("", index=dataframe.index)).astype(str)
    allocations = categories.isin(ALLOCATION_CATEGORIES) | kinds.eq("contribution")
    amounts = dataframe["amount"].apply(lambda value: abs(normalize_amount(value)))
    dates = dataframe["date"].astype(str).str.strip()
    periods = dates.str.slice(0, 7)
    currencies = dataframe["currency"].apply(lambda value: str(value or "SGD").strip().upper())
    keys = "allocation|" + periods + "|" + amounts.map(lambda value: f"{value:.2f}") + "|" + currencies
    return keys.where(allocations & periods.str.match(r"^\d{4}-\d{2}$") & amounts.gt(0), "")


def compact_description(value: object) -> str:
    return " ".join(
        re.sub(r"[^A-Z0-9]+", " ", normalize_description(value)).split()
    )


def descriptions_look_like_same_transaction(left: object, right: object) -> bool:
    left_text = compact_description(left)
    right_text = compact_description(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    if min(len(left_text), len(right_text)) >= 6 and (
        left_text in right_text or right_text in left_text
    ):
        return True
    if SequenceMatcher(None, left_text, right_text).ratio() >= 0.86:
        return True

    left_tokens = set(left_text.split())
    right_tokens = set(right_text.split())
    overlap = len(left_tokens & right_tokens)
    return overlap >= 2 and overlap / len(left_tokens | right_tokens) >= 0.75


def overlapping_screenshot_duplicate_mask(dataframe: pd.DataFrame) -> pd.Series:
    """Flag likely duplicate rows from overlapping screenshots within one upload."""
    mask = pd.Series(False, index=dataframe.index)
    if dataframe.empty:
        return mask

    working = dataframe.copy()
    working["_amount"] = working["amount"].apply(
        lambda value: round(abs(normalize_amount(value)), 2)
    )
    working["_source"] = working["source"].astype(str).str.upper().str.strip()
    working["_flow"] = working["money_flow"].astype(str).str.lower().str.strip()
    working["_currency"] = working["currency"].astype(str).str.upper().str.strip()
    working["_date"] = working["date"].astype(str).str.strip()

    eligible = working[
        working["_date"].ne("")
        & working["_source"].ne("")
        & working["_source"].ne("UNKNOWN")
        & working["_flow"].isin(["inflow", "outflow"])
        & working["_amount"].gt(0)
        & working["description"].astype(str).str.strip().ne("")
    ]
    group_columns = ["_date", "_source", "_flow", "_currency", "_amount"]
    for _, group in eligible.groupby(group_columns, sort=False):
        retained_indices: list[int] = []
        for index, row in group.iterrows():
            reference = str(row.get("transaction_reference", "")).strip().upper()
            matched = False
            for retained_index in retained_indices:
                retained = working.loc[retained_index]
                retained_reference = str(
                    retained.get("transaction_reference", "")
                ).strip().upper()
                same_reference = bool(reference and reference == retained_reference)
                conflicting_references = bool(reference and retained_reference and reference != retained_reference)
                if same_reference or (not conflicting_references and descriptions_look_like_same_transaction(
                    row["description"], retained["description"]
                )):
                    matched = True
                    break
            if matched:
                mask.at[index] = True
            else:
                retained_indices.append(index)

    return mask


def overlapping_screenshot_duplicate_matches(dataframe: pd.DataFrame) -> dict[object, object]:
    matches: dict[object, object] = {}
    if dataframe.empty:
        return matches

    working = dataframe.copy()
    working["_amount"] = working["amount"].apply(
        lambda value: round(abs(normalize_amount(value)), 2)
    )
    working["_source"] = working["source"].astype(str).str.upper().str.strip()
    working["_flow"] = working["money_flow"].astype(str).str.lower().str.strip()
    working["_currency"] = working["currency"].astype(str).str.upper().str.strip()
    working["_date"] = working["date"].astype(str).str.strip()
    eligible = working[
        working["_date"].ne("")
        & working["_source"].ne("")
        & working["_source"].ne("UNKNOWN")
        & working["_flow"].isin(["inflow", "outflow"])
        & working["_amount"].gt(0)
        & working["description"].astype(str).str.strip().ne("")
    ]
    group_columns = ["_date", "_source", "_flow", "_currency", "_amount"]
    for _, group in eligible.groupby(group_columns, sort=False):
        retained_indices: list[object] = []
        for index, row in group.iterrows():
            reference = str(row.get("transaction_reference", "")).strip().upper()
            for retained_index in retained_indices:
                retained = working.loc[retained_index]
                retained_reference = str(
                    retained.get("transaction_reference", "")
                ).strip().upper()
                same_reference = bool(reference and reference == retained_reference)
                conflicting_references = bool(reference and retained_reference and reference != retained_reference)
                if same_reference or (not conflicting_references and descriptions_look_like_same_transaction(
                    row["description"], retained["description"]
                )):
                    matches[index] = retained_index
                    break
            if index not in matches:
                retained_indices.append(index)
    return matches


def duplicate_comparisons(dataframe: pd.DataFrame) -> list[dict[str, object]]:
    if dataframe.empty:
        return []

    comparisons = []
    seen_hashes: dict[str, object] = {}
    seen_keys: dict[str, object] = {}
    overlap_matches = overlapping_screenshot_duplicate_matches(dataframe)
    transaction_keys = review_duplicate_keys(dataframe)
    allocation_keys = allocation_collision_keys(dataframe)

    for index, row in dataframe.iterrows():
        reference_index = None
        reason = ""
        transaction_hash = str(row.get("transaction_hash", "")).strip()
        transaction_key = str(transaction_keys.at[index]).strip()
        allocation_key = str(allocation_keys.at[index]).strip()
        if transaction_hash and transaction_hash in seen_hashes:
            reference_index = seen_hashes[transaction_hash]
            reason = "Exact extracted transaction"
        elif transaction_key and transaction_key in seen_keys:
            reference_index = seen_keys[transaction_key]
            reason = "Same date, source, amount, flow, and description"
        elif index in overlap_matches:
            reference_index = overlap_matches[index]
            reason = "Same date, source, amount, flow, and similar description"
        elif allocation_key and allocation_key in seen_keys:
            reference_index = seen_keys[allocation_key]
            reason = "Possible second leg of the same monthly investment/savings allocation"

        if reference_index is not None:
            comparisons.append(
                {
                    "reference_index": reference_index,
                    "duplicate_index": index,
                    "reason": reason,
                }
            )
            continue
        if transaction_hash:
            seen_hashes[transaction_hash] = index
        if transaction_key:
            seen_keys[transaction_key] = index
        if allocation_key:
            seen_keys[allocation_key] = index

    return comparisons


def render_duplicate_comparisons(dataframe: pd.DataFrame) -> None:
    comparisons = duplicate_comparisons(dataframe)
    sheet_matches = [
        index
        for index, row in dataframe.iterrows()
        if str(row.get("matched_workbook_id", "")).strip()
        and str(row.get("matched_worksheet", "")).strip()
    ]
    total_comparisons = len(comparisons) + len(sheet_matches)
    if not total_comparisons:
        return

    with st.expander(
        f"Possible duplicates ({total_comparisons})",
        expanded=True,
    ):
        st.warning(
            "Possible duplicate transactions were found. Compare the evidence before "
            "recording either row separately."
        )
        fields = ["date", "source", "description", "amount", "currency", "money_flow", "category", "transaction_reference"]
        for comparison in comparisons:
            reference_index = comparison["reference_index"]
            duplicate_index = comparison["duplicate_index"]
            reference = dataframe.loc[reference_index]
            duplicate = dataframe.loc[duplicate_index]
            st.markdown("### Screenshot vs screenshot")
            st.caption(
                f"{comparison['reason']}. These came from "
                f"{reference.get('image_filename', 'screenshot 1')} and "
                f"{duplicate.get('image_filename', 'screenshot 2')}."
            )
            image_columns = st.columns(2)
            for column, row, label in [
                (image_columns[0], reference, "First occurrence"),
                (image_columns[1], duplicate, "Possible duplicate"),
            ]:
                image_path = Path(str(row.get("archive_path", "")))
                filename = str(row.get("image_filename", "Screenshot"))
                column.markdown(f"**{label}**")
                if image_path.is_file():
                    column.image(str(image_path), caption=filename, width=420)
                else:
                    column.info(f"Preview unavailable: {filename}")
            comparison_frame = pd.DataFrame(
                {
                    "field": fields,
                    "first occurrence": [reference.get(field, "") for field in fields],
                    "possible duplicate": [duplicate.get(field, "") for field in fields],
                }
            )
            st.dataframe(comparison_frame, hide_index=True, width="stretch")

        for index in sheet_matches:
            uploaded = dataframe.loc[index]
            try:
                recorded = json.loads(str(uploaded.get("matched_sheet_data", "{}")))
            except (json.JSONDecodeError, TypeError):
                recorded = {}
            st.markdown(
                f"### Screenshot vs Google Sheets\n"
                f"**Already recorded:** "
                f"{uploaded.get('matched_worksheet', '')} row {uploaded.get('matched_row', '')}"
            )
            comparison_frame = pd.DataFrame(
                {
                    "field": fields,
                    "uploaded screenshot": [uploaded.get(field, "") for field in fields],
                    "recorded sheet row": [recorded.get(field, "") for field in fields],
                }
            )
            evidence_column, match_column = st.columns([1, 1])
            image_path = Path(str(uploaded.get("archive_path", "")))
            if image_path.is_file():
                evidence_column.image(
                    str(image_path),
                    caption=str(uploaded.get("image_filename", "Uploaded screenshot")),
                    width=420,
                )
            else:
                evidence_column.info("The uploaded screenshot preview is unavailable.")
            match_column.dataframe(
                comparison_frame,
                hide_index=True,
                width="stretch",
            )
            if uploaded.get("matched_workbook_id"):
                match_column.link_button(
                    "Open recorded transaction",
                    worksheet_url(
                        str(uploaded.get("matched_workbook_id")),
                        int(float(uploaded.get("matched_worksheet_id", 0) or 0)),
                    ) if uploaded.get("matched_worksheet_id") else
                    f"https://docs.google.com/spreadsheets/d/{uploaded.get('matched_workbook_id')}/edit",
                )

def append_preview_metrics(dataframe: pd.DataFrame) -> dict[str, float | int]:
    included = dataframe[bool_series(dataframe, "include_in_append")].copy()
    income = expense = offsets = allocations = 0.0
    if not included.empty:
        included["category"] = included["category"].replace({
            "Reimbursement": "Reimbursements", "Bills": "Bills / Recurring Commitments"
        })
        eligible = pd.Series(True, index=included.index)
        for mask in statement_review_reasons(included).values():
            eligible &= ~mask
        transaction_types = included.get("transaction_type", pd.Series("", index=included.index)).astype(str)
        eligible &= ~transaction_types.eq("transfer")
        if "reimbursement_type" in included:
            eligible &= ~included["reimbursement_type"].astype(str).eq("self_transfer")
        included = included[eligible]
        amounts = pd.to_numeric(included["amount"], errors="coerce")
        categories = included["category"].astype(str)
        inflows = included["money_flow"].astype(str).eq("inflow") & amounts.gt(0)
        outflows = included["money_flow"].astype(str).eq("outflow") & amounts.lt(0)
        income = float(amounts[inflows & categories.isin(INCOME_CATEGORIES)].sum())
        expense = float(-amounts[outflows & categories.isin(
            VARIABLE_EXPENSE_CATEGORIES + FIXED_EXPENSE_CATEGORIES
        )].sum())
        offsets = float(amounts[inflows & categories.isin(EXPENSE_OFFSET_INFLOW_CATEGORIES)].sum())
        allocations = float(-amounts[outflows & categories.isin(ALLOCATION_CATEGORIES)
                                    & bool_series(included, "allocation_confirmed")].sum())
    operating = income - expense + offsets
    return {
        "included": int(len(included)),
        "income": income,
        "expense": expense,
        "offsets": offsets,
        "operating": operating,
        "operating_surplus": operating,
        "allocations": allocations,
        "final": operating - allocations,
        "final_surplus": operating - allocations,
        "gross_spend": expense,
        "net_spend": expense - offsets,
    }


def append_date_options(dataframe: pd.DataFrame) -> list[str]:
    if dataframe.empty or "date" not in dataframe:
        return []
    dates = sorted(
        {
            str(value).strip()
            for value in dataframe["date"]
            if is_valid_iso_date(value)
        }
    )
    return dates


def filter_to_append_dates(
    dataframe: pd.DataFrame,
    selected_dates: list[str],
) -> pd.DataFrame:
    if dataframe.empty or not selected_dates:
        filtered = dataframe.copy()
        if "include_in_append" in filtered:
            filtered["include_in_append"] = False
        return filtered

    selected = set(selected_dates)
    filtered = dataframe.copy()
    date_is_selected = filtered["date"].astype(str).str.strip().isin(selected)
    filtered.loc[~date_is_selected, "include_in_append"] = False
    return filtered


def is_valid_iso_date(value: object) -> bool:
    try:
        datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def render_merchant_rules_screen() -> None:
    st.title("Merchant rules")
    st.caption("These local rules reuse your chosen category when the same merchant appears again.")
    rules = merchant_rule_records()
    if not rules:
        st.info("No merchant rules have been learned yet. Review and append a categorized transaction to create one.")
        return

    rules_dataframe = pd.DataFrame(rules)
    rules_dataframe["forget"] = False
    edited = st.data_editor(
        rules_dataframe,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_order=[
            "merchant",
            "source",
            "flow",
            "category",
            "uses",
            "updated_at",
            "forget",
            "rule_key",
        ],
        column_config={
            "merchant": st.column_config.TextColumn("merchant", disabled=True),
            "source": st.column_config.TextColumn("source", disabled=True),
            "flow": st.column_config.TextColumn("flow", disabled=True),
            "category": st.column_config.SelectboxColumn(
                "category", options=CATEGORY_OPTIONS
            ),
            "uses": st.column_config.NumberColumn("uses", disabled=True),
            "updated_at": st.column_config.TextColumn("last updated", disabled=True),
            "forget": st.column_config.CheckboxColumn("forget this rule?"),
            "rule_key": None,
        },
        key="merchant_rules_editor",
    )
    if st.button("Save merchant rules", type="primary"):
        changed = update_merchant_rules(edited.to_dict(orient="records"))
        if changed:
            st.success(f"Saved {changed} merchant rule change(s).")
            st.rerun()
        else:
            st.info("No merchant rule changes to save.")


def render_complete_step() -> None:
    st.subheader("Complete")
    links = st.session_state.get("last_append_tab_links", [])
    audit_rows = st.session_state.get("last_append_audit_rows", [])
    if not links and not audit_rows:
        st.info("No completed append for this upload yet.")
        st.caption("Review the transactions, then use Confirm to append them.")
        return
    if audit_rows:
        appended = sum(int(row.get("appended", 0)) for row in audit_rows)
        skipped = sum(int(row.get("skipped_duplicates", 0)) for row in audit_rows)
        failed = sum(not bool(row.get("verified", False)) for row in audit_rows)
        columns = st.columns(3)
        columns[0].metric("Appended", appended)
        columns[1].metric("Skipped", skipped)
        columns[2].metric("Failed", failed)
        with st.expander("Append audit", expanded=False):
            st.dataframe(pd.DataFrame(audit_rows), hide_index=True, width="stretch")
    if links:
        st.markdown("### Open updated Google Sheets")
        columns = st.columns(min(3, len(links)))
        for index, link in enumerate(links):
            columns[index % len(columns)].link_button(
                link["label"], link["url"], width="stretch"
            )


def render_confirm_step(current_df: pd.DataFrame, *, check_duplicates: bool) -> None:
    st.subheader("Confirm")
    date_options = append_date_options(current_df)
    selected_append_dates = st.multiselect(
        "Dates to append",
        options=date_options,
        default=date_options,
        help="Remove dates to skip them for this append only.",
        key=f"append_dates_{st.session_state.get('upload_batch_id', 'default')}",
    )
    append_df = filter_to_append_dates(current_df, selected_append_dates)
    preview = append_preview_metrics(append_df)
    counts = st.columns(4)
    counts[0].metric("Will append", preview["included"])
    counts[1].metric("Needs attention", int((guided_workflow_state(current_df) == "needs_attention").sum()))
    counts[2].metric("Excluded", int((guided_workflow_state(current_df) == "excluded").sum()))
    counts[3].metric("Dates", len(selected_append_dates))
    statement = st.columns(3)
    statement[0].metric("Income", f"${preview['income']:,.2f}")
    statement[1].metric("Expenditure", f"${preview['expense']:,.2f}")
    statement[2].metric("Offsets", f"${preview['offsets']:,.2f}")
    result = st.columns(3)
    result[0].metric("Operating result", f"${preview['operating']:,.2f}")
    result[1].metric("Allocations", f"${preview['allocations']:,.2f}")
    result[2].metric("Final result", f"${preview['final']:,.2f}")

    if not check_duplicates:
        manual_recovery_mode = st.checkbox(
            "Manual recovery mode: append without duplicate checking",
            value=False,
            help="Use only after manually verifying every included row.",
        )
        st.error("Duplicate checking is off.")
    else:
        manual_recovery_mode = False

    blockers = blocking_issues(append_df)
    if not check_duplicates and not manual_recovery_mode:
        blockers.append("Duplicate checking is off and manual recovery is not confirmed.")
    required_actions, recommended_actions = append_action_items(append_df)
    with st.container(border=True):
        if blockers or required_actions:
            st.error("Resolve the remaining items before appending.")
            for action in dict.fromkeys([*blockers, *required_actions]):
                st.write(f"- {action}")
        else:
            st.success("Ready to append the included transactions.")
        if recommended_actions:
            with st.expander("Recommended checks", expanded=False):
                for action in recommended_actions:
                    st.write(f"- {action}")

    active_batch_id = st.session_state.get("active_batch_id")
    already_completed = bool(active_batch_id) and (
        st.session_state.get("last_append_completed_batch_id") == active_batch_id
    )
    append_clicked = st.button(
        "Append transactions",
        type="primary",
        width="stretch",
        disabled=already_completed,
    )
    if not append_clicked:
        if already_completed:
            st.info("This upload has already been appended.")
        return
    if blockers or required_actions:
        st.error(
            "Nothing was written. Return to Review and resolve the required items "
            "listed above, then try again."
        )
        return

    st.session_state.append_in_progress = True
    try:
        reviewed = current_df.copy()
        if check_duplicates:
            included_before_refresh = int(
                reviewed["include_in_append"].apply(normalize_bool).sum()
            )
            with st.spinner("Running the final Google Sheets duplicate check..."):
                reviewed = apply_existing_sheet_duplicate_check(
                    apply_workflow_state(reviewed), force_refresh=True
                )
            st.session_state.edited_transactions_df = reviewed
            st.session_state.transactions_df = reviewed
            included_after_refresh = int(
                reviewed["include_in_append"].apply(normalize_bool).sum()
            )
            if included_after_refresh < included_before_refresh:
                duplicate_count = included_before_refresh - included_after_refresh
                st.session_state.final_duplicate_notice = (
                    f"The final check found {duplicate_count} transaction(s) already in "
                    "Google Sheets. They were unticked; compare them before continuing."
                )
                st.session_state.pending_workflow_stage = "Review"
                st.rerun()
        selected = set(selected_append_dates)
        dataframe = reviewed[
            reviewed["include_in_append"].apply(normalize_bool)
            & reviewed["date"].astype(str).str.strip().isin(selected)
        ].copy()
        final_blockers = blocking_issues(dataframe)
        if final_blockers:
            raise ValueError(" ".join(final_blockers))
        sheet = SheetClient.from_env()
        audits = sheet.append_transactions_by_period(
            dataframe, skip_duplicates=check_duplicates
        )
        appended = sum(audit.appended for audit in audits)
        skipped = sum(audit.skipped_duplicates for audit in audits)
        if not all(audit.verified for audit in audits):
            raise RuntimeError("Some appended rows could not be verified in Google Sheets.")
        save_review_memory(
            reviewed[reviewed["date"].astype(str).str.strip().isin(selected)].copy()
        )
        st.session_state.last_append_tab_links = [
            {
                "label": f"Open {audit.month} {audit.year}",
                "url": worksheet_url(audit.spreadsheet_id, audit.worksheet_id),
            }
            for audit in audits if audit.appended and audit.verified
        ]
        for spreadsheet_id in sorted({audit.spreadsheet_id for audit in audits if audit.verified}):
            destination = sheet.client.open_by_key(spreadsheet_id)
            summary = destination.worksheet("Summary")
            st.session_state.last_append_tab_links.append({
                "label": f"Open {destination.title} Summary",
                "url": worksheet_url(spreadsheet_id, summary.id),
            })
        audit_rows = [
            {
                "year_file": audit.year,
                "month_tab": audit.month,
                "attempted": audit.attempted,
                "appended": audit.appended,
                "skipped_duplicates": audit.skipped_duplicates,
                "verified": audit.verified,
            }
            for audit in audits
        ]
        st.session_state.last_append_audit_rows = audit_rows
        persist_append_completion(dataframe, audits)
        st.session_state.last_append_completed_batch_id = active_batch_id
        st.session_state.existing_sheet_match_cache = {}
        st.session_state.pending_workflow_stage = "Complete"
        if skipped:
            st.toast(f"Appended {appended}; skipped {skipped} duplicate(s).")
        else:
            st.toast(f"Appended and verified {appended} transaction(s).")
        st.rerun()
    except Exception as exc:
        st.error(f"Could not append to Google Sheets: {exc}")
    finally:
        st.session_state.append_in_progress = False


with st.sidebar:
    st.header("Workspace")
    app_view = st.radio("Workspace", ["Transactions", "Merchant rules"])
    with st.expander("Advanced settings", expanded=False):
        model = st.text_input("OpenAI model", value=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
        st.write("Google Drive folder")
        st.code(os.getenv("GOOGLE_DRIVE_FOLDER_ID", "Set GOOGLE_DRIVE_FOLDER_ID in .env"), language=None)
        if os.getenv("GOOGLE_SHEET_ID") and not os.getenv("GOOGLE_DRIVE_FOLDER_ID"):
            st.write("Fallback Google Sheet")
            st.code(os.getenv("GOOGLE_SHEET_ID", ""), language=None)
        st.write("Screenshot archive")
        st.code(str(ARCHIVE_DIR), language=None)
        st.write("Review memory")
        st.code(str(memory_path()), language=None)
        archive_screenshots = st.checkbox("Archive uploaded screenshots locally", value=True)
        save_raw_text = st.checkbox("Save raw OCR text to Google Sheets", value=False)
        check_duplicates = st.checkbox("Check duplicates before append", value=True)
        st.write("Recommendation agents")
        run_category_agent = st.checkbox("Category agent", value=True)
        run_anomaly_agent = st.checkbox("Anomaly agent", value=True)
        run_insight_agent = st.checkbox("Insight agent", value=True)
        if st.button("Refresh Google Sheets structure", width="stretch"):
            try:
                with st.spinner("Refreshing year workbooks, dropdowns, and summaries..."):
                    refresh_audits = SheetClient.from_env().refresh_existing_workbook_structures()
                if not refresh_audits:
                    st.warning("No year workbook with month tabs was found.")
                else:
                    migrated = sum(audit.migrated_categories for audit in refresh_audits)
                    st.success(
                        f"Refreshed {len(refresh_audits)} year workbook(s); "
                        f"updated {migrated} historical category label(s)."
                    )
                    for audit in refresh_audits:
                        st.link_button(
                            f"Open {audit.year} Summary",
                            worksheet_url(
                                audit.spreadsheet_id,
                                audit.summary_worksheet_id,
                            ),
                            width="stretch",
                        )
            except Exception as exc:
                st.error(f"Could not refresh Google Sheets: {exc}")

if app_view == "Merchant rules":
    render_merchant_rules_screen()
    st.stop()

pending_stage = st.session_state.pop("pending_workflow_stage", None)
if pending_stage:
    st.session_state.workflow_nav = pending_stage
elif "workflow_nav" not in st.session_state:
    st.session_state.workflow_nav = (
        "Review" if "transactions_df" in st.session_state else "Upload"
    )
st.markdown(
    """
    <div class="workflow-heading">
        <strong>ExpenditureAI</strong>
        <span>UOB / DBS PayLah screenshots &rarr; reviewed transactions &rarr; year/month Google Sheets</span>
    </div>
    """,
    unsafe_allow_html=True,
)
workflow_stage = st.segmented_control(
    "Upload workflow",
    ["Upload", "Review", "Confirm", "Complete"],
    format_func=lambda stage: {
        "Upload": "1  UPLOAD",
        "Review": "2  REVIEW",
        "Confirm": "3  CONFIRM",
        "Complete": "4  COMPLETE",
    }[stage],
    key="workflow_nav",
    label_visibility="collapsed",
    width="stretch",
)
st.session_state.workflow_stage = workflow_stage or "Upload"
workflow_context = {
    "Review": "Correct uncertain rows, compare duplicates, and choose exactly what should be recorded.",
    "Confirm": "Check the final financial effect. Google Sheets is not changed until you append.",
    "Complete": "Review the append audit and open every affected month tab or annual Summary.",
}
if workflow_stage in workflow_context:
    st.markdown(
        f'<div class="workflow-context">{workflow_context[workflow_stage]}</div>',
        unsafe_allow_html=True,
    )

if workflow_stage == "Upload":
    st.subheader("Upload inbox")
    recent_batches = batch_store().list_batches()[:5]
else:
    recent_batches = []
if workflow_stage == "Upload" and recent_batches:
    with st.expander("Recent upload batches", expanded=False):
        st.dataframe(pd.DataFrame([
            {
                "created": batch.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                "status": batch.status.value,
                "screenshots": len(batch.screenshots),
                "transactions": len(batch.transactions),
                "append attempts": len(batch.append_audits),
            }
            for batch in recent_batches
        ]), hide_index=True, use_container_width=True)
        batch_labels = {
            f"{batch.created_at.astimezone():%Y-%m-%d %H:%M} | {batch.status.value} | {len(batch.transactions)} transactions": batch
            for batch in recent_batches
        }
        selected_batch_label = st.selectbox("Inspect batch", list(batch_labels))
        selected_batch = batch_labels[selected_batch_label]
        if selected_batch.transactions:
            st.dataframe(pd.DataFrame([
                {
                    "state": transaction.status.value,
                    "date": transaction.data.get("date", ""),
                    "description": transaction.data.get("description", ""),
                    "category": transaction.data.get("category", ""),
                    "amount": transaction.data.get("amount", ""),
                    "decision": (transaction.user_decision or {}).get("action", ""),
                }
                for transaction in selected_batch.transactions
            ]), hide_index=True, use_container_width=True)
        for audit in selected_batch.append_audits:
            for destination in audit.affected_destinations:
                if destination.startswith("https://"):
                    st.link_button("Open saved destination", destination)
if workflow_stage == "Upload":
    uploaded_files = st.file_uploader(
        "Upload UOB TMRW, DBS PayLah, or DBS banking screenshots",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
    )
    consent = st.checkbox(
        "I understand screenshots are sent to OpenAI for extraction, archived locally if enabled, and confirmed rows are saved to Google Sheets.",
        value=True,
    )
else:
    uploaded_files = []
    consent = True

current_upload_signature = upload_signature(uploaded_files)
if uploaded_files:
    upload_is_processed = (
        current_upload_signature == st.session_state.get("processed_upload_signature")
    )
    with st.expander(
        f"Selected screenshots ({len(uploaded_files)})",
        expanded=not upload_is_processed,
    ):
        render_upload_preview(uploaded_files)
        states = st.session_state.get("screenshot_states", {})
        if states:
            st.dataframe(
                pd.DataFrame([
                    {"screenshot": filename, "status": state["status"]}
                    for filename, state in states.items()
                ]),
                hide_index=True,
                width="stretch",
            )

process_upload = bool(
    uploaded_files
    and consent
    and current_upload_signature != st.session_state.get("processed_upload_signature")
)

if process_upload:
    if not os.getenv("OPENAI_API_KEY"):
        st.error("Set OPENAI_API_KEY in your .env file first.")
        st.stop()

    with st.spinner("Reading screenshots and checking your transaction history..."):
        create_persistent_batch(uploaded_files)
        st.session_state.upload_batch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        st.session_state.insight_overview = ""
        st.session_state.transactions_df = extract_all(
            uploaded_files,
            model,
            archive_screenshots=archive_screenshots,
            save_raw_text=save_raw_text,
            run_category_agent=run_category_agent,
            run_anomaly_agent=run_anomaly_agent,
            run_insight_agent=run_insight_agent,
        )
        st.session_state.transactions_df = apply_existing_sheet_duplicate_check(
            apply_workflow_state(st.session_state.transactions_df)
        )
        st.session_state.pop("edited_transactions_df", None)
        st.session_state.pending_workflow_stage = "Review"
        st.session_state.pop("last_append_tab_links", None)
        st.session_state.pop("last_append_audit_rows", None)
        persist_batch_results(st.session_state.transactions_df)
        st.session_state.processed_upload_signature = current_upload_signature
        st.rerun()

failed_names = [
    filename for filename, state in st.session_state.get("screenshot_states", {}).items()
    if state.get("status") == "Failed"
]
if workflow_stage == "Upload" and failed_names:
    st.error(f"{len(failed_names)} screenshot(s) failed: {', '.join(failed_names)}")
    if st.button("Retry failed screenshots", type="secondary"):
        retry_files = [item for item in uploaded_files if item.name in failed_names]
        previous_states = dict(st.session_state.get("screenshot_states", {}))
        recovered = extract_all(
            retry_files,
            model,
            archive_screenshots=archive_screenshots,
            save_raw_text=save_raw_text,
            run_category_agent=run_category_agent,
            run_anomaly_agent=run_anomaly_agent,
            run_insight_agent=run_insight_agent,
        )
        retry_states = dict(st.session_state.get("screenshot_states", {}))
        previous_states.update(retry_states)
        st.session_state.screenshot_states = previous_states
        existing = st.session_state.get("transactions_df", pd.DataFrame())
        if not existing.empty and "image_filename" in existing:
            existing = existing[~existing["image_filename"].isin(failed_names)]
        st.session_state.transactions_df = apply_existing_sheet_duplicate_check(
            apply_workflow_state(pd.concat([existing, recovered], ignore_index=True))
        )
        st.session_state.edited_transactions_df = st.session_state.transactions_df.copy()
        st.session_state.pending_workflow_stage = "Review"
        persist_batch_results(st.session_state.transactions_df)
        st.rerun()

if "transactions_df" not in st.session_state and workflow_stage != "Upload":
    st.info("Upload and process at least one screenshot before continuing to this step.")
    st.stop()

if "transactions_df" in st.session_state:
    st.session_state.transactions_df = apply_workflow_state(st.session_state.transactions_df)
    if "edited_transactions_df" not in st.session_state:
        st.session_state.edited_transactions_df = st.session_state.transactions_df.copy()

    if workflow_stage == "Upload":
        st.stop()
    if workflow_stage == "Confirm":
        render_confirm_step(
            apply_workflow_state(st.session_state.edited_transactions_df),
            check_duplicates=check_duplicates,
        )
        st.stop()
    if workflow_stage == "Complete":
        render_complete_step()
        st.stop()

    st.subheader("Review")
    if st.session_state.pop("final_duplicate_notice", None):
        st.warning(
            "The final check found transaction(s) already in Google Sheets. "
            "They are unticked below so you can inspect the match before continuing."
        )
    if st.session_state.transactions_df.empty:
        st.info("No transactions were extracted. Try a clearer screenshot or crop closer to the transaction list.")
    else:
        if st.session_state.get("insight_overview"):
            st.info(f"Insight agent: {st.session_state.insight_overview}")

        st.subheader("Transaction Review")
        st.session_state.transactions_df["workflow_state"] = guided_workflow_state(
            st.session_state.transactions_df
        )
        render_duplicate_comparisons(st.session_state.transactions_df)
        review_dataframe = st.session_state.transactions_df[
            st.session_state.transactions_df["workflow_state"].eq("needs_attention")
        ].copy()
        review_dataframe["_row_id"] = review_dataframe.index
        review_color_scale = amount_intensity_scale(review_dataframe)
        review_dataframe["flow_signal"] = review_dataframe.apply(
            lambda row: "Amount missing" if normalize_bool(row.get("amount_missing", False)) else flow_signal(
                row.get("money_flow"), row.get("amount"), review_color_scale
            ),
            axis=1,
        )
        review_columns = ["_row_id", "flow_signal"] + [
            column for column in REVIEW_COLUMNS if column in review_dataframe.columns
        ]

        if review_dataframe.empty:
            st.success("Nothing needs attention. Review the ready rows, then append when satisfied.")
            st.session_state.edited_transactions_df = apply_workflow_state(
                st.session_state.transactions_df
            )
        else:
            issue_options = {
                f"{row.get('date', '')} | {row.get('description', '')} | ${abs(float(row.get('amount', 0) or 0)):,.2f}": int(index)
                for index, row in review_dataframe.iterrows()
            }
            selected_issue = st.selectbox("Resolve attention item", list(issue_options))
            selected_index = issue_options[selected_issue]
            selected_row = st.session_state.transactions_df.loc[selected_index]
            evidence_col, decision_col = st.columns([1, 1])
            with evidence_col:
                matching_upload = next(
                    (item for item in uploaded_files if item.name == selected_row.get("image_filename")),
                    None,
                )
                if matching_upload is not None:
                    st.image(matching_upload, caption=matching_upload.name, width=420)
                else:
                    st.info("Screenshot preview is unavailable; the archived path is retained in the batch record.")
            with decision_col:
                st.markdown(f"**{selected_row.get('description', 'Transaction')}**")
                st.write(selected_row.get("review_note") or "Review the extracted fields.")
                if selected_row.get("matched_worksheet"):
                    st.markdown(
                        f"**Recorded match:** {selected_row.get('matched_worksheet')} "
                        f"row {selected_row.get('matched_row')}"
                    )
                    try:
                        matched_data = json.loads(str(selected_row.get("matched_sheet_data", "{}")))
                    except json.JSONDecodeError:
                        matched_data = {}
                    if matched_data:
                        st.dataframe(pd.DataFrame([{
                            field: matched_data.get(field, "")
                            for field in ["date", "description", "amount", "category", "money_flow"]
                        }]), hide_index=True, use_container_width=True)
                    if selected_row.get("matched_workbook_id"):
                        st.link_button(
                            "Open recorded transaction",
                            worksheet_url(
                                str(selected_row.get("matched_workbook_id")),
                                int(float(selected_row.get("matched_worksheet_id", 0) or 0)),
                            ) if selected_row.get("matched_worksheet_id") else
                            f"https://docs.google.com/spreadsheets/d/{selected_row.get('matched_workbook_id')}/edit",
                        )
                recommendation = st.session_state.get("recommendations", {}).get(
                    str(selected_row.get("transaction_hash", "")), {}
                )
                if recommendation:
                    st.caption(
                        f"Recommendation: {recommendation.get('category', '')} / "
                        f"{recommendation.get('money_flow', '')} "
                        f"({float(recommendation.get('confidence', 0)):.0%} confidence)"
                    )
                    for evidence in recommendation.get("evidence", []):
                        st.write(f"- {evidence}")
                action_cols = st.columns(3)
                if action_cols[0].button("Use suggestion", use_container_width=True):
                    if recommendation and recommendation.get("source") != "deterministic_fallback":
                        st.session_state.transactions_df.at[selected_index, "category"] = recommendation["category"]
                        st.session_state.transactions_df.at[selected_index, "money_flow"] = recommendation["money_flow"]
                    st.session_state.transactions_df.at[selected_index, "include_in_append"] = True
                    st.session_state.transactions_df.at[selected_index, "decision_source"] = "user"
                    st.session_state.setdefault("batch_user_decisions", {})[
                        str(selected_row.get("transaction_hash", ""))
                    ] = {"action": "use_suggestion", "at": datetime.now().isoformat()}
                    st.session_state.transactions_df = apply_workflow_state(st.session_state.transactions_df)
                    persist_batch_results(
                        st.session_state.transactions_df,
                        refresh_recommendations=False,
                    )
                    st.rerun()
                if action_cols[1].button("Keep excluded", use_container_width=True):
                    st.session_state.transactions_df.at[selected_index, "include_in_append"] = False
                    st.session_state.transactions_df.at[selected_index, "status"] = "ignored"
                    st.session_state.transactions_df.at[selected_index, "ignore_reason"] = "User excluded in guided inbox"
                    st.session_state.transactions_df.at[selected_index, "decision_source"] = "user"
                    st.session_state.setdefault("batch_user_decisions", {})[
                        str(selected_row.get("transaction_hash", ""))
                    ] = {"action": "keep_excluded", "at": datetime.now().isoformat()}
                    persist_batch_results(
                        st.session_state.transactions_df,
                        refresh_recommendations=False,
                    )
                    st.rerun()
                duplicate_issue = bool(re.search(
                    "duplicate|already recorded", str(selected_row.get("review_note", "")), re.I
                ))
                if action_cols[2].button(
                    "Record separately", disabled=not duplicate_issue, use_container_width=True
                ):
                    st.session_state.transactions_df.at[selected_index, "duplicate_override"] = True
                    st.session_state.transactions_df.at[selected_index, "include_in_append"] = True
                    st.session_state.transactions_df.at[selected_index, "decision_source"] = "user"
                    st.session_state.setdefault("batch_user_decisions", {})[
                        str(selected_row.get("transaction_hash", ""))
                    ] = {"action": "record_separately", "at": datetime.now().isoformat()}
                    st.session_state.transactions_df = apply_existing_sheet_duplicate_check(
                        apply_workflow_state(st.session_state.transactions_df)
                    )
                    persist_batch_results(
                        st.session_state.transactions_df,
                        refresh_recommendations=False,
                    )
                    st.rerun()
            st.caption(
                "Yellow rows need your action before append. Resolved rows are shaded by flow: green inflow, red outflow, darker means larger."
            )
            review_editor_data = review_dataframe[review_columns].style.apply(
                highlight_review_rows,
                scale=review_color_scale,
                axis=1,
            )
            edited = st.data_editor(
                review_editor_data,
                column_order=review_columns,
                hide_index=True,
                num_rows="fixed",
                use_container_width=True,
                height=min(650, 120 + len(review_dataframe) * 54),
                column_config={
                    "_row_id": st.column_config.NumberColumn(
                        "row",
                        disabled=True,
                        help="Internal row number used to keep edits aligned.",
                    ),
                    "flow_signal": st.column_config.TextColumn(
                        "flow signal",
                        disabled=True,
                        help="Color cue for money flow and amount size.",
                    ),
                    "include_in_append": st.column_config.CheckboxColumn(
                        "append?",
                        help="Only checked rows will be appended to Google Sheets.",
                    ),
                    "status": st.column_config.TextColumn(
                        "status",
                        disabled=True,
                        help="ready, needs_review, duplicate, or ignored.",
                    ),
                    "category": st.column_config.SelectboxColumn(
                        "category",
                        options=CATEGORY_OPTIONS,
                        help="AI suggested category. Change it here if it is wrong.",
                    ),
                    "amount_original": st.column_config.TextColumn(
                        "OCR amount",
                        disabled=True,
                        help="Original amount text from extraction before normalization.",
                    ),
                    "amount_parse_error": st.column_config.CheckboxColumn(
                        "amount issue?",
                        disabled=True,
                        help="True when the amount could not be parsed. Edit amount to fix it.",
                    ),
                    "amount_missing": st.column_config.CheckboxColumn(
                        "amount missing?",
                        disabled=True,
                    ),
                    "allocation_confirmed": st.column_config.CheckboxColumn(
                        "new capital confirmed?",
                        help="I confirm this is new external capital counted once, not both bank and brokerage legs, reinvestment, or rebalancing. Select append? to include it.",
                    ),
                    "duplicate_override": st.column_config.CheckboxColumn(
                        "separate duplicate?",
                        help="Tick only when this duplicate-looking row is genuinely a separate transaction.",
                    ),
                    "reimbursement_candidate": st.column_config.CheckboxColumn(
                        "reimbursement?",
                        help="Only inflow rows can be reimbursements. Outflows are reset to false.",
                    ),
                    "reimbursement_type": st.column_config.SelectboxColumn(
                        "reimbursement type",
                        options=[
                            "friend_repayment",
                            "merchant_refund",
                            "cashback",
                            "salary",
                            "self_transfer",
                            "unknown",
                        ],
                    ),
                    "money_flow": st.column_config.SelectboxColumn(
                        "flow",
                        options=["outflow", "inflow", "neutral", "unknown"],
                    ),
                },
                key=f"review_editor_{st.session_state.get('upload_batch_id', 'default')}",
            )
            st.session_state.edited_transactions_df = merge_review_edits(
                st.session_state.transactions_df,
                edited,
            )
            persist_batch_results(
                st.session_state.edited_transactions_df,
                refresh_recommendations=False,
            )

        current_df = st.session_state.edited_transactions_df
        current_df["workflow_state"] = guided_workflow_state(current_df)
        ready_rows = current_df[current_df["workflow_state"].isin(["ready", "approved"])]
        excluded_rows = current_df[current_df["workflow_state"].eq("excluded")]
        with st.expander(f"Ready ({len(ready_rows)})", expanded=review_dataframe.empty):
            if ready_rows.empty:
                st.caption("No routine transactions are ready yet.")
            else:
                ready_amounts = pd.to_numeric(ready_rows["amount"], errors="coerce").fillna(0)
                ready_metrics = st.columns(3)
                ready_metrics[0].metric("Transactions", len(ready_rows))
                ready_metrics[1].metric("Inflows", f"${ready_amounts[ready_amounts > 0].sum():,.2f}")
                ready_metrics[2].metric("Outflows", f"${abs(ready_amounts[ready_amounts < 0].sum()):,.2f}")
                ready_editor = ready_rows.copy()
                ready_editor["_row_id"] = ready_editor.index
                ready_columns = [
                    "_row_id",
                    "include_in_append",
                    "date",
                    "description",
                    "category",
                    "amount",
                    "money_flow",
                    "allocation_confirmed",
                    "duplicate_override",
                ]
                ready_columns = [column for column in ready_columns if column in ready_editor.columns]
                edited_ready = st.data_editor(
                    ready_editor[ready_columns],
                    column_order=ready_columns,
                    hide_index=True,
                    num_rows="fixed",
                    use_container_width=True,
                    height=min(520, 110 + len(ready_editor) * 46),
                    column_config={
                        "_row_id": st.column_config.NumberColumn("row", disabled=True),
                        "include_in_append": st.column_config.CheckboxColumn(
                            "append?",
                            help="Only checked rows will be appended to Google Sheets.",
                        ),
                        "date": st.column_config.TextColumn("date"),
                        "description": st.column_config.TextColumn("description"),
                        "category": st.column_config.SelectboxColumn(
                            "category",
                            options=CATEGORY_OPTIONS,
                            help="Change the category here if the suggestion is wrong.",
                        ),
                        "amount": st.column_config.NumberColumn("amount", format="%.2f"),
                        "money_flow": st.column_config.SelectboxColumn(
                            "flow",
                            options=["outflow", "inflow", "neutral", "unknown"],
                        ),
                        "allocation_confirmed": st.column_config.CheckboxColumn(
                            "new capital confirmed?",
                            help="Tick only for fresh investment or savings contributions counted once.",
                        ),
                        "duplicate_override": st.column_config.CheckboxColumn(
                            "separate duplicate?",
                            help="Tick only when this duplicate-looking row is genuinely separate.",
                        ),
                    },
                    key=f"ready_editor_{st.session_state.get('upload_batch_id', 'default')}",
                )
                st.session_state.edited_transactions_df = merge_review_edits(
                    current_df,
                    edited_ready,
                )
                persist_batch_results(
                    st.session_state.edited_transactions_df,
                    refresh_recommendations=False,
                )
                current_df = st.session_state.edited_transactions_df
        with st.expander(f"Excluded ({len(excluded_rows)})", expanded=False):
            if excluded_rows.empty:
                st.caption("No excluded transfers or duplicate rows.")
            else:
                st.dataframe(
                    excluded_rows[[column for column in IGNORED_COLUMNS if column in excluded_rows.columns]],
                    hide_index=True, use_container_width=True,
                )
        workflow_counts = guided_workflow_state(current_df)
        unresolved_count = int(workflow_counts.eq("needs_attention").sum())
        included_count = int(current_df["include_in_append"].apply(normalize_bool).sum())
        with st.container(border=True):
            summary_columns = st.columns(3)
            summary_columns[0].metric("Included", included_count)
            summary_columns[1].metric("Needs attention", unresolved_count)
            summary_columns[2].metric("Excluded", int(workflow_counts.eq("excluded").sum()))
            if unresolved_count:
                st.caption(
                    "You can preview Confirm now, but Append remains disabled until "
                    "every attention item is resolved or excluded."
                )
            elif not included_count:
                st.caption(
                    "No transactions are currently ticked. You can still open Confirm "
                    "to inspect the batch summary."
                )
            if st.button(
                "Continue to confirm",
                type="primary",
                width="stretch",
            ):
                st.session_state.pending_workflow_stage = "Confirm"
                st.rerun()
        st.stop()

        date_options = append_date_options(current_df)
        selected_append_dates = st.multiselect(
            "Dates to append",
            options=date_options,
            default=date_options,
            help="By default, every extracted valid date is selected. Remove dates here to skip them for this append only.",
            key=f"append_dates_{st.session_state.get('upload_batch_id', 'default')}",
        )
        append_df = filter_to_append_dates(current_df, selected_append_dates)
        excluded_by_date = int(
            (
                current_df["include_in_append"].apply(normalize_bool)
                & ~current_df["date"].astype(str).str.strip().isin(set(selected_append_dates))
            ).sum()
        )
        if excluded_by_date:
            st.info(
                f"{excluded_by_date} otherwise-appendable row(s) are excluded by the date selection for this append only."
            )

        ready_count = int((current_df["status"] == "ready").sum())
        review_count = int((current_df["status"] == "needs_review").sum())
        ignored_count = int((current_df["status"] == "ignored").sum())
        duplicate_like_count = int(
            (
                current_df["review_note"].astype(str).str.contains(
                    "duplicate|already recorded",
                    case=False,
                    na=False,
                )
                | current_df["status"].astype(str).eq("duplicate")
            ).sum()
        )
        preview = append_preview_metrics(append_df)

        summary_cols = st.columns(5)
        summary_cols[0].metric("Ready", ready_count)
        summary_cols[1].metric("Needs review", review_count)
        summary_cols[2].metric("Ignored", ignored_count)
        summary_cols[3].metric("Duplicate-looking", duplicate_like_count)
        summary_cols[4].metric("Will append", preview["included"])
        statement_cols = st.columns(3)
        for column, label, key in zip(statement_cols, ["Income", "Expense", "Offsets"], ["income", "expense", "offsets"]):
            column.metric(label, f"${preview[key]:,.2f}")
        balance_cols = st.columns(3)
        for column, label, key in zip(balance_cols, ["Operating surplus / deficit", "Allocations", "Final surplus / deficit"], ["operating", "allocations", "final"]):
            column.metric(label, f"${preview[key]:,.2f}")
        st.caption(
            "Summary tables only count rows where the Google Sheet check column is Yes. "
            "Newly appended rows default to No until you tally them."
        )
        if not check_duplicates:
            manual_recovery_mode = st.checkbox(
                "Manual recovery mode: append without duplicate checking",
                value=False,
                help="Only use this if Google duplicate checking is intentionally unavailable and you have manually verified the rows.",
            )
            st.error(
                "Duplicate checking is off. Turn it back on unless you are intentionally doing a manual recovery append."
            )
        else:
            manual_recovery_mode = False

        render_duplicate_comparisons(current_df)

        anomalies = anomaly_rows(append_df[bool_series(append_df, "include_in_append")])
        with st.expander(
            f"Anomalies ({len(anomalies)})",
            expanded=False,
        ):
            if anomalies.empty:
                st.success("No included anomaly rows detected.")
            else:
                st.caption("Review these rows in the table above. Anomalies are advisory unless the app shows a red blocker below.")
                st.dataframe(
                    anomalies,
                    hide_index=True,
                    use_container_width=True,
                    height=min(420, 90 + len(anomalies) * 48),
                )

        blockers = blocking_issues(append_df)
        if not check_duplicates and not manual_recovery_mode:
            blockers.append(
                "Duplicate checking is off. Enable manual recovery mode only after manually verifying these rows."
            )
        required_actions, recommended_actions = append_action_items(append_df)
        with st.container(border=True):
            st.markdown("### Before You Append")
            if required_actions:
                st.error(
                    "You need to complete these action(s) before the append button will unlock."
                )
                for action in required_actions:
                    st.markdown(f"- {action}")
            else:
                st.success("No blocking issues. You can append the included rows.")

            if recommended_actions:
                st.info("Recommended checks before saving to Google Sheets.")
                for action in recommended_actions:
                    st.markdown(f"- {action}")

        active_batch_id = st.session_state.get("active_batch_id")
        append_already_completed = bool(active_batch_id) and (
            st.session_state.get("last_append_completed_batch_id") == active_batch_id
        )

        left, right = st.columns([1, 3])
        with left:
            append_clicked = st.button(
                "Append included rows",
                type="primary",
                disabled=(
                    bool(blockers)
                    or append_already_completed
                ),
            )
        with right:
            st.caption("Review dates, signs, and descriptions carefully before appending.")

        if append_clicked:
            st.session_state.append_in_progress = True
            try:
                reviewed_dataframe = st.session_state.edited_transactions_df.copy()
                if check_duplicates:
                    with st.spinner("Checking Google Sheets for duplicates one last time..."):
                        reviewed_dataframe = apply_existing_sheet_duplicate_check(
                            apply_workflow_state(reviewed_dataframe),
                            force_refresh=True,
                        )
                    st.session_state.edited_transactions_df = reviewed_dataframe
                selected = set(selected_append_dates)
                dataframe = reviewed_dataframe[
                    reviewed_dataframe["include_in_append"].apply(normalize_bool)
                    & reviewed_dataframe["date"].astype(str).str.strip().isin(selected)
                ].copy()
                append_blockers = blocking_issues(dataframe)
                if append_blockers:
                    raise ValueError(" ".join(append_blockers))
                sheet = SheetClient.from_env()
                audits = sheet.append_transactions_by_period(
                    dataframe,
                    skip_duplicates=check_duplicates,
                )
                appended = sum(audit.appended for audit in audits)
                skipped = sum(audit.skipped_duplicates for audit in audits)
                if all(audit.verified for audit in audits):
                    memory_saved = save_review_memory(
                        reviewed_dataframe[
                            reviewed_dataframe["date"].astype(str).str.strip().isin(selected)
                        ].copy()
                    )
                    if memory_saved:
                        st.info(f"Saved {memory_saved} local review-memory decision(s).")
                    st.success(f"Appended and verified {appended} transaction row(s).")
                else:
                    st.error("Some rows could not be verified after append. Check the audit details below.")
                if skipped:
                    st.info(f"Skipped {skipped} duplicate row(s).")

                st.session_state.last_append_tab_links = [
                    {
                        "label": f"Open {audit.month} {audit.year} tab",
                        "url": worksheet_url(audit.spreadsheet_id, audit.worksheet_id),
                    }
                    for audit in audits
                    if audit.appended and audit.verified
                ]
                summary_links = []
                for spreadsheet_id in sorted({audit.spreadsheet_id for audit in audits if audit.verified}):
                    destination = sheet.client.open_by_key(spreadsheet_id)
                    summary = destination.worksheet("Summary")
                    summary_links.append({
                        "label": f"Open {destination.title} Summary",
                        "url": worksheet_url(spreadsheet_id, summary.id),
                    })
                st.session_state.last_append_tab_links.extend(summary_links)
                persist_append_completion(dataframe, audits)
                st.session_state.last_append_completed_batch_id = st.session_state.get("active_batch_id")
                st.session_state.existing_sheet_match_cache = {}

                audit_rows = [
                    {
                        "year_file": audit.year,
                        "month_tab": audit.month,
                        "attempted": audit.attempted,
                        "appended": audit.appended,
                        "skipped_duplicates": audit.skipped_duplicates,
                        "verified": audit.verified,
                        "created_year_file": audit.created_spreadsheet,
                        "created_month_tab": audit.created_worksheet,
                        "spreadsheet_id": audit.spreadsheet_id,
                    }
                    for audit in audits
                ]
                st.dataframe(pd.DataFrame(audit_rows), hide_index=True, use_container_width=True)
            except Exception as exc:
                st.error(f"Could not append to Google Sheets: {exc}")
            finally:
                st.session_state.append_in_progress = False

        last_append_tab_links = st.session_state.get("last_append_tab_links", [])
        if last_append_tab_links:
            st.markdown("### Open updated Google Sheets")
            for link in last_append_tab_links:
                st.link_button(link["label"], link["url"])
