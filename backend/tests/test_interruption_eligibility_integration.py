"""Prepared real-route regressions, for root's authorized disposable DB harness."""
from datetime import UTC, datetime, timedelta
import uuid


def missed_prescriptions(client):
    now = datetime.now(UTC)
    response = client.post("/api/plans", json={"name": "Synthetic interruption", "activityType": "running", "startDate": (now-timedelta(days=7)).date().isoformat()})
    assert response.status_code == 201
    plan = response.json()
    rows = []
    for i in range(3):
        scheduled = (now-timedelta(days=i+1)).isoformat()
        response = client.post("/api/queue", json={"planId": plan["id"], "activityType": "running", "title": "Synthetic missed run", "scheduledDate": scheduled, "workoutData": {"activityType": "running", "singleGoal": {"type": "distance", "unit": "meters", "value": 5000}}})
        assert response.status_code == 201
        row = response.json()
        body = {"id": str(uuid.uuid4()), "workoutId": row["id"], "workoutName": "Synthetic missed run", "scheduledDate": scheduled, "detectedAt": now.isoformat(), "reason": "busy", "action": "skip", "dismissed": False}
        assert client.post("/api/workouts/feedback", json=body).status_code == 201
        rows.append((row, body))
    return plan, rows


def assert_context(status, count):
    assert status["assessment"]["explicitMissCount"] == count
    assert status["assessment"]["interruptionReview"] is (count >= 3)
    assert len(status["athleteReports"]["missedWorkoutFeedback"]) == count
    assert status["athleteReports"]["coverage"] == "unknown"
    assert status["athleteReports"]["readiness"] == "unknown"
    assert status["assessment"]["volumeStatus"] == "recorded_load_monitoring"


def test_actual_completed_to_skipped_transition_keeps_timestamp_and_excludes_miss(client_a):
    plan, rows = missed_prescriptions(client_a)
    assert_context(client_a.get("/api/coaching/status").json(), 3)
    target = rows[-1][0]
    completed = client_a.patch("/api/queue/"+target["id"]+"/status", json={"status": "completed"})
    assert completed.status_code == 200
    timestamp = completed.json()["completed_at"]
    assert timestamp is not None
    skipped = client_a.patch("/api/queue/"+target["id"]+"/status", json={"status": "skipped"})
    assert skipped.status_code == 200 and skipped.json()["completed_at"] == timestamp
    assert_context(client_a.get("/api/coaching/status").json(), 2)
    assert client_a.post("/api/coaching/reviews", json={"idempotencyKey": "completed-skip-context", "planId": plan["id"]}).status_code == 201
    token = client_a.post("/api/auth/tokens", json={"name": "synthetic worker", "scope": "coach_worker"}).json()["token"]
    job = client_a.post("/api/coaching/worker/claim", json={}, headers={"Authorization": "Bearer "+token}).json()["job"]
    context = job["request"]["context"]
    assert context["assessment"]["explicitMissCount"] == 2
    assert context["athleteReports"] == client_a.get("/api/coaching/status").json()["athleteReports"]
    assert client_a.get("/api/coaching/proposals").json() == []


def test_owned_feedback_upsert_and_actual_reschedule_context(client_a, client_b):
    _, rows = missed_prescriptions(client_a)
    assert_context(client_a.get("/api/coaching/status").json(), 3)
    target, feedback = rows[-1]
    assert client_b.post("/api/workouts/feedback", json={**feedback, "id": str(uuid.uuid4())}).status_code == 404
    assert_context(client_b.get("/api/coaching/status").json(), 0)
    # The existing producer upserts one feedback per owned workout identity.
    assert client_a.post("/api/workouts/feedback", json={**feedback, "id": str(uuid.uuid4())}).status_code == 201
    assert_context(client_a.get("/api/coaching/status").json(), 3)
    response = client_a.patch("/api/queue/"+target["id"], json={"scheduledDate": (datetime.now(UTC)+timedelta(days=3)).isoformat()})
    assert response.status_code == 200
    assert_context(client_a.get("/api/coaching/status").json(), 2)
