"""Persisted malformed forecasts cannot abort other athletes' shared scan."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.coaching_scheduler import publish_forecast, scan_reviews
from app.models.plan import Plan
from tests.test_coaching_structural import approved_program

MALFORMED = [None, {}, [None], [{"id": 1}], [{"id": True}], [{"id": {}}], [{"id": []}], [{"id": "bad"}]]


@pytest.mark.parametrize("malformed", MALFORMED)
@pytest.mark.parametrize("caller", ["direct", "shared_scanner"])
def test_invalid_owned_forecast_isolated_from_other_athlete(client_a, client_b, session_factory, malformed, caller):
    now, bad_id = approved_program(client_a)
    _, _good_id = approved_program(client_b)
    metadata = client_a.get("/api/plans/" + bad_id).json()["metadata"]
    metadata["forecast"] = malformed
    changed = client_a.patch("/api/plans/" + bad_id, json={"metadata": metadata})
    assert changed.status_code == 200, changed.text
    before_bad = client_a.get("/api/queue", params={"limit": 200}).json()
    before_good = client_b.get("/api/queue", params={"limit": 200}).json()
    with session_factory() as db:
        if caller == "direct":
            plan = db.get(Plan, uuid.UUID(bad_id))
            publish_forecast(db, plan.user_id, plan, now + timedelta(days=35))
            db.commit()
        else:
            scan_reviews(db, now + timedelta(days=35))
    assert client_a.get("/api/queue", params={"limit": 200}).json() == before_bad
    assert client_a.get("/api/plans/" + bad_id).json()["metadata"]["forecast"] == malformed
    result = client_a.get("/api/coaching/programs/" + bad_id)
    assert result.status_code == 200, result.text
    assert result.json()["publicationReview"]["status"] == "review_required"
    assert result.json()["forecast"] is None
    if caller == "shared_scanner":
        assert len(client_b.get("/api/queue", params={"limit": 200}).json()) > len(before_good)
        assert client_b.get("/api/coaching/jobs").json()


@pytest.mark.parametrize(
    "field,value",
    [
        ("scheduledDate", None),
        ("scheduledDate", 1),
        ("scheduledDate", {}),
        ("scheduledDate", "2027-01-01T00:00:00"),
        ("scheduledDate", "bad"),
        ("scheduledDate", "9999-12-31T23:59:59-12:00"),
        ("scheduledDate", "0001-01-01T00:00:00+14:00"),
        ("composition", None),
        ("composition", []),
        ("composition", {}),
        ("title", None),
        ("title", []),
        ("date", 1),
        ("date", "bad"),
        ("date", "1900-01-01"),
        ("dose", {"distance_meters": 5000, "estimated_duration_seconds": "bad"}),
        ("dose", {"distance_meters": True, "estimated_duration_seconds": 100}),
        ("dose", {"distance_meters": 5000, "estimated_duration_seconds": -1}),
        ("dose", None),
        ("dose", []),
        ("dose", 1),
    ],
)
def test_invalid_complete_session_fields_do_not_abort_shared_scan(client_a, client_b, session_factory, field, value):
    now, bad_id = approved_program(client_a)
    _, _good_id = approved_program(client_b)
    metadata = client_a.get("/api/plans/" + bad_id).json()["metadata"]
    metadata["forecast"][0][field] = value
    before_forecast = metadata["forecast"]
    assert client_a.patch("/api/plans/" + bad_id, json={"metadata": metadata}).status_code == 200
    before_bad = client_a.get("/api/queue", params={"limit": 200}).json()
    before_good = client_b.get("/api/queue", params={"limit": 200}).json()
    with session_factory() as db:
        scan_reviews(db, now + timedelta(days=35))
    assert client_a.get("/api/queue", params={"limit": 200}).json() == before_bad
    assert client_a.get("/api/plans/" + bad_id).json()["metadata"]["forecast"] == before_forecast
    program = client_a.get("/api/coaching/programs/" + bad_id)
    assert program.status_code == 200 and program.json()["forecast"] is None
    assert program.json()["publicationReview"]["status"] == "review_required"
    assert len(client_b.get("/api/queue", params={"limit": 200}).json()) > len(before_good)
    assert client_b.get("/api/coaching/jobs").json()
    rebuilt = client_a.post(
        "/api/coaching/programs/" + bad_id + "/rebuild",
        json={
            "idempotencyKey": "malformed current immutable forecast",
            "interruption": {"confirmed": False},
            "coverage": {"complete": False},
        },
    )
    assert rebuilt.status_code == 422, rebuilt.text


@pytest.mark.parametrize("dose", [None, [], 1])
def test_remaining_generator_rejects_nonobject_immutable_dose_without_mutation(dose):
    import copy

    from fastapi import HTTPException

    from app.coaching_program import generate_remaining_program
    from tests.test_coaching_program import PLAN_ID, remaining_fixture

    profile, forecast, now, context = remaining_fixture()
    forecast[0]["dose"] = dose
    original = copy.deepcopy(forecast)
    with pytest.raises(HTTPException) as rejected:
        generate_remaining_program(PLAN_ID, profile, forecast, now, context)
    assert rejected.value.status_code == 422
    assert forecast == original


NESTED_INVALID = [
    {"blocks": [None]},
    {"blocks": ["bad"]},
    {"blocks": {}},
    {"blocks": [{"iterations": "four", "steps": []}]},
    {"blocks": [{"iterations": True, "steps": []}]},
    {"blocks": [{"iterations": 1.5, "steps": []}]},
    {"blocks": [{"steps": [None]}]},
    {"blocks": [{"steps": {}}]},
    {"blocks": [{"steps": [{"purpose": [], "goal": {"type": "open"}}]}]},
    {"blocks": [{"steps": [{"goal": []}]}]},
    {"blocks": [{"steps": [{"alert": []}]}]},
    {"warmup": []},
    {"cooldown": []},
    {"singleGoal": []},
    {"singleGoal": {"type": "distance", "unit": "meters", "value": "5000"}},
    {"singleGoal": {"type": "distance", "unit": "meters", "value": True}},
    {"blocks": [{"steps": [{"goal": {"type": "open"}, "alert": {"type": "speed", "min": "fast", "max": 4}}]}]},
]


@pytest.mark.parametrize("composition", NESTED_INVALID)
@pytest.mark.parametrize("caller", ["direct", "shared_scanner"])
def test_nested_forecast_composition_is_never_published(client_a, client_b, session_factory, composition, caller):
    now, bad_id = approved_program(client_a)
    _, _good_id = approved_program(client_b)
    metadata = client_a.get("/api/plans/" + bad_id).json()["metadata"]
    target = next(
        f for f in metadata["forecast"] if datetime.fromisoformat(f["scheduledDate"]) > now + timedelta(days=35)
    )
    target["composition"] = composition
    assert client_a.patch("/api/plans/" + bad_id, json={"metadata": metadata}).status_code == 200
    before_bad = client_a.get("/api/queue", params={"limit": 200}).json()
    before_good = client_b.get("/api/queue", params={"limit": 200}).json()
    with session_factory() as db:
        if caller == "direct":
            plan = db.get(Plan, uuid.UUID(bad_id))
            publish_forecast(db, plan.user_id, plan, now + timedelta(days=35))
            db.commit()
        else:
            scan_reviews(db, now + timedelta(days=35))
    assert client_a.get("/api/queue", params={"limit": 200}).json() == before_bad
    invalid = client_a.get("/api/coaching/programs/" + bad_id)
    assert invalid.status_code == 200 and invalid.json()["forecast"] is None
    assert invalid.json()["publicationReview"]["status"] == "review_required"
    if caller == "shared_scanner":
        assert len(client_b.get("/api/queue", params={"limit": 200}).json()) > len(before_good)
        assert client_b.get("/api/coaching/jobs").json()


@pytest.mark.parametrize("composition", [{"blocks": [None]}, {"blocks": [{"iterations": "four", "steps": []}]}])
def test_malformed_supported_queue_shape_rejected_before_persistence(client_a, composition):
    from fastapi.testclient import TestClient

    from app.main import app

    safe = TestClient(app, headers=dict(client_a.headers), raise_server_exceptions=False)
    before = client_a.get("/api/queue", params={"limit": 200}).json()
    response = safe.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Malformed shape",
            "scheduledDate": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
            "workoutData": composition,
        },
    )
    assert response.status_code == 422, response.text
    assert client_a.get("/api/queue", params={"limit": 200}).json() == before


@pytest.mark.parametrize("composition", [{"blocks": [None]}, {"blocks": [{"iterations": "four", "steps": []}]}])
def test_historical_malformed_queue_has_unknown_metrics_and_claim_makes_progress(
    client_a, session_factory, composition
):
    from fastapi.testclient import TestClient
    from sqlalchemy import update

    from app.main import app
    from app.models.queue import WorkoutQueue

    now, pid = approved_program(client_a)
    item = client_a.post(
        "/api/queue",
        json={
            "activityType": "running",
            "title": "Historical unknown structure",
            "scheduledDate": (now + timedelta(days=5)).isoformat(),
            "workoutData": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
        },
    ).json()
    # Represent previously persisted legacy facts; production history is not rewritten.
    with session_factory() as db:
        db.execute(
            update(WorkoutQueue).where(WorkoutQueue.id == uuid.UUID(item["id"])).values(workout_data=composition)
        )
        db.commit()
    request = client_a.post("/api/coaching/reviews", json={"planId": pid, "idempotencyKey": "legacy malformed shape"})
    assert request.status_code == 201, request.text
    token = client_a.post(
        "/api/auth/tokens", json={"name": "Synthetic legacy reviewer", "scope": "coach_worker"}
    ).json()["token"]
    safe = TestClient(app, headers={"Authorization": "Bearer " + token}, raise_server_exceptions=False)
    response = safe.post("/api/coaching/worker/claim", json={})
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job and job["id"] == request.json()["id"]
    context = job["request"]["context"]
    unknown = [week for week in context["metrics"]["weeks"].values() if week.get("unknown_planned_sessions")]
    assert unknown and unknown[0]["planned_distance_meters"] is None
    assert unknown[0]["planned_duration_seconds"] is None
    assert unknown[0]["actual_vs_planned_distance_ratio"] is None
    saved = next(row for row in client_a.get("/api/coaching/jobs").json() if row["id"] == job["id"])
    assert saved["attempts"] == 1 and saved["status"] == "processing"
    assert (
        next(row for row in client_a.get("/api/queue", params={"limit": 200}).json() if row["id"] == item["id"])[
            "workout_data"
        ]
        == composition
    )


@pytest.mark.parametrize("compact", ["20261001", "2026-W40-4"])
def test_noncanonical_forecast_calendar_date_is_unknown_and_not_complete(client_a, compact):
    _, pid = approved_program(client_a)
    metadata = client_a.get("/api/plans/" + pid).json()["metadata"]
    metadata["forecast"][0]["date"] = compact
    assert client_a.patch("/api/plans/" + pid, json={"metadata": metadata}).status_code == 200
    program = client_a.get("/api/coaching/programs/" + pid)
    assert program.status_code == 200 and program.json()["forecast"] is None
    assert program.json()["publicationReview"]["status"] == "review_required"
    assert client_a.get("/api/plans/" + pid).json()["finishable"] is False


def test_noncanonical_past_date_never_enters_complete_remaining_explanation(
    client_a, monkeypatch, session_factory, user_a
):
    from tests.test_coaching_review_scope import rebuild_fixture

    current, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    metadata = client_a.get("/api/plans/" + pid).json()["metadata"]
    past = next(f for f in metadata["forecast"] if datetime.fromisoformat(f["scheduledDate"]) < current)
    past["date"] = past["date"].replace("-", "")
    assert client_a.patch("/api/plans/" + pid, json={"metadata": metadata}).status_code == 200
    response = client_a.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    if response.status_code == 422:
        assert "forecast" in str(response.json()["detail"]).lower()
        return
    assert response.status_code == 201, response.text
    explained = client_a.post(
        "/api/coaching/jobs/" + response.json()["previewJobId"] + "/explanation",
        json={"idempotencyKey": "noncanonical past date calendar"},
    )
    assert explained.status_code == 201, explained.text
    summary = explained.json()["data"]["summary"]
    assert past["id"] not in {row["id"] for row in summary["remainingCalendar"]}
    assert summary["pastExcludedCount"] == sum(
        datetime.fromisoformat(f["scheduledDate"]) < current for f in metadata["forecast"]
    )


@pytest.mark.parametrize(
    "malformation", ["past_date", "protected_duration", "missing_type", "unsupported_type", "nonstring_type"]
)
def test_rebuild_malformed_immutable_calendar_or_dose_is_controlled(
    client_a, monkeypatch, session_factory, user_a, malformation, freeze_coaching_clock
):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_coaching_review_scope import rebuild_fixture

    # Friday noon UTC remains Friday after five weeks, placing the issued
    # Saturday 07:00 Brisbane session inside the following 24-hour horizon.
    base = datetime(2026, 10, 9, 12, tzinfo=UTC)
    helper_module = rebuild_fixture.__globals__["approved_program"].__module__
    freeze_coaching_clock(base, helper_module)
    current, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    metadata = client_a.get("/api/plans/" + pid).json()["metadata"]
    if malformation == "past_date":
        target = next(f for f in metadata["forecast"] if datetime.fromisoformat(f["scheduledDate"]) > current)
        target["date"] = "1900-01-01"
    else:
        # An issued session within the immutable protection horizon retains its dose.
        target = next(
            f
            for f in metadata["forecast"]
            if current < datetime.fromisoformat(f["scheduledDate"]) <= current + timedelta(hours=24)
        )
        if malformation == "protected_duration":
            target["dose"]["estimated_duration_seconds"] = "bad"
        elif malformation == "missing_type":
            target.pop("session_type")
        else:
            target["session_type"] = "mystery" if malformation == "unsupported_type" else []
    assert client_a.patch("/api/plans/" + pid, json={"metadata": metadata}).status_code == 200
    safe = TestClient(app, headers=dict(client_a.headers), raise_server_exceptions=False)
    response = safe.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert response.status_code == 422, response.text
    assert client_a.get("/api/plans/" + pid).json()["metadata"] == metadata


@pytest.mark.parametrize(
    "field,value",
    [
        ("scheduledDate", "9999-12-31T13:00:00+00:00"),
        ("dose", {"distance_meters": 5000, "estimated_duration_seconds": 10**400}),
    ],
)
def test_forecast_conversion_and_numeric_bounds_are_safe_for_every_consumer(field, value):
    from app.coaching_forecast import validated_forecast

    session = {
        "id": str(uuid.uuid4()),
        "date": "9999-12-31",
        "scheduledDate": "9999-12-31T13:00:00+00:00",
        "title": "Bounds",
        "composition": {"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}},
    }
    if field == "dose":
        session.update(date="2027-01-01", scheduledDate="2027-01-01T07:00:00+10:00")
    session[field] = value
    assert validated_forecast([session]) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("programFacts", None),
        ("programFacts", []),
        ("programFacts", {"available_days": {}}),
        ("goals", {"type": "10k"}),
        ("goals", [None]),
        ("goals", [{"type": []}]),
        ("goals", [{"type": "mystery"}]),
        ("goals", [{"type": "10k", "race_date": []}]),
        ("goals", [{"type": "10k", "target_seconds": "bad"}]),
    ],
)
def test_malformed_program_metadata_does_not_abort_shared_scan(client_a, client_b, session_factory, field, value):
    now, bad_id = approved_program(client_a)
    approved_program(client_b)
    metadata = client_a.get("/api/plans/" + bad_id).json()["metadata"]
    metadata[field] = value
    assert client_a.patch("/api/plans/" + bad_id, json={"metadata": metadata}).status_code == 200
    before_bad = client_a.get("/api/queue", params={"limit": 200}).json()
    before_good = client_b.get("/api/queue", params={"limit": 200}).json()
    with session_factory() as db:
        scan_reviews(db, now + timedelta(days=35))
    assert client_a.get("/api/queue", params={"limit": 200}).json() == before_bad
    program = client_a.get("/api/coaching/programs/" + bad_id)
    assert program.status_code == 200 and program.json()["publicationReview"]["status"] == "review_required"
    assert client_a.get("/api/plans/" + bad_id).json()["finishable"] is False
    assert client_a.get("/api/plans/" + bad_id).json()["metadata"] == metadata
    assert len(client_b.get("/api/queue", params={"limit": 200}).json()) > len(before_good)
    assert client_b.get("/api/coaching/jobs").json()


@pytest.mark.parametrize(
    "field,value", [("programFacts", None), ("programFacts", []), ("goals", {"type": "10k"}), ("goals", [{"type": []}])]
)
def test_rebuild_malformed_program_metadata_is_controlled(client_a, monkeypatch, session_factory, user_a, field, value):
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_coaching_review_scope import rebuild_fixture

    _, pid, body = rebuild_fixture(client_a, monkeypatch, session_factory, user_a)
    metadata = client_a.get("/api/plans/" + pid).json()["metadata"]
    metadata[field] = value
    assert client_a.patch("/api/plans/" + pid, json={"metadata": metadata}).status_code == 200
    safe = TestClient(app, headers=dict(client_a.headers), raise_server_exceptions=False)
    response = safe.post("/api/coaching/programs/" + pid + "/rebuild", json=body)
    assert response.status_code == 422, response.text
    assert client_a.get("/api/plans/" + pid).json()["metadata"] == metadata
