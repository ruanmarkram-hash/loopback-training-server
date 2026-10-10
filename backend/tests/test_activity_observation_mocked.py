"""Real typed route/service tests against an in-memory database boundary.

Run directly with qualified backend dependencies; never opens a database or
uses the PostgreSQL conftest. Real PostgreSQL lock/rollback proofs are separate.
"""
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "postgresql+psycopg://unused:unused@127.0.0.1:1/unused"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.sql import operators
from app.activity_observation import canonical_workout, digest
from app import activity_observation_service as service
from app.models.activity_observation import ActivityObservation, ActivityObservationMember
from app.models.coaching import ReviewJob
from app.models.user import User
from app.models.workout import Workout
from app.routes import activity_observation as route
from app.routes import admin as admin_route
from app.routes.workouts import create_workout
from app.schemas.activity_observation import ExistingActivityAck, ManifestPage, ObservationBegin, QueryOutcome
from app.schemas.workout import WorkoutCreate

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


class MemoryDB:
    """SQLAlchemy's database boundary only; no mocking of receipt policy."""
    def __init__(self):
        self.rows = []
        self.new = []
        self.commits = 0

    def add(self, row):
        if row not in self.rows:
            self.rows.append(row)
            self.new.append(row)

    def get(self, model, key):
        for row in self.rows:
            if not isinstance(row, model):
                continue
            actual = ((row.observation_id, row.source_id) if model is ActivityObservationMember
                      else getattr(row, "id", None))
            if actual == key:
                return row
        return None

    def scalar(self, statement):
        rows = self.scalars(statement).all()
        return rows[0] if rows else None

    def scalars(self, statement):
        model = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if isinstance(row, model)]
        for condition in statement._where_criteria:
            name, expected = condition.left.key, condition.right.value
            if condition.operator is operators.eq:
                rows = [row for row in rows if getattr(row, name) == expected]
            else:
                raise AssertionError("Unexpected database predicate in bounded mock")
        for order in reversed(statement._order_by_clauses):
            reverse = getattr(order, "modifier", None) is operators.desc_op
            column = order.element if reverse else order
            rows.sort(key=lambda row: getattr(row, column.key), reverse=reverse)
        return SimpleNamespace(all=lambda: rows)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1
        self.new.clear()

    def refresh(self, row):
        pass


