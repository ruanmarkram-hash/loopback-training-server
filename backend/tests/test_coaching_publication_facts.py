"""Current facts remain authoritative after approval and before future publication."""

import uuid
from datetime import timedelta

import pytest

from app.coaching_scheduler import publish_forecast, scan_reviews
from app.models.plan import Plan
from tests.test_coaching_structural import accepted_reduction, approved_program


@pytest.mark.parametrize(
    "facts", [{"restrictions": ["No running until reviewed"]}, {"available_days": []}, {"available_days": ["sun"]}]
)
@pytest.mark.parametrize("caller", ["direct", "scanner"])
def test_publication_requires_current_facts(client_a, session_factory, facts, caller):
    now, pid = approved_program(client_a)
    before = client_a.get("/api/queue").json()
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    assert client_a.put("/api/coaching/profile", json=facts).status_code == 200
    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        if caller == "direct":
            publish_forecast(db, plan.user_id, plan, now + timedelta(days=35))
            db.commit()
        else:
            scan_reviews(db, now + timedelta(days=35))
    assert client_a.get("/api/queue").json() == before
    assert client_a.get("/api/plans/" + pid).json()["revision"] == revision
    program = client_a.get("/api/coaching/programs/" + pid).json()
    assert program["publicationReview"]["status"] == "review_required"
    assert any(not f["prescription_committed"] for f in program["forecast"])


@pytest.mark.parametrize("change", ["profile", "activity"])
def test_undo_approval_rechecks_original_evidence(client_a, change):
    _, plan, _, target, _ = accepted_reduction(client_a)
    preview = client_a.post(
        "/api/coaching/plans/" + plan["id"] + "/undo-preview",
        json={"targetRevision": target, "idempotencyKey": "changed facts undo"},
    )
    assert preview.status_code == 201, preview.text
    proposal = preview.json()["proposal"]
    before = client_a.get("/api/queue").json()
    if change == "profile":
        assert client_a.put("/api/coaching/profile", json={"available_days": ["sun"]}).status_code == 200
    else:
        from datetime import UTC, datetime

        now = datetime.now(UTC) - timedelta(days=1)
        assert (
            client_a.post(
                "/api/workouts",
                json={
                    "id": str(uuid.uuid4()),
                    "activityType": "running",
                    "startDate": now.isoformat(),
                    "endDate": (now + timedelta(minutes=30)).isoformat(),
                    "duration": 1800,
                    "totalDistance": 5000,
                },
            ).status_code
            == 201
        )
    response = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert response.status_code == 409, response.text
    assert client_a.get("/api/queue").json() == before


def test_compatible_publication_and_cached_profile_refresh(client_a, session_factory):
    from app.models.coaching import AthleteProfile

    now, pid = approved_program(client_a)
    assert (
        client_a.put("/api/coaching/profile", json={"available_days": ["mon", "wed", "sat", "sun"]}).status_code == 200
    )
    before = client_a.get("/api/queue").json()
    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        loaded = db.get(AthleteProfile, plan.user_id)
        assert loaded.data["available_days"]
        assert (
            client_a.put(
                "/api/coaching/profile",
                json={"restrictions": ["Maximum 10 minute sessions"], "available_days": ["mon", "wed", "sat", "sun"]},
            ).status_code
            == 200
        )
        publish_forecast(db, plan.user_id, plan, now + timedelta(days=35))
        db.commit()
    assert client_a.get("/api/queue").json() == before
    assert (
        client_a.put("/api/coaching/profile", json={"available_days": ["mon", "wed", "sat", "sun"]}).status_code == 200
    )
    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        publish_forecast(db, plan.user_id, plan, now + timedelta(days=35))
        db.commit()
    assert len(client_a.get("/api/queue").json()) > len(before)


@pytest.mark.parametrize("caller", ["direct", "scanner"])
@pytest.mark.parametrize("scope", ["other_plan", "no_plan", "same_plan"])
@pytest.mark.parametrize("dated", [True, False])
def test_rolling_publication_rejects_unreconciled_owned_commitments(client_a, session_factory, caller, scope, dated):
    now, pid = approved_program(client_a)
    program = client_a.get("/api/coaching/programs/" + pid).json()
    future = next(f for f in program["forecast"] if not f["prescription_committed"])
    other = (
        client_a.post(
            "/api/plans",
            json={"name": "Other calendar", "activityType": "running", "startDate": now.date().isoformat()},
        ).json()
        if scope == "other_plan"
        else None
    )
    extra = client_a.post(
        "/api/queue",
        json={
            "planId": other["id"] if other else (pid if scope == "same_plan" else None),
            "activityType": "running",
            "title": "Unreconciled commitment",
            "scheduledDate": future["scheduledDate"] if dated else None,
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 8000}},
        },
    )
    assert extra.status_code == 201, extra.text
    before = client_a.get("/api/queue").json()
    from datetime import datetime

    publish_at = datetime.fromisoformat(future["scheduledDate"]) - timedelta(days=3)
    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        if caller == "direct":
            publish_forecast(db, plan.user_id, plan, publish_at)
            db.commit()
        else:
            scan_reviews(db, publish_at)
    assert client_a.get("/api/queue").json() == before
    result = client_a.get("/api/coaching/programs/" + pid).json()
    assert result["publicationReview"]["status"] == "review_required"
    assert extra.json()["id"] in result["publicationReview"]["unrepresentedCommittedWorkoutIds"]
    assert result["forecast"] == program["forecast"]


@pytest.mark.parametrize("excluded", ["completed", "skipped", "past", "foreign"])
def test_publication_preserves_excluded_commitments_and_matching_managed_sessions(
    client_a, client_b, session_factory, excluded
):
    now, pid = approved_program(client_a)
    program = client_a.get("/api/coaching/programs/" + pid).json()
    future = next(f for f in program["forecast"] if not f["prescription_committed"])
    from datetime import datetime

    publish_at = datetime.fromisoformat(future["scheduledDate"]) - timedelta(days=3)
    owner = client_b if excluded == "foreign" else client_a
    extra = owner.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Excluded commitment",
            "scheduledDate": (now - timedelta(days=1)).isoformat() if excluded == "past" else None,
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 8000}},
        },
    ).json()
    if excluded in {"completed", "skipped"}:
        assert owner.patch("/api/queue/" + extra["id"] + "/status", json={"status": excluded}).status_code == 200
    before = client_a.get("/api/queue").json()
    other_before = client_b.get("/api/queue").json()
    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        publish_forecast(db, plan.user_id, plan, publish_at)
        db.commit()
    after = client_a.get("/api/queue").json()
    assert len(after) > len(before)
    for row in before:
        assert next(q for q in after if q["id"] == row["id"]) == row
    assert client_b.get("/api/queue").json() == other_before
    assert client_a.get("/api/coaching/programs/" + pid).json()["publicationReview"]["status"] == "compatible"
