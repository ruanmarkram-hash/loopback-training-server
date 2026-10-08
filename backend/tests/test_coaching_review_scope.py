"""Requested-plan and committed-calendar regressions at PostgreSQL/API seams."""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_coaching_structural import advance_clock, approved_program


def test_plan_requested_review_scopes_context_and_rejects_owned_other_plan(client_a):
    plans, queues = [], []
    for label in ("A", "B"):
        plan = client_a.post(
            "/api/plans",
            json={"name": label, "activityType": "running", "startDate": datetime.now(UTC).date().isoformat()},
        ).json()
        plans.append(plan)
        queues.append(
            client_a.post(
                "/api/queue",
                json={
                    "planId": plan["id"],
                    "activityType": "running",
                    "title": label,
                    "scheduledDate": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
                    "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
                },
            ).json()
        )
    assert (
        client_a.post(
            "/api/coaching/reviews", json={"planId": plans[0]["id"], "idempotencyKey": "owned two plans"}
        ).status_code
        == 201
    )
    token = client_a.post("/api/auth/tokens", json={"name": "scoped synthetic worker", "scope": "coach_worker"}).json()[
        "token"
    ]
    headers = {"Authorization": "Bearer " + token}
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    context = job["request"]["context"]
    # Even a hostile candidate for an owned other plan must fail at result validation.
    response = client_a.post(
        "/api/coaching/worker/jobs/" + job["id"] + "/result",
        headers=headers,
        json={
            "leaseToken": job["leaseToken"],
            "result": {
                "status": "ok",
                "output": {
                    "status": "propose",
                    "reason": "Synthetic wrong requested plan",
                    "proposedChanges": [
                        {
                            "workoutId": queues[1]["id"],
                            "field": "distance_meters",
                            "value": 4750,
                            "reason": "Synthetic reduction",
                        }
                    ],
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "dead_letter", response.json()
    assert client_a.get("/api/coaching/proposals").json() == []
    assert set(context["planRevisions"]) == {plans[0]["id"]}
    assert {q["planId"] for q in context["futureWorkouts"]} == {plans[0]["id"]}
    assert context["metricsScope"] == "all_owned_recorded_running_activities"
    assert client_a.get("/api/queue").json()[1]["workout_data"]["singleGoal"]["value"] == 5000


def rebuild_fixture(client, monkeypatch, session_factory, user_a):
    from app.coaching_scheduler import publish_forecast
    from app.models.plan import Plan

    now, pid = approved_program(client)
    advance_clock(monkeypatch, now, 35)
    current = now + timedelta(days=35)
    with session_factory() as db:
        publish_forecast(db, user_a[0], db.get(Plan, uuid.UUID(pid)), current)
        db.commit()
    for days in [2, 4, 6, 9, 11, 13]:
        at = current - timedelta(days=days)
        assert (
            client.post(
                "/api/workouts",
                json={
                    "id": str(uuid.uuid4()),
                    "activityType": "running",
                    "startDate": at.isoformat(),
                    "endDate": (at + timedelta(minutes=30)).isoformat(),
                    "duration": 1800,
                    "totalDistance": 5000,
                },
            ).status_code
            == 201
        )
    local = current.astimezone(ZoneInfo("Australia/Brisbane"))
    body = {
        "idempotencyKey": "calendar regression",
        "interruption": {
            "start_date": (now + timedelta(days=1)).date().isoformat(),
            "end_date": (now + timedelta(days=19)).date().isoformat(),
            "confirmed": True,
        },
        "coverage": {
            "start_date": (local - timedelta(days=14)).date().isoformat(),
            "end_date": (local - timedelta(days=1)).date().isoformat(),
            "complete": True,
        },
    }
    return current, pid, body


@pytest.mark.parametrize("terminal", ["completed", "skipped", "deleted"])
def test_future_retired_history_is_not_outstanding_preparation(
    client_a, monkeypatch, session_factory, user_a, terminal
):
    import app.coaching_program as program

    current, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    item = next(q for q in client_a.get("/api/queue").json() if datetime.fromisoformat(q["scheduled_date"]) > current)
    if terminal == "deleted":
        assert client_a.delete("/api/queue/" + item["id"]).status_code == 204
    else:
        assert client_a.patch("/api/queue/" + item["id"] + "/status", json={"status": terminal}).status_code == 200
    original = program.generate_remaining_program
    captured = {}

    def recording(*args):
        captured.update(args[-1])
        return original(*args)

    monkeypatch.setattr(program, "generate_remaining_program", recording)
    before = client_a.get("/api/queue").json()
    response = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert response.status_code == 201, response.text
    assert item["id"] in captured.get("retired_session_ids", [])
    if terminal == "completed":
        assert item["id"] in captured.get("completed_session_ids", [])
    explanation = client_a.post(
        "/api/coaching/jobs/" + response.json()["previewJobId"] + "/explanation",
        json={"idempotencyKey": "terminal history annotation"},
    )
    assert explanation.status_code == 201, explanation.text
    summary = explanation.json()["data"]["summary"]
    assert item["id"] in summary["retiredSessionIds"]
    assert item["id"] not in {row["id"] for row in summary["remainingCalendar"]}
    assert client_a.get("/api/queue").json() == before


@pytest.mark.parametrize("hours", [12, 72, None])
def test_added_committed_workout_blocks_incomplete_calendar_rebuild(
    client_a, monkeypatch, session_factory, user_a, hours
):
    current, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    extra = client_a.post(
        "/api/queue",
        json={
            "planId": pid,
            "activityType": "running",
            "title": "Extra committed hard run",
            "scheduledDate": (current + timedelta(hours=hours)).isoformat() if hours is not None else None,
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 8000}},
        },
    ).json()
    before = client_a.get("/api/queue").json()
    response = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "review_required", response.json()["limitations"]
    assert response.json()["forecast"] is None and response.json()["proposal"] is None
    assert extra["id"] in response.json()["evidence"]["unrepresentedCommittedWorkoutIds"]
    explanation = client_a.post(
        "/api/coaching/jobs/" + response.json()["previewJobId"] + "/explanation",
        json={"idempotencyKey": "incomplete calendar annotation"},
    )
    assert explanation.status_code == 201, explanation.text
    summary = explanation.json()["data"]["summary"]
    assert summary["remainingCalendarComplete"] is False
    assert extra["id"] in summary["evidence"]["unrepresentedCommittedWorkoutIds"]
    assert client_a.get("/api/queue").json() == before


@pytest.mark.parametrize("terminal", ["completed", "skipped"])
def test_explanation_cannot_rebind_stale_rebuild_to_new_terminal_state(
    client_a, monkeypatch, session_factory, user_a, terminal
):
    current, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    preview = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body).json()
    assert preview["status"] == "forecast_ready"
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    item = next(q for q in client_a.get("/api/queue").json() if datetime.fromisoformat(q["scheduled_date"]) > current)
    changed = client_a.patch("/api/queue/" + item["id"] + "/status", json={"status": terminal})
    assert changed.status_code == 200 and changed.json()["status"] == terminal
    assert client_a.get("/api/plans/" + pid).json()["revision"] == revision
    before = client_a.get("/api/coaching/jobs").json()
    response = client_a.post(
        "/api/coaching/jobs/" + preview["previewJobId"] + "/explanation",
        json={"idempotencyKey": "stale terminal facts"},
    )
    assert response.status_code == 409, response.json()
    assert client_a.get("/api/coaching/jobs").json() == before
    assert next(q for q in client_a.get("/api/queue").json() if q["id"] == item["id"])["status"] == terminal


def test_undo_explanation_cannot_claim_complete_calendar_with_undated_commitment(client_a):
    from test_coaching_structural import accepted_reduction

    _, plan, _, target, _ = accepted_reduction(client_a)
    extra = client_a.post(
        "/api/queue",
        json={
            "planId": plan["id"],
            "activityType": "running",
            "title": "Unresolved date",
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
        },
    )
    assert extra.status_code == 201
    preview = client_a.post(
        "/api/coaching/plans/" + plan["id"] + "/undo-preview",
        json={"targetRevision": target, "idempotencyKey": "undated undo preview"},
    )
    assert preview.status_code == 201
    before = client_a.get("/api/coaching/jobs").json()
    response = client_a.post(
        "/api/coaching/jobs/" + preview.json()["previewJobId"] + "/explanation",
        json={"idempotencyKey": "undated calendar annotation"},
    )
    assert response.status_code == 422, response.json()
    assert "schedule" in response.json()["detail"].lower()
    assert client_a.get("/api/coaching/jobs").json() == before


@pytest.mark.parametrize("kind", ["initial", "undo", "blocked_rebuild"])
def test_original_preview_binding_rejects_changed_profile_for_all_structural_paths(client_a, kind):
    from test_coaching_structural import accepted_reduction

    if kind == "initial":
        now = datetime.now(UTC)
        response = client_a.post(
            "/api/coaching/program",
            json={
                "goal": {"type": "10k", "race_date": (now + timedelta(days=150)).date().isoformat()},
                "available_days": ["mon", "wed", "sat"],
                "start_date": (now + timedelta(days=3)).date().isoformat(),
                "benchmark": {
                    "pace_seconds_per_km": 360,
                    "weekly_distance_meters": 15000,
                    "long_run_meters": 6000,
                    "source": "Synthetic explicit baseline",
                    "observed_at": now.isoformat(),
                },
            },
        )
    elif kind == "undo":
        _, plan, _, target, _ = accepted_reduction(client_a)
        response = client_a.post(
            "/api/coaching/plans/" + plan["id"] + "/undo-preview",
            json={"targetRevision": target, "idempotencyKey": "binding undo"},
        )
    else:
        _, pid = approved_program(client_a)
        response = client_a.post(
            "/api/coaching/programs/" + pid + "/rebuild",
            json={
                "idempotencyKey": "binding unknown",
                "interruption": {"confirmed": False},
                "coverage": {"complete": False},
            },
        )
        assert response.json()["proposal"] is None
    assert response.status_code == 201, response.text
    assert (
        client_a.put(
            "/api/coaching/profile", json={"restrictions": ["Synthetic explicit no-running restriction"]}
        ).status_code
        == 200
    )
    before = client_a.get("/api/coaching/jobs").json()
    explanation = client_a.post(
        "/api/coaching/jobs/" + response.json()["previewJobId"] + "/explanation",
        json={"idempotencyKey": "new key cannot refresh stale evidence"},
    )
    assert explanation.status_code == 409, explanation.json()
    assert client_a.get("/api/coaching/jobs").json() == before


@pytest.mark.parametrize("other_plan", [True, False])
@pytest.mark.parametrize("dated", [True, False])
def test_undo_explanation_covers_all_owned_commitments(client_a, other_plan, dated):
    from test_coaching_structural import accepted_reduction

    now, plan, _, target, _ = accepted_reduction(client_a)
    other = (
        client_a.post(
            "/api/plans",
            json={"name": "Other owned calendar", "activityType": "running", "startDate": now.date().isoformat()},
        ).json()
        if other_plan
        else None
    )
    extra = client_a.post(
        "/api/queue",
        json={
            "planId": other["id"] if other else None,
            "activityType": "running",
            "title": "Other owned commitment",
            "scheduledDate": (now + timedelta(days=20)).isoformat() if dated else None,
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 8000}},
        },
    )
    assert extra.status_code == 201, extra.text
    preview = client_a.post(
        "/api/coaching/plans/" + plan["id"] + "/undo-preview",
        json={"targetRevision": target, "idempotencyKey": "global undo calendar"},
    )
    assert preview.status_code == 201, preview.text
    before_queue = client_a.get("/api/queue").json()
    before_jobs = client_a.get("/api/coaching/jobs").json()
    response = client_a.post(
        "/api/coaching/jobs/" + preview.json()["previewJobId"] + "/explanation",
        json={"idempotencyKey": "global undo explanation"},
    )
    if dated:
        assert response.status_code == 201, response.text
        summary = response.json()["data"]["summary"]
        row = next(row for row in summary["remainingCalendar"] if row["workoutId"] == extra.json()["id"])
        assert row["targets"]["singleGoal"]["value"] == 8000
        assert row["prescriptionRevision"] and row["contentHash"]
        assert summary["remainingCalendarComplete"] is True
    else:
        assert response.status_code == 422, response.text
        assert "schedule" in response.json()["detail"].lower()
        assert client_a.get("/api/coaching/jobs").json() == before_jobs
    assert client_a.get("/api/queue").json() == before_queue