class OwnedReceiptTests(unittest.TestCase):
    def test_admin_projection_keeps_legacy_shape_without_reading_observation(self):
        class ObservationBlindUser(SimpleNamespace):
            @property
            def activity_observation_state(self):
                raise AssertionError("Admin DTO must not read device observation state")
        user = ObservationBlindUser(id=uuid.uuid4(), username="synthetic_admin_boundary",
                                    display_name="SYNTHETIC boundary", role="user", is_active=True,
                                    data_consent=["training"], data_consent_updated_at=None,
                                    data_consent_reported_at=None)
        output = admin_route._to_out(user, 1, None).model_dump(by_alias=True)
        self.assertEqual(set(output), {"id", "username", "displayName", "role", "isActive", "tokenCount",
                                      "lastSeenAt", "lastWorkoutSyncAt", "lastHealthDate", "dataConsent",
                                      "dataConsentUpdatedAt", "dataConsentReportedAt"})
        self.assertEqual(output["dataConsent"], ["training"])
        self.assertIsNone(output["lastWorkoutSyncAt"])

    def setUp(self):
        self.db = MemoryDB()
        self.user = User(id=uuid.uuid4(), username="synthetic", role="user", is_active=True,
                         data_consent=["training"], activity_observation_state=None)
        self.token = uuid.uuid4()
        self.request = SimpleNamespace(state=SimpleNamespace(token_scope="device", token_id=self.token))
        self.db.add(self.user)
        self.db.commit()

    def begin(self, **changes):
        data = dict(observationId=str(uuid.uuid4()), producer="synthetic-test/1", source="healthkit",
                    inventoryRevision=1, rangeStart=(NOW-timedelta(days=180)).isoformat(), rangeEnd=NOW.isoformat(),
                    timezone="UTC", boundary="strict_start_date", queryMethod="sample_no_limit")
        return service.begin(self.db, self.user, self.token, ObservationBegin(**{**data, **changes}), NOW)

    def workout(self):
        row = Workout(id=uuid.uuid4(), user_id=self.user.id, activity_type="running",
                      start_date=NOW-timedelta(days=1), end_date=NOW-timedelta(days=1)+timedelta(minutes=30),
                      duration=1800.0, total_distance=5000.0, data={}, source_evidence_revision=1,
                      source_withdrawn=False)
        row.source_evidence_hash = digest(canonical_workout(row))
        self.db.add(row)
        return row

    def register(self, receipt, workout):
        service.add_page(self.db, self.user, self.token, receipt.id, 0, ManifestPage(members=[
            dict(sourceId=str(workout.id), payloadDigest=workout.source_evidence_hash)]))
        rows = service.members(self.db, receipt)
        service.record_query(self.db, self.user, self.token, receipt.id, QueryOutcome(
            status="succeeded", observedCount=1, manifestDigest=service.manifest_digest(rows)))

    def test_wrong_token_and_cross_actor_fail_before_route_commit(self):
        receipt = self.begin()
        initial = self.db.commits
        self.request.state.token_id = uuid.uuid4()
        with self.assertRaises(HTTPException) as error:
            route.finalize(receipt.id, self.request, self.db, self.user)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.db.commits, initial)
        other = User(id=uuid.uuid4(), role="user", data_consent=["training"])
        self.db.add(other)
        with self.assertRaises(HTTPException):
            service.owned_receipt(self.db, other, receipt.id, self.token)
        self.assertEqual(receipt.status, "pending")

    def test_worker_and_admin_cannot_publish_device_receipt(self):
        self.request.state.token_scope = "coach_worker"
        with self.assertRaises(HTTPException):
            service.device_actor(self.request, self.user)
        self.request.state.token_scope = "device"
        self.user.role = "admin"
        with self.assertRaises(HTTPException):
            service.device_actor(self.request, self.user)

    def test_finalize_rejects_pending_then_acknowledges_actual_owned_content_once(self):
        receipt, workout = self.begin(), self.workout()
        self.register(receipt, workout)
        with self.assertRaises(HTTPException):
            service.finalize(self.db, self.user, self.token, receipt.id, NOW)
        service.acknowledge(self.db, self.user, self.token, receipt.id, workout.id, ExistingActivityAck(serverPayloadDigest=workout.source_evidence_hash, workoutRevision=1), NOW)
        service.finalize(self.db, self.user, self.token, receipt.id, NOW)
        first = receipt.finalized_at
        service.finalize(self.db, self.user, self.token, receipt.id, NOW+timedelta(seconds=5))
        self.assertEqual(receipt.finalized_at, first)
        self.assertEqual(len([row for row in self.db.rows if isinstance(row, ReviewJob)]), 1)
        self.assertEqual(service.current_snapshot(self.db, self.user)["acknowledgedCount"], 1)
        wire = route.response(self.db, receipt)
        self.assertEqual(wire["lastSuccessfulReconciliationAt"], first.isoformat())
        self.assertEqual(wire["finalizedAt"], first.isoformat())
        self.assertEqual(service.snapshot(self.db, receipt)["lastSuccessfulReconciliationAt"], first.isoformat())
        newer = self.begin(inventoryRevision=2)
        self.assertIsNone(route.response(self.db, newer)["lastSuccessfulReconciliationAt"])
        service.finalize(self.db, self.user, self.token, receipt.id, NOW+timedelta(seconds=10))
        self.assertEqual(route.response(self.db, receipt)["lastSuccessfulReconciliationAt"], first.isoformat())

    def test_changed_after_acknowledgement_invalidates_terminal_projection(self):
        receipt, workout = self.begin(), self.workout()
        self.register(receipt, workout)
        service.acknowledge(self.db, self.user, self.token, receipt.id, workout.id, ExistingActivityAck(serverPayloadDigest=workout.source_evidence_hash, workoutRevision=1), NOW)
        service.finalize(self.db, self.user, self.token, receipt.id, NOW)
        workout.total_distance = 6000.0
        self.assertEqual(service.current_snapshot(self.db, self.user)["invalidatedCount"], 1)
        self.assertEqual(receipt.status, "finalized")

    def test_changed_registered_payload_rejected_before_workout_write(self):
        receipt, workout = self.begin(), self.workout()
        self.register(receipt, workout)
        body = dict(id=str(workout.id), activityType="running", startDate=workout.start_date,
                    endDate=workout.end_date, duration=1800, totalDistance=6000, data={},
                    observation=dict(observationId=str(receipt.id), sourceId=str(workout.id),
                                     payloadDigest=workout.source_evidence_hash))
        commits = self.db.commits
        with self.assertRaises(HTTPException):
            create_workout(WorkoutCreate(**body), self.db, self.user, self.request)
        self.assertEqual(workout.total_distance, 5000)
        self.assertEqual(self.db.commits, commits)

    def test_manifest_replay_and_sealed_query_reject_conflicts(self):
        receipt, workout = self.begin(), self.workout()
        page = ManifestPage(members=[dict(sourceId=workout.id, payloadDigest=workout.source_evidence_hash)])
        service.add_page(self.db, self.user, self.token, receipt.id, 0, page)
        service.add_page(self.db, self.user, self.token, receipt.id, 0, page)
        self.assertEqual(len(service.members(self.db, receipt)), 1)
        with self.assertRaises(HTTPException):
            service.add_page(self.db, self.user, self.token, receipt.id, 0,
                            ManifestPage(members=[dict(sourceId=workout.id, payloadDigest="f"*64)]))
        with self.assertRaises(HTTPException):
            service.record_query(self.db, self.user, self.token, receipt.id,
                                 QueryOutcome(status="succeeded", observedCount=1, manifestDigest="f"*64))

    def test_unchanged_rescan_no_job_then_staleness_exactly_once(self):
        receipt, workout = self.begin(), self.workout()
        self.register(receipt, workout)
        service.acknowledge(self.db, self.user, self.token, receipt.id, workout.id, ExistingActivityAck(serverPayloadDigest=workout.source_evidence_hash, workoutRevision=1), NOW)
        service.finalize(self.db, self.user, self.token, receipt.id, NOW)
        second = self.begin(inventoryRevision=2, rangeEnd=(NOW-timedelta(seconds=1)).isoformat())
        self.register(second, workout)
        service.acknowledge(self.db, self.user, self.token, second.id, workout.id, ExistingActivityAck(serverPayloadDigest=workout.source_evidence_hash, workoutRevision=1), NOW)
        service.finalize(self.db, self.user, self.token, second.id, NOW)
        self.assertEqual(len([r for r in self.db.rows if isinstance(r, ReviewJob)]), 1)
        service.scan_staleness(self.db, self.user, NOW+timedelta(seconds=901))
        service.scan_staleness(self.db, self.user, NOW+timedelta(seconds=902))
        self.assertEqual(len([r for r in self.db.rows if isinstance(r, ReviewJob)]), 2)

    def test_transport_only_client_digest_change_does_not_create_review_job(self):
        receipt, workout = self.begin(), self.workout()
        logical_id, issued_revision = uuid.uuid4(), uuid.uuid4()
        workout.plan_workout_id = logical_id
        workout.data = {"prescriptionRevision": str(issued_revision)}
        workout.source_evidence_hash = digest(canonical_workout(workout))
        self.register(receipt, workout)
        ack = ExistingActivityAck(serverPayloadDigest=workout.source_evidence_hash, workoutRevision=1)
        service.acknowledge(self.db, self.user, self.token, receipt.id, workout.id, ack, NOW)
        service.finalize(self.db, self.user, self.token, receipt.id, NOW)
        # Existing legacy transport accepts a revision alias, normalized server-side
        # to the same logical prescription. Its incoming hash is different, while
        # the actual stored content/hash/revision being ACKed is unchanged.
        alias_wire = SimpleNamespace(**{name: getattr(workout, name) for name in canonical_workout(workout)})
        alias_wire.plan_workout_id = issued_revision
        client_alias_hash = digest(canonical_workout(alias_wire))
        self.assertNotEqual(client_alias_hash, workout.source_evidence_hash)
        second = self.begin(inventoryRevision=2)
        service.add_page(self.db, self.user, self.token, second.id, 0, ManifestPage(members=[
            dict(sourceId=workout.id, payloadDigest=client_alias_hash)]))
        service.record_query(self.db, self.user, self.token, second.id, QueryOutcome(status="succeeded",
            observedCount=1, manifestDigest=service.manifest_digest(service.members(self.db, second))))
        service.acknowledge(self.db, self.user, self.token, second.id, workout.id, ack, NOW)
        service.finalize(self.db, self.user, self.token, second.id, NOW)
        self.assertEqual(len([r for r in self.db.rows if isinstance(r, ReviewJob)]), 1)

    def test_superseded_pending_upload_cannot_overwrite_corrected_workout(self):
        receipt, workout = self.begin(), self.workout()
        original_hash = workout.source_evidence_hash
        self.register(receipt, workout)
        self.begin(inventoryRevision=2)
        workout.total_distance = 6000.0
        workout.source_evidence_hash = digest(canonical_workout(workout))
        workout.source_evidence_revision = 2
        commits = self.db.commits
        original = WorkoutCreate(id=workout.id, activityType="running", startDate=workout.start_date,
            endDate=workout.end_date, duration=1800, totalDistance=5000, data={}, observation=dict(
                observationId=receipt.id, sourceId=workout.id, payloadDigest=original_hash))
        with self.assertRaises(HTTPException) as error:
            create_workout(original, self.db, self.user, self.request)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(workout.total_distance, 6000)
        self.assertEqual(workout.source_evidence_revision, 2)
        self.assertEqual(self.db.commits, commits)

    def test_superseded_pending_withdrawal_cannot_mutate_current_workout(self):
        receipt, workout = self.begin(), self.workout()
        service.add_page(self.db, self.user, self.token, receipt.id, 0, ManifestPage(members=[
            dict(sourceId=workout.id, disposition="source_withdrawn", explicitSourceDeletion=True)]))
        service.record_query(self.db, self.user, self.token, receipt.id, QueryOutcome(status="succeeded",
            observedCount=0, manifestDigest=service.manifest_digest(service.members(self.db, receipt))))
        self.begin(inventoryRevision=2)
        with self.assertRaises(HTTPException) as error:
            route.finalize(receipt.id, self.request, self.db, self.user)
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(workout.source_withdrawn)
        self.assertEqual(workout.total_distance, 5000)
        self.assertEqual(receipt.status, "pending")

    def test_superseded_pending_ack_cannot_adopt_current_content(self):
        receipt, workout = self.begin(), self.workout()
        self.register(receipt, workout)
        self.begin(inventoryRevision=2)
        ack = ExistingActivityAck(serverPayloadDigest=workout.source_evidence_hash, workoutRevision=1)
        with self.assertRaises(HTTPException):
            route.ack(receipt.id, workout.id, ack, self.request, self.db, self.user)
        self.assertEqual(service.members(self.db, receipt)[0].disposition, "pending")

    def test_empty_finalization_is_unknown_and_cannot_invent_load(self):
        receipt = self.begin()
        service.record_query(self.db, self.user, self.token, receipt.id,
                             QueryOutcome(status="succeeded", observedCount=0, manifestDigest=digest([])))
        service.finalize(self.db, self.user, self.token, receipt.id, NOW)
        projection = service.semantic_context(self.db, self.user, NOW)
        self.assertEqual(projection["sourceCoverage"], "read_access_unknown")
        self.assertEqual(projection["trainingCoverage"], "unknown")
        self.assertIsNone(route.response(self.db, receipt)["lastSuccessfulReconciliationAt"])
        failed = self.begin(inventoryRevision=2)
        service.record_query(self.db, self.user, self.token, failed.id,
                             QueryOutcome(status="failed", observedCount=0, manifestDigest=digest([]), errorCode="query_failed"))
        service.finalize(self.db, self.user, self.token, failed.id, NOW)
        self.assertIsNone(route.response(self.db, failed)["lastSuccessfulReconciliationAt"])
        self.assertFalse(any(isinstance(r, Workout) for r in self.db.rows))

    def test_withdrawal_requires_explicit_fact_and_retains_recorded_load(self):
        with self.assertRaises(ValidationError):
            ManifestPage(members=[dict(sourceId=uuid.uuid4(), disposition="source_withdrawn")])
        receipt, workout = self.begin(), self.workout()
        service.add_page(self.db, self.user, self.token, receipt.id, 0, ManifestPage(members=[
            dict(sourceId=workout.id, disposition="source_withdrawn", explicitSourceDeletion=True)]))
        service.record_query(self.db, self.user, self.token, receipt.id, QueryOutcome(
            status="succeeded", observedCount=0, manifestDigest=service.manifest_digest(service.members(self.db, receipt))))
        service.finalize(self.db, self.user, self.token, receipt.id, NOW)
        self.assertTrue(workout.source_withdrawn)
        self.assertEqual(workout.total_distance, 5000)
        self.assertIs(self.db.get(Workout, workout.id), workout)

    def test_old_revision_and_new_token_cannot_adopt_another_receipt(self):
        receipt = self.begin()
        with self.assertRaises(HTTPException):
            self.begin()
        with self.assertRaises(HTTPException):
            service.owned_receipt(self.db, self.user, receipt.id, uuid.uuid4())


if __name__ == "__main__":
    unittest.main()
