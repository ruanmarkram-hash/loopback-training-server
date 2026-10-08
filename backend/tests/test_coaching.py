"""Adaptive contracts at the real API/Postgres seam, synthetic users only."""

from datetime import datetime, timedelta, timezone
import pytest


def test_profile_keeps_benchmark_separate_from_goal_and_unknown_history(client_a, client_b):
    body = {
        "benchmark": {"pace_seconds_per_km": 360, "source": "athlete"},
        "goal": {"type": "10k", "target_seconds": 3300},
        "available_days": ["mon", "wed", "sat"],
        "timezone": "Australia/Brisbane",
    }
    r = client_a.put("/api/coaching/profile", json=body)
    assert r.status_code == 200
    assert client_a.get("/api/coaching/profile").json()["benchmark"]["pace_seconds_per_km"] == 360
    assert client_b.get("/api/coaching/profile").json() is None
    status = client_a.get("/api/coaching/status").json()
    assert status["metrics"]["actual_distance_meters"] == 0
    assert status["coverage"]["history"] == "unknown"


def test_worker_token_cannot_read_health_mutate_plan_or_approve(client_a):
    token = client_a.post("/api/auth/tokens", json={"name": "coach", "scope": "coach_worker"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    for method, path in [
        ("get", "/api/plans"),
        ("get", "/api/health/metrics"),
        ("get", "/api/coaching/status"),
        ("post", "/api/admin/users"),
    ]:
        assert getattr(client_a, method)(path, headers=headers).status_code == 403
    assert client_a.post("/api/coaching/worker/claim", json={}, headers=headers).status_code == 200


def test_review_is_durable_idempotent_leased_and_proposal_only(client_a, client_b):
    one = client_a.post("/api/coaching/reviews", json={"idempotencyKey": "fixture-review"})
    assert one.status_code == 201
    assert (
        client_a.post("/api/coaching/reviews", json={"idempotencyKey": "fixture-review"}).json()["id"]
        == one.json()["id"]
    )
    token = client_a.post("/api/auth/tokens", json={"name": "worker", "scope": "coach_worker"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    assert job["request"]["version"] == 1
    assert client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"] is None
    assert client_b.get("/api/coaching/jobs").json() == []
    result = {
        "leaseToken": job["leaseToken"],
        "result": {
            "status": "ok",
            "output": {"status": "monitoring", "reason": "Unknown history", "proposedChanges": []},
            "usage": {},
        },
    }
    assert (
        client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", json=result, headers=headers).status_code
        == 200
    )
    assert client_a.get("/api/coaching/jobs").json()[0]["status"] == "completed"
    assert client_a.get("/api/coaching/proposals").json() == []
    assert client_a.get("/api/plans").json() == []


def test_volume_proposal_requires_approval_is_idempotent_and_preserves_history(client_a, client_b):
    now = datetime.now(timezone.utc)
    plan = client_a.post(
        "/api/plans", json={"name": "Synthetic", "activityType": "running", "startDate": now.date().isoformat()}
    ).json()
    q = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Easy",
            "planId": plan["id"],
            "workoutData": {
                "scheduledDate": (now + timedelta(days=3)).isoformat(),
                "blocks": [
                    {
                        "iterations": 1,
                        "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 4000}}],
                    }
                ],
            },
        },
    ).json()
    client_a.post("/api/coaching/reviews", json={"idempotencyKey": "reduce", "planId": plan["id"]}).json()
    token = client_a.post("/api/auth/tokens", json={"name": "worker", "scope": "coach_worker"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    result = {
        "leaseToken": job["leaseToken"],
        "result": {
            "status": "ok",
            "output": {
                "status": "propose",
                "reason": "Conservative reduction synthetic fixture",
                "proposedChanges": [
                    {"workoutId": q["id"], "field": "distance_meters", "value": 3800, "reason": "Fixture"}
                ],
            },
            "usage": {},
        },
    }
    assert (
        client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", json=result, headers=headers).status_code
        == 200
    )
    proposal = client_a.get("/api/coaching/proposals").json()[0]
    assert client_a.get("/api/queue").json()[0]["workout_data"]["blocks"][0]["steps"][0]["goal"]["value"] == 4000
    assert (
        client_b.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        ).status_code
        == 404
    )
    assert (
        client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
            headers=headers,
        ).status_code
        == 403
    )
    from concurrent.futures import ThreadPoolExecutor

    def approve():
        return client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: approve(), range(2)))
    accepted = responses[0]
    assert all(r.status_code == 200 and r.json() == accepted.json() for r in responses)
    assert accepted.json()["planRevision"] == proposal["base_revision"] + 1
    assert client_a.get("/api/coaching/status").json()["assessment"]["cooldownActive"] is True
    assert accepted.status_code == 200
    assert (
        client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        ).json()
        == accepted.json()
    )
    assert client_a.get("/api/queue").json()[0]["workout_data"]["blocks"][0]["steps"][0]["goal"]["value"] == 3800
    revisions = client_a.get("/api/coaching/prescriptions/" + q["id"]).json()
    assert revisions[0]["snapshot"]["composition"]["blocks"][0]["steps"][0]["goal"]["value"] == 4000
    assert revisions[-1]["snapshot"]["composition"]["blocks"][0]["steps"][0]["goal"]["value"] == 3800
    history = client_a.get("/api/coaching/plans/" + plan["id"] + "/revisions").json()
    assert [h["revision"] for h in history] == [1, 2, 3]
    assert history[1]["snapshot"]["prescriptions"][0]["composition"]["blocks"][0]["steps"][0]["goal"]["value"] == 4000
    assert history[2]["snapshot"]["prescriptions"][0]["composition"]["blocks"][0]["steps"][0]["goal"]["value"] == 3800
    assert client_b.get("/api/coaching/plans/" + plan["id"] + "/revisions").status_code == 404


