"""New standalone programs do not implicitly merge existing commitments."""

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.parametrize("commitment", ["same_day", "overload", "unknown_date"])
def test_initial_program_fails_closed_with_unreconciled_committed_running_calendar(client_a, commitment):
    now = datetime.now(UTC)
    plan = client_a.post(
        "/api/plans",
        json={"name": "Existing athlete calendar", "activityType": "running", "startDate": now.date().isoformat()},
    ).json()
    start = (now + timedelta(days=3)).date()
    body = {
        "goal": {"type": "10k", "race_date": (now + timedelta(days=150)).date().isoformat()},
        "available_days": ["mon", "wed", "sat"],
        "start_date": start.isoformat(),
        "benchmark": {
            "pace_seconds_per_km": 360,
            "weekly_distance_meters": 15000,
            "long_run_meters": 6000,
            "source": "Synthetic explicit current ability",
            "observed_at": now.isoformat(),
        },
    }
    # Same day chosen from the actual deterministic draft calendar, before issuing a request.
    from uuid import uuid4

    from app.coaching_program import program_forecast

    forecast, _ = program_forecast(
        uuid4(), body["goal"], body["available_days"], body["benchmark"], start, "Australia/Brisbane"
    )
    at = forecast[0]["scheduledDate"] if commitment == "same_day" else (now + timedelta(days=5)).isoformat()
    q = client_a.post(
        "/api/queue",
        json={
            "planId": plan["id"],
            "activityType": "running",
            "title": "Already committed run",
            "scheduledDate": None if commitment == "unknown_date" else at,
            "workoutData": {
                "singleGoal": {
                    "type": "distance",
                    "unit": "meters",
                    "value": 45000 if commitment == "overload" else 5000,
                }
            },
        },
    )
    assert q.status_code == 201
    before = client_a.get("/api/queue").json()
    response = client_a.post("/api/coaching/program", json=body)
    if response.status_code == 201:
        # Preserve the concrete unsafe acceptance evidence, not only a preview assertion.
        proposal = response.json()["proposal"]
        applied = client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        )
        pytest.fail(f"Unreconciled {commitment} preview accepted HTTP{applied.status_code}: {applied.json()}")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["status"] == "review_required"
    assert response.json()["detail"]["forecast"] is None and response.json()["detail"]["proposal"] is None
    assert q.json()["id"] in response.json()["detail"]["committedWorkoutIds"]
    assert client_a.get("/api/queue").json() == before
    assert len(client_a.get("/api/plans").json()) == 1


def test_legacy_initial_draft_approval_rechecks_combined_calendar_even_with_matching_evidence(client_a, monkeypatch):
    import app.routes.coaching as routes

    now = datetime.now(UTC)
    plan = client_a.post(
        "/api/plans",
        json={"name": "Preserved prior plan", "activityType": "running", "startDate": now.date().isoformat()},
    ).json()
    q = client_a.post(
        "/api/queue",
        json={
            "planId": plan["id"],
            "activityType": "running",
            "title": "Prior committed run",
            "scheduledDate": (now + timedelta(days=5)).isoformat(),
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
        },
    ).json()
    body = {
        "goal": {"type": "10k", "race_date": (now + timedelta(days=150)).date().isoformat()},
        "available_days": ["mon", "wed", "sat"],
        "start_date": (now + timedelta(days=3)).date().isoformat(),
        "benchmark": {
            "pace_seconds_per_km": 360,
            "weekly_distance_meters": 15000,
            "long_run_meters": 6000,
            "source": "Synthetic legacy explicit ability",
            "observed_at": now.isoformat(),
        },
    }
    # Fixture only: represent a durable pre-gate draft using its original HTTP producer.
    # Actual earlier RED proves this producer accepted such calendars; approval is unstubbed.
    with monkeypatch.context() as legacy:
        legacy.setattr(routes, "require_initial_calendar", lambda *args, **kwargs: None)
        preview = client_a.post("/api/coaching/program", json=body)
    assert preview.status_code == 201, preview.text
    proposal = preview.json()["proposal"]
    before = client_a.get("/api/queue").json()
    histories = client_a.get("/api/coaching/plans/" + plan["id"] + "/revisions").json()
    result = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert result.status_code == 409, result.json()
    assert result.json()["detail"]["status"] == "review_required"
    assert q["id"] in result.json()["detail"]["committedWorkoutIds"]
    assert client_a.get("/api/queue").json() == before
    assert client_a.get("/api/coaching/plans/" + plan["id"] + "/revisions").json() == histories
    assert client_a.get("/api/plans/" + preview.json()["plan"]["id"]).json()["status"] == "draft"
    assert client_a.get("/api/coaching/proposals").json()[0]["status"] == "superseded"


@pytest.mark.parametrize("outside", ["completed", "after_program"])
def test_initial_gate_does_not_delete_history_or_block_commitments_outside_its_window(client_a, outside):
    now = datetime.now(UTC)
    race = now + timedelta(days=150)
    plan = client_a.post(
        "/api/plans",
        json={"name": "Other legitimate plan", "activityType": "running", "startDate": now.date().isoformat()},
    ).json()
    at = (race + timedelta(days=7)).isoformat() if outside == "after_program" else (now + timedelta(days=5)).isoformat()
    q = client_a.post(
        "/api/queue",
        json={
            "planId": plan["id"],
            "activityType": "running",
            "title": "Preserved commitment",
            "scheduledDate": at,
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
        },
    ).json()
    if outside == "completed":
        assert client_a.patch("/api/queue/" + q["id"] + "/status", json={"status": "completed"}).status_code == 200
    before = client_a.get("/api/queue").json()
    response = client_a.post(
        "/api/coaching/program",
        json={
            "goal": {"type": "10k", "race_date": race.date().isoformat()},
            "available_days": ["mon", "wed", "sat"],
            "start_date": (now + timedelta(days=3)).date().isoformat(),
            "benchmark": {
                "pace_seconds_per_km": 360,
                "weekly_distance_meters": 15000,
                "long_run_meters": 6000,
                "source": "Synthetic explicit ability",
                "observed_at": now.isoformat(),
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["proposal"]["status"] == "awaiting_approval"
    assert client_a.get("/api/queue").json() == before
