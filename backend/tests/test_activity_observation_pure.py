"""Run directly: no PostgreSQL, config, credentials or database conftest."""
import sys
import unittest
import json
import uuid
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.activity_observation import query_exhaustion, observation_projection, semantic_observation, canonical_workout, digest, semantic_inventory

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def receipt(**changes):
    value = dict(status="finalized", queryStatus="succeeded", queryMethod="sample_no_limit",
                 observedCount=1, acknowledgedCount=1, pendingCount=0, failedCount=0,
                 duplicateCount=0, withdrawnCount=0, finalizedAt=NOW.isoformat(),
                 rangeStart=(NOW-timedelta(days=180)).isoformat(), rangeEnd=NOW.isoformat(),
                 source="healthkit", exhaustion="exhausted", contentDigest="a"*64)
    return {**value, **changes}


class ObservationContractTests(unittest.TestCase):
    def test_client_transport_hash_is_not_persisted_inventory_semantics(self):
        stored = dict(sourceId="synthetic-source", disposition="pending", aliasOf=None,
                      explicitSourceDeletion=False, payloadDigest="a"*64,
                      serverPayloadDigest="c"*64, revision=1)
        self.assertEqual(semantic_inventory([stored]), semantic_inventory([{**stored, "payloadDigest": "b"*64}]))
        self.assertNotEqual(semantic_inventory([stored]), semantic_inventory([{**stored, "serverPayloadDigest": "d"*64}]))
        self.assertNotEqual(semantic_inventory([stored]), semantic_inventory([{**stored, "revision": 2}]))

    def test_typed_numeric_defaults_utc_and_volatile_hash_parity(self):
        value = SimpleNamespace(activity_type="running", start_date=NOW, end_date=NOW,
                                duration=1800, total_distance=5000, data={"samples": [1, 2.0], "scanAt": "old"})
        expected = digest(canonical_workout(value))
        value.duration, value.total_distance = 1800.0, 5000.0
        value.start_date = NOW.astimezone(__import__("datetime").timezone(timedelta(hours=10)))
        value.data["scanAt"] = "new"
        self.assertEqual(digest(canonical_workout(value)), expected)
        value.data["samples"].reverse()
        self.assertNotEqual(digest(canonical_workout(value)), expected)
        value.duration = float("nan")
        with self.assertRaises(ValueError):
            canonical_workout(value)

    def test_public_cross_language_golden_vectors(self):
        vectors = json.loads(Path(__file__).with_name("activity_observation_v1_vectors.json").read_text())
        for case in vectors:
            with self.subTest(case=case["name"]):
                self.assertEqual(digest(case["canonical"]), case["sha256"])

    def test_capped200_is_not_the_no_limit_query(self):
        self.assertEqual(query_exhaustion("sample_capped200", "succeeded", 200), "truncated")
        self.assertEqual(query_exhaustion("sample_capped200", "succeeded", 199), "exhausted")
        self.assertEqual(query_exhaustion("sample_no_limit", "succeeded", 201), "exhausted")
        self.assertEqual(query_exhaustion("sample_no_limit", "failed", 0), "unknown")

    def test_empty_enumeration_never_proves_read_access_or_training_completeness(self):
        actual = observation_projection(receipt(observedCount=0, acknowledgedCount=0), NOW)
        self.assertEqual(actual["sourceCoverage"], "read_access_unknown")
        self.assertEqual(actual["trainingCoverage"], "unknown")

    def test_pending_failed_or_stale_receipt_does_not_establish_source_coverage(self):
        for change in ({"status": "pending"}, {"failedCount": 1}, {"pendingCount": 1}, {"invalidatedCount": 1}, {"observedCount": 2},
                       {"exhaustion": "truncated"}, {"finalizedAt": (NOW-timedelta(seconds=901)).isoformat()},
                       {"finalizedAt": (NOW+timedelta(seconds=1)).isoformat()}):
            with self.subTest(change=change):
                self.assertEqual(observation_projection(receipt(**change), NOW)["sourceCoverage"], "unknown")

    def test_refresh_timestamps_and_irrelevant_boundary_churn_are_not_semantic_delta(self):
        first = semantic_observation(receipt(), NOW, [NOW-timedelta(days=2)])
        second = semantic_observation(receipt(finalizedAt=(NOW-timedelta(seconds=2)).isoformat(),
                                     rangeEnd=(NOW-timedelta(seconds=1)).isoformat()), NOW, [NOW-timedelta(days=2)])
        self.assertEqual(first, second)
        self.assertNotEqual(first, semantic_observation(receipt(contentDigest="b"*64), NOW, [NOW-timedelta(days=2)]))
        self.assertNotEqual(first, semantic_observation(receipt(), NOW+timedelta(seconds=901), [NOW-timedelta(days=2)]))

    def test_effective_scheduled_coverage_change_is_semantic(self):
        instant = NOW-timedelta(days=1)
        self.assertNotEqual(semantic_observation(receipt(), NOW, [instant]),
                            semantic_observation(receipt(rangeStart=NOW.isoformat()), NOW, [instant]))

    def test_projection_contains_aggregates_not_source_identifiers(self):
        actual = observation_projection(receipt(tokenId="secret", sourceIds=["private"], deviceId="private"), NOW)
        self.assertEqual(actual["sourceCoverage"], "within_window_observed")
        self.assertNotIn("tokenId", actual)
        self.assertNotIn("sourceIds", actual)
        self.assertNotIn("deviceId", actual)


if __name__ == "__main__":
    unittest.main()