def test_foreign_plan_link_rejected_all_queue_writes(client_a, client_b):
    now = datetime.now(timezone.utc)
    p = client_b.post(
        "/api/plans", json={"name": "Private", "activityType": "running", "startDate": now.date().isoformat()}
    ).json()
    payload = {"activityType": "running", "title": "Should not attach", "planId": p["id"]}
    assert client_a.post("/api/queue", json=payload).status_code == 404
    assert client_a.post("/api/queue/batch", json=[payload]).status_code == 404
    q = client_a.post("/api/queue", json={"activityType": "running", "title": "Own"}).json()
    assert client_a.patch("/api/queue/" + q["id"], json={"planId": p["id"]}).status_code == 404
    assert client_a.get("/api/queue").json()[0]["plan_id"] is None


def test_invalid_composition_is_blocked_and_has_no_persistent_effect(client_a):
    r = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Unsafe",
            "workoutData": {
                "blocks": [
                    {
                        "iterations": 1,
                        "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": -4000}}],
                    }
                ]
            },
        },
    )
    assert r.status_code == 422
    assert client_a.get("/api/queue").json() == []


def test_same_activity_reimport_does_not_trigger_duplicate_review(client_a):
    import uuid

    now = datetime.now(timezone.utc)
    body = {
        "id": str(uuid.uuid4()),
        "activityType": "running",
        "startDate": now.isoformat(),
        "endDate": (now + timedelta(minutes=30)).isoformat(),
        "duration": 1800,
        "totalDistance": 5000,
    }
    assert client_a.post("/api/workouts", json=body).status_code == 201
    assert client_a.post("/api/workouts", json=body).status_code == 201
    jobs = client_a.get("/api/coaching/jobs").json()
    assert len(jobs) == 1
    assert client_a.get("/api/coaching/assessments").json()[0]["execution"] == "not_assessable"
    assert client_a.get("/api/coaching/status").json()["metrics"]["actual_distance_meters"] == 5000


def test_program_is_forecast_until_approved_and_missing_ability_is_not_invented(client_a, monkeypatch):
    import app.routes.coaching as routes

    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(routes, "datetime", Clock)
    body = {
        "goal": {"type": "10k", "race_date": (now + timedelta(weeks=12)).date().isoformat()},
        "available_days": ["mon", "wed", "sat"],
        "start_date": (now + timedelta(days=2)).date().isoformat(),
    }
    r = client_a.post("/api/coaching/program", json=body)
    assert r.status_code == 422
    body["benchmark"] = {
        "pace_seconds_per_km": 360,
        "weekly_distance_meters": 15000,
        "long_run_meters": 6000,
        "source": "synthetic fixture",
        "observed_at": now.isoformat(),
    }
    preview = client_a.post("/api/coaching/program", json=body)
    assert preview.status_code == 201, preview.text
    proposal = preview.json()["proposal"]
    plan = preview.json()["plan"]
    assert len(plan["forecast"]) > 20
    assert all(s["status"] == "forecast" for s in plan["forecast"])
    assert client_a.get("/api/queue").json() == []
    r = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert r.status_code == 200
    assert r.json()["planRevision"] == proposal["base_revision"] + 1
    repeated = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert repeated.json() == r.json()
    queue = client_a.get("/api/queue").json()
    assert queue and len(queue) <= 10
    assert max(datetime.fromisoformat(q["scheduled_date"]) for q in queue) < now + timedelta(days=15)
    assert client_a.get("/api/plans").json()[0]["status"] == "active"
    assert client_a.get("/api/plans").json()[0]["metadata"]["goals"][0]["race_date"] == body["goal"]["race_date"]


def test_stale_action_cannot_overwrite_newer_prescription_and_wire_has_identity(client_a):
    now = datetime.now(timezone.utc)
    original = {
        "displayName": "Easy",
        "scheduledDate": (now + timedelta(days=3)).isoformat(),
        "blocks": [
            {
                "iterations": 1,
                "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 4000}}],
            }
        ],
    }
    q = client_a.post("/api/queue", json={"activityType": "running", "title": "Easy", "workoutData": original}).json()
    edit = {**original, "displayName": "Old edit"}
    action = client_a.post(
        "/api/workouts/actions", json={"workoutId": q["id"], "action": "edit", "composition": edit}
    ).json()
    assert action["composition"]["id"] == q["id"]
    assert action["composition"]["prescriptionRevision"]
    client_a.patch("/api/queue/" + q["id"], json={"workoutData": {**original, "displayName": "New desired"}})
    assert client_a.get("/api/workouts/actions").json() == []
    ack = client_a.delete("/api/workouts/actions/" + action["id"])
    assert ack.status_code == 200 and ack.json()["applied"] is False
    assert client_a.get("/api/queue").json()[0]["workout_data"]["displayName"] == "New desired"


