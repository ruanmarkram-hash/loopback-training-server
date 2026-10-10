"""Controlled UTC/local-day boundary through real API and persisted dates."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

NOW = datetime(2026, 10, 8, 22, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    import app.validation_service as validation
    from app.routes import nutrition, plans, schedule

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    class ProcessDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 9)  # The actual image's process-local Brisbane day.

    for module in (plans, schedule, nutrition, validation):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(nutrition, "date", ProcessDate)


def profile(client, zone):
    if zone is not None:
        response = client.put("/api/coaching/profile", json={"timezone": zone})
        assert response.status_code == 200, response.text


def plan(client, *, zone=None, activity="running", end="2026-10-08", name="Synthetic local calendar"):
    body = {"name": name, "activityType": activity, "startDate": "2026-10-01", "endDate": end}
    if zone is not None:
        body["metadata"] = {"timezone": zone}
    response = client.post("/api/plans", json=body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "plan_zone,profile_zone,day",
    [
        (None, None, "2026-10-09"),
        (None, "America/Los_Angeles", "2026-10-08"),
        ("Pacific/Auckland", "America/Los_Angeles", "2026-10-09"),
        ("America/Los_Angeles", "Australia/Brisbane", "2026-10-08"),
        ("UTC", "Australia/Brisbane", "2026-10-08"),
    ],
)
def test_finishability_and_completion_use_plan_profile_or_default_calendar(client_a, plan_zone, profile_zone, day):
    profile(client_a, profile_zone)
    yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    created = plan(client_a, zone=plan_zone, end=yesterday)
    assert created["finishable"] is True
    pid = created["id"]
    assert client_a.get("/api/plans/" + pid).json()["finishable"] is True
    assert next(p for p in client_a.get("/api/plans").json() if p["id"] == pid)["finishable"] is True
    patched = client_a.patch("/api/plans/" + pid, json={"description": "Preserve local retirement"})
    assert patched.status_code == 200 and patched.json()["finishable"] is True
    completed = client_a.post(
        "/api/plans/" + pid + "/complete", json={"feedback": "Synthetic timezone feedback", "rating": 4}
    )
    assert completed.status_code == 200, completed.text
    before = completed.json()["plan"]["metadata"]["completion"]
    assert before["completed_on"] == day
    profile(client_a, "UTC" if day.endswith("09") else "Pacific/Auckland")
    saved = client_a.get("/api/plans/" + pid).json()
    assert saved["metadata"]["completion"] == before and saved["status"] == "completed"
    notes = client_a.get("/api/plan-notes", params={"plan_id": pid}).json()
    assert len(notes) == 1 and notes[0]["body"] == "Synthetic timezone feedback"


@pytest.mark.parametrize("zone", ["Unknown/Calendar", "", None, 4, [], {}])
def test_unknown_explicit_zone_never_authorizes_completion(client_a, zone):
    created = plan(client_a, end="2026-10-07")
    assert client_a.patch("/api/plans/" + created["id"], json={"metadata": {"timezone": zone}}).status_code == 200
    response = client_a.post("/api/plans/" + created["id"] + "/complete", json={})
    assert response.status_code == 409, response.text
    saved = client_a.get("/api/plans/" + created["id"]).json()
    assert saved["status"] == "active" and saved["finishable"] is False


def test_next_plan_window_is_checked_in_its_own_calendar(client_a):
    current = plan(client_a, zone="America/Los_Angeles", end="2026-10-07", name="Current")
    expired = plan(client_a, zone="Australia/Brisbane", end="2026-10-08", name="Expired locally")
    live = plan(client_a, zone="America/Los_Angeles", end="2026-10-08", name="Still current locally")
    assert client_a.patch("/api/plans/" + live["id"], json={"startDate": "2026-10-02"}).status_code == 200
    response = client_a.post("/api/plans/" + current["id"] + "/complete", json={})
    assert response.status_code == 200, response.text
    assert response.json()["next_plan"]["id"] == live["id"]
    assert client_a.get("/api/plans/" + expired["id"]).json()["finishable"] is True


@pytest.mark.parametrize(
    "metadata_zone,schedule_zone,profile_zone,started,completed_day,skipped,remaining",
    [
        (None, None, None, "2026-10-08T20:00:00+00:00", "2026-10-09", 2, 0),
        (None, "America/Los_Angeles", "Australia/Brisbane", "2026-10-08T06:00:00+00:00", "2026-10-07", 0, 2),
        (
            "America/Los_Angeles",
            "Pacific/Auckland",
            "Australia/Brisbane",
            "2026-10-08T06:00:00+00:00",
            "2026-10-07",
            0,
            2,
        ),
        (None, "UTC", "Australia/Brisbane", "2026-10-08T06:00:00+00:00", "2026-10-08", 1, 1),
    ],
)
def test_strength_progress_and_calendar_match_same_local_dates(
    client_a, metadata_zone, schedule_zone, profile_zone, started, completed_day, skipped, remaining
):
    profile(client_a, profile_zone)
    created = plan(client_a, zone=metadata_zone, activity="strength", end="2026-10-09")
    body = {
        "startDate": "2026-10-07",
        "weeks": 1,
        "days": {d: {"title": "Synthetic strength", "routineId": "synthetic"} for d in ("wed", "thu", "fri")},
    }
    if schedule_zone is not None:
        body["timezone"] = schedule_zone
    assert client_a.put("/api/plans/" + created["id"] + "/schedule", json=body).status_code == 200
    start = datetime.fromisoformat(started)
    upload = client_a.post(
        "/api/workouts",
        json={
            "id": str(uuid.uuid4()),
            "activityType": "traditionalStrength",
            "source": "synthetic.calendar",
            "startDate": start.isoformat(),
            "endDate": (start + timedelta(hours=1)).isoformat(),
            "duration": 3600,
        },
    )
    assert upload.status_code == 201, upload.text
    current = client_a.get("/api/plans/" + created["id"]).json()
    assert current["progress"] == {
        "runs_total": 3,
        "runs_completed": 1,
        "runs_skipped": skipped,
        "runs_remaining": remaining,
    }
    entries = client_a.get("/api/schedule/calendar", params={"from": "2026-10-07", "to": "2026-10-09"}).json()[
        "entries"
    ]
    own = [e for e in entries if e["planId"] == created["id"]]
    assert [e["date"] for e in own if e["completed"]] == [completed_day]


def test_calendar_defaults_and_run_conflicts_use_default_local_day(client_a):
    created = plan(client_a, activity="strength", end="2026-10-09")
    assert (
        client_a.put(
            "/api/plans/" + created["id"] + "/schedule",
            json={"startDate": "2026-10-09", "weeks": 1, "days": {"fri": {"title": "Local strength"}}},
        ).status_code
        == 200
    )
    assert (
        client_a.post(
            "/api/queue",
            json={"activityType": "running", "title": "Local early run", "scheduledDate": "2026-10-08T20:00:00+00:00"},
        ).status_code
        == 201
    )
    response = client_a.get("/api/schedule/calendar")
    assert response.status_code == 200 and response.json()["from"] == "2026-10-09"
    today = [e for e in response.json()["entries"] if e["date"] == "2026-10-09"]
    assert {e["kind"] for e in today} == {"run", "strength"} and all(e["conflict"] for e in today)
    schedule = client_a.get("/api/plans/" + created["id"] + "/schedule").json()
    assert schedule["sessions"][0]["conflict"] is True


@pytest.mark.parametrize(
    "zone,day", [("America/Los_Angeles", "2026-10-08"), ("Australia/Brisbane", "2026-10-09"), (None, "2026-10-08")]
)
def test_nutrition_default_end_uses_its_documented_selected_timezone(client_a, zone, day):
    params = {"start_date": "2026-10-01"}
    if zone:
        params["timezone"] = zone
    response = client_a.get("/api/nutrition/summary", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["end_date"] == day


def test_validation_counts_upcoming_local_calendar_not_previous_local_day(client_a):
    created = plan(client_a, zone="Australia/Brisbane", end="2026-10-22")
    for hour in (10, 20):
        response = client_a.post(
            "/api/queue",
            json={
                "planId": created["id"],
                "activityType": "running",
                "title": "Synthetic local dose",
                "scheduledDate": f"2026-10-08T{hour}:00:00+00:00",
                "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
            },
        )
        assert response.status_code == 201
    response = client_a.post("/api/plans/" + created["id"] + "/validate")
    assert response.status_code == 200, response.text
    assert sum(week["planned_km"] for week in response.json()["weeks"]) == 5


@pytest.mark.parametrize(
    "zone,day", [(None, "2026-10-09"), ("Pacific/Auckland", "2026-10-09"), ("America/Los_Angeles", "2026-10-08")]
)
def test_completion_stamp_uses_the_known_local_day_without_rewriting_history(client_a, zone, day):
    created = plan(client_a, zone=zone, end=day)
    response = client_a.post(
        "/api/plans/" + created["id"] + "/complete", json={"feedback": "Synthetic calendar history"}
    )
    assert response.status_code == 200, response.text
    completion = response.json()["plan"]["metadata"]["completion"]
    assert completion["completed_on"] == day
    assert client_a.get("/api/plans/" + created["id"]).json()["metadata"]["completion"] == completion


def test_validation_uses_each_owned_queue_calendar(client_a):
    target = plan(client_a, zone="Australia/Brisbane", end="2026-10-22")
    other = plan(client_a, zone="America/Los_Angeles", end="2026-10-22")
    queued = client_a.post(
        "/api/queue",
        json={
            "planId": other["id"],
            "activityType": "running",
            "title": "Previous own calendar day",
            "scheduledDate": "2026-10-09T06:00:00Z",
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
        },
    )
    assert queued.status_code == 201
    result = client_a.post("/api/plans/" + target["id"] + "/validate")
    assert result.status_code == 200
    assert sum(w["planned_km"] for w in result.json()["weeks"]) == 0


def test_strength_conflicts_agree_with_each_run_calendar(client_a):
    strength = plan(client_a, zone="Australia/Brisbane", activity="strength", end="2026-10-09")
    running = plan(client_a, zone="America/Los_Angeles")
    assert (
        client_a.post(
            "/api/queue",
            json={
                "planId": running["id"],
                "activityType": "running",
                "title": "LA Thursday run",
                "scheduledDate": "2026-10-09T06:00:00Z",
            },
        ).status_code
        == 201
    )
    response = client_a.put(
        "/api/plans/" + strength["id"] + "/schedule",
        json={"startDate": "2026-10-09", "weeks": 1, "days": {"fri": {"title": "Friday strength"}}},
    )
    assert response.status_code == 200
    calendar = client_a.get("/api/schedule/calendar", params={"from": "2026-10-08", "to": "2026-10-09"}).json()
    assert all(not e["conflict"] for e in calendar["entries"])
    assert response.json()["sessions"][0]["conflict"] is False


def test_rejected_schedule_replacement_preserves_persisted_metadata(client_a):
    created = plan(client_a, activity="strength")
    previous = {
        "timezone": "Unknown/Calendar",
        "schedule": {"startDate": "2026-10-01", "weeks": 1, "days": {"thu": {"title": "Original"}}},
    }
    assert client_a.patch("/api/plans/" + created["id"], json={"metadata": previous}).status_code == 200
    response = client_a.put(
        "/api/plans/" + created["id"] + "/schedule",
        json={"startDate": "2026-10-09", "weeks": 1, "days": {"fri": {"title": "Replacement"}}},
    )
    assert response.status_code == 409
    assert client_a.get("/api/plans/" + created["id"]).json()["metadata"] == previous


@pytest.mark.parametrize(
    "run_zone,profile_zone,at",
    [("America/Los_Angeles", None, "2026-10-10T06:00:00Z"), (None, "America/Los_Angeles", "2026-10-10T06:00:00Z")],
)
def test_conflict_candidate_bounds_include_run_own_calendar(client_a, run_zone, profile_zone, at):
    profile(client_a, profile_zone)
    strength = plan(client_a, zone="Australia/Brisbane", activity="strength", end="2026-10-09")
    running = plan(client_a, zone=run_zone) if run_zone else None
    body = {"activityType": "running", "title": "Own Friday late run", "scheduledDate": at}
    if running:
        body["planId"] = running["id"]
    assert client_a.post("/api/queue", json=body).status_code == 201
    response = client_a.put(
        "/api/plans/" + strength["id"] + "/schedule",
        json={"startDate": "2026-10-09", "weeks": 1, "days": {"fri": {"title": "Friday strength"}}},
    )
    assert response.status_code == 200
    assert response.json()["sessions"][0]["conflict"] is True
    calendar = client_a.get("/api/schedule/calendar", params={"from": "2026-10-09", "to": "2026-10-09"}).json()
    assert len(calendar["entries"]) == 2 and all(e["conflict"] for e in calendar["entries"])


def test_unknown_owned_queue_zone_cannot_claim_known_validation_or_conflicts(client_a):
    target = plan(client_a, zone="Australia/Brisbane", activity="strength")
    running = plan(client_a, zone="Unknown/Calendar")
    assert (
        client_a.post(
            "/api/queue",
            json={
                "planId": running["id"],
                "activityType": "running",
                "title": "Unknown calendar",
                "scheduledDate": "2026-10-09T06:00:00Z",
            },
        ).status_code
        == 201
    )
    valid_running = plan(client_a, zone="Australia/Brisbane")
    validation = client_a.post("/api/plans/" + valid_running["id"] + "/validate")
    assert validation.status_code == 200 and validation.json()["weeks"] == []
    assert validation.json()["warnings"][0]["code"] == "calendar_timezone_unknown"
    response = client_a.put(
        "/api/plans/" + target["id"] + "/schedule",
        json={"startDate": "2026-10-09", "weeks": 1, "days": {"fri": {"title": "Friday strength"}}},
    )
    assert response.status_code == 409
    assert "schedule" not in client_a.get("/api/plans/" + target["id"]).json()["metadata"]
