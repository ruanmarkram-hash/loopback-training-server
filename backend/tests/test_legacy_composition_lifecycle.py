"""Persisted legacy prescriptions can retire without rewriting their body."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.main import app
from app.models.coaching import PrescriptionRevision
from app.models.queue import WorkoutQueue
from app.routes.coaching import changed_composition


@pytest.mark.parametrize("operation", ["status", "sync_delete", "sync_patch", "skip", "delete"])
def test_legacy_invalid_composition_lifecycle_preserves_history(client_a, session_factory, operation):
    now = datetime.now(UTC)
    composition = {"blocks": [None]}
    created = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Legacy retirement",
            "scheduledDate": (now + timedelta(days=5)).isoformat(),
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
        },
    )
    assert created.status_code == 201
    item_id = created.json()["id"]
    with session_factory() as db:
        db.execute(update(WorkoutQueue).where(WorkoutQueue.id == uuid.UUID(item_id)).values(workout_data=composition))
        db.commit()
    safe = TestClient(app, headers=dict(client_a.headers), raise_server_exceptions=False)
    if operation == "status":
        response = safe.patch("/api/queue/" + item_id + "/status", json={"status": "completed"})
    elif operation == "sync_delete":
        response = safe.delete("/api/workouts/queue/" + item_id)
    elif operation == "sync_patch":
        response = safe.patch("/api/workouts/queue/" + item_id)
    elif operation == "skip":
        response = safe.post(
            "/api/workouts/feedback",
            json={
                "id": str(uuid.uuid4()),
                "workoutId": item_id,
                "workoutName": "Legacy retirement",
                "scheduledDate": now.isoformat(),
                "detectedAt": now.isoformat(),
                "reason": "busy",
                "action": "skip",
                "dismissed": False,
            },
        )
    else:
        response = safe.delete("/api/queue/" + item_id)
    assert response.status_code in (200, 201, 204), response.text
    with session_factory() as db:
        row = db.get(WorkoutQueue, uuid.UUID(item_id))
        if operation == "delete":
            assert row is None
            revisions = db.scalars(
                select(PrescriptionRevision).where(PrescriptionRevision.workout_id == uuid.UUID(item_id))
            ).all()
            assert any(
                r.snapshot.get("state") == "deleted" and r.snapshot.get("composition") == composition for r in revisions
            )
        else:
            assert row.workout_data == composition
            assert row.status == {"status": "completed", "skip": "skipped"}.get(operation, "synced")


def test_single_goal_unknown_unit_rejected_before_persistence(client_a):
    response = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Unknown unit",
            "workoutData": {"singleGoal": {"type": "distance", "unit": "km", "value": 5}},
        },
    )
    assert response.status_code == 422, response.text


def test_unknown_single_goal_unit_cannot_bypass_change_bound():
    with pytest.raises(HTTPException) as caught:
        changed_composition(
            SimpleNamespace(workout_data={"singleGoal": {"type": "distance", "unit": "km", "value": 5}}),
            {"field": "distance_meters", "value": 5},
        )
    assert caught.value.status_code == 422


def test_supported_single_goal_kilometers_apply_physical_change_bound():
    item = SimpleNamespace(workout_data={"singleGoal": {"type": "distance", "unit": "kilometers", "value": 5}})
    with pytest.raises(HTTPException):
        changed_composition(item, {"field": "distance_meters", "value": 5})
    composition, old = changed_composition(item, {"field": "distance_meters", "value": 4750})
    assert old == 5000 and composition["singleGoal"]["value"] == 4.75


def test_persisted_unknown_single_goal_unit_worker_cannot_claim_bounded_change(client_a, session_factory):
    now = datetime.now(UTC)
    plan = client_a.post(
        "/api/plans", json={"name": "Legacy units", "activityType": "running", "startDate": now.date().isoformat()}
    ).json()
    created = client_a.post(
        "/api/queue",
        json={
            "planId": plan["id"],
            "activityType": "running",
            "title": "Legacy kilometers",
            "scheduledDate": (now + timedelta(days=5)).isoformat(),
            "workoutData": {"singleGoal": {"type": "distance", "unit": "kilometers", "value": 5}},
        },
    )
    assert created.status_code == 201
    item_id = created.json()["id"]
    legacy = {"singleGoal": {"type": "distance", "unit": "km", "value": 5}}
    with session_factory() as db:
        db.execute(update(WorkoutQueue).where(WorkoutQueue.id == uuid.UUID(item_id)).values(workout_data=legacy))
        db.commit()
    token = client_a.post("/api/auth/tokens", json={"name": "Synthetic bounded units", "scope": "coach_worker"}).json()[
        "token"
    ]
    headers = {"Authorization": "Bearer " + token}
    assert (
        client_a.post(
            "/api/coaching/reviews", json={"planId": plan["id"], "idempotencyKey": "Legacy unit bound"}
        ).status_code
        == 201
    )
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    response = client_a.post(
        "/api/coaching/worker/jobs/" + job["id"] + "/result",
        headers=headers,
        json={
            "leaseToken": job["leaseToken"],
            "result": {
                "status": "ok",
                "output": {
                    "status": "propose",
                    "reason": "Synthetic unit bound",
                    "proposedChanges": [
                        {"workoutId": item_id, "field": "distance_meters", "value": 5, "reason": "Synthetic reduction"}
                    ],
                },
                "usage": {},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "dead_letter", response.text
    assert client_a.get("/api/coaching/proposals").json() == []
    with session_factory() as db:
        assert db.get(WorkoutQueue, uuid.UUID(item_id)).workout_data == legacy
