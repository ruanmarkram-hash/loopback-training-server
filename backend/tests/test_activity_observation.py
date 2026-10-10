"""Prepared PostgreSQL/API integration cases. Root runs against isolated test PG only."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.activity_observation import canonical_workout, digest
from app.activity_observation_service import scan_staleness
from app.models.activity_observation import ActivityObservationMember
from app.models.coaching import PrescriptionRevision, ReviewJob
from app.models.user import User
from app.models.workout import Workout
from app.schemas.workout import WorkoutCreate


def begin(client, revision=1, method="sample_no_limit"):
    now = datetime.now(UTC)-timedelta(seconds=2)
    body = dict(observationId=str(uuid.uuid4()), producer="synthetic-test/1", source="healthkit",
                inventoryRevision=revision, rangeStart=(now-timedelta(days=180)).isoformat(), rangeEnd=now.isoformat(),
                timezone="UTC", boundary="strict_start_date", queryMethod=method)
    response = client.post("/api/activity-observations", json=body)
    assert response.status_code == 201, response.text
    return body["observationId"]


def workout_payload(**changes):
    start = datetime.now(UTC)-timedelta(days=2)
    return {**dict(id=str(uuid.uuid4()), activityType="running", startDate=start.isoformat(),
                   endDate=(start+timedelta(minutes=30)).isoformat(), duration=1800, totalDistance=5000, data={}), **changes}


def declare(client, observation_id, body, disposition="pending", **changes):
    content_hash = digest(canonical_workout(WorkoutCreate(**body)))
    member = dict(sourceId=body["id"], payloadDigest=content_hash, disposition=disposition,
                  aliasOf=None, explicitSourceDeletion=False)
    member.update(changes)
    response = client.put(f"/api/activity-observations/{observation_id}/manifest/0", json={"members": [member]})
    assert response.status_code == 200, response.text
    observed = 0 if disposition == "source_withdrawn" else 1
    response = client.put(f"/api/activity-observations/{observation_id}/query", json=dict(
        status="succeeded", observedCount=observed, manifestDigest=digest([member])))
    assert response.status_code == 200, response.text
    return content_hash


def upload(client, observation_id, body, content_hash):
    return client.post("/api/workouts", json={**body, "observation": dict(
        observationId=observation_id, sourceId=body["id"], payloadDigest=content_hash)})


def observation_jobs(session_factory, user_id):
    with session_factory() as db:
        return [j for j in db.scalars(select(ReviewJob).where(ReviewJob.user_id == user_id)).all()
                if j.idempotency_key.startswith("observation:")]


def test_owned_device_receipts_and_typed_inputs(client_a, client_b, client_admin):
    oid = begin(client_a)
    assert client_b.get(f"/api/activity-observations/{oid}").status_code == 404
    assert client_b.post(f"/api/activity-observations/{oid}/finalize").status_code == 404
    assert client_admin.get(f"/api/activity-observations/{oid}").status_code == 403
    assert client_a.put(f"/api/activity-observations/{oid}/query", json=dict(
        status="succeeded", observedCount=True, manifestDigest=digest([]))).status_code == 422
    assert client_a.put(f"/api/activity-observations/{oid}/manifest/100", json={"members": []}).status_code == 422


def test_pending_member_cannot_finalize_then_real_upload_ack_and_retry_are_stable(client_a, user_a, session_factory):
    oid, body = begin(client_a), workout_payload()
    content_hash = declare(client_a, oid, body)
    assert client_a.post(f"/api/activity-observations/{oid}/finalize").status_code == 409
    first = upload(client_a, oid, body, content_hash)
    assert first.status_code == 201, first.text
    record = first.json()
    assert record["source_evidence_hash"] == content_hash
    assert record["source_evidence_revision"] == 1
    second = upload(client_a, oid, body, content_hash)
    assert second.status_code == 201, second.text
    assert second.json()["source_evidence_revision"] == record["source_evidence_revision"]
    assert second.json()["updated_at"] == record["updated_at"]
    final = client_a.post(f"/api/activity-observations/{oid}/finalize")
    assert final.status_code == 200, final.text
    assert final.json()["sourceCoverage"] == "within_window_observed"
    assert final.json()["trainingCoverage"] == "unknown"
    assert final.json()["lastSuccessfulReconciliationAt"] == final.json()["finalizedAt"]
    assert client_a.get(f"/api/activity-observations/{oid}").json()["lastSuccessfulReconciliationAt"] == final.json()["finalizedAt"]
    retry = client_a.post(f"/api/activity-observations/{oid}/finalize")
    assert retry.json()["finalizedAt"] == final.json()["finalizedAt"]
    assert retry.json()["lastSuccessfulReconciliationAt"] == final.json()["finalizedAt"]
    assert len(observation_jobs(session_factory, user_a[0])) == 1


def test_changed_payload_cannot_hide_under_registered_digest(client_a):
    oid, body = begin(client_a), workout_payload()
    h = declare(client_a, oid, body)
    rejected = upload(client_a, oid, {**body, "totalDistance": 6000}, h)
    assert rejected.status_code == 409
    assert client_a.get(f"/api/workouts/{body['id']}").status_code == 404
    assert client_a.get(f"/api/activity-observations/{oid}").json()["pendingCount"] == 1


def test_prescription_alias_normalization_binds_actual_server_content(client_a, user_a, session_factory):
    queued = client_a.post("/api/queue", json=dict(title="Synthetic issued run", activityType="running",
                          workoutData={"activityType": "running", "singleGoal": {
                              "type": "distance", "value": 5000, "unit": "meters"}}))
    assert queued.status_code == 201, queued.text
    issued = queued.json()
    with session_factory() as db:
        original = db.scalar(select(PrescriptionRevision).where(PrescriptionRevision.workout_id == uuid.UUID(issued["id"])))
        original_id = str(original.id)
    body = workout_payload(planWorkoutId=original_id)
    oid = begin(client_a)
    client_hash = declare(client_a, oid, body)
    response = upload(client_a, oid, body, client_hash)
    assert response.status_code == 201, response.text
    actual = response.json()
    assert actual["plan_workout_id"] == issued["id"]
    assert actual["source_evidence_hash"] != client_hash
    with session_factory() as db:
        row = db.get(Workout, uuid.UUID(body["id"]))
        member = db.get(ActivityObservationMember, (uuid.UUID(oid), row.id))
        assert member.payload_digest == client_hash
        assert member.server_payload_digest == digest(canonical_workout(row)) == actual["source_evidence_hash"]
    assert client_a.post(f"/api/activity-observations/{oid}/finalize").status_code == 200
    # Same normalized stored evidence, now using the logical-ID transport form.
    # Its incoming digest differs from the prior alias but must not create a job.
    second = begin(client_a, 2)
    logical_body = {**body, "planWorkoutId": issued["id"], "data": {"prescriptionRevision": original_id}}
    normalized_hash = declare(client_a, second, logical_body)
    assert normalized_hash == actual["source_evidence_hash"]
    ack = client_a.post(f"/api/activity-observations/{second}/members/{body['id']}/ack", json=dict(
        serverPayloadDigest=actual["source_evidence_hash"], workoutRevision=actual["source_evidence_revision"]))
    assert ack.status_code == 200, ack.text
    assert client_a.post(f"/api/activity-observations/{second}/finalize").status_code == 200
    assert len(observation_jobs(session_factory, user_a[0])) == 1


def test_unchanged_rescan_then_staleness_are_one_transition_each(client_a, user_a, session_factory):
    body = workout_payload()
    oid = begin(client_a)
    h = declare(client_a, oid, body)
    actual = upload(client_a, oid, body, h).json()
    assert client_a.post(f"/api/activity-observations/{oid}/finalize").status_code == 200
    second = begin(client_a, 2)
    declare(client_a, second, body)
    ack = client_a.post(f"/api/activity-observations/{second}/members/{body['id']}/ack", json=dict(
        serverPayloadDigest=actual["source_evidence_hash"], workoutRevision=actual["source_evidence_revision"]))
    assert ack.status_code == 200, ack.text
    assert ack.json()["memberAck"]["serverPayloadDigest"] == actual["source_evidence_hash"]
    final = client_a.post(f"/api/activity-observations/{second}/finalize")
    assert final.status_code == 200, final.text
    assert len(observation_jobs(session_factory, user_a[0])) == 1
    at = datetime.fromisoformat(final.json()["finalizedAt"])+timedelta(seconds=901)
    for _ in range(2):
        with session_factory() as db:
            user = db.get(User, user_a[0])
            scan_staleness(db, user, at)
            db.commit()
    assert len(observation_jobs(session_factory, user_a[0])) == 2


def test_terminal_ack_invalidation_and_revision_mismatch_fail_closed(client_a):
    oid, body = begin(client_a), workout_payload()
    h = declare(client_a, oid, body)
    actual = upload(client_a, oid, body, h).json()
    assert client_a.post(f"/api/activity-observations/{oid}/finalize").status_code == 200
    corrected = client_a.post("/api/workouts", json={**body, "totalDistance": 5500})
    assert corrected.status_code == 201
    assert corrected.json()["source_evidence_revision"] == actual["source_evidence_revision"]+1
    historical = client_a.get(f"/api/activity-observations/{oid}").json()
    assert historical["invalidatedCount"] == 1
    assert historical["sourceCoverage"] == "unknown"
    new_id = begin(client_a, 2)
    declare(client_a, new_id, body)
    assert client_a.post(f"/api/activity-observations/{new_id}/members/{body['id']}/ack", json=dict(
        serverPayloadDigest=actual["source_evidence_hash"], workoutRevision=actual["source_evidence_revision"])).status_code == 409


def test_empty_and_failed_queries_preserve_unknown(client_a):
    oid = begin(client_a)
    outcome = dict(status="succeeded", observedCount=0, manifestDigest=digest([]))
    assert client_a.put(f"/api/activity-observations/{oid}/query", json=outcome).status_code == 200
    final = client_a.post(f"/api/activity-observations/{oid}/finalize").json()
    assert final["sourceCoverage"] == "read_access_unknown"
    assert final["trainingCoverage"] == "unknown"
    failed_id = begin(client_a, 2)
    failure = {**outcome, "status": "failed", "errorCode": "query_failed"}
    assert client_a.put(f"/api/activity-observations/{failed_id}/query", json=failure).status_code == 200
    failed = client_a.post(f"/api/activity-observations/{failed_id}/finalize").json()
    assert failed["status"] == "failed"
    assert failed["exhaustion"] == "unknown"
    assert client_a.get("/api/workouts").json() == []


def test_explicit_source_withdrawal_retains_actual_workout_and_distance(client_a):
    body = workout_payload()
    assert client_a.post("/api/workouts", json=body).status_code == 201
    oid = begin(client_a)
    declare(client_a, oid, body, "source_withdrawn", payloadDigest=None, explicitSourceDeletion=True)
    assert client_a.post(f"/api/activity-observations/{oid}/finalize").status_code == 200
    retained = client_a.get(f"/api/workouts/{body['id']}").json()
    assert retained["total_distance"] == 5000
    assert retained["source_withdrawn"] is True
    assert len(client_a.get("/api/workouts").json()) == 1


def test_concurrent_finalize_creates_one_owned_observation_job(client_a, user_a, session_factory):
    oid, body = begin(client_a), workout_payload()
    h = declare(client_a, oid, body)
    assert upload(client_a, oid, body, h).status_code == 201
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda _: client_a.post(f"/api/activity-observations/{oid}/finalize"), range(2)))
    assert [response.status_code for response in outcomes] == [200, 200]
    assert outcomes[0].json()["finalizedAt"] == outcomes[1].json()["finalizedAt"]
    assert len(observation_jobs(session_factory, user_a[0])) == 1


def test_superseded_pending_upload_and_withdrawal_cannot_change_new_revision(client_a):
    body = workout_payload()
    assert client_a.post("/api/workouts", json=body).status_code == 201
    old_upload = begin(client_a)
    old_hash = declare(client_a, old_upload, body)
    old_withdrawal = begin(client_a, 2)
    declare(client_a, old_withdrawal, body, "source_withdrawn", payloadDigest=None, explicitSourceDeletion=True)
    current = begin(client_a, 3)
    corrected = {**body, "totalDistance": 6000}
    current_hash = declare(client_a, current, corrected)
    applied = upload(client_a, current, corrected, current_hash)
    assert applied.status_code == 201, applied.text
    assert client_a.post(f"/api/activity-observations/{current}/finalize").status_code == 200
    before = client_a.get(f"/api/workouts/{body['id']}").json()
    assert upload(client_a, old_upload, body, old_hash).status_code == 409
    assert client_a.post(f"/api/activity-observations/{old_withdrawal}/finalize").status_code == 409
    assert client_a.post(f"/api/activity-observations/{old_upload}/members/{body['id']}/ack", json=dict(
        serverPayloadDigest=before["source_evidence_hash"], workoutRevision=before["source_evidence_revision"])).status_code == 409
    after = client_a.get(f"/api/workouts/{body['id']}").json()
    assert after["total_distance"] == 6000
    assert after["source_withdrawn"] is False
    assert after["source_evidence_hash"] == before["source_evidence_hash"]
    assert after["source_evidence_revision"] == before["source_evidence_revision"]
    # Already-terminal retries remain immutable even after a new pending scan.
    terminal = client_a.get(f"/api/activity-observations/{current}").json()
    begin(client_a, 4)
    retry = client_a.post(f"/api/activity-observations/{current}/finalize")
    assert retry.status_code == 200
    assert retry.json()["finalizedAt"] == terminal["finalizedAt"]


def test_legacy_api_update_serializes_before_receipt_finalization(client_a, engine):
    """Verify the existing auth lock, without claiming a prior production race."""
    from threading import Event
    from sqlalchemy import event
    body = workout_payload()
    oid = begin(client_a)
    h = declare(client_a, oid, body)
    assert upload(client_a, oid, body, h).status_code == 201
    writer_waiting, release_writer, final_lock_attempted = Event(), Event(), Event()

    def pause_owned_update(connection, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("UPDATE WORKOUT SET"):
            writer_waiting.set()
            if not release_writer.wait(5):
                raise AssertionError("Bounded test did not release the legacy writer")
        elif writer_waiting.is_set() and "FROM USERS" in normalized and "FOR UPDATE" in normalized:
            final_lock_attempted.set()

    event.listen(engine, "before_cursor_execute", pause_owned_update)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            writer = workers.submit(client_a.post, "/api/workouts", json={**body, "totalDistance": 6000})
            try:
                assert writer_waiting.wait(5), "Legacy update did not reach owned transaction boundary"
                final = workers.submit(client_a.post, f"/api/activity-observations/{oid}/finalize")
                assert final_lock_attempted.wait(5), "Finalization did not attempt the athlete lock"
                assert not final.done(), "Finalization crossed an already-held athlete mutation lock"
            finally:
                release_writer.set()
            assert writer.result(timeout=10).status_code == 201
            assert final.result(timeout=10).status_code == 409
    finally:
        release_writer.set()
        event.remove(engine, "before_cursor_execute", pause_owned_update)
    actual = client_a.get(f"/api/activity-observations/{oid}").json()
    assert actual["status"] == "pending"
    assert actual["invalidatedCount"] == 1
    assert actual["sourceCoverage"] == "unknown"
