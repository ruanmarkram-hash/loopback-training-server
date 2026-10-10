"""Approved whole-program progress and completion at PostgreSQL/API seams."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_coaching_structural import approved_program


def start_program_clock(monkeypatch, now):
    from app.routes import plans

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (now + timedelta(days=4)).astimezone(tz or UTC)

    monkeypatch.setattr(plans, "datetime", Clock)


@pytest.mark.parametrize("terminal", ["completed", "skipped"])
def test_all_initially_issued_sessions_do_not_complete_whole_program(client_a, monkeypatch, session_factory, terminal):
    now, pid = approved_program(client_a)
    start_program_clock(monkeypatch, now)
    forecast = client_a.get("/api/coaching/programs/" + pid).json()["forecast"]
    issued = client_a.get("/api/queue", params={"limit": 200}).json()
    for q in issued:
        assert client_a.patch("/api/queue/" + q["id"] + "/status", json={"status": terminal}).status_code == 200
    plan = client_a.get("/api/plans/" + pid).json()
    assert plan["progress"]["runs_total"] == len(forecast)
    assert plan["progress"]["runs_remaining"] == len(forecast) - len(issued)
    assert plan["finishable"] is False
    before = client_a.get("/api/queue", params={"limit": 200}).json()
    response = client_a.post("/api/plans/" + pid + "/complete", json={"feedback": "Premature"})
    assert response.status_code == 409, response.text
    assert client_a.get("/api/plans/" + pid).json()["status"] == "active"
    assert client_a.get("/api/queue", params={"limit": 200}).json() == before
    from app.coaching_scheduler import publish_forecast
    from app.models.plan import Plan

    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        publish_forecast(db, plan.user_id, plan, now + timedelta(days=35))
        db.commit()
    assert len(client_a.get("/api/queue", params={"limit": 200}).json()) > len(before)


@pytest.mark.parametrize("route", ["complete", "patch"])
def test_explicit_completion_cannot_stop_unpublished_program(client_a, route):
    _, pid = approved_program(client_a)
    response = (
        client_a.post("/api/plans/" + pid + "/complete", json={})
        if route == "complete"
        else client_a.patch("/api/plans/" + pid, json={"status": "completed"})
    )
    assert response.status_code == 409, response.text
    assert client_a.get("/api/plans/" + pid).json()["status"] == "active"


@pytest.mark.parametrize("retirement", ["completed", "mixed_deleted"])
def test_full_approved_logical_calendar_retired_allows_completion(client_a, monkeypatch, session_factory, retirement):
    now, pid = approved_program(client_a)
    start_program_clock(monkeypatch, now)
    assert (
        client_a.put(
            "/api/coaching/profile", json={"available_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        ).status_code
        == 200
    )
    from app.coaching_scheduler import publish_forecast
    from app.models.plan import Plan

    with session_factory() as db:
        plan = db.get(Plan, uuid.UUID(pid))
        for days in range(0, 165, 7):
            publish_forecast(db, plan.user_id, plan, now + timedelta(days=days))
            db.flush()
        db.commit()
    forecast = client_a.get("/api/coaching/programs/" + pid).json()["forecast"]
    issued = client_a.get("/api/queue", params={"limit": 200}).json()
    assert len(issued) == len(forecast), [
        (f["scheduledDate"], f["title"]) for f in forecast if f["id"] not in {q["id"] for q in issued}
    ]
    for index, row in enumerate(issued):
        if retirement == "mixed_deleted" and index > 0:
            assert client_a.delete("/api/queue/" + row["id"]).status_code == 204
        else:
            assert (
                client_a.patch("/api/queue/" + row["id"] + "/status", json={"status": "completed"}).status_code == 200
            )
    before = client_a.get("/api/plans/" + pid).json()
    assert before["progress"]["runs_total"] == len(forecast)
    assert before["progress"]["runs_remaining"] == 0
    assert before["finishable"] is True
    if retirement == "mixed_deleted":
        assert before["progress"]["runs_retired"] == len(forecast) - 1
        assert before["progress"]["runs_completed"] == 1
        assert before["progress"]["runs_skipped"] == 0
    assert client_a.post("/api/plans/" + pid + "/complete", json={}).status_code == 200


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        [],
        [None],
        [{"id": "unknown"}],
        [{"id": 1, "scheduledDate": "2027-01-01T00:00:00+00:00"}],
        [{"id": True, "scheduledDate": "2027-01-01T00:00:00+00:00"}],
        [{"id": {}, "scheduledDate": "2027-01-01T00:00:00+00:00"}],
        [{"id": [], "scheduledDate": "2027-01-01T00:00:00+00:00"}],
    ],
)
def test_unknown_approved_forecast_never_reports_finishable_or_completes(client_a, monkeypatch, malformed):
    now, pid = approved_program(client_a)
    start_program_clock(monkeypatch, now)
    for row in client_a.get("/api/queue", params={"limit": 200}).json():
        assert client_a.patch("/api/queue/" + row["id"] + "/status", json={"status": "completed"}).status_code == 200
    metadata = client_a.get("/api/plans/" + pid).json()["metadata"]
    metadata["forecast"] = malformed
    changed = client_a.patch("/api/plans/" + pid, json={"metadata": metadata})
    assert changed.status_code == 200, changed.text
    assert changed.json()["finishable"] is False
    fetched = client_a.get("/api/plans/" + pid)
    assert fetched.status_code == 200 and fetched.json()["finishable"] is False
    listed = client_a.get("/api/plans")
    assert listed.status_code == 200
    assert next(row for row in listed.json() if row["id"] == pid)["finishable"] is False
    assert client_a.post("/api/plans/" + pid + "/complete", json={}).status_code == 409


def test_duplicate_forecast_uuid_is_counted_once(client_a):
    _, pid = approved_program(client_a)
    original = client_a.get("/api/plans/" + pid).json()
    metadata = original["metadata"]
    metadata["forecast"].append(metadata["forecast"][0])
    changed = client_a.patch("/api/plans/" + pid, json={"metadata": metadata})
    assert changed.status_code == 200, changed.text
    assert changed.json()["progress"] == original["progress"]
