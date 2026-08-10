from __future__ import annotations

import os
import re
import shutil
import tempfile
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from enrichment_agents import enrich_transactions
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
    CATEGORY_OPTIONS,
    EXPENSE_OFFSET_INFLOW_CATEGORIES,
    INFLOW_CATEGORY_OPTIONS,
    OUTFLOW_CATEGORY_OPTIONS,
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
        flex: 0 0 360px;
        scroll-snap-align: start;
    }
    .screenshot-card img {
        width: 100%;
        max-height: 760px;
        object-fit: contain;
        background: #f8fafc;
        border-radius: 6px;
    }
    .screenshot-caption {
        margin-top: 6px;
        color: #94a3b8;
        text-align: center;
        word-break: break-word;
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
    "amount",
    "money_flow",
    "category",
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
    "Carousell Sales",
    "Cashbacks & Refunds",
}


def bool_series(dataframe: pd.DataFrame, column: str, default: bool = False) -> pd.Series:
    if dataframe.empty:
        return pd.Series(dtype=bool, index=dataframe.index)
    if column not in dataframe:
        return pd.Series(default, index=dataframe.index)
    return dataframe[column].apply(normalize_bool)


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

    try:
        extractions = {}
        with ThreadPoolExecutor(max_workers=vision_worker_count(len(jobs))) as executor:
            futures = {
                executor.submit(
                    extract_transactions_from_image,
                    job["path"],
                    model=model,
                    today=today_iso(),
                ): job
                for job in jobs
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                job = futures[future]
                extractions[job["index"]] = future.result()
                progress.progress(completed / len(jobs))

        for job in jobs:
            extraction = extractions[job["index"]]
            rows.extend(
                extraction_to_rows(
                    extraction,
                    filename=job["filename"],
                    archive_path=job["archive_path"],
                )
            )

            for warning in extraction.warnings:
                st.warning(f"{job['filename']}: {warning}")
    finally:
        for job in jobs:
            job["path"].unlink(missing_ok=True)

    dataframe = rows_to_dataframe(rows)
    if not save_raw_text and not dataframe.empty:
        dataframe["raw_text"] = ""

    dataframe = apply_review_memory(dataframe)
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
            dataframe = apply_review_memory(dataframe)
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


def apply_existing_sheet_duplicate_check(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    working = dataframe.copy()
    st.session_state.duplicate_check_error = ""
    try:
        sheet = SheetClient.from_env()
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


def is_paylah_wallet_topup(dataframe: pd.DataFrame) -> pd.Series:
    sources = dataframe["source"].astype(str).str.upper().str.strip()
    descriptions = dataframe["description"].apply(normalize_description)
    compact_descriptions = descriptions.str.replace(r"[^A-Z0-9]+", " ", regex=True)
    has_topup = compact_descriptions.str.contains(r"\bTOP\s*UP\b", regex=True)
    has_wallet_or_paylah = compact_descriptions.str.contains(
        r"\b(?:WALLET|PAYLAH)\b", regex=True
    )
    paylah_wallet_topup = (
        sources.eq("DBS_PAYLAH")
        & (
            descriptions.eq("TOP UP MY WALLET")
            | (has_topup & has_wallet_or_paylah)
        )
    )
    dbs_bank_paylah_topup = (
        sources.eq("DBS_BANK")
        & has_topup
        & compact_descriptions.str.contains(r"\bPAYLAH\b", regex=True)
    )
    return (
        paylah_wallet_topup
        | dbs_bank_paylah_topup
    )


def is_uob_ebanking_payment(dataframe: pd.DataFrame) -> pd.Series:
    sources = dataframe["source"].astype(str).str.upper().str.strip()
    descriptions = dataframe["description"].apply(normalize_description)
    return sources.eq("UOB_TMRW") & descriptions.str.startswith("PAYMT THRU E-BANK")


def apply_category_flow_rules(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    working = dataframe.copy()
    inflow_mask = working["money_flow"].astype(str).eq("inflow")
    outflow_mask = working["money_flow"].astype(str).eq("outflow")
    category = working["category"].astype(str)

    invalid_inflow = inflow_mask & ~category.isin(INFLOW_CATEGORY_OPTIONS)
    working.loc[invalid_inflow, "category"] = "Reimbursement"
    working.loc[invalid_inflow, "category_reason"] = (
        "Adjusted because inflow categories are limited to Reimbursement, "
        "Carousell Sales, Cashbacks & Refunds, or GVs & Prize Award."
    )

    invalid_outflow = outflow_mask & ~category.isin(OUTFLOW_CATEGORY_OPTIONS)
    working.loc[invalid_outflow, "category"] = "Others"
    working.loc[invalid_outflow, "category_reason"] = (
        "Adjusted because outflow rows use expense categories."
    )

    non_reimbursement_inflow = (
        inflow_mask
        & working["category"].astype(str).isin(NON_REIMBURSEMENT_INFLOW_CATEGORIES)
    )
    working.loc[non_reimbursement_inflow, "reimbursement_candidate"] = False
    working.loc[non_reimbursement_inflow, "reimbursement_for"] = ""
    working.loc[non_reimbursement_inflow, "reimbursement_for_category"] = ""

    working.loc[
        inflow_mask & working["category"].astype(str).eq("Reimbursement"),
        "reimbursement_candidate",
    ] = True
    working.loc[
        inflow_mask
        & working["category"].astype(str).eq("Reimbursement")
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

    paylah_topup_mask = is_paylah_wallet_topup(working)
    uob_ebanking_mask = is_uob_ebanking_payment(working)
    topup_mask = paylah_topup_mask | uob_ebanking_mask
    working.loc[topup_mask, "status"] = "ignored"
    working.loc[topup_mask, "include_in_append"] = False
    working.loc[topup_mask, "ignore_reason"] = "PayLah wallet top-up"
    working.loc[topup_mask, "review_note"] = "Ignored by default: PayLah wallet top-up"
    working.loc[topup_mask, "money_flow"] = "neutral"
    working.loc[topup_mask, "transaction_type"] = "transfer"
    working.loc[topup_mask, "reimbursement_type"] = "self_transfer"
    working.loc[topup_mask, "reimbursement_candidate"] = False
    working.loc[topup_mask, "reimbursement_for"] = ""
    working.loc[topup_mask, "category"] = "Transfer"
    working.loc[uob_ebanking_mask, "ignore_reason"] = "UOB e-banking payment"
    working.loc[uob_ebanking_mask, "review_note"] = (
        "Ignored by default: UOB PAYMT THRU E-BANK"
    )

    # A flow edit is authoritative: keep its amount sign correct automatically.
    amounts = pd.to_numeric(working["amount"], errors="coerce").fillna(0)
    flows = working["money_flow"].astype(str).str.lower()
    working.loc[flows.eq("outflow"), "amount"] = -amounts[flows.eq("outflow")].abs()
    working.loc[flows.eq("inflow"), "amount"] = amounts[flows.eq("inflow")].abs()

    non_inflow_mask = ~working["money_flow"].astype(str).eq("inflow") & ~topup_mask
    working.loc[non_inflow_mask, "reimbursement_candidate"] = False
    working.loc[non_inflow_mask, "reimbursement_type"] = "unknown"
    working.loc[non_inflow_mask, "reimbursement_for"] = ""

    non_reimbursement_inflow_type = (
        working["money_flow"].astype(str).eq("inflow")
        & working["reimbursement_type"].astype(str).eq("cashback")
        & ~topup_mask
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
        & ~topup_mask
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
    ] = "Reimbursement"
    standalone_reimbursement_mask = (
        working["money_flow"].astype(str).eq("inflow")
        & bool_series(working, "reimbursement_candidate")
        & ~working["reimbursement_type"].astype(str).isin(["cashback", "salary"])
    )
    working.loc[standalone_reimbursement_mask, "category"] = "Reimbursement"
    working = apply_category_flow_rules(working)

    invalid_date_mask = working["date"].apply(lambda value: not is_valid_iso_date(value))
    working.loc[invalid_date_mask & ~topup_mask, "status"] = "needs_review"
    working.loc[invalid_date_mask & ~topup_mask, "include_in_append"] = False
    working.loc[invalid_date_mask & ~topup_mask, "review_note"] = "Fix date before append"

    amount_parse_error_mask = bool_series(working, "amount_parse_error") & ~topup_mask
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
        & ~topup_mask
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
        )
        & ~topup_mask
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
        ~topup_mask
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
            & ~topup_mask
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
        & ~topup_mask
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
        for column in editable_columns:
            merged.at[row_id, column] = row[column]

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
    has_required_fields = (
        dataframe["date"].astype(str).str.strip().ne("")
        & dataframe["description"].astype(str).str.strip().ne("")
    )
    return keys.where(has_required_fields, "")


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
                if same_reference or descriptions_look_like_same_transaction(
                    row["description"], retained["description"]
                ):
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
                if same_reference or descriptions_look_like_same_transaction(
                    row["description"], retained["description"]
                ):
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

    for index, row in dataframe.iterrows():
        reference_index = None
        reason = ""
        transaction_hash = str(row.get("transaction_hash", "")).strip()
        transaction_key = str(transaction_keys.at[index]).strip()
        if transaction_hash and transaction_hash in seen_hashes:
            reference_index = seen_hashes[transaction_hash]
            reason = "Exact extracted transaction"
        elif transaction_key and transaction_key in seen_keys:
            reference_index = seen_keys[transaction_key]
            reason = "Same date, source, amount, flow, and description"
        elif index in overlap_matches:
            reference_index = overlap_matches[index]
            reason = "Same date, source, amount, flow, and similar description"

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

    return comparisons


def render_duplicate_comparisons(dataframe: pd.DataFrame) -> None:
    comparisons = duplicate_comparisons(dataframe)
    if not comparisons:
        return

    with st.expander(f"Duplicate comparisons ({len(comparisons)})", expanded=False):
        st.caption("Compare the retained row with its possible overlap, then use separate duplicate? in the review table only when both are real transactions.")
        fields = ["date", "source", "description", "amount", "currency", "money_flow", "category"]
        for comparison in comparisons:
            reference_index = comparison["reference_index"]
            duplicate_index = comparison["duplicate_index"]
            reference = dataframe.loc[reference_index]
            duplicate = dataframe.loc[duplicate_index]
            st.caption(
                f"Row {duplicate_index} compared with row {reference_index}: {comparison['reason']}"
            )
            comparison_frame = pd.DataFrame(
                {
                    "field": fields,
                    f"row {reference_index}": [reference.get(field, "") for field in fields],
                    f"row {duplicate_index}": [duplicate.get(field, "") for field in fields],
                }
            )
            st.dataframe(comparison_frame, hide_index=True, use_container_width=True)
            image_columns = st.columns(2)
            for column, row, row_index in [
                (image_columns[0], reference, reference_index),
                (image_columns[1], duplicate, duplicate_index),
            ]:
                image_path = Path(str(row.get("archive_path", "")))
                if image_path.is_file():
                    column.image(str(image_path), caption=f"row {row_index} screenshot")


def append_preview_metrics(dataframe: pd.DataFrame) -> dict[str, float | int]:
    included = dataframe[bool_series(dataframe, "include_in_append")].copy()
    if included.empty:
        return {
            "included": 0,
            "gross_spend": 0.0,
            "offsets": 0.0,
            "net_spend": 0.0,
        }

    amounts = pd.to_numeric(included["amount"], errors="coerce").fillna(0)
    gross_spend = amounts[
        included["money_flow"].astype(str).eq("outflow")
        & ~included["category"].astype(str).eq("Transfer")
    ].abs().sum()
    offsets = amounts[
        included["money_flow"].astype(str).eq("inflow")
        & included["category"].astype(str).isin(EXPENSE_OFFSET_INFLOW_CATEGORIES)
    ].abs().sum()

    return {
        "included": int(len(included)),
        "gross_spend": float(gross_spend),
        "offsets": float(offsets),
        "net_spend": float(gross_spend - offsets),
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


st.title("ExpenditureAI")
st.caption("UOB / DBS PayLah screenshots -> reviewed transactions -> year/month Google Sheets")

with st.sidebar:
    st.header("Settings")
    app_view = st.radio("Workspace", ["Transactions", "Merchant rules"])
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
    st.write("Agents")
    run_category_agent = st.checkbox("Category agent", value=True)
    run_anomaly_agent = st.checkbox("Anomaly agent", value=True)
    run_insight_agent = st.checkbox("Insight agent", value=True)

if app_view == "Merchant rules":
    render_merchant_rules_screen()
    st.stop()

uploaded_files = st.file_uploader(
    "Upload UOB TMRW, DBS PayLah, or DBS banking screenshots",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
)

consent = st.checkbox(
    "I understand screenshots are sent to OpenAI for extraction, archived locally if enabled, and confirmed rows are saved to Google Sheets.",
    value=True,
)

if uploaded_files:
    render_upload_preview(uploaded_files)

extract_clicked = st.button(
    "Extract transactions",
    type="primary",
    disabled=not uploaded_files or not consent,
)

if extract_clicked:
    if not os.getenv("OPENAI_API_KEY"):
        st.error("Set OPENAI_API_KEY in your .env file first.")
        st.stop()

    with st.spinner("Reading screenshots with OpenAI Vision..."):
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

if "transactions_df" in st.session_state:
    st.session_state.transactions_df = apply_workflow_state(st.session_state.transactions_df)
    st.session_state.transactions_df = apply_existing_sheet_duplicate_check(
        st.session_state.transactions_df
    )
    st.subheader("Review")
    if st.session_state.transactions_df.empty:
        st.info("No transactions were extracted. Try a clearer screenshot or crop closer to the transaction list.")
    else:
        if st.session_state.get("insight_overview"):
            st.info(f"Insight agent: {st.session_state.insight_overview}")

        st.subheader("Transaction Review")
        review_dataframe = st.session_state.transactions_df[
            ~st.session_state.transactions_df["status"].isin(["ignored", "duplicate"])
        ].copy()
        review_dataframe["_row_id"] = review_dataframe.index
        review_color_scale = amount_intensity_scale(review_dataframe)
        review_dataframe["flow_signal"] = review_dataframe.apply(
            lambda row: flow_signal(row.get("money_flow"), row.get("amount"), review_color_scale),
            axis=1,
        )
        review_columns = ["_row_id", "flow_signal"] + [
            column for column in REVIEW_COLUMNS if column in review_dataframe.columns
        ]

        if review_dataframe.empty:
            st.info("No appendable rows in this upload. Check ignored/skipped rows below.")
            st.session_state.edited_transactions_df = apply_workflow_state(
                st.session_state.transactions_df
            )
        else:
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
            st.session_state.edited_transactions_df = apply_existing_sheet_duplicate_check(
                st.session_state.edited_transactions_df
            )

        current_df = st.session_state.edited_transactions_df
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

        summary_cols = st.columns(6)
        summary_cols[0].metric("Ready", ready_count)
        summary_cols[1].metric("Needs review", review_count)
        summary_cols[2].metric("Ignored", ignored_count)
        summary_cols[3].metric("Duplicate-looking", duplicate_like_count)
        summary_cols[4].metric("Will append", preview["included"])
        summary_cols[5].metric("Net spend impact", f"${preview['net_spend']:,.2f}")

        st.caption(
            "Append preview: "
            f"${preview['gross_spend']:,.2f} gross spend - "
            f"${preview['offsets']:,.2f} offsets = "
            f"${preview['net_spend']:,.2f} net spend. "
            "Only checked rows are included."
        )
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

        ignored_rows = current_df[current_df["status"].isin(["ignored", "duplicate"])]
        if not ignored_rows.empty:
            with st.expander("Ignored and skipped rows", expanded=False):
                display_columns = [
                    column for column in IGNORED_COLUMNS if column in ignored_rows.columns
                ]
                st.dataframe(
                    ignored_rows[display_columns],
                    hide_index=True,
                    use_container_width=True,
                    height=min(420, 90 + len(ignored_rows) * 48),
                )

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

        left, right = st.columns([1, 3])
        with left:
            append_clicked = st.button(
                "Append included rows",
                type="primary",
                disabled=bool(blockers),
            )
        with right:
            st.caption("Review dates, signs, and descriptions carefully before appending.")

        if append_clicked:
            try:
                reviewed_dataframe = st.session_state.edited_transactions_df.copy()
                selected = set(selected_append_dates)
                dataframe = reviewed_dataframe[
                    reviewed_dataframe["include_in_append"].apply(normalize_bool)
                    & reviewed_dataframe["date"].astype(str).str.strip().isin(selected)
                ].copy()
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

        last_append_tab_links = st.session_state.get("last_append_tab_links", [])
        if last_append_tab_links:
            st.markdown("### Open updated Google Sheets")
            for link in last_append_tab_links:
                st.link_button(link["label"], link["url"])
