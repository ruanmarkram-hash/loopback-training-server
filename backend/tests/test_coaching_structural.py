"""Persistent structural approval effects, with synthetic worker candidates only."""

from datetime import UTC, datetime, timedelta

import pytest


def accepted_reduction(client):
    now = datetime.now(UTC)
    plan = client.post(
        "/api/plans", json={"name": "Undo fixture", "activityType": "running", "startDate": now.date().isoformat()}
    ).json()
    queues = []
    for day in [3, 7]:
        queues.append(
            client.post(
                "/api/queue",
                json={
                    "planId": plan["id"],
                    "activityType": "running",
                    "title": "Future run",
                    "scheduledDate": (now + timedelta(days=day)).isoformat(),
                    "workoutData": {
                        "activityType": "running",
                        "singleGoal": {"type": "distance", "value": 5000, "unit": "meters"},
                    },
                },
            ).json()
        )
    target = client.get("/api/plans/" + plan["id"]).json()["revision"]
    worker = client.post(
        "/api/auth/tokens", json={"name": "structural fixture worker", "scope": "coach_worker"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + worker}
    client.post("/api/coaching/reviews", json={"idempotencyKey": "structural candidate", "planId": plan["id"]})
    job = client.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    response = client.post(
        "/api/coaching/worker/jobs/" + job["id"] + "/result",
        headers=headers,
        json={
            "leaseToken": job["leaseToken"],
            "result": {
                "status": "ok",
                "output": {
                    "status": "propose",
                    "reason": "Synthetic bounded reduction candidate",
                    "proposedChanges": [
                        {"workoutId": q["id"], "field": "distance_meters", "value": 4750, "reason": "Fixture reduction"}
                        for q in queues
                    ],
                },
                "usage": {},
            },
        },
    )
    assert response.status_code == 200
    proposal = client.get("/api/coaching/proposals").json()[0]
    applied = client.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert applied.status_code == 200
    assert applied.json()["planRevision"] == target + 1
    return now, plan, queues, target, headers


def advance_clock(monkeypatch, now, days):
    import app.coaching_structural as structural
    import app.routes.coaching as routes

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = now + timedelta(days=days)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(routes, "datetime", Clock)
    monkeypatch.setattr(structural, "datetime", Clock)


def test_forward_undo_creates_new_revision_preserves_completed_and_history(client_a, monkeypatch):
    now, plan, queues, target, _ = accepted_reduction(client_a)
    assert client_a.patch("/api/queue/" + queues[0]["id"] + "/status", json={"status": "completed"}).status_code == 200
    advance_clock(monkeypatch, now, 4)
    history_before = client_a.get("/api/coaching/plans/" + plan["id"] + "/revisions").json()
    preview = client_a.post(
        "/api/coaching/plans/" + plan["id"] + "/undo-preview",
        json={"targetRevision": target, "idempotencyKey": "undo explicit"},
    )
    assert preview.status_code == 201
    proposal = preview.json()["proposal"]
    assert proposal["type"] == "future_undo"
    assert [x["workoutId"] for x in proposal["structuralChanges"]] == [queues[1]["id"]]
    before = {x["id"]: x for x in client_a.get("/api/queue").json()}
    assert before[queues[1]["id"]]["workout_data"]["singleGoal"]["value"] == 4750
    request = {"expectedRevision": proposal["base_revision"]}
    applied = client_a.post("/api/coaching/proposals/" + proposal["id"] + "/accept", json=request)
    assert applied.status_code == 200
    assert applied.json()["planRevision"] == target + 2
    assert client_a.post("/api/coaching/proposals/" + proposal["id"] + "/accept", json=request).json() == applied.json()
    after = {x["id"]: x for x in client_a.get("/api/queue").json()}
    assert after[queues[0]["id"]]["status"] == "completed"
    assert after[queues[0]["id"]]["workout_data"]["singleGoal"]["value"] == 4750
    assert after[queues[1]["id"]]["workout_data"]["singleGoal"]["value"] == 5000
    history_after = client_a.get("/api/coaching/plans/" + plan["id"] + "/revisions").json()
    old = {x["revision"]: x for x in history_after}
    assert all(old[x["revision"]] == x for x in history_before)


def test_undo_owner_worker_and_new_freeze_fail_without_mutation(client_a, client_b, monkeypatch):
    now, plan, _queues, target, worker = accepted_reduction(client_a)
    advance_clock(monkeypatch, now, 4)
    endpoint = "/api/coaching/plans/" + plan["id"] + "/undo-preview"
    body = {"targetRevision": target, "idempotencyKey": "freeze preview"}
    assert client_b.post(endpoint, json=body).status_code == 404
    assert client_a.post(endpoint, json=body, headers=worker).status_code == 403
    proposal = client_a.post(endpoint, json=body).json()["proposal"]
    before = client_a.get("/api/queue").json()
    advance_clock(monkeypatch, now, 6.6)
    response = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert response.status_code == 409
    assert client_a.get("/api/queue").json() == before
    assert (
        next(x for x in client_a.get("/api/coaching/proposals").json() if x["id"] == proposal["id"])["status"]
        == "superseded"
    )


def approved_program(client):
    now = datetime.now(UTC)
    body = dict(
        goal=dict(type="10k", race_date=(now + timedelta(days=150)).date().isoformat()),
        available_days=["mon", "wed", "sat"],
        start_date=(now + timedelta(days=3)).date().isoformat(),
        benchmark=dict(
            pace_seconds_per_km=360,
            weekly_distance_meters=15000,
            long_run_meters=6000,
            source="synthetic recorded baseline",
            observed_at=now.isoformat(),
        ),
    )
    r = client.post("/api/coaching/program", json=body)
    assert r.status_code == 201, r.text
    proposal = r.json()["proposal"]
    assert (
        client.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        ).status_code
        == 200
    )
    return now, r.json()["plan"]["id"]