def test_activity_delete_removes_assessment_and_produces_new_review(client_a):
    import uuid

    now = datetime.now(timezone.utc)
    w = str(uuid.uuid4())
    body = {
        "id": w,
        "activityType": "running",
        "startDate": now.isoformat(),
        "endDate": (now + timedelta(minutes=30)).isoformat(),
        "duration": 1800,
        "totalDistance": 5000,
    }
    client_a.post("/api/workouts", json=body)
    assert client_a.get("/api/coaching/assessments").json()
    assert client_a.delete("/api/workouts/" + w).status_code == 204
    assert client_a.get("/api/coaching/assessments").json() == []
    assert any(j["data"]["trigger"] == "activity_deleted" for j in client_a.get("/api/coaching/jobs").json())


def test_one_rep_or_supplied_targets_cannot_prove_original_prescribed_work(client_a):
    import uuid

    now = datetime.now(timezone.utc)
    comp = {
        "blocks": [
            {
                "iterations": 3,
                "steps": [
                    {
                        "purpose": "work",
                        "goal": {"type": "time", "unit": "seconds", "value": 60},
                        "alert": {"type": "speed", "min": 2.5, "max": 3.0},
                    },
                    {"purpose": "recovery", "goal": {"type": "time", "unit": "seconds", "value": 30}},
                ],
            }
        ]
    }
    q = client_a.post("/api/queue", json={"activityType": "running", "title": "3 reps", "workoutData": comp}).json()
    revision = client_a.get("/api/workouts/queue").json()[0]["prescriptionRevision"]
    now = datetime.now(timezone.utc)
    body = {
        "id": str(uuid.uuid4()),
        "activityType": "running",
        "startDate": now.isoformat(),
        "endDate": (now + timedelta(minutes=5)).isoformat(),
        "duration": 300,
        "totalDistance": 800,
        "planWorkoutId": q["id"],
        "data": {
            "prescriptionRevision": revision,
            "prescribedSegments": [
                {
                    "stepIndex": 0,
                    "purpose": "work",
                    "distanceMeters": 180,
                    "durationSeconds": 60,
                    "targetMinSecondsPerKm": 300,
                    "targetMaxSecondsPerKm": 400,
                    "coverage": 1,
                    "measurementSource": "healthkit",
                    "sampleCoverage": 1,
                }
            ],
        },
    }
    client_a.post("/api/workouts", json=body)
    assessment = client_a.get("/api/coaching/assessments").json()[0]
    assert assessment["structure"] == "partial"
    assert assessment["pace_evidence"] == "ineligible"


