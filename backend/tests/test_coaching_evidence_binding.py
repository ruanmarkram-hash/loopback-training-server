"""Adaptive claim/result/approval bind real owned evidence, not refreshed labels."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest


def claimed_review(client):
    now = datetime.now(UTC)
    plan = client.post(
        "/api/plans",
        json={"name": "Reviewed evidence fixture", "activityType": "running", "startDate": now.date().isoformat()},
    ).json()
    queue = client.post(
        "/api/queue",
        json={
            "planId": plan["id"],
            "activityType": "running",
            "title": "Reviewed future run",
            "scheduledDate": (now + timedelta(days=7)).isoformat(),
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
        },
    ).json()
    activity = {
        "id": str(uuid.uuid4()),
        "activityType": "running",
        "startDate": (now - timedelta(days=2)).isoformat(),
        "endDate": (now - timedelta(days=2) + timedelta(minutes=30)).isoformat(),
        "duration": 1800,
        "totalDistance": 5000,
    }
    assert client.post("/api/workouts", json=activity).status_code == 201
    for automatic in client.get("/api/coaching/jobs").json():
        assert client.post("/api/coaching/jobs/" + automatic["id"] + "/cancel", json={}).status_code == 200
    review = client.post(
        "/api/coaching/reviews", json={"planId": plan["id"], "idempotencyKey": "exact reviewed facts"}
    ).json()
    token = client.post("/api/auth/tokens", json={"name": "Synthetic evidence worker", "scope": "coach_worker"}).json()[
        "token"
    ]
    headers = {"Authorization": "Bearer " + token}
    job = client.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    assert job["id"] == review["id"]
    result = {
        "leaseToken": job["leaseToken"],
        "result": {
            "status": "ok",
            "output": {
                "status": "propose",
                "reason": "Synthetic candidate from exact claimed facts",
                "proposedChanges": [
                    {
                        "workoutId": queue["id"],
                        "field": "distance_meters",
                        "value": 4750,
                        "reason": "Conservative synthetic reduction",
                    }
                ],
            },
        },
    }
    return plan, queue, activity, job, headers, result


def change_evidence(client, kind, queue, activity):
    if kind == "activity":
        response = client.post("/api/workouts", json={**activity, "totalDistance": 7000})
        assert response.status_code == 201
    elif kind == "profile":
        response = client.put("/api/coaching/profile", json={"available_days": ["tue", "thu"], "timezone": "UTC"})
        assert response.status_code == 200
    else:
        response = client.post(
            "/api/workouts/feedback",
            json={
                "id": str(uuid.uuid4()),
                "workoutId": queue["id"],
                "workoutName": queue["title"],
                "scheduledDate": queue["scheduled_date"],
                "detectedAt": datetime.now(UTC).isoformat(),
                "reason": "tired",
                "reasonNote": "Synthetic newly supplied fact",
                "action": "adjust",
                "dismissed": False,
            },
        )
        assert response.status_code == 201


@pytest.mark.parametrize("kind", ["activity", "profile", "feedback"])
def test_changed_claim_evidence_rejects_worker_candidate_without_fresh_labels(client_a, kind):
    plan, queue, activity, job, headers, result = claimed_review(client_a)
    revision = client_a.get("/api/plans/" + plan["id"]).json()["revision"]
    change_evidence(client_a, kind, queue, activity)
    assert client_a.get("/api/plans/" + plan["id"]).json()["revision"] == revision
    before = client_a.get("/api/queue").json()
    response = client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", headers=headers, json=result)
    assert response.status_code == 200
    assert response.json()["status"] == "dead_letter", response.json()
    assert "evidence" in response.json()["data"]["failureReason"].lower()
    assert client_a.get("/api/coaching/proposals").json() == []
    assert client_a.get("/api/queue").json() == before


@pytest.mark.parametrize("kind", ["activity", "profile", "feedback"])
def test_changed_reviewed_evidence_rejects_volume_approval_without_plan_revision_change(client_a, kind):
    plan, queue, activity, job, headers, result = claimed_review(client_a)
    response = client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", headers=headers, json=result)
    assert response.status_code == 200
    proposal = client_a.get("/api/coaching/proposals").json()[0]
    change_evidence(client_a, kind, queue, activity)
    assert client_a.get("/api/plans/" + plan["id"]).json()["revision"] == proposal["base_revision"]
    before = client_a.get("/api/queue").json()
    histories = client_a.get("/api/coaching/plans/" + plan["id"] + "/revisions").json()
    response = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert response.status_code == 409, response.json()
    assert "evidence" in response.json()["detail"].lower()
    assert client_a.get("/api/queue").json() == before
    assert client_a.get("/api/coaching/plans/" + plan["id"] + "/revisions").json() == histories
    assert client_a.get("/api/coaching/proposals").json()[0]["status"] == "superseded"


def test_stale_review_retry_claims_fresh_evidence_and_preserves_exact_provenance(client_a):
    from app.coaching_service import checksum

    plan, queue, activity, job, headers, result = claimed_review(client_a)
    change_evidence(client_a, "profile", queue, activity)
    stale = client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", headers=headers, json=result)
    assert stale.json()["status"] == "dead_letter"
    assert client_a.post("/api/coaching/jobs/" + job["id"] + "/retry", json={}).status_code == 200
    fresh = client_a.post("/api/coaching/worker/claim", headers=headers, json={}).json()["job"]
    assert fresh["id"] == job["id"] and fresh["leaseToken"] != job["leaseToken"]
    assert checksum(fresh["request"]["context"]) != checksum(job["request"]["context"])
    result["leaseToken"] = fresh["leaseToken"]
    assert (
        client_a.post("/api/coaching/worker/jobs/" + fresh["id"] + "/result", headers=headers, json=result).json()[
            "status"
        ]
        == "completed"
    )
    proposal = client_a.get("/api/coaching/proposals").json()[0]
    assert proposal["reviewedEvidenceChecksum"] == checksum(fresh["request"]["context"])
    assert proposal["metrics"] == fresh["request"]["context"]["metrics"]
    assert proposal["evidenceSummary"] == {
        k: v for k, v in fresh["request"]["context"]["assessment"].items() if k != "metrics"
    }
    assert (
        client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        ).status_code
        == 200
    )
    assert client_a.get("/api/plans/" + plan["id"]).json()["revision"] == proposal["base_revision"] + 1
    assert (
        client_a.get("/api/coaching/prescriptions/" + queue["id"]).json()[-1]["snapshot"]["composition"]["singleGoal"][
            "value"
        ]
        == 4750
    )


@pytest.mark.parametrize("phase", ["result", "approval"])
def test_training_sharing_change_invalidates_reviewed_context(client_a, user_a, session_factory, phase):
    from app.models.user import User

    _plan, _queue, _activity, job, headers, result = claimed_review(client_a)
    if phase == "approval":
        assert (
            client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", headers=headers, json=result).json()[
                "status"
            ]
            == "completed"
        )
        proposal = client_a.get("/api/coaching/proposals").json()[0]
    with session_factory() as db:
        actor = db.get(User, user_a[0])
        actor.data_consent = [d for d in actor.data_consent if d != "training"]
        db.commit()
    before = client_a.get("/api/queue").json()
    if phase == "result":
        response = client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", headers=headers, json=result)
        assert response.status_code == 200 and response.json()["status"] == "dead_letter"
        assert client_a.get("/api/coaching/proposals").json() == []
    else:
        response = client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        )
        assert response.status_code == 409
        assert client_a.get("/api/coaching/proposals").json()[0]["status"] == "superseded"
    assert client_a.get("/api/queue").json() == before