def test_rebuild_unknown_coverage_has_no_proposal_or_persistent_effects(client_a, client_b):
    _, pid = approved_program(client_a)
    endpoint = "/api/coaching/programs/" + pid + "/rebuild"
    body = dict(idempotencyKey="unknown rebuild", interruption=dict(confirmed=False), coverage=dict(complete=False))
    before = client_a.get("/api/queue").json()
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    assert client_b.post(endpoint, json=body).status_code == 404
    response = client_a.post(endpoint, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "review_required"
    assert response.json()["forecast"] is None and response.json()["proposal"] is None
    assert client_a.get("/api/queue").json() == before
    assert client_a.get("/api/plans/" + pid).json()["revision"] == revision
    assert client_a.post(endpoint, json=body).json() == response.json()
    assert client_a.post(endpoint, json={**body, "coverage": dict(complete=True)}).status_code == 422


def test_rebuild_owned_load_approval_and_corrected_activity_invalidates_preview(
    client_a, monkeypatch, session_factory, user_a
):
    import uuid

    now, pid = approved_program(client_a)
    advance_clock(monkeypatch, now, 35)
    current = now + timedelta(days=35)
    from app.coaching_scheduler import publish_forecast
    from app.models.plan import Plan

    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        publish_forecast(db, user_a[0], plan, current)
        db.commit()
    existing_future = {
        q["id"] for q in client_a.get("/api/queue").json() if datetime.fromisoformat(q["scheduled_date"]) > current
    }
    assert existing_future
    activity_ids = []
    for days in [2, 4, 6, 9, 11, 13]:
        at = current - timedelta(days=days)
        wid = str(uuid.uuid4())
        activity_ids.append(wid)
        response = client_a.post(
            "/api/workouts",
            json=dict(
                id=wid,
                activityType="running",
                startDate=at.isoformat(),
                endDate=(at + timedelta(minutes=30)).isoformat(),
                duration=1800,
                totalDistance=5000,
            ),
        )
        assert response.status_code == 201
    from zoneinfo import ZoneInfo

    local = current.astimezone(ZoneInfo("Australia/Brisbane"))
    body = dict(
        idempotencyKey="supported rebuild",
        interruption=dict(
            start_date=(now + timedelta(days=1)).date().isoformat(),
            end_date=(now + timedelta(days=19)).date().isoformat(),
            confirmed=True,
        ),
        coverage=dict(
            start_date=(local - timedelta(days=14)).date().isoformat(),
            end_date=(local - timedelta(days=1)).date().isoformat(),
            complete=True,
        ),
    )
    before = client_a.get("/api/queue").json()
    response = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "forecast_ready", response.json()["limitations"]
    proposal = response.json()["proposal"]
    assert proposal["type"] == "program_rebuild"
    assert client_a.get("/api/queue").json() == before
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    assert client_a.delete("/api/workouts/" + activity_ids[0]).status_code == 204
    assert client_a.get("/api/plans/" + pid).json()["revision"] == revision
    rejected = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert rejected.status_code == 409, rejected.text
    assert client_a.get("/api/queue").json() == before
    body["idempotencyKey"] = "after correction"
    second = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert second.status_code == 201, second.text
    proposal = second.json()["proposal"]
    assert proposal is not None, second.json()["limitations"]
    history = client_a.get("/api/coaching/plans/" + pid + "/revisions").json()
    result = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["planRevision"] == revision + 1
    published = client_a.get("/api/coaching/programs/" + pid).json()
    assert published["forecastApproved"] is True
    after_queue = {q["id"]: q for q in client_a.get("/api/queue").json()}
    matching = [f for f in published["forecast"] if f["id"] in existing_future]
    assert matching
    for f in matching:
        assert f["id"] in after_queue, "Replacement of same logical ID must stay published"
        assert after_queue[f["id"]]["workout_data"] == f["composition"]
    after = client_a.get("/api/coaching/plans/" + pid + "/revisions").json()
    assert all(x in after for x in history)
    assert (
        client_a.put(
            "/api/coaching/profile", json=dict(available_days=[], timezone="Australia/Brisbane", units="metric")
        ).status_code
        == 200
    )
    body["idempotencyKey"] = "explicit unknown availability"
    unknown = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert unknown.status_code == 201, unknown.text
    assert unknown.json()["status"] == "review_required" and unknown.json()["proposal"] is None
    assert unknown.json()["forecast"] is None
    assert (
        client_a.put(
            "/api/coaching/profile",
            json=dict(
                available_days=["mon", "wed", "sat"],
                timezone="Australia/Brisbane",
                units="metric",
                restrictions=["Reported pain; review required"],
            ),
        ).status_code
        == 200
    )
    body["idempotencyKey"] = "explicit restrictions"
    restricted = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert restricted.status_code == 201, restricted.text
    assert restricted.json()["status"] == "review_required" and restricted.json()["proposal"] is None


def test_explanation_only_cli_annotation_cannot_mutate_proposal_or_plan(client_a, client_b):
    _, pid = approved_program(client_a)
    preview = client_a.post(
        "/api/coaching/programs/" + pid + "/rebuild",
        json=dict(idempotencyKey="explain unknown", interruption=dict(confirmed=False), coverage=dict(complete=False)),
    ).json()
    endpoint = "/api/coaching/jobs/" + preview["previewJobId"] + "/explanation"
    assert client_b.post(endpoint, json={"idempotencyKey": "annotation"}).status_code == 404
    requested = client_a.post(endpoint, json={"idempotencyKey": "annotation"})
    assert requested.status_code == 201, requested.text
    explanation = requested.json()
    assert explanation["status"] == "pending"
    assert client_a.post(endpoint, json={"idempotencyKey": "annotation"}).json() == explanation
    token = client_a.post(
        "/api/auth/tokens", json={"name": "synthetic explanation worker", "scope": "coach_worker"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    assert client_a.post(endpoint, json={"idempotencyKey": "forbidden"}, headers=headers).status_code == 403
    before = client_a.get("/api/queue").json()
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    assert job["id"] == explanation["id"]
    assert job["request"]["context"]["task"] == "explanation_only"
    summary = job["request"]["context"]["preview"]
    assert summary["remainingCalendarComplete"] is True
    assert len(summary["remainingCalendar"]) == len(preview["preserved_forecast"])
    assert summary["fullForecastSessionCount"] == len(preview["preserved_forecast"])
    assert summary["plannedNotRecorded"] is True
    response = client_a.post(
        "/api/coaching/worker/jobs/" + job["id"] + "/result",
        headers=headers,
        json={
            "leaseToken": job["leaseToken"],
            "result": {
                "status": "ok",
                "output": {
                    "status": "insufficient_evidence",
                    "reason": "Synthetic transport annotation, not actual CLI evidence. Coverage remains unknown; request complete history before rebuilding.",
                    "proposedChanges": [],
                },
                "usage": {},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert response.json()["data"]["explanation"]["untrusted_annotation"] is True
    assert client_a.get("/api/queue").json() == before
    assert client_a.get("/api/plans/" + pid).json()["revision"] == revision
    assert client_a.get("/api/coaching/proposals").json()[0]["type"] == "initial_program"


def test_explanation_rejects_changes_and_stale_profile_without_state_mutation(client_a):
    _, pid = approved_program(client_a)
    preview = client_a.post(
        "/api/coaching/programs/" + pid + "/rebuild",
        json=dict(idempotencyKey="explain rejected", interruption=dict(confirmed=False), coverage=dict(complete=False)),
    ).json()
    endpoint = "/api/coaching/jobs/" + preview["previewJobId"] + "/explanation"
    requested = client_a.post(endpoint, json={"idempotencyKey": "candidate forbidden"}).json()
    assert requested["status"] == "pending"
    token = client_a.post(
        "/api/auth/tokens", json={"name": "rejected explanation worker", "scope": "coach_worker"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + token}
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
                    "reason": "Unsafe explanation candidate",
                    "proposedChanges": [
                        {
                            "workoutId": "00000000-0000-0000-0000-000000000000",
                            "field": "distance_meters",
                            "value": 1000,
                            "reason": "not allowed",
                        }
                    ],
                },
                "usage": {},
            },
        },
    )
    assert response.status_code == 200 and response.json()["status"] == "dead_letter"
    assert "cannot propose" in response.json()["data"]["failureReason"]
    second = client_a.post(endpoint, json={"idempotencyKey": "stale profile"}).json()
    assert (
        client_a.put(
            "/api/coaching/profile", json=dict(available_days=["mon"], timezone="Australia/Brisbane", units="metric")
        ).status_code
        == 200
    )
    assert client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"] is None
    jobs = {x["id"]: x for x in client_a.get("/api/coaching/jobs").json()}
    assert jobs[second["id"]]["status"] == "dead_letter"
    assert jobs[second["id"]]["data"]["failure"] == "stale_explanation"


def test_explicit_restriction_blocks_initial_program_and_new_approval(client_a):
    now = datetime.now(UTC)
    body = dict(
        goal=dict(type="10k", race_date=(now + timedelta(days=100)).date().isoformat()),
        available_days=["mon", "wed", "sat"],
        start_date=(now + timedelta(days=3)).date().isoformat(),
        benchmark=dict(
            pace_seconds_per_km=360,
            weekly_distance_meters=15000,
            long_run_meters=6000,
            source="synthetic baseline",
            observed_at=now.isoformat(),
        ),
    )
    draft = client_a.post("/api/coaching/program", json=body)
    assert draft.status_code == 201
    proposal = draft.json()["proposal"]
    assert proposal["previewJobId"] == draft.json()["previewJobId"]
    assert client_a.get("/api/coaching/proposals").json()[0]["previewJobId"] == proposal["previewJobId"]
    pid = draft.json()["plan"]["id"]
    before = client_a.get("/api/plans").json()
    assert (
        client_a.put(
            "/api/coaching/profile",
            json=dict(
                available_days=["mon", "wed", "sat"],
                timezone="Australia/Brisbane",
                units="metric",
                restrictions=["No running until reviewed"],
            ),
        ).status_code
        == 200
    )
    rejected = client_a.post("/api/coaching/program", json={**body, "idempotencyKey": "restricted new program"})
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["status"] == "review_required"
    assert rejected.json()["detail"]["restrictions"] == ["No running until reviewed"]
    assert client_a.get("/api/plans").json() == before
    accept = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert accept.status_code == 409, accept.text
    assert client_a.get("/api/plans/" + pid).json()["status"] == "draft"
    assert client_a.get("/api/queue").json() == []


def test_multi_field_workout_approval_emits_only_final_action(client_a):
    now = datetime.now(UTC)
    plan = client_a.post(
        "/api/plans", json=dict(name="Multi-field action", activityType="running", startDate=now.date().isoformat())
    ).json()
    comp = dict(
        activityType="running",
        scheduledDate=(now + timedelta(days=4)).isoformat(),
        blocks=[
            dict(
                iterations=1,
                steps=[
                    dict(purpose="work", goal=dict(type="distance", unit="meters", value=1000)),
                    dict(purpose="work", goal=dict(type="time", unit="seconds", value=600)),
                ],
            )
        ],
    )
    queue = client_a.post(
        "/api/queue", json=dict(activityType="running", planId=plan["id"], title="Mixed goals", workoutData=comp)
    ).json()
    assert client_a.patch("/api/queue/" + queue["id"] + "/status", json={"status": "synced"}).status_code == 200
    token = client_a.post(
        "/api/auth/tokens", json={"name": "synthetic multi-field worker", "scope": "coach_worker"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    client_a.post("/api/coaching/reviews", json={"idempotencyKey": "multi-field", "planId": plan["id"]})
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
                    "reason": "Synthetic two-field reduction",
                    "proposedChanges": [
                        dict(workoutId=queue["id"], field="distance_meters", value=950, reason="Fixture"),
                        dict(workoutId=queue["id"], field="duration_seconds", value=570, reason="Fixture"),
                    ],
                },
            },
        },
    )
    assert response.json()["status"] == "completed", response.text
    proposal = client_a.get("/api/coaching/proposals").json()[0]
    applied = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert applied.status_code == 200, applied.text
    actions = client_a.get("/api/workouts/actions").json()
    assert len(actions) == 1, "Exactly one final desired action per logical workout"
    desired = client_a.get("/api/queue").json()[0]["workout_data"]
    assert desired["blocks"][0]["steps"][0]["goal"]["value"] == 950
    assert desired["blocks"][0]["steps"][1]["goal"]["value"] == 570
    assert client_a.delete("/api/workouts/actions/" + actions[0]["id"]).status_code == 200
    assert client_a.get("/api/queue").json()[0]["workout_data"] == desired


@pytest.mark.parametrize("changed_fact", ["profile", "aged_benchmark"])
def test_initial_approval_rechecks_current_explicit_evidence(client_a, monkeypatch, changed_fact):
    now = datetime.now(UTC)
    body = dict(
        goal=dict(type="10k", race_date=(now + timedelta(days=100)).date().isoformat()),
        available_days=["mon", "wed", "sat"],
        start_date=(now + timedelta(days=5)).date().isoformat(),
        benchmark=dict(
            pace_seconds_per_km=360,
            weekly_distance_meters=15000,
            long_run_meters=6000,
            source="synthetic near-age-limit benchmark",
            observed_at=(now - timedelta(days=41)).isoformat(),
        ),
    )
    preview = client_a.post("/api/coaching/program", json=body)
    assert preview.status_code == 201, preview.text
    proposal = preview.json()["proposal"]
    pid = preview.json()["plan"]["id"]
    if changed_fact == "profile":
        assert (
            client_a.put(
                "/api/coaching/profile",
                json={"available_days": ["sun"], "timezone": "Australia/Brisbane", "units": "metric"},
            ).status_code
            == 200
        )
    else:
        advance_clock(monkeypatch, now, 2)
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    assert revision == proposal["base_revision"]
    response = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": revision}
    )
    assert response.status_code == 409, response.text
    assert client_a.get("/api/plans/" + pid).json()["status"] == "draft"
    assert client_a.get("/api/queue").json() == []


