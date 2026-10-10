"""Derived evidence contract; no raw HealthKit payloads or network calls."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid
from app.coaching_policy import assess


def sample():
    revision = uuid.uuid4()
    start = datetime(2026, 10, 1, 9, tzinfo=UTC)
    steps = [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 1000}, "alert": {"type": "speed", "unit": "metersPerSecond", "min": 2.5, "max": 3}}]
    prescription = SimpleNamespace(id=revision, created_at=start-timedelta(days=1), snapshot={"composition": {"trainingPurpose": "quality", "blocks": [{"iterations": 2, "steps": steps}]}})
    workout = SimpleNamespace(start_date=start, data={"prescriptionRevision": str(revision), "privateNote": "never export", "route": [1, 2], "prescribedSegments": [{"stepIndex": i, "purpose": "work", "distanceMeters": 1000, "durationSeconds": pace, "sampleCoverage": .95, "measurementSource": "healthkit", "targetPaceMin": 1} for i, pace in enumerate([270, 280])]})
    return workout, prescription


def test_eligible_quality_exports_bounded_original_measured_evidence():
    workout, prescription = sample()
    result = assess(workout, prescription)
    evidence = result["qualityEvidence"]
    assert result["pace_evidence"] == "eligible"
    assert abs(evidence["paceVariabilityFraction"] - 10/275) < 1e-12
    assert {k:v for k,v in evidence.items() if k!="paceVariabilityFraction"} == {"sessionDate": workout.start_date.isoformat(), "prescriptionRevision": str(prescription.id), "qualityStepCount": 2, "measuredPaceMinSecondsPerKm": 270, "measuredPaceMaxSecondsPerKm": 280, "originalPaceMinSecondsPerKm": 1000/3, "originalPaceMaxSecondsPerKm": 400, "minimumSampleCoverage": .95, "minimumStepCompletion": 1}
    assert "privateNote" not in repr(result) and "route" not in result
    assert "prescribedSegments" not in result


def assert_disqualified_session_cannot_export_positive_evidence(bad):
    workout, prescription = sample()
    if bad == "revision": workout.data["prescriptionRevision"] = "unknown"
    if bad == "partial": workout.data["prescribedSegments"].pop()
    if bad == "coverage": workout.data["prescribedSegments"][0]["sampleCoverage"] = .5
    if bad == "variable": workout.data["prescribedSegments"][1]["durationSeconds"] = 500
    if bad == "easy": prescription.snapshot["composition"]["trainingPurpose"] = "easy"
    if bad == "issuance": prescription.created_at = workout.start_date + timedelta(seconds=1)
    if bad == "nonfinite": workout.data["prescribedSegments"][0]["durationSeconds"] = float("inf")
    if bad == "overflow_bounds": prescription.snapshot["composition"]["blocks"][0]["steps"][0]["alert"].update(min=1e-308, max=1e-308)
    if bad == "overflow_completion": prescription.snapshot["composition"]["blocks"][0]["steps"][0]["goal"]["value"] = 1e-308
    result = assess(workout, prescription)
    assert result["pace_evidence"] != "eligible"
    assert "qualityEvidence" not in result


def test_disqualified_sessions_cannot_export_positive_evidence():
    for bad in ["revision", "partial", "coverage", "variable", "easy", "issuance", "nonfinite", "overflow_bounds", "overflow_completion"]:
        assert_disqualified_session_cannot_export_positive_evidence(bad)


def test_actual_claim_context_contains_derived_evidence_not_raw_activity(client_a, session_factory):
    from sqlalchemy import update
    from app.models.coaching import PrescriptionRevision
    now = datetime.now(UTC)
    plan = client_a.post("/api/plans", json={"name": "Synthetic context", "activityType": "running", "startDate": (now-timedelta(days=20)).date().isoformat()}).json()
    _workout, original = sample()
    q = client_a.post("/api/queue", json={"planId": plan["id"], "activityType": "running", "title": "Synthetic historical quality", "scheduledDate": (now-timedelta(days=5)).isoformat(), "workoutData": original.snapshot["composition"]}).json()
    revision = client_a.get("/api/coaching/prescriptions/"+q["id"]).json()[0]["id"]
    with session_factory() as db:
        db.execute(update(PrescriptionRevision).where(PrescriptionRevision.id==uuid.UUID(revision)).values(created_at=now-timedelta(days=20)))
        db.commit()
    start = now-timedelta(days=5)
    body = {"id": str(uuid.uuid4()), "activityType": "running", "startDate": start.isoformat(), "endDate": (start+timedelta(seconds=550)).isoformat(), "duration": 550, "totalDistance": 2000, "planWorkoutId": q["id"], "data": {**_workout.data, "prescriptionRevision": revision}}
    assert client_a.post("/api/workouts", json=body).status_code == 201
    assert client_a.post("/api/coaching/reviews", json={"idempotencyKey": "synthetic-derived-context", "planId": plan["id"]}).status_code == 201
    worker = client_a.post("/api/auth/tokens", json={"name": "synthetic context worker", "scope": "coach_worker"}).json()["token"]
    context = client_a.post("/api/coaching/worker/claim", json={}, headers={"Authorization": "Bearer "+worker}).json()["job"]["request"]["context"]
    evidence = context["assessments"][0]["qualityEvidence"]
    assert evidence["sessionDate"] == start.isoformat()
    assert evidence["measuredPaceMinSecondsPerKm"] == 270
    assert evidence["originalPaceMinSecondsPerKm"] == 1000/3
    assert evidence["prescriptionRevision"] == revision
    assert "never export" not in repr(context)
    assert "prescribedSegments" not in repr(context)
    assert "privateNote" not in repr(context)


def test_subnormal_quality_pace_mean_does_not_underflow_or_raise():
    workout, prescription = sample()
    step=prescription.snapshot["composition"]["blocks"][0]["steps"][0]
    step["goal"]["value"]=1
    prescription.snapshot["composition"]["blocks"][0]["iterations"]=100
    prescription.snapshot["composition"]["blocks"][0]["steps"]=[step.copy() for _ in range(20)]
    workout.data["prescribedSegments"]=[{"stepIndex":i,"purpose":"work","distanceMeters":1,"durationSeconds":5e-324,"measurementSource":"healthkit","sampleCoverage":1} for i in range(2000)]
    result=assess(workout,prescription)
    assert result["pace_evidence"]=="eligible"
    assert result["qualityEvidence"]["paceVariabilityFraction"]==0
    assert result["qualityEvidence"]["qualityStepCount"]==2000


def test_underflowing_converted_speed_is_ineligible_without_exception():
    workout, prescription = sample()
    alert = prescription.snapshot["composition"]["blocks"][0]["steps"][0]["alert"]
    alert.update(unit="kilometersPerHour", min=5e-324, max=5e-324)
    result = assess(workout, prescription)
    assert result["pace_evidence"] == "ineligible"
    assert "qualityEvidence" not in result