def test_malformed_worker_candidate_is_terminal_and_private(client_a, client_b):
    client_a.post("/api/coaching/reviews", json={"idempotencyKey": "malformed"})
    token = client_a.post("/api/auth/tokens", json={"name": "worker", "scope": "coach_worker"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    response = client_a.post(
        "/api/coaching/worker/jobs/" + job["id"] + "/result",
        headers=headers,
        json={"leaseToken": job["leaseToken"], "result": {"status": "ok", "output": {}}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "dead_letter"
    assert response.json()["data"]["failure"] == "policy_rejected"
    assert client_a.get("/api/coaching/proposals").json() == []
    assert client_b.post("/api/coaching/jobs/" + job["id"] + "/retry").status_code == 404
    assert client_a.post("/api/coaching/jobs/" + job["id"] + "/retry").json()["status"] == "pending"
    assert client_a.post("/api/coaching/jobs/" + job["id"] + "/cancel").json()["status"] == "cancelled"
    assert client_a.get("/api/plans").json() == []


def test_native_speed_units_and_original_targets_are_canonical():
    import uuid
    from types import SimpleNamespace

    from app.coaching_policy import assess
    from app.routes.coaching import changed_composition

    revision = uuid.uuid4()
    composition = {
        "trainingPurpose": "quality",
        "blocks": [
            {
                "iterations": 3,
                "steps": [
                    {
                        "purpose": "work",
                        "goal": {"type": "distance", "unit": "meters", "value": 1000},
                        "alert": {"type": "speed", "unit": "kilometersPerHour", "min": 10, "max": 12},
                    }
                ],
            }
        ],
    }
    prescription = SimpleNamespace(
        id=revision, snapshot={"composition": composition}, created_at=datetime.now(timezone.utc) - timedelta(days=1)
    )
    segments = [
        {
            "stepIndex": i,
            "purpose": "work",
            "distanceMeters": 1000,
            "durationSeconds": 330,
            "measurementSource": "healthkit",
            "sampleCoverage": 1,
            "targetPaceMin": 1,
            "targetPaceMax": 2,
        }
        for i in range(3)
    ]
    workout = SimpleNamespace(
        start_date=datetime.now(timezone.utc),
        data={"prescriptionRevision": str(revision), "prescribedSegments": segments},
    )
    assert assess(workout, prescription)["execution"] == "within_targets"
    changed, old = changed_composition(
        SimpleNamespace(workout_data=composition), {"field": "pace_seconds_per_km", "value": 330}
    )
    assert abs(old - 3600 / 11) < 0.00001
    assert changed["blocks"][0]["steps"][0]["alert"]["unit"] == "kilometersPerHour"
    workout.data["prescribedSegments"] = segments[:1]
    assert assess(workout, prescription)["structure"] == "partial"
    composition["trainingPurpose"] = "easy"
    workout.data["prescribedSegments"] = segments
    assert assess(workout, prescription)["pace_evidence"] == "ineligible"


def test_policy_repeated_quality_mixed_single_and_local_partial_week():
    import uuid
    from types import SimpleNamespace

    from app.coaching_policy import deterministic_review

    now = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
    workouts = [
        SimpleNamespace(
            id=uuid.uuid4(),
            activity_type="running",
            start_date=now - timedelta(days=i + 1),
            total_distance=5000,
            duration=1500,
            effort_score=8,
        )
        for i in range(3)
    ]
    evidence = [
        SimpleNamespace(workout_id=w.id, data={"pace_evidence": "eligible", "execution": "over_target"})
        for w in workouts
    ]
    workouts.append(
        SimpleNamespace(
            id=uuid.uuid4(),
            activity_type="running",
            start_date=now - timedelta(days=5),
            total_distance=10000,
            duration=3300,
            effort_score=4,
        )
    )
    decision = deterministic_review(workouts, evidence, [], [], now, "America/New_York")
    assert decision["paceDirection"] == "faster"
    assert decision["metrics"]["timezone"] == "America/New_York"
    partial = [x for x in decision["metrics"]["weeks"].values() if x["partial_week"]]
    assert partial and all(x["actual_vs_planned_distance_ratio"] is None for x in partial)
    assert sum(x["hard_session_count"] for x in decision["metrics"]["weeks"].values()) == 3
    evidence[0].data["execution"] = "under_target"
    assert deterministic_review(workouts, evidence, [], [], now)["paceStatus"] == "mixed_evidence"
    assert deterministic_review(workouts, evidence[:1], [], [], now)["paceDirection"] is None
    for x in evidence:
        x.data["execution"] = "under_target"
    assert deterministic_review(workouts, evidence, [], [], now)["paceDirection"] == "slower"
    skips = [SimpleNamespace(action="skip", dismissed=False, scheduled_date=now - timedelta(days=i)) for i in range(3)]
    assert deterministic_review(workouts, [], skips, [], now)["interruptionReview"] is True


def test_immutable_device_identity_resolves_original_owned_version(client_a, client_b):
    import uuid

    now = datetime.now(timezone.utc)
    q = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Original",
            "workoutData": {
                "blocks": [
                    {
                        "iterations": 1,
                        "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 1000}}],
                    }
                ]
            },
        },
    ).json()
    original = client_a.get("/api/coaching/prescriptions/" + q["id"]).json()[0]
    client_a.patch("/api/queue/" + q["id"], json={"title": "Changed"})
    item = {
        "id": q["id"],
        "logicalWorkoutId": q["id"],
        "devicePlanId": original["id"],
        "prescriptionRevision": original["id"],
        "contentHash": original["content_hash"],
        "observedAt": now.isoformat(),
        "displayName": "Original",
        "date": {"year": 2026, "month": 10, "day": 8, "hour": 6, "minute": 0},
    }
    assert client_a.put("/api/workouts/inventory", json=[item]).status_code == 200
    assert client_b.put("/api/workouts/inventory", json=[item]).status_code == 422
    observed = client_a.get("/api/workouts/inventory").json()[0]
    assert observed["device_plan_id"] == original["id"]
    body = {
        "id": str(uuid.uuid4()),
        "activityType": "running",
        "startDate": now.isoformat(),
        "endDate": (now + timedelta(minutes=5)).isoformat(),
        "duration": 300,
        "totalDistance": 1000,
        "planWorkoutId": original["id"],
    }
    r = client_a.post("/api/workouts", json=body)
    assert r.status_code == 201
    saved = client_a.get("/api/workouts/" + body["id"]).json()
    assert saved["plan_workout_id"] == q["id"]
    assert saved["data"]["prescriptionRevision"] == original["id"]
    assert client_a.get("/api/coaching/assessments").json()[0]["pace_evidence"] == "ineligible"
    body["id"] = str(uuid.uuid4())
    assert client_b.post("/api/workouts", json=body).status_code == 404


def test_manual_batch_allocates_one_coherent_plan_revision(client_a):
    now = datetime.now(timezone.utc)
    p = client_a.post(
        "/api/plans", json={"name": "Batch", "activityType": "running", "startDate": now.date().isoformat()}
    ).json()
    rows = [
        {
            "activityType": "running",
            "title": "Easy " + str(i),
            "planId": p["id"],
            "workoutData": {
                "scheduledDate": (now + timedelta(days=i + 3)).isoformat(),
                "blocks": [
                    {
                        "iterations": 1,
                        "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 1000}}],
                    }
                ],
            },
        }
        for i in range(4)
    ]
    assert client_a.post("/api/queue/batch", json=rows).status_code == 201
    assert client_a.get("/api/plans/" + p["id"]).json()["revision"] == p["revision"] + 1
    assert len(client_a.get("/api/queue").json()) == 4


@pytest.mark.parametrize("delivery_status", [None, "fetched", "synced", "material"])
def test_migrated_unversioned_action_ack_still_writes_back(client_a, session_factory, delivery_status):
    import uuid
    from sqlalchemy import update, insert
    from app.models.queue import WorkoutQueue
    from app.models.action import WorkoutAction

    user = client_a.get("/api/auth/me").json()["user"]["id"]
    original = {
        "displayName": "Legacy",
        "blocks": [
            {
                "iterations": 1,
                "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 1000}}],
            }
        ],
    }
    q = client_a.post("/api/queue", json={"activityType": "running", "title": "Legacy", "workoutData": original}).json()
    aid = uuid.uuid4()
    edit = {**original, "displayName": "Legacy edited"}
    with session_factory() as db:
        db.execute(update(WorkoutQueue).where(WorkoutQueue.id == uuid.UUID(q["id"])).values(prescription_revision=None))
        db.execute(
            insert(WorkoutAction).values(
                id=aid, user_id=uuid.UUID(user), workout_id=uuid.UUID(q["id"]), action="edit", composition=edit
            )
        )
        db.commit()
    if delivery_status == "material":
        client_a.patch("/api/queue/" + q["id"], json={"title": "Manual desired"})
        assert client_a.get("/api/workouts/actions").json() == []
        assert client_a.delete("/api/workouts/actions/" + str(aid)).json()["applied"] is False
        assert client_a.get("/api/queue").json()[0]["title"] == "Manual desired"
        return
    if delivery_status:
        assert client_a.patch("/api/queue/" + q["id"] + "/status", json={"status": delivery_status}).status_code == 200
    assert len(client_a.get("/api/workouts/actions").json()) == 1
    assert client_a.delete("/api/workouts/actions/" + str(aid)).json()["applied"] is True
    saved = client_a.get("/api/queue").json()[0]
    assert saved["title"] == "Legacy edited"
    assert saved["workout_data"] == edit


def test_ack_retains_exact_version_installed_by_device(client_a):
    now = datetime.now(timezone.utc)
    original = {
        "displayName": "Original",
        "scheduledDate": (now + timedelta(days=3)).isoformat(),
        "blocks": [
            {
                "iterations": 1,
                "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 1000}}],
            }
        ],
    }
    q = client_a.post(
        "/api/queue", json={"activityType": "running", "title": "Original", "workoutData": original}
    ).json()
    client_a.patch("/api/queue/" + q["id"] + "/status", json={"status": "synced"})
    edit = {
        **original,
        "displayName": "Edited",
        "scheduledDate": (now + timedelta(days=4)).astimezone(timezone(timedelta(hours=10))).isoformat(),
    }
    action = client_a.post(
        "/api/workouts/actions", json={"workoutId": q["id"], "action": "edit", "composition": edit}
    ).json()
    desired = action["composition"]["prescriptionRevision"]
    assert client_a.delete("/api/workouts/actions/" + action["id"]).json()["applied"] is True
    scheduled = client_a.get("/api/workouts/queue/scheduled", params={"from": now.isoformat()}).json()[0]
    assert scheduled["prescriptionRevision"] == desired
    assert scheduled["contentHash"] == action["composition"]["contentHash"]
    assert scheduled["displayName"] == "Edited"


@pytest.mark.parametrize("changed_fact", ["corrected_activity", "new_restriction"])
def test_accept_rechecks_corrected_quality_evidence_without_plan_edit(client_a, session_factory, changed_fact):
    import uuid

    now = datetime.now(timezone.utc)
    p = client_a.post(
        "/api/plans", json={"name": "Pace review", "activityType": "running", "startDate": now.date().isoformat()}
    ).json()
    comp = {
        "trainingPurpose": "quality",
        "scheduledDate": (now + timedelta(days=4)).isoformat(),
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
        "/api/queue", json={"activityType": "running", "title": "Quality", "planId": p["id"], "workoutData": comp}
    ).json()
    revision = client_a.get("/api/coaching/prescriptions/" + q["id"]).json()[0]["id"]
    # Synthetic historical issuance predates every measured activity; never retrofit real targets.
    from app.models.coaching import PrescriptionRevision
    from sqlalchemy import update

    with session_factory() as db:
        db.execute(
            update(PrescriptionRevision)
            .where(PrescriptionRevision.id == uuid.UUID(revision))
            .values(created_at=now - timedelta(days=20))
        )
        db.commit()

    def workout(day, seconds, identifier=None, exact=True):
        start = now - timedelta(days=day)
        body = {
            "id": identifier or str(uuid.uuid4()),
            "activityType": "running",
            "startDate": start.isoformat(),
            "endDate": (start + timedelta(seconds=seconds)).isoformat(),
            "duration": seconds,
            "totalDistance": 1000 if exact else 10000,
        }
        if exact:
            body.update(
                planWorkoutId=q["id"],
                data={
                    "prescriptionRevision": revision,
                    "prescribedSegments": [
                        {
                            "stepIndex": 0,
                            "purpose": "work",
                            "distanceMeters": 1000,
                            "durationSeconds": seconds,
                            "measurementSource": "healthkit",
                            "sampleCoverage": 1,
                        }
                    ],
                },
            )
        assert client_a.post("/api/workouts", json=body).status_code == 201
        return body["id"]

    workout(10, 3600, exact=False)
    ids = [workout(i + 1, 270) for i in range(3)]
    assert client_a.get("/api/coaching/status").json()["assessment"]["paceDirection"] == "faster"
    worker = client_a.post("/api/auth/tokens", json={"name": "worker", "scope": "coach_worker"}).json()["token"]
    headers = {"Authorization": "Bearer " + worker}
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    result = {
        "leaseToken": job["leaseToken"],
        "result": {
            "status": "ok",
            "output": {
                "status": "propose",
                "reason": "Synthetic repeated quality",
                "proposedChanges": [
                    {"workoutId": q["id"], "field": "pace_seconds_per_km", "value": 350, "reason": "Fixture"}
                ],
            },
        },
    }
    response = client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", json=result, headers=headers)
    assert response.json()["status"] == "completed"
    proposal = client_a.get("/api/coaching/proposals").json()[0]
    if changed_fact == "corrected_activity":
        workout(1, 500, identifier=ids[0])
        assert client_a.get("/api/coaching/status").json()["assessment"]["paceDirection"] is None
    else:
        assert (
            client_a.put(
                "/api/coaching/profile",
                json={
                    "timezone": "Australia/Brisbane",
                    "units": "metric",
                    "available_days": ["mon", "wed", "sat"],
                    "restrictions": ["Maximum10minute sessions"],
                },
            ).status_code
            == 200
        )
        assert client_a.get("/api/coaching/status").json()["assessment"]["paceDirection"] == "faster"
    assert client_a.get("/api/plans/" + p["id"]).json()["revision"] == proposal["base_revision"]
    accepted = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert accepted.status_code == 409
    assert client_a.get("/api/coaching/proposals").json()[0]["status"] == "superseded"
    assert client_a.get("/api/queue").json()[0]["workout_data"] == comp


def test_initial_program_does_not_silently_drop_first_frozen_session(client_a):
    now = datetime.now(timezone.utc)
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    days = [names[(now.weekday() + i) % 7] for i in (0, 2, 4)]
    body = {
        "goal": {"type": "first_5k", "race_date": (now + timedelta(weeks=14)).date().isoformat()},
        "benchmark": {
            "level": "beginner",
            "weekly_distance_meters": 0,
            "long_run_meters": 0,
            "observed_at": now.isoformat(),
            "source": "explicit synthetic beginner",
        },
        "available_days": days,
        "start_date": now.date().isoformat(),
        "timezone": "UTC",
        "idempotencyKey": "too-soon",
    }
    response = client_a.post("/api/coaching/program", json=body)
    assert response.status_code == 422
    assert "first session" in str(response.json()["detail"]).lower()
    assert client_a.get("/api/plans").json() == []
    assert client_a.get("/api/queue").json() == []


def test_program_retry_recovers_original_intent_after_clock_advances(client_a, monkeypatch):
    from app.routes import coaching

    now = datetime.now(timezone.utc)
    body = {
        "goal": {"type": "10k", "race_date": (now + timedelta(weeks=12)).date().isoformat()},
        "benchmark": {
            "pace_seconds_per_km": 360,
            "weekly_distance_meters": 15000,
            "long_run_meters": 6000,
            "source": "synthetic supplied",
            "observed_at": now.isoformat(),
        },
        "available_days": ["mon", "wed", "sat"],
        "start_date": (now + timedelta(days=3)).date().isoformat(),
        "timezone": "UTC",
        "idempotencyKey": "recover-intent",
    }
    first = client_a.post("/api/coaching/program", json=body)
    assert first.status_code == 201

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return (now + timedelta(days=5)).astimezone(tz or timezone.utc)

    monkeypatch.setattr(coaching, "datetime", Later)
    again = client_a.post("/api/coaching/program", json=body)
    assert again.status_code == 201 and again.json()["proposal"]["id"] == first.json()["proposal"]["id"]
    changed = {**body, "available_days": ["tue", "thu", "sat"]}
    assert client_a.post("/api/coaching/program", json=changed).status_code == 409
    assert len(client_a.get("/api/plans").json()) == 1
    assert client_a.get("/api/queue").json() == []


def test_deleted_foreign_queue_history_cannot_enter_owned_assessment(client_a, client_b):
    import uuid

    comp = {
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
    q = client_b.post(
        "/api/queue", json={"activityType": "running", "title": "Private quality", "workoutData": comp}
    ).json()
    revision = client_b.get("/api/coaching/prescriptions/" + q["id"]).json()[0]["id"]
    assert client_b.delete("/api/queue/" + q["id"]).status_code == 204
    now = datetime.now(timezone.utc)
    body = {
        "id": str(uuid.uuid4()),
        "activityType": "running",
        "startDate": now.isoformat(),
        "endDate": (now + timedelta(seconds=300)).isoformat(),
        "duration": 300,
        "totalDistance": 1000,
        "planWorkoutId": q["id"],
        "data": {
            "prescriptionRevision": revision,
            "prescribedSegments": [
                {
                    "stepIndex": 0,
                    "purpose": "work",
                    "distanceMeters": 1000,
                    "durationSeconds": 300,
                    "measurementSource": "healthkit",
                    "sampleCoverage": 1,
                }
            ],
        },
    }
    assert client_a.post("/api/workouts", json=body).status_code == 404
    assert client_a.get("/api/workouts").json() == []
    assert client_a.get("/api/coaching/assessments").json() == []

    # An unrecognized legacy logical UUID may remain unmatched, never use foreign history.
    body["id"] = str(uuid.uuid4())
    body["data"] = {}
    assert client_a.post("/api/workouts", json=body).status_code == 201
    unknown = client_a.get("/api/coaching/assessments").json()[0]
    assert unknown["match"] == "unmatched" and unknown["prescription_revision"] is None


def test_plan_move_revises_both_histories_and_supersedes_old_proposal(client_a):
    now = datetime.now(timezone.utc)

    def plan(name):
        return client_a.post(
            "/api/plans", json={"name": name, "activityType": "running", "startDate": now.date().isoformat()}
        ).json()

    a = plan("A")
    b = plan("B")
    comp = {
        "scheduledDate": (now + timedelta(days=10)).isoformat(),
        "blocks": [
            {
                "iterations": 1,
                "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 4000}}],
            }
        ],
    }
    q = client_a.post(
        "/api/queue", json={"activityType": "running", "title": "Move", "planId": a["id"], "workoutData": comp}
    ).json()
    client_a.post("/api/coaching/reviews", json={"idempotencyKey": "move", "planId": a["id"]})
    token = client_a.post("/api/auth/tokens", json={"name": "worker", "scope": "coach_worker"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    result = {
        "leaseToken": job["leaseToken"],
        "result": {
            "status": "ok",
            "output": {
                "status": "propose",
                "reason": "Synthetic reduction",
                "proposedChanges": [
                    {"workoutId": q["id"], "field": "distance_meters", "value": 3800, "reason": "Fixture"}
                ],
            },
        },
    }
    assert (
        client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", json=result, headers=headers).json()[
            "status"
        ]
        == "completed"
    )
    proposal = client_a.get("/api/coaching/proposals").json()[0]
    assert client_a.patch("/api/queue/" + q["id"], json={"planId": b["id"]}).status_code == 200
    assert client_a.get("/api/plans/" + a["id"]).json()["revision"] == proposal["base_revision"] + 1
    assert client_a.get("/api/plans/" + b["id"]).json()["revision"] == b["revision"] + 1
    assert (
        client_a.post(
            "/api/coaching/proposals/" + proposal["id"] + "/accept",
            json={"expectedRevision": proposal["base_revision"]},
        ).status_code
        == 409
    )
    assert client_a.get("/api/coaching/proposals").json()[0]["status"] == "superseded"
    histories_a = client_a.get("/api/coaching/plans/" + a["id"] + "/revisions").json()
    histories_b = client_a.get("/api/coaching/plans/" + b["id"] + "/revisions").json()
    assert histories_a[-1]["snapshot"]["prescriptions"] == []
    assert histories_b[-1]["snapshot"]["prescriptions"][0]["workout_id"] == q["id"]
    assert client_a.get("/api/queue").json()[0]["workout_data"] == comp


@pytest.mark.parametrize("naive_timestamp", [False, True])
def test_activity_cannot_retrofit_targets_issued_after_it_ran(client_a, naive_timestamp):
    import uuid

    now = datetime.now(timezone.utc)
    comp = {
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
        "/api/queue", json={"activityType": "running", "title": "New targets", "workoutData": comp}
    ).json()
    revision = client_a.get("/api/coaching/prescriptions/" + q["id"]).json()[0]["id"]
    start = now - timedelta(days=1)
    if naive_timestamp:
        start = start.replace(tzinfo=None)
    body = {
        "id": str(uuid.uuid4()),
        "activityType": "running",
        "startDate": start.isoformat(),
        "endDate": (start + timedelta(seconds=300)).isoformat(),
        "duration": 300,
        "totalDistance": 1000,
        "planWorkoutId": q["id"],
        "data": {
            "prescriptionRevision": revision,
            "prescribedSegments": [
                {
                    "stepIndex": 0,
                    "purpose": "work",
                    "distanceMeters": 1000,
                    "durationSeconds": 300,
                    "measurementSource": "healthkit",
                    "sampleCoverage": 1,
                }
            ],
        },
    }
    assert client_a.post("/api/workouts", json=body).status_code == 201
    assessment = client_a.get("/api/coaching/assessments").json()[0]
    assert assessment["pace_evidence"] == "ineligible"
    assert ("timezone" if naive_timestamp else "after") in assessment["reason"].lower()
    assert client_a.get("/api/coaching/status").json()["metrics"]["actual_distance_meters"] == 1000


def test_legacy_unhooked_plan_move_cannot_cross_proposal_plan_binding(client_a, session_factory):
    """Migration/import fixture omits revision hooks; approval must independently bind its plan."""
    import uuid
    from sqlalchemy import update
    from app.models.queue import WorkoutQueue

    now = datetime.now(timezone.utc)

    def plan(name):
        return client_a.post(
            "/api/plans", json={"name": name, "activityType": "running", "startDate": now.date().isoformat()}
        ).json()

    a = plan("Original")
    b = plan("Other")
    comp = {
        "scheduledDate": (now + timedelta(days=8)).isoformat(),
        "blocks": [
            {
                "iterations": 1,
                "steps": [{"purpose": "work", "goal": {"type": "distance", "unit": "meters", "value": 4000}}],
            }
        ],
    }
    q = client_a.post(
        "/api/queue", json={"activityType": "running", "title": "Legacy move", "planId": a["id"], "workoutData": comp}
    ).json()
    client_a.post("/api/coaching/reviews", json={"idempotencyKey": "legacy-plan-move", "planId": a["id"]})
    token = client_a.post("/api/auth/tokens", json={"name": "worker", "scope": "coach_worker"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    job = client_a.post("/api/coaching/worker/claim", json={}, headers=headers).json()["job"]
    result = {
        "leaseToken": job["leaseToken"],
        "result": {
            "status": "ok",
            "output": {
                "status": "propose",
                "reason": "Synthetic conservative reduction",
                "proposedChanges": [
                    {"workoutId": q["id"], "field": "distance_meters", "value": 3800, "reason": "Fixture"}
                ],
            },
        },
    }
    assert (
        client_a.post("/api/coaching/worker/jobs/" + job["id"] + "/result", json=result, headers=headers).json()[
            "status"
        ]
        == "completed"
    )
    proposal = client_a.get("/api/coaching/proposals").json()[0]
    with session_factory() as db:
        db.execute(update(WorkoutQueue).where(WorkoutQueue.id == uuid.UUID(q["id"])).values(plan_id=uuid.UUID(b["id"])))
        db.commit()
    assert client_a.get("/api/plans/" + a["id"]).json()["revision"] == proposal["base_revision"]
    assert client_a.get("/api/coaching/status").json()["assessment"]["cooldownActive"] is False
    response = client_a.post(
        "/api/coaching/proposals/" + proposal["id"] + "/accept", json={"expectedRevision": proposal["base_revision"]}
    )
    assert response.status_code == 409
    assert client_a.get("/api/coaching/proposals").json()[0]["status"] == "superseded"
    assert client_a.get("/api/plans/" + b["id"]).json()["revision"] == b["revision"]
    assert client_a.get("/api/queue").json()[0]["workout_data"] == comp


def test_stale_independent_session_title_edit_preserves_committed_composition(client_a, session_factory):
    import uuid
    from app.models.queue import WorkoutQueue
    from app.models.coaching import PrescriptionRevision
    from app.coaching_service import prescription_snapshot

    q = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Original",
            "workoutData": {
                "displayName": "Original",
                "activityType": "running",
                "singleGoal": {"type": "distance", "value": 5000, "unit": "meters"},
            },
        },
    ).json()
    with session_factory() as stale, session_factory() as accepted:
        first = stale.get(WorkoutQueue, uuid.UUID(q["id"]))
        second = accepted.get(WorkoutQueue, first.id)
        second.workout_data = {
            **second.workout_data,
            "singleGoal": {"type": "distance", "value": 5200, "unit": "meters"},
        }
        accepted.commit()
        first.title = "Manual title"
        stale.commit()
    with session_factory() as observed:
        current = observed.get(WorkoutQueue, uuid.UUID(q["id"]))
        revision = observed.get(PrescriptionRevision, current.prescription_revision)
        assert current.workout_data["singleGoal"]["value"] == 5200
        assert revision.snapshot == prescription_snapshot(current)
        assert revision.snapshot["composition"]["singleGoal"]["value"] == 5200


def test_concurrent_http_title_patch_waits_for_prescription_transaction(client_a, session_factory):
    import uuid
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from app.coaching_service import lock_athlete, prescription_snapshot
    from app.models.queue import WorkoutQueue
    from app.models.coaching import PrescriptionRevision

    q = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Original",
            "workoutData": {
                "displayName": "Original",
                "activityType": "running",
                "singleGoal": {"type": "distance", "value": 5000, "unit": "meters"},
            },
        },
    ).json()
    with session_factory() as approval, ThreadPoolExecutor(max_workers=1) as pool:
        item = approval.get(WorkoutQueue, uuid.UUID(q["id"]))
        lock_athlete(approval, item.user_id)
        pending = pool.submit(client_a.patch, "/api/queue/" + q["id"], json={"title": "Concurrent title"})
        try:
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.2)
            item.workout_data = {
                **item.workout_data,
                "singleGoal": {"type": "distance", "value": 5200, "unit": "meters"},
            }
            approval.commit()
            assert pending.result(timeout=5).status_code == 200
        finally:
            approval.rollback()
    with session_factory() as observed:
        current = observed.get(WorkoutQueue, uuid.UUID(q["id"]))
        revision = observed.get(PrescriptionRevision, current.prescription_revision)
        assert current.title == "Concurrent title"
        assert current.workout_data["singleGoal"]["value"] == 5200
        assert revision.snapshot == prescription_snapshot(current)


def test_clean_loaded_plan_and_sibling_are_refreshed_for_revision(client_a, session_factory):
    import uuid
    from sqlalchemy import select
    from app.models.plan import Plan
    from app.models.queue import WorkoutQueue
    from app.models.coaching import PlanRevision

    plan = client_a.post(
        "/api/plans", json={"name": "Before", "activityType": "running", "startDate": "2026-10-01"}
    ).json()
    ids = [
        client_a.post(
            "/api/queue",
            json={
                "planId": plan["id"],
                "activityType": "running",
                "title": str(n),
                "workoutData": {
                    "activityType": "running",
                    "singleGoal": {"type": "distance", "value": 5000, "unit": "meters"},
                },
            },
        ).json()["id"]
        for n in range(2)
    ]
    with session_factory() as stale, session_factory() as concurrent:
        old_plan = stale.get(Plan, uuid.UUID(plan["id"]))
        rows = [stale.get(WorkoutQueue, uuid.UUID(x)) for x in ids]
        updated_plan = concurrent.get(Plan, old_plan.id)
        updated_plan.name = "Current plan"
        sibling = concurrent.get(WorkoutQueue, rows[1].id)
        sibling.workout_data = {
            **sibling.workout_data,
            "singleGoal": {"type": "distance", "value": 5200, "unit": "meters"},
        }
        concurrent.commit()
        rows[0].title = "Manual title"
        stale.commit()
    with session_factory() as observed:
        current = observed.get(Plan, uuid.UUID(plan["id"]))
        latest = observed.scalar(
            select(PlanRevision).where(PlanRevision.plan_id == current.id, PlanRevision.revision == current.revision)
        )
        assert latest.snapshot["name"] == current.name == "Current plan"
        recorded = next(x for x in latest.snapshot["prescriptions"] if x["workout_id"] == ids[1])
        assert recorded["composition"]["singleGoal"]["value"] == 5200