def test_maximum_structural_retry_keys_persist_and_replay(client_a):
    _, pid = approved_program(client_a)
    body = dict(idempotencyKey="x" * 200, interruption=dict(confirmed=False), coverage=dict(complete=False))
    endpoint = "/api/coaching/programs/" + pid + "/rebuild"
    response = client_a.post(endpoint, json=body)
    assert response.status_code == 201, response.text
    assert client_a.post(endpoint, json=body).json() == response.json()
    explain = "/api/coaching/jobs/" + response.json()["previewJobId"] + "/explanation"
    requested = client_a.post(explain, json={"idempotencyKey": "y" * 200})
    assert requested.status_code == 201, requested.text
    assert requested.json()["clientRequestKeyHash"]
    assert requested.json()["requestType"] == "structural_explanation"
    assert client_a.post(explain, json={"idempotencyKey": "y" * 200}).json() == requested.json()


def test_maximum_undo_key_keeps_one_durable_preview(client_a, monkeypatch):
    now, plan, _queues, target, _worker = accepted_reduction(client_a)
    advance_clock(monkeypatch, now, 4)
    body = dict(targetRevision=target, idempotencyKey="z" * 200)
    endpoint = "/api/coaching/plans/" + plan["id"] + "/undo-preview"
    first = client_a.post(endpoint, json=body)
    assert first.status_code == 201, first.text
    assert client_a.post(endpoint, json=body).json() == first.json()
    requested = client_a.post(
        "/api/coaching/jobs/" + first.json()["previewJobId"] + "/explanation",
        json={"idempotencyKey": "undo explanation policy"},
    )
    assert requested.status_code == 201, requested.text
    assert requested.json()["data"]["summary"]["policyVersion"] == first.json()["proposal"]["policyVersion"]
    summary = requested.json()["data"]["summary"]
    assert summary["remainingCalendar"], "Undo must not claim an empty complete calendar"
    assert summary["targetRevision"] == target
    assert summary["undoChanges"][0]["before"]["targets"]["singleGoal"]["value"] == 4750
    assert summary["undoChanges"][0]["after"]["targets"]["singleGoal"]["value"] == 5000
    assert summary["undoChanges"][0]["before"]["prescriptionRevision"]
    assert summary["undoChanges"][0]["after"]["prescriptionRevision"]


