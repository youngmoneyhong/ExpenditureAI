from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BatchStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    NEEDS_ATTENTION = "needs_attention"
    READY = "ready"
    APPENDING = "appending"
    COMPLETE = "complete"
    FAILED = "failed"


class ScreenshotStatus(str, Enum):
    QUEUED = "queued"
    READING = "reading"
    CHECKING_HISTORY = "checking_history"
    READY = "ready"
    FAILED = "failed"


class TransactionStatus(str, Enum):
    READY = "ready"
    NEEDS_ATTENTION = "needs_attention"
    EXCLUDED = "excluded"
    APPROVED = "approved"
    APPENDED = "appended"


class TransactionProvenance(BaseModel):
    screenshot_id: str
    image_filename: str = ""
    image_region: dict[str, float] | None = None
    extraction_confidence: float | None = Field(default=None, ge=0, le=1)
    matched_workbook_id: str = ""
    matched_worksheet: str = ""
    matched_row: int | None = Field(default=None, ge=1)
    decision_source: str = "deterministic"


class ScreenshotRecord(BaseModel):
    screenshot_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    filename: str
    content_hash: str
    status: ScreenshotStatus = ScreenshotStatus.QUEUED
    error: str = ""
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class TransactionRecord(BaseModel):
    transaction_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    fingerprint: str
    status: TransactionStatus = TransactionStatus.NEEDS_ATTENTION
    data: dict[str, Any] = Field(default_factory=dict)
    provenance: TransactionProvenance
    recommendation: dict[str, Any] | None = None
    review_reasons: list[str] = Field(default_factory=list)
    user_decision: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class AppendAuditRecord(BaseModel):
    attempted_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    status: str
    appended_transaction_ids: list[str] = Field(default_factory=list)
    skipped_transaction_ids: list[str] = Field(default_factory=list)
    failed_transaction_ids: list[str] = Field(default_factory=list)
    affected_destinations: list[str] = Field(default_factory=list)
    error: str = ""


class BatchRecord(BaseModel):
    batch_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: BatchStatus = BatchStatus.QUEUED
    screenshots: list[ScreenshotRecord] = Field(default_factory=list)
    transactions: list[TransactionRecord] = Field(default_factory=list)
    append_audits: list[AppendAuditRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


def screenshot_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def transaction_fingerprint(transaction: dict[str, Any]) -> str:
    fields = (
        str(transaction.get("date", "")).strip(),
        str(transaction.get("source", "")).strip().upper(),
        " ".join(str(transaction.get("description", "")).upper().split()),
        str(transaction.get("money_flow", "")).strip().lower(),
        str(transaction.get("amount", "")).strip(),
        str(transaction.get("currency", "SGD")).strip().upper(),
        str(transaction.get("transaction_reference", "")).strip().upper(),
    )
    return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()


def default_data_directory() -> Path:
    configured = os.getenv("EXPENDITURE_AI_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "ExpenditureAI" / "guided_inbox"


class BatchStore:
    """Atomic JSON persistence for guided-inbox batches and append audits."""

    def __init__(self, data_directory: str | Path | None = None):
        self.data_directory = Path(data_directory) if data_directory else default_data_directory()
        self.batch_directory = self.data_directory / "batches"
        self.batch_directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create_batch(self, screenshots: list[ScreenshotRecord] | None = None) -> BatchRecord:
        batch = BatchRecord(screenshots=screenshots or [])
        return self.save(batch)

    def get(self, batch_id: str) -> BatchRecord:
        path = self._path(batch_id)
        if not path.exists():
            raise KeyError(f"Unknown batch: {batch_id}")
        with self._lock:
            return BatchRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, batch: BatchRecord) -> BatchRecord:
        batch.updated_at = _utc_now()
        payload = batch.model_dump_json(indent=2)
        path = self._path(batch.batch_id)
        with self._lock:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{batch.batch_id}-", suffix=".tmp", dir=self.batch_directory
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, path)
            finally:
                temporary_path.unlink(missing_ok=True)
        return batch

    def list_batches(self) -> list[BatchRecord]:
        records = [
            BatchRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.batch_directory.glob("*.json")
        ]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def set_screenshot_status(
        self, batch_id: str, screenshot_id: str, status: ScreenshotStatus, error: str = ""
    ) -> BatchRecord:
        batch = self.get(batch_id)
        screenshot = next((item for item in batch.screenshots if item.screenshot_id == screenshot_id), None)
        if screenshot is None:
            raise KeyError(f"Unknown screenshot: {screenshot_id}")
        screenshot.status = status
        screenshot.error = error
        screenshot.updated_at = _utc_now()
        return self.save(batch)

    def upsert_transaction(self, batch_id: str, transaction: TransactionRecord) -> BatchRecord:
        batch = self.get(batch_id)
        for index, existing in enumerate(batch.transactions):
            if existing.transaction_id == transaction.transaction_id:
                transaction.updated_at = _utc_now()
                batch.transactions[index] = transaction
                break
        else:
            batch.transactions.append(transaction)
        return self.save(batch)

    def record_append_audit(self, batch_id: str, audit: AppendAuditRecord) -> BatchRecord:
        batch = self.get(batch_id)
        batch.append_audits.append(audit)
        return self.save(batch)

    def _path(self, batch_id: str) -> Path:
        if not batch_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in batch_id):
            raise ValueError("batch_id contains unsupported characters")
        return self.batch_directory / f"{batch_id}.json"
