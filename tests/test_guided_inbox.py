import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from recommendation_service import (
    RecommendationCache,
    RecommendationClient,
    RecommendationRequest,
)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class GuidedInboxStoreTests(unittest.TestCase):
    def test_batch_lifecycle_and_append_audit_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BatchStore(directory)
            screenshot = ScreenshotRecord(
                filename="dbs.png", content_hash=screenshot_hash(b"image")
            )
            batch = store.create_batch([screenshot])
            store.set_screenshot_status(
                batch.batch_id, screenshot.screenshot_id, ScreenshotStatus.READING
            )
            transaction_data = {
                "date": "2026-09-09",
                "source": "DBS_BANK",
                "description": "CAFE",
                "amount": "12.50",
                "currency": "SGD",
                "money_flow": "outflow",
            }
            transaction = TransactionRecord(
                fingerprint=transaction_fingerprint(transaction_data),
                status=TransactionStatus.APPROVED,
                data=transaction_data,
                provenance=TransactionProvenance(
                    screenshot_id=screenshot.screenshot_id,
                    image_filename=screenshot.filename,
                    image_region={"x": 0.1, "y": 0.2, "width": 0.8, "height": 0.1},
                    extraction_confidence=0.94,
                    matched_workbook_id="book-2026",
                    matched_worksheet="September",
                    matched_row=14,
                    decision_source="user",
                ),
            )
            store.upsert_transaction(batch.batch_id, transaction)
            store.record_append_audit(
                batch.batch_id,
                AppendAuditRecord(
                    status="complete",
                    appended_transaction_ids=[transaction.transaction_id],
                    affected_destinations=["2026/September"],
                ),
            )

            restored = BatchStore(directory).get(batch.batch_id)
            self.assertEqual(restored.screenshots[0].status, ScreenshotStatus.READING)
            self.assertEqual(restored.transactions[0].status, TransactionStatus.APPROVED)
            self.assertEqual(restored.transactions[0].provenance.matched_row, 14)
            self.assertEqual(restored.append_audits[0].appended_transaction_ids, [transaction.transaction_id])
            self.assertTrue((Path(directory) / "batches" / f"{batch.batch_id}.json").exists())

    def test_invalid_batch_identifier_cannot_escape_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BatchStore(directory)
            with self.assertRaises(ValueError):
                store.get("../outside")


class RecommendationServiceTests(unittest.TestCase):
    def setUp(self):
        self.request = RecommendationRequest(
            screenshot_hash="screen-hash",
            transaction_fingerprint="txn-fingerprint",
            transaction={"category": "Food", "money_flow": "outflow", "amount": -8},
        )

    def test_external_result_is_validated_and_cached_by_both_hashes(self):
        response = {
            "category": "Food",
            "money_flow": "outflow",
            "confidence": 0.93,
            "duplicate": {"likelihood": 0.1},
            "evidence": ["Merchant history"],
            "review_reasons": [],
            "source": "external",
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = RecommendationCache(directory)
            client = RecommendationClient(
                endpoint="https://recommend.invalid/v1/recommend",
                timeout_seconds=0.25,
                cache=cache,
            )
            with patch(
                "recommendation_service.urlopen",
                return_value=FakeResponse(json.dumps(response).encode("utf-8")),
            ) as call:
                first = client.recommend(self.request)
                second = client.recommend(self.request)

            self.assertEqual(first.confidence, 0.93)
            self.assertEqual(second.source, "cache")
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.kwargs["timeout"], 0.25)

    def test_malformed_output_uses_read_only_deterministic_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            client = RecommendationClient(
                endpoint="https://recommend.invalid",
                cache=RecommendationCache(directory),
            )
            with patch(
                "recommendation_service.urlopen",
                return_value=FakeResponse(b'{"confidence": "invalid"}'),
            ):
                result = client.recommend(self.request)

            self.assertEqual(result.source, "deterministic_fallback")
            self.assertEqual(result.category, "Food")
            self.assertEqual(result.money_flow, "outflow")
            self.assertEqual(result.confidence, 0)
            self.assertTrue(result.review_reasons)

    def test_timeout_uses_fallback_without_caching_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = RecommendationCache(directory)
            client = RecommendationClient(
                endpoint="https://recommend.invalid", cache=cache
            )
            with patch("recommendation_service.urlopen", side_effect=TimeoutError("slow")):
                result = client.recommend(self.request)

            self.assertEqual(result.source, "deterministic_fallback")
            self.assertIsNone(cache.get("screen-hash", "txn-fingerprint"))


if __name__ == "__main__":
    unittest.main()