def test_complete_explanation_request_utf8_limit_fails_without_lease_or_mutation(client_a, session_factory):
    import uuid

    from app.models.coaching import ReviewJob

    _, pid = approved_program(client_a)
    preview = client_a.post(
        "/api/coaching/programs/" + pid + "/rebuild",
        json=dict(
            idempotencyKey="oversized explanation preview",
            interruption=dict(confirmed=False),
            coverage=dict(complete=False),
        ),
    ).json()
    job = client_a.post(
        "/api/coaching/jobs/" + preview["previewJobId"] + "/explanation", json={"idempotencyKey": "bounded request"}
    ).json()
    with session_factory() as db:
        row = db.get(ReviewJob, uuid.UUID(job["id"]))
        row.data = {**row.data, "summary": {**row.data["summary"], "syntheticUtf8SizeFixture": "😀" * 10000}}
        db.commit()
    before = client_a.get("/api/queue").json()
    revision = client_a.get("/api/plans/" + pid).json()["revision"]
    token = client_a.post(
        "/api/auth/tokens", json={"name": "synthetic bounded worker", "scope": "coach_worker"}
    ).json()["token"]
    assert (
        client_a.post("/api/coaching/worker/claim", json={}, headers={"Authorization": "Bearer " + token}).json()["job"]
        is None
    )
    stored = next(j for j in client_a.get("/api/coaching/jobs").json() if j["id"] == job["id"])
    assert stored["status"] == "dead_letter" and stored["attempts"] == 0
    assert stored["data"]["failure"] == "context_too_large"
    assert "32KiB" in stored["data"]["failureReason"]
    assert client_a.get("/api/queue").json() == before
    assert client_a.get("/api/plans/" + pid).json()["revision"] == revision


