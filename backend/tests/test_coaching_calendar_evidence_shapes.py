"""Owned complete-calendar boundaries and optional execution evidence shapes."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_coaching_review_scope import rebuild_fixture


def extra_commitment(client, current, other_plan):
    pid = None
    if other_plan:
        p = client.post(
            "/api/plans",
            json={"name": "Separate commitment", "activityType": "running", "startDate": current.date().isoformat()},
        )
        assert p.status_code == 201
        pid = p.json()["id"]
    response = client.post(
        "/api/queue",
        json={
            "planId": pid,
            "activityType": "running",
            "title": "Other calendar commitment",
            "scheduledDate": (current + timedelta(days=3)).isoformat(),
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 20000}},
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.parametrize("other_plan", [False, True])
def test_rebuild_accounts_for_all_owned_outstanding_runs(client_a, monkeypatch, session_factory, user_a, other_plan):
    current, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    extra = extra_commitment(client_a, current, other_plan)
    before = client_a.get("/api/queue").json()
    response = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "review_required"
    assert response.json()["forecast"] is None and response.json()["proposal"] is None
    assert extra["id"] in response.json()["evidence"]["unrepresentedCommittedWorkoutIds"]
    assert client_a.get("/api/queue").json() == before


@pytest.mark.parametrize(
    "segment",
    [
        None,
        "not an object",
        2,
        [],
        {"stepIndex": False},
        {"stepIndex": 0, "purpose": "work", "durationSeconds": {}, "distanceMeters": 1000},
    ],
)
@pytest.mark.parametrize("upsert", [False, True])
def test_malformed_optional_segment_preserves_upload_but_never_execution(client_a, segment, upsert):
    composition = {
        "trainingPurpose": "quality",
        "blocks": [
            {
                "iterations": 1,
                "steps": [
                    {
                        "purpose": "work",
                        "goal": {"type": "distance", "unit": "meters", "value": 1000},
                        "alert": {"type": "speed", "unit": "metersPerSecond", "min": 2.5, "max": 3},
                    }
                ],
            }
        ],
    }
    q = client_a.post(
        "/api/queue", json={"activityType": "running", "title": "Issued quality", "workoutData": composition}
    ).json()
    revision = client_a.get("/api/coaching/prescriptions/" + q["id"]).json()[0]["id"]
    start = datetime.now(UTC) + timedelta(seconds=1)
    body = {
        "id": str(uuid.uuid4()),
        "activityType": "running",
        "startDate": start.isoformat(),
        "endDate": (start + timedelta(seconds=300)).isoformat(),
        "duration": 300,
        "totalDistance": 1000,
        "planWorkoutId": q["id"],
        "data": {"prescriptionRevision": revision, "prescribedSegments": [segment]},
    }
    if upsert:
        assert client_a.post("/api/workouts", json={**body, "data": {}}).status_code == 201
    response = client_a.post("/api/workouts", json=body)
    assert response.status_code == 201, response.text
    assert len(client_a.get("/api/workouts").json()) == 1
    assessment = client_a.get("/api/coaching/assessments").json()[0]
    assert assessment["pace_evidence"] == "ineligible"
    assert assessment["execution"] == "not_assessable"


@pytest.mark.parametrize("other_plan", [False, True])
def test_new_other_commitment_invalidates_rebuild_approval(client_a, monkeypatch, session_factory, user_a, other_plan):
    current, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    preview = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert preview.status_code == 201, preview.text
    assert preview.json()["status"] == "forecast_ready", preview.text
    proposal = preview.json()["proposal"]
    extra_commitment(client_a, current, other_plan)
    before = client_a.get("/api/queue").json()
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    assert revision == proposal["base_revision"]
    response = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": revision}
    )
    assert response.status_code == 409, response.text
    assert client_a.get("/api/queue").json() == before
    assert client_a.get("/api/plans/" + pid).json()["revision"] == revision
