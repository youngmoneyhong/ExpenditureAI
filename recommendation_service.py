from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, ValidationError

from batch_store import default_data_directory


class DuplicateMatch(BaseModel):
    likelihood: float = Field(default=0, ge=0, le=1)
    workbook_id: str = ""
    worksheet: str = ""
    row: int | None = Field(default=None, ge=1)
    matched_fields: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    category: str
    money_flow: str
    confidence: float = Field(ge=0, le=1)
    duplicate: DuplicateMatch = Field(default_factory=DuplicateMatch)
    transfer_interpretation: str = ""
    allocation_interpretation: str = ""
    evidence: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    source: str = "external"


class RecommendationRequest(BaseModel):
    screenshot_hash: str
    transaction_fingerprint: str
    transaction: dict[str, Any]
    provenance: dict[str, Any] = Field(default_factory=dict)
    merchant_history: list[dict[str, Any]] = Field(default_factory=list)
    possible_matches: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    schema_version: str = "1"


Fallback = Callable[[RecommendationRequest, str], Recommendation]


def deterministic_fallback(request: RecommendationRequest, reason: str = "") -> Recommendation:
    """Return a conservative recommendation; this function has no write capability."""
    transaction = request.transaction
    category = str(transaction.get("category") or "Others")
    money_flow = str(transaction.get("money_flow") or "neutral")
    review_reasons = ["External recommendation unavailable"]
    if reason:
        review_reasons.append(reason)
    return Recommendation(
        category=category,
        money_flow=money_flow,
        confidence=0,
        evidence=["Existing normalized transaction fields retained"],
        review_reasons=review_reasons,
        source="deterministic_fallback",
    )


class RecommendationCache:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory else default_data_directory() / "recommendation_cache"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def get(self, screenshot_digest: str, transaction_digest: str, context: str = "") -> Recommendation | None:
        path = self._path(screenshot_digest, transaction_digest, context)
        if not path.exists():
            return None
        try:
            return Recommendation.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError):
            return None

    def put(self, screenshot_digest: str, transaction_digest: str, recommendation: Recommendation, context: str = "") -> None:
        path = self._path(screenshot_digest, transaction_digest, context)
        with self._lock:
            fd, name = tempfile.mkstemp(prefix=".recommendation-", suffix=".tmp", dir=self.directory)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(recommendation.model_dump_json(indent=2))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def _path(self, screenshot_digest: str, transaction_digest: str, context: str = "") -> Path:
        key = hashlib.sha256(f"{screenshot_digest}:{transaction_digest}:{context}".encode("utf-8")).hexdigest()
        return self.directory / f"{key}.json"


class RecommendationClient:
    def __init__(
        self,
        endpoint: str | None = None,
        timeout_seconds: float | None = None,
        cache: RecommendationCache | None = None,
        fallback: Fallback = deterministic_fallback,
    ):
        self.endpoint = endpoint if endpoint is not None else os.getenv("EXPENDITURE_AI_RECOMMENDATION_URL", "")
        configured_timeout = os.getenv("EXPENDITURE_AI_RECOMMENDATION_TIMEOUT_SECONDS", "5")
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else float(configured_timeout)
        self.api_key = os.getenv("EXPENDITURE_AI_RECOMMENDATION_API_KEY", "")
        self.cache = cache or RecommendationCache()
        self.fallback = fallback

    def recommend(self, request: RecommendationRequest) -> Recommendation:
        cache_context = f"{request.schema_version}:{request.model}:{self.endpoint}"
        cached = self.cache.get(request.screenshot_hash, request.transaction_fingerprint, cache_context)
        if cached is not None:
            return cached.model_copy(update={"source": "cache"})

        if not self.endpoint:
            return self.fallback(request, "Recommendation endpoint is not configured")

        try:
            recommendation = self._request(request)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
            return self.fallback(request, f"{type(error).__name__}: {error}")

        self.cache.put(request.screenshot_hash, request.transaction_fingerprint, recommendation, cache_context)
        return recommendation

    def _request(self, payload: RecommendationRequest) -> Recommendation:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint,
            data=payload.model_dump_json().encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("utf-8")
        return Recommendation.model_validate(json.loads(body))