def test_rebuild_changed_protected_actual_prescription_blocks_stale_forecast_budget(
    client_a, monkeypatch, session_factory, user_a
):
    import copy
    import uuid
    from zoneinfo import ZoneInfo

    from app.coaching_scheduler import publish_forecast
    from app.models.plan import Plan

    now, pid = approved_program(client_a)
    advance_clock(monkeypatch, now, 35)
    current = now + timedelta(days=35)
    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        publish_forecast(db, user_a[0], plan, current)
        db.commit()
    future = [q for q in client_a.get("/api/queue").json() if datetime.fromisoformat(q["scheduled_date"]) > current]
    assert future
    item = future[0]
    comp = copy.deepcopy(item["workout_data"])
    comp["blocks"][0]["steps"][0]["goal"]["value"] += 100
    patched = client_a.patch(
        "/api/queue/" + item["id"],
        json={"scheduledDate": (current + timedelta(hours=12)).isoformat(), "workoutData": comp},
    )
    assert patched.status_code == 200, patched.text
    for days in [2, 4, 6, 9, 11, 13]:
        at = current - timedelta(days=days)
        assert (
            client_a.post(
                "/api/workouts",
                json=dict(
                    id=str(uuid.uuid4()),
                    activityType="running",
                    startDate=at.isoformat(),
                    endDate=(at + timedelta(minutes=30)).isoformat(),
                    duration=1800,
                    totalDistance=5000,
                ),
            ).status_code
            == 201
        )
    local = current.astimezone(ZoneInfo("Australia/Brisbane"))
    body = dict(
        idempotencyKey="protected actual changed",
        interruption=dict(
            start_date=(now + timedelta(days=1)).date().isoformat(),
            end_date=(now + timedelta(days=19)).date().isoformat(),
            confirmed=True,
        ),
        coverage=dict(
            start_date=(local - timedelta(days=14)).date().isoformat(),
            end_date=(local - timedelta(days=1)).date().isoformat(),
            complete=True,
        ),
    )
    before = client_a.get("/api/queue").json()
    result = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert result.status_code == 201, result.text
    assert result.json()["status"] == "review_required", result.json()["limitations"]
    assert result.json()["proposal"] is None and result.json()["forecast"] is None
    assert any(x["workoutId"] == item["id"] for x in result.json()["evidence"]["protectedPrescriptionMismatches"])
    assert client_a.get("/api/queue").json() == before


