"""Literal physical-unit expectations through the reducer and persisted worker context."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.plan_validation import session_from_composition

CASES = [
    ({"singleGoal": {"type": "distance", "unit": "meters", "value": 5000}}, 5000, 5000 / 2.6),
    (
        {"blocks": [{"iterations": 1, "steps": [{"goal": {"type": "distance", "unit": "miles", "value": 5}}]}]},
        8046.72,
        8046.72 / 2.6,
    ),
    (
        {
            "blocks": [
                {
                    "iterations": 1,
                    "steps": [
                        {
                            "goal": {"type": "time", "unit": "seconds", "value": 600},
                            "alert": {"type": "speed", "unit": "kilometersPerHour", "min": 9, "max": 9},
                        }
                    ],
                }
            ]
        },
        1500,
        600,
    ),
]


@pytest.mark.parametrize("composition,meters,seconds", CASES)
def test_supported_physical_units_are_reduced_exactly(composition, meters, seconds):
    result = session_from_composition(composition, date(2026, 10, 9), "Synthetic supported units", 2.6)
    assert result.has_data
    assert result.distance_m == pytest.approx(meters)
    assert result.duration_s == pytest.approx(seconds)
    assert result.hard is False


@pytest.mark.parametrize("composition,meters,seconds", CASES)
def test_actual_owned_queue_worker_metrics_preserve_units(client_a, composition, meters, seconds):
    now = datetime.now(UTC)
    plan = client_a.post(
        "/api/plans", json={"name": "Units", "activityType": "running", "startDate": now.date().isoformat()}
    ).json()
    created = client_a.post(
        "/api/queue",
        json={
            "planId": plan["id"],
            "activityType": "running",
            "title": "Known units",
            "scheduledDate": (now + timedelta(days=5)).isoformat(),
            "workoutData": composition,
        },
    )
    assert created.status_code == 201, created.text
    requested = client_a.post("/api/coaching/reviews", json={"planId": plan["id"], "idempotencyKey": "Physical units"})
    assert requested.status_code == 201
    token = client_a.post("/api/auth/tokens", json={"name": "Synthetic unit reviewer", "scope": "coach_worker"}).json()[
        "token"
    ]
    claimed = client_a.post("/api/coaching/worker/claim", json={}, headers={"Authorization": "Bearer " + token})
    assert claimed.status_code == 200, claimed.text
    weeks = claimed.json()["job"]["request"]["context"]["metrics"]["weeks"].values()
    assert sum(week["planned_distance_meters"] for week in weeks) == pytest.approx(meters)
    assert sum(week["planned_duration_seconds"] for week in weeks) == pytest.approx(seconds)


def test_warmup_cooldown_repetition_and_intensity_use_same_speed_units():
    step = {
        "goal": {"type": "time", "unit": "minutes", "value": 1},
        "alert": {"type": "speed", "unit": "kilometersPerHour", "min": 9, "max": 9},
    }
    result = session_from_composition(
        {"warmup": step, "cooldown": step, "blocks": [{"iterations": 2, "steps": [{**step, "purpose": "work"}]}]},
        date(2026, 10, 9),
        "Four minutes",
        2.6,
    )
    assert result.distance_m == 600
    assert result.duration_s == 240
    assert result.hard is False


@pytest.mark.parametrize(
    "goal", [{"type": "distance", "unit": "furlongs", "value": 5}, {"type": "time", "unit": "fortnights", "value": 1}]
)
def test_unknown_goal_units_are_unknown_not_assumed_metric(goal):
    result = session_from_composition(
        {"blocks": [{"steps": [{"goal": goal}]}]}, date(2026, 10, 9), "Unknown units", 2.6
    )
    assert result.has_data is False and result.estimated is True


def test_unsupported_speed_unit_is_an_explicit_estimate_and_never_hard():
    result = session_from_composition(
        {
            "blocks": [
                {
                    "steps": [
                        {
                            "goal": {"type": "time", "unit": "seconds", "value": 600},
                            "alert": {"type": "speed", "unit": "furlongsPerFortnight", "min": 9, "max": 9},
                        }
                    ]
                }
            ]
        },
        date(2026, 10, 9),
        "Unknown speed unit",
        2.6,
    )
    assert result.distance_m == 1560 and result.duration_s == 600
    assert result.estimated is True and result.hard is False


@pytest.mark.parametrize(
    "goal", [{"type": "distance", "unit": "furlongs", "value": 1}, {"type": "time", "unit": "fortnights", "value": 1}]
)
def test_legacy_unsupported_goal_unit_execution_stays_unknown(goal):
    import uuid
    from types import SimpleNamespace

    from app.coaching_policy import assess

    now = datetime.now(UTC)
    revision = uuid.uuid4()
    prescription = SimpleNamespace(
        id=revision,
        created_at=now - timedelta(days=1),
        snapshot={
            "composition": {
                "trainingPurpose": "quality",
                "blocks": [
                    {
                        "steps": [
                            {
                                "purpose": "work",
                                "goal": goal,
                                "alert": {"type": "speed", "unit": "metersPerSecond", "min": 2, "max": 4},
                            }
                        ]
                    }
                ],
            }
        },
    )
    workout = SimpleNamespace(
        start_date=now,
        data={
            "prescriptionRevision": str(revision),
            "prescribedSegments": [
                {
                    "stepIndex": 0,
                    "purpose": "work",
                    "distanceMeters": 1000,
                    "durationSeconds": 330,
                    "measurementSource": "healthkit",
                    "sampleCoverage": 1,
                }
            ],
        },
    )
    result = assess(workout, prescription)
    assert result["execution"] == "not_assessable" and result["structure"] == "unknown"
    assert "unit" in result["reason"].lower()


def test_adaptive_race_date_uses_validated_goals_and_keeps_legacy_manual_shape():
    from app.plan_validation import extract_race_date

    assert extract_race_date({"forecastApproved": True, "goals": [{"type": "10k", "race_date": "2027-01-01"}]}) == date(
        2027, 1, 1
    )
    assert extract_race_date({"forecastApproved": True, "race_date": "2027-01-01", "goals": [{"type": []}]}) is None
    assert extract_race_date({"goals": {"race_date": "2027-01-01"}}) == date(2027, 1, 1)
