"""Nutrition default calendar is independent of the process-local date."""

from datetime import UTC, date, datetime

from app.routes import nutrition

NOW = datetime(2026, 10, 8, 22, tzinfo=UTC)


def test_process_local_day_cannot_override_documented_utc_calendar(client_a, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    class BrisbaneDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 9)

    monkeypatch.setattr(nutrition, "datetime", Clock)
    monkeypatch.setattr(nutrition, "date", BrisbaneDate)
    response = client_a.get("/api/nutrition/summary", params={"start_date": "2026-10-09"})
    assert response.status_code == 400
    assert response.json()["detail"] == "end_date must not precede start_date"
    valid = client_a.get("/api/nutrition/summary", params={"start_date": "2026-10-08"})
    assert valid.status_code == 200 and valid.json()["end_date"] == "2026-10-08"