def test_initial_supported_key_replay_terminal_opaque_binding_and_overlimit_rejection(client_a, client_b):
    import hashlib

    now = datetime.now(UTC)
    body = dict(
        idempotencyKey="k" * 100,
        goal=dict(type="10k", race_date=(now + timedelta(days=100)).date().isoformat()),
        available_days=["mon", "wed", "sat"],
        start_date=(now + timedelta(days=3)).date().isoformat(),
        benchmark=dict(
            pace_seconds_per_km=360,
            weekly_distance_meters=15000,
            long_run_meters=6000,
            source="synthetic retry evidence",
            observed_at=now.isoformat(),
        ),
    )
    first = client_a.post("/api/coaching/program", json=body)
    assert first.status_code == 201, first.text
    same = client_a.post("/api/coaching/program", json=body)
    assert same.status_code == 201 and same.json()["proposal"]["id"] == first.json()["proposal"]["id"]
    assert client_a.post("/api/coaching/program", json={**body, "idempotencyKey": "k" * 121}).status_code == 422
    proposal = first.json()["proposal"]
    expected = hashlib.sha256(body["idempotencyKey"].encode("utf-8")).hexdigest()
    assert proposal["clientRequestKeyHash"] == expected and proposal["requestType"] == "initial_program"
    assert proposal["requestPlanId"] == first.json()["plan"]["id"]
    assert (
        client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        ).status_code
        == 200
    )
    status = client_a.get("/api/coaching/status").json()
    recovered = next(x for x in status["proposals"] if x["clientRequestKeyHash"] == expected)
    assert recovered["status"] == "applied" and recovered["previewJobId"] == first.json()["previewJobId"]
    job = next(x for x in status["jobs"] if x["id"] == recovered["previewJobId"])
    assert job["clientRequestKeyHash"] == expected and job["ownerId"] == recovered["ownerId"]
    assert all(x.get("clientRequestKeyHash") != expected for x in client_b.get("/api/coaching/status").json()["jobs"])
    assert client_a.post("/api/coaching/program", json=body).json()["proposal"]["status"] == "applied"


def test_supported_review_and_scheduler_key_replay_are_bounded_and_owned(client_a, session_factory, user_a):
    from sqlalchemy import select

    from app.coaching_scheduler import scan_reviews
    from app.coaching_service import enqueue
    from app.models.coaching import ReviewJob

    body = {"idempotencyKey": "r" * 100}
    first = client_a.post("/api/coaching/reviews", json=body)
    assert first.status_code == 201
    assert client_a.post("/api/coaching/reviews", json=body).json()["id"] == first.json()["id"]
    assert client_a.post("/api/coaching/reviews", json={"idempotencyKey": "r" * 121}).status_code == 422
    approved_program(client_a)
    with session_factory() as db:
        a = enqueue(db, user_a[0], "synthetic-scheduler:" + ("long-event:" * 30), {"trigger": "synthetic_event"})
        b = enqueue(db, user_a[0], "synthetic-scheduler:" + ("long-event:" * 30), {"trigger": "synthetic_event"})
        assert a is b and len(a.idempotency_key) <= 128
        db.commit()
        scan_reviews(db)
        ids = [j.id for j in db.scalars(select(ReviewJob).where(ReviewJob.user_id == user_a[0]))]
        scan_reviews(db)
        assert [j.id for j in db.scalars(select(ReviewJob).where(ReviewJob.user_id == user_a[0]))] == ids
