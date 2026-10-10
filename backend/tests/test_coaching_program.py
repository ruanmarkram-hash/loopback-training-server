"""Public pure forecast seam; these tests do not open the integration database."""

import copy
import itertools
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.coaching_program import generate_remaining_program, program_forecast


@pytest.fixture(autouse=True)
def wire():
    """Override the API harness's autouse fixture for this pure module."""
    yield


PLAN_ID = uuid.UUID("48b2c8be-8956-49f3-bad5-d600af387382")
DAYS = ["mon", "wed", "sat"]


def remaining_fixture(kind="10k", weeks=20, elapsed=8):
    old, end = preview(kind, weeks=weeks)
    first = date.fromisoformat(old[0]["date"])
    as_of = datetime.combine(first + timedelta(weeks=elapsed), datetime.min.time(), UTC)
    interruption_end = as_of.date() - timedelta(days=21)
    profile = {
        "goal": {"type": kind, "race_date": end.isoformat()},
        "available_days": DAYS,
        "benchmark": facts(),
        "timezone": "Australia/Brisbane",
    }
    evidence = {"source": "verified synthetic running inventory", "observed_at": as_of.isoformat(), "confidence": 0.9}
    context = {
        "interruption": {
            **evidence,
            "verified": True,
            "start_date": (interruption_end - timedelta(days=20)).isoformat(),
            "end_date": interruption_end.isoformat(),
        },
        "coverage": {
            **evidence,
            "status": "complete",
            "start_date": (as_of.date() - timedelta(days=20)).isoformat(),
            "end_date": (as_of.date() - timedelta(days=1)).isoformat(),
        },
        "load": {
            **facts(weekly_distance_meters=12000, long_run_meters=4800),
            **evidence,
            "window_start": (as_of.date() - timedelta(days=14)).isoformat(),
            "window_end": (as_of.date() - timedelta(days=1)).isoformat(),
        },
        "protected_session_ids": [],
    }
    return profile, old, as_of, context


def test_remaining_program_recovers_rebuilds_tapers_without_mutating_or_moving_event():
    profile, old, as_of, context = remaining_fixture()
    original = copy.deepcopy((profile, old, context))
    future = next(s for s in old if date.fromisoformat(s["date"]) > as_of.date())
    context["protected_session_ids"] = [future["id"]]
    original = copy.deepcopy((profile, old, context))
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "forecast_ready"
    assert result["end_date"] == profile["goal"]["race_date"]
    assert (profile, old, context) == original
    frozen = [s for s in old if date.fromisoformat(s["date"]) <= as_of.date() or s["id"] == future["id"]]
    assert all(s in result["forecast"] for s in frozen)
    changed = [s for s in result["forecast"] if s not in frozen and s["session_type"] != "event"]
    assert {"recovery", "build", "taper"} <= {s["phase"] for s in changed}
    assert all(
        s["session_type"] != "quality"
        for s in changed
        if date.fromisoformat(s["date"]) < as_of.date() + timedelta(days=15)
    )
    assert result["forecast"][-1]["date"] == old[-1]["date"]
    assert future["id"] not in result["replacement_session_ids"]


@pytest.mark.parametrize(
    "defect",
    ["unknown", "partial", "stale", "missing_confidence", "unverified", "short_window", "pre_interruption_load"],
)
def test_remaining_program_requires_fresh_supported_coverage_and_current_load(defect):
    profile, old, as_of, context = remaining_fixture()
    if defect in ("unknown", "partial"):
        context["coverage"]["status"] = defect
    elif defect == "stale":
        context["load"]["observed_at"] = (as_of - timedelta(days=8)).isoformat()
    elif defect == "missing_confidence":
        context["load"].pop("confidence")
    elif defect == "unverified":
        context["interruption"]["verified"] = False
    elif defect == "short_window":
        context["load"]["window_start"] = (as_of.date() - timedelta(days=7)).isoformat()
    else:
        context["load"]["window_start"] = context["interruption"]["start_date"]
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "review_required"
    assert result["forecast"] is None and result["replacement_session_ids"] == []
    assert result["preserved_forecast"] == old
    assert result["limitations"] and result["options"]


def test_remaining_program_near_event_explains_infeasibility_without_cramming():
    profile, old, as_of, context = remaining_fixture(elapsed=19)
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "goal_infeasible"
    assert result["forecast"] is None
    assert result["preserved_forecast"] == old
    assert result["end_date"] == old[-1]["date"]
    assert any("event" in option.lower() for option in result["options"])


def test_remaining_program_never_replaces_missed_distance_or_exceeds_supported_baseline():
    profile, old, as_of, context = remaining_fixture()
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    by_week = {}
    for s in result["forecast"]:
        d = date.fromisoformat(s["date"])
        if d > as_of.date() and s["session_type"] != "event":
            monday = d - timedelta(days=d.weekday())
            by_week.setdefault(monday, []).append(s)
    first = min(by_week)
    assert sum(s["dose"]["distance_meters"] for s in by_week[first]) <= 12000 * 0.7
    assert max(sum(s["dose"]["distance_meters"] for s in v) for v in by_week.values()) <= 12000 * 1.5
    assert all(s["dose"]["distance_meters"] <= 4800 * 1.5 for v in by_week.values() for s in v)


def test_remaining_program_rejects_event_change_unknown_protection_and_naive_clock():
    profile, old, as_of, context = remaining_fixture()
    for change in ("event", "protection", "clock"):
        p, c = copy.deepcopy(profile), copy.deepcopy(context)
        clock = as_of
        if change == "event":
            p["goal"]["race_date"] = (date.fromisoformat(old[-1]["date"]) + timedelta(days=1)).isoformat()
        elif change == "protection":
            c["protected_session_ids"] = [str(uuid.uuid4())]
        else:
            clock = as_of.replace(tzinfo=None)
        with pytest.raises(HTTPException) as error:
            generate_remaining_program(PLAN_ID, p, old, clock, c)
        assert error.value.status_code == 422


@pytest.mark.parametrize(
    "defect", ["missing_pace", "zero_load", "future_observation", "low_confidence", "stale_window"]
)
def test_remaining_program_unknown_present_ability_never_reuses_old_profile_fitness(defect):
    profile, old, as_of, context = remaining_fixture()
    if defect == "missing_pace":
        context["load"].pop("pace_seconds_per_km")
    elif defect == "zero_load":
        context["load"]["weekly_distance_meters"] = 0
    elif defect == "future_observation":
        context["coverage"]["observed_at"] = (as_of + timedelta(seconds=1)).isoformat()
    elif defect == "low_confidence":
        context["coverage"]["confidence"] = 0.49
    else:
        context["coverage"]["end_date"] = (as_of.date() - timedelta(days=2)).isoformat()
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "review_required" and result["forecast"] is None
    assert result["preserved_forecast"] == old


def test_remaining_program_protected_work_consumes_budget_and_has_no_new_adjacent_hard_session():
    profile, old, as_of, context = remaining_fixture()
    future = [s for s in old if date.fromisoformat(s["date"]) > as_of.date() and s["session_type"] != "event"]
    protected = future[0]
    context["protected_session_ids"] = [protected["id"]]
    context["completed_session_ids"] = [future[1]["id"]]
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "forecast_ready"
    assert protected in result["forecast"] and future[1] in result["forecast"]
    for s in result["forecast"]:
        if s["id"] not in context["protected_session_ids"] + context["completed_session_ids"] and s["session_type"] in (
            "quality",
            "long",
        ):
            assert all(
                abs((date.fromisoformat(s["date"]) - date.fromisoformat(p["date"])).days) >= 2
                for p in (protected, future[1])
                if p["session_type"] in ("quality", "long")
            )
    assert future[1]["id"] not in result["replacement_session_ids"]


def test_remaining_program_uses_explicit_clock_instead_of_machine_today():
    profile, old, as_of, context = remaining_fixture()
    delta = timedelta(days=-728)
    shifted = copy.deepcopy(old)
    for s in shifted:
        s["date"] = (date.fromisoformat(s["date"]) + delta).isoformat()
        s["scheduledDate"] = (datetime.fromisoformat(s["scheduledDate"]) + delta).isoformat()
        s["composition"]["scheduledDate"] = s["scheduledDate"]
        s["id"] = str(uuid.uuid5(PLAN_ID, "forecast:" + s["date"]))
    profile["goal"]["race_date"] = shifted[-1]["date"]
    for name in ("interruption", "coverage", "load"):
        context[name]["observed_at"] = (datetime.fromisoformat(context[name]["observed_at"]) + delta).isoformat()
        for key in ("start_date", "end_date", "window_start", "window_end"):
            if key in context[name]:
                context[name][key] = (date.fromisoformat(context[name][key]) + delta).isoformat()
    result = generate_remaining_program(PLAN_ID, profile, shifted, as_of + delta, context)
    assert result["status"] == "forecast_ready"
    assert result["end_date"] == shifted[-1]["date"]


def test_remaining_program_changed_availability_cannot_create_new_work_within_24_hours():
    profile, old, as_of, context = remaining_fixture()
    as_of += timedelta(hours=12)
    profile["available_days"] = ["tue", "thu", "sat"]
    old_ids = {s["id"] for s in old}
    omitted_id = str(uuid.uuid5(PLAN_ID, "forecast:" + (as_of.date() + timedelta(days=1)).isoformat()))
    assert omitted_id not in old_ids
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "forecast_ready"
    assert all(
        datetime.fromisoformat(s["scheduledDate"]) > as_of + timedelta(hours=24)
        for s in result["forecast"]
        if s["id"] not in old_ids
    )
    assert omitted_id not in result["replacement_session_ids"]
    assert any("24" in limit for limit in result["limitations"])


def calendar_training_weeks(sessions):
    weeks = {}
    for s in sessions:
        if s["session_type"] != "event":
            d = date.fromisoformat(s["date"])
            weeks.setdefault(d - timedelta(days=d.weekday()), []).append(s)
    return weeks


def test_remaining_program_protected_quality_counts_toward_one_per_calendar_week():
    profile, old, as_of, context = remaining_fixture()
    protected = next(
        s
        for s in old
        if s["session_type"] == "quality" and date.fromisoformat(s["date"]) >= as_of.date() + timedelta(days=14)
    )
    context["protected_session_ids"] = [protected["id"]]
    profile["available_days"] = ["wed", "fri", "sun"]
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "forecast_ready"
    assert protected in result["forecast"]
    assert all(
        sum(s["session_type"] == "quality" for s in week) <= 1
        for week in calendar_training_weeks(result["forecast"]).values()
    )


def test_remaining_program_rechecks_feasibility_after_all_protected_budget_reductions():
    profile, _, as_of, context = remaining_fixture()
    old, end = preview(days=["mon", "tue", "wed", "fri", "sun"], weeks=20)
    profile["goal"]["race_date"] = end.isoformat()
    profile["available_days"] = ["tue", "thu", "sat"]
    context["load"].update(weekly_distance_meters=15000, long_run_meters=6000)
    context["protected_session_ids"] = [
        s["id"] for s in old if date.fromisoformat(s["date"]) > as_of.date() and s["session_type"] == "easy"
    ]
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "goal_infeasible"
    assert result["forecast"] is None and result["preserved_forecast"] == old
    assert any("final" in limit.lower() and "preparation" in limit.lower() for limit in result["limitations"])


def test_remaining_program_final_doses_cannot_grow_beyond_prior_calendar_week_and_session_peaks():
    profile, old, as_of, context = remaining_fixture()
    context["load"].update(weekly_distance_meters=50000, long_run_meters=20000)
    old_weeks = calendar_training_weeks(old)
    weekly_ceiling = max(sum(s["dose"]["distance_meters"] for s in week) for week in old_weeks.values())
    session_ceiling = max(s["dose"]["distance_meters"] for week in old_weeks.values() for s in week)
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "forecast_ready"
    assert (
        max(
            sum(s["dose"]["distance_meters"] for s in week)
            for week in calendar_training_weeks(result["forecast"]).values()
        )
        <= weekly_ceiling
    )
    assert (
        max(s["dose"]["distance_meters"] for s in result["forecast"] if s["session_type"] != "event") <= session_ceiling
    )


def test_remaining_program_protected_generated_slot_consumes_its_budget_exactly_once():
    profile, old, as_of, context = remaining_fixture()
    baseline = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    monday = as_of.date() + timedelta(days=7)
    baseline_week = calendar_training_weeks(baseline["forecast"])[monday]
    protected = next(s for s in baseline_week if date.fromisoformat(s["date"]).weekday() == 2)
    old = [copy.deepcopy(protected) if s["id"] == protected["id"] else s for s in old]
    context["protected_session_ids"] = [protected["id"]]
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "forecast_ready" and protected in result["forecast"]
    expected = sum(s["dose"]["distance_meters"] for s in baseline_week)
    actual = sum(s["dose"]["distance_meters"] for s in calendar_training_weeks(result["forecast"])[monday])
    assert actual == expected


@pytest.mark.parametrize("inside_boundary", [True, False])
def test_remaining_program_auto_protects_existing_session_through_exact_24_hour_boundary(inside_boundary):
    profile, _, as_of, context = remaining_fixture()
    old, end = preview(days=["tue", "thu", "sat"], weeks=20)
    profile["goal"]["race_date"] = end.isoformat()
    profile["timezone"] = "Australia/Brisbane"
    existing = next(s for s in old if date.fromisoformat(s["date"]) == as_of.date() + timedelta(days=1))
    scheduled = datetime.fromisoformat(existing["scheduledDate"])
    clock = scheduled - timedelta(hours=24)
    if not inside_boundary:
        clock -= timedelta(seconds=1)
    # Observation was just before the rebuild request; protection hints are absent.
    for name in ("interruption", "coverage", "load"):
        context[name]["observed_at"] = clock.isoformat()
    before = copy.deepcopy(old)
    result = generate_remaining_program(PLAN_ID, profile, old, clock, context)
    assert result["status"] == "forecast_ready"
    assert old == before
    if inside_boundary:
        assert existing in result["forecast"]
        assert existing["id"] not in result["replacement_session_ids"]
    else:
        assert existing not in result["forecast"]
        assert existing["id"] in result["replacement_session_ids"]


def facts(**changes):
    return dict(
        {
            "pace_seconds_per_km": 360,
            "weekly_distance_meters": 15000,
            "long_run_meters": 6000,
            "source": "synthetic fixture",
            "observed_at": datetime.now(UTC).isoformat(),
            "level": "established",
        },
        **changes,
    )


def preview(kind="10k", days=None, benchmark=None, weeks=12, **goal_fields):
    today = datetime.now(UTC).date()
    start = today + timedelta(days=(7 - today.weekday()) % 7 + 7)
    goal = dict(type=kind, race_date=(start + timedelta(weeks=weeks)).isoformat(), **goal_fields)
    return program_forecast(PLAN_ID, goal, days or DAYS, benchmark or facts(), start, "Australia/Brisbane")


def test_full_forecast_has_distinct_doses_phases_and_exact_event():
    forecast, end = preview()
    assert {"foundation", "build", "recovery", "taper", "event"} <= {s["phase"] for s in forecast}
    assert {"easy", "long", "quality", "event"} <= {s["session_type"] for s in forecast}
    event = forecast[-1]
    assert event["date"] == end.isoformat()
    assert event["title"] == "10 km event"
    assert event["composition"]["blocks"][0]["steps"][0]["goal"]["value"] == 10000
    assert all(s["status"] == "forecast" and s["estimated"] for s in forecast)
    assert all(not s.get("prescription_committed", False) for s in forecast)
    assert len({s["id"] for s in forecast}) == len(forecast)


def test_infeasible_fixed_marathon_is_explained_not_relabelled_as_ready():
    with pytest.raises(HTTPException) as error:
        preview("marathon", benchmark=facts(weekly_distance_meters=25000, long_run_meters=10000), weeks=16)
    assert error.value.status_code == 422
    assert error.value.detail["status"] == "goal_constraints"
    assert error.value.detail["fixed_event_unchanged"] is True
    assert "projected" in error.value.detail["reason"].lower()


@pytest.mark.parametrize(
    "kind,weekly,longest,weeks",
    [
        ("first_5k", 15000, 6000, 8),
        ("5k", 15000, 6000, 8),
        ("10k", 15000, 6000, 12),
        ("half_marathon", 30000, 12000, 16),
        ("marathon", 60000, 25000, 20),
    ],
)
def test_supported_race_goals_have_named_events_and_no_training_pace_prediction(kind, weekly, longest, weeks):
    sessions, end = preview(kind, benchmark=facts(weekly_distance_meters=weekly, long_run_meters=longest), weeks=weeks)
    assert sessions[-1]["phase"] == "event"
    assert sessions[-1]["date"] == end.isoformat()
    assert sessions[-1]["dose"]["estimated_duration_seconds"] is None
    assert sessions[-1]["week_summary"]["event_excluded"] is True
    assert all("alert" not in step for s in sessions for b in s["composition"]["blocks"] for step in b["steps"])


def test_aspirational_finish_time_does_not_prescribe_faster_pace_or_more_load():
    evidence = facts()
    original, _ = preview(benchmark=evidence)
    ambitious, _ = preview(benchmark=evidence, target_seconds=900)
    assert [s["composition"] for s in original] == [s["composition"] for s in ambitious]
    assert [s["dose"] for s in original] == [s["dose"] for s in ambitious]
    assert any("aspiration" in c for c in ambitious[0]["constraints"])


@pytest.mark.parametrize("available", [[], ["mon", "mon"], ["monday"], ["mon", "tue", "wed", "thu", "fri", "sat"]])
def test_availability_must_be_one_to_five_distinct_named_weekdays(available):
    with pytest.raises(HTTPException) as error:
        program_forecast(
            PLAN_ID,
            {"type": "general_fitness"},
            available,
            facts(),
            datetime.now(UTC).date() + timedelta(days=2),
            "UTC",
        )
    assert error.value.status_code == 422
    assert error.value.detail["status"] == "onboarding_required"


@pytest.mark.parametrize(
    "available",
    [["sat"], ["wed", "sat"], ["mon", "wed", "sat"], ["mon", "tue", "thu", "sat"], ["mon", "tue", "wed", "thu", "fri"]],
)
def test_frequency_changes_dose_without_inventing_makeup_runs(available):
    start = datetime.now(UTC).date() + timedelta(days=7)
    start -= timedelta(days=start.weekday())
    sessions, _ = program_forecast(PLAN_ID, {"type": "general_fitness"}, available, facts(), start, "UTC")
    first = [s for s in sessions if s["date"] < (start + timedelta(days=7)).isoformat()]
    assert len(first) == len(available)
    assert {date.fromisoformat(s["date"]).weekday() for s in first} == {
        ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(d) for d in available
    }
    assert sum(s["dose"]["distance_meters"] for s in first) <= 15000
    assert max(s["dose"]["distance_meters"] for s in first) <= 6000
    assert (
        first[0]["week_summary"]["long_run_share"] <= {1: 1.0, 2: 0.551, 3: 0.401, 4: 0.351, 5: 0.301}[len(available)]
    )
    assert not any(s["phase"] == "taper" for s in sessions)


def test_build_and_recovery_are_bounded_and_taper_reduces_load():
    sessions, _ = preview(weeks=20)
    weeks = {s["week_summary"]["week_start"]: s["week_summary"] for s in sessions}
    totals = [s["planned_distance_meters"] for s in weeks.values()]
    assert max(totals) <= 22500
    assert totals[3] < totals[2] * 0.85
    assert all(total <= max(totals[:i]) * 1.031 for i, total in enumerate(totals) if i > 0)
    taper = [s for s in sessions if s["phase"] == "taper"]
    assert taper and not any(s["session_type"] in ("long", "quality") for s in taper)
    assert taper[0]["week_summary"]["planned_distance_meters"] < max(totals) * 0.6
    assert max(s["dose"]["distance_meters"] for s in sessions if s["session_type"] != "event") <= 9000


def test_quality_and_long_runs_are_never_stacked_even_across_week_boundary():
    for available in (["mon", "wed", "sun"], ["fri", "sat", "sun"], ["mon", "tue", "wed", "thu", "fri"]):
        sessions, _ = preview("5k", days=available)
        demanding = [date.fromisoformat(s["date"]) for s in sessions if s["session_type"] in ("quality", "long")]
        assert all((b - a).days >= 2 for a, b in itertools.pairwise(demanding))
        assert all(s["week_summary"]["quality_session_count"] <= 1 for s in sessions)


def test_partial_first_week_does_not_redistribute_omitted_work():
    monday = datetime.now(UTC).date() + timedelta(days=7)
    monday -= timedelta(days=monday.weekday())
    full, _ = program_forecast(PLAN_ID, {"type": "general_fitness"}, DAYS, facts(), monday, "UTC")
    partial, _ = program_forecast(
        PLAN_ID, {"type": "general_fitness"}, DAYS, facts(), monday + timedelta(days=2), "UTC"
    )
    for session in partial[:2]:
        counterpart = next(s for s in full if s["date"] == session["date"])
        assert session["dose"] == counterpart["dose"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", " "),
        ("source", None),
        ("observed_at", None),
        ("weekly_distance_meters", None),
        ("long_run_meters", None),
        ("pace_seconds_per_km", None),
    ],
)
def test_missing_current_evidence_requests_onboarding(field, value):
    with pytest.raises(HTTPException) as error:
        preview(benchmark=facts(**{field: value}))
    assert error.value.status_code == 422
    assert error.value.detail["status"] == "onboarding_required"
    assert "benchmark." + field in error.value.detail["required_fields"]


@pytest.mark.parametrize(
    "changed",
    [
        {"weekly_distance_meters": float("inf")},
        {"long_run_meters": -1},
        {"pace_seconds_per_km": float("nan")},
        {"confidence": float("inf")},
        {"confidence": -0.1},
        {"confidence": True},
        {"source": 42},
        {"observed_at": "2026-10-01"},
        {"observed_at": "not a timestamp"},
        {"long_run_meters": 16000},
    ],
)
def test_invalid_benchmark_quantities_and_provenance_are_rejected(changed):
    with pytest.raises(HTTPException) as error:
        preview(benchmark=facts(**changed))
    assert error.value.status_code == 422


@pytest.mark.parametrize("age", [timedelta(days=43), timedelta(days=-1)])
def test_stale_or_future_evidence_is_not_current_ability(age):
    with pytest.raises(HTTPException) as error:
        preview(benchmark=facts(observed_at=(datetime.now(UTC) - age).isoformat()))
    assert error.value.detail["status"] == "onboarding_required"


@pytest.mark.parametrize("race", ["not-a-date", "2026-02-30", ""])
def test_required_event_date_is_valid_and_not_improvised(race):
    with pytest.raises(HTTPException) as error:
        program_forecast(
            PLAN_ID,
            {"type": "10k", "race_date": race},
            DAYS,
            facts(),
            datetime.now(UTC).date() + timedelta(days=7),
            "UTC",
        )
    assert error.value.status_code == 422


@pytest.mark.parametrize("weeks", [0, 7, 53])
def test_insufficient_or_unbounded_event_horizon_is_rejected(weeks):
    with pytest.raises(HTTPException) as error:
        preview(weeks=weeks)
    assert error.value.detail["fixed_event_unchanged"] is True


def test_local_timezone_date_and_forecast_ids_are_stable():
    sessions, _ = preview()
    reordered, _ = preview(days=list(reversed(DAYS)))
    assert [(s["id"], s["composition"]) for s in sessions] == [(s["id"], s["composition"]) for s in reordered]
    assert all(datetime.fromisoformat(s["scheduledDate"]).utcoffset() == timedelta(hours=10) for s in sessions)
    assert all(datetime.fromisoformat(s["scheduledDate"]).date().isoformat() == s["date"] for s in sessions)
    with pytest.raises(HTTPException) as error:
        program_forecast(
            PLAN_ID,
            {"type": "general_fitness"},
            DAYS,
            facts(),
            datetime.now(UTC).date() + timedelta(days=2),
            "Invalid/Zone",
        )
    assert error.value.status_code == 422


def test_c25k_requires_explicit_low_history_and_progresses_to_estimated_continuous_running():
    start = datetime.now(UTC).date() + timedelta(days=7)
    start -= timedelta(days=start.weekday())
    beginner = facts(level="beginner", weekly_distance_meters=0, long_run_meters=0, pace_seconds_per_km=None)
    sessions, end = program_forecast(PLAN_ID, {"type": "first_5k"}, DAYS, beginner, start, "UTC")
    runs = [s for s in sessions if s["session_type"] == "walk_run"]
    assert len(runs) == 36
    assert (end - start).days == 83
    assert runs[0]["dose"]["running_seconds"] == 480
    assert runs[-1]["dose"]["running_seconds"] == 1800
    assert runs[-1]["composition"]["blocks"][0]["iterations"] == 1
    assert all(s["dose"]["distance_meters"] is None for s in sessions)
    assert all(s["week_summary"]["planned_distance_meters"] is None for s in sessions)
    assert any("5 km completion is unverified" in c for c in sessions[-1]["constraints"])
    missing = dict(beginner, weekly_distance_meters=None)
    with pytest.raises(HTTPException) as error:
        program_forecast(PLAN_ID, {"type": "first_5k"}, DAYS, missing, start, "UTC")
    assert "benchmark.weekly_distance_meters" in error.value.detail["required_fields"]


def test_c25k_uses_spaced_run_days_and_walking_on_extra_available_days():
    start = datetime.now(UTC).date() + timedelta(days=7)
    available = ["mon", "tue", "wed", "thu", "fri"]
    beginner = facts(level="beginner", weekly_distance_meters=0, long_run_meters=0, pace_seconds_per_km=None)
    sessions, _ = program_forecast(PLAN_ID, {"type": "first_5k"}, available, beginner, start, "UTC")
    runs = [date.fromisoformat(s["date"]) for s in sessions if s["session_type"] == "walk_run"]
    assert len(runs) == 36
    assert all((b - a).days >= 2 for a, b in itertools.pairwise(runs))
    assert any(s["session_type"] == "walk" for s in sessions)


def test_beginner_fixed_event_cannot_skip_required_exposures_or_rest():
    beginner = facts(level="beginner", weekly_distance_meters=0, long_run_meters=0, pace_seconds_per_km=None)
    with pytest.raises(HTTPException) as error:
        preview("first_5k", benchmark=beginner, weeks=8)
    assert error.value.detail["fixed_event_unchanged"] is True


@pytest.mark.parametrize(
    "available,weeks", [(["sat"], 36), (["wed", "sat"], 18), (DAYS, 12), (["fri", "sat", "sun"], 18)]
)
def test_c25k_lower_frequency_extends_forecast_without_compressing_exposures(available, weeks):
    start = datetime.now(UTC).date() + timedelta(days=7)
    beginner = facts(level="beginner", weekly_distance_meters=0, long_run_meters=0, pace_seconds_per_km=None)
    sessions, end = program_forecast(PLAN_ID, {"type": "first_5k"}, available, beginner, start, "UTC")
    runs = [s for s in sessions if s["session_type"] == "walk_run"]
    assert (end - start).days + 1 == weeks * 7
    assert len(runs) == 36
    assert runs[-1]["composition"]["blocks"][0]["iterations"] == 1
    assert runs[-1]["dose"]["running_seconds"] == 1800


def test_goal_doses_include_all_warmup_cooldown_and_repeated_recoveries():
    from app.plan_validation import session_from_composition

    sessions, _ = preview()
    for s in sessions:
        reduced = session_from_composition(s["composition"], date.fromisoformat(s["date"]), s["title"], 1000 / 360)
        assert reduced.distance_m == pytest.approx(s["dose"]["distance_meters"], abs=0.5)
        if s["session_type"] != "event":
            assert reduced.duration_s == pytest.approx(s["dose"]["estimated_duration_seconds"], abs=1)


def test_c25k_taper_reduces_time_and_event_does_not_fake_distance_estimates():
    beginner = facts(level="beginner", weekly_distance_meters=0, long_run_meters=0, pace_seconds_per_km=None)
    sessions, end = preview("first_5k", benchmark=beginner, weeks=14)
    taper = [s for s in sessions if s["phase"] == "taper" and s["session_type"] == "walk_run"]
    assert taper and max(s["dose"]["running_seconds"] for s in taper) <= 900
    assert sessions[-1]["date"] == end.isoformat()
    assert sessions[-1]["dose"]["distance_meters"] == 5000
    assert all(s["dose"]["distance_meters"] is None for s in sessions[:-1])


@pytest.mark.parametrize(
    "goal",
    [
        {"type": "injury_recovery"},
        {"type": "general_fitness", "race_date": "2027-03-01"},
        {"type": "5k", "target_seconds": -1, "race_date": "2027-03-01"},
    ],
)
def test_unsupported_goal_or_invalid_aspiration_does_not_create_a_program(goal):
    with pytest.raises(HTTPException) as error:
        program_forecast(PLAN_ID, goal, DAYS, facts(), datetime.now(UTC).date() + timedelta(days=7), "UTC")
    assert error.value.status_code == 422


def test_evidence_age_has_an_exact_42_day_boundary_and_zero_confidence_stays_explicit(monkeypatch):
    import app.coaching_program as module

    fixed = datetime(2026, 10, 8, 23, 30, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", Clock)
    start = date(2026, 10, 10)
    current = facts(observed_at=(fixed - timedelta(days=42)).isoformat(), confidence=0)
    sessions, _ = program_forecast(PLAN_ID, {"type": "general_fitness"}, DAYS, current, start, "UTC")
    assert sessions[0]["benchmark_evidence"]["confidence"] == 0
    with pytest.raises(HTTPException) as error:
        program_forecast(
            PLAN_ID,
            {"type": "general_fitness"},
            DAYS,
            dict(current, observed_at=(fixed - timedelta(days=42, seconds=1)).isoformat()),
            start,
            "UTC",
        )
    assert error.value.detail["status"] == "onboarding_required"
    with pytest.raises(HTTPException) as error:
        # UTC Oct8 is already Oct9 in Brisbane, even though the route may use UTC.
        program_forecast(PLAN_ID, {"type": "general_fitness"}, DAYS, current, date(2026, 10, 8), "Australia/Brisbane")
    assert error.value.status_code == 422


def test_pre_event_rest_is_preserved_and_availability_exception_is_disclosed():
    sessions, end = preview()
    assert date.fromisoformat(sessions[-2]["date"]) < end - timedelta(days=2)
    assert any("outside normal training availability" in c for c in sessions[-1]["constraints"]) is False
    sessions, end = preview(days=["tue", "thu", "sat"])
    assert sessions[-1]["date"] == end.isoformat()
    assert any("outside normal training availability" in c for c in sessions[-1]["constraints"])


def test_c25k_recovery_holds_progression_and_running_dose_has_a_bounded_peak():
    start = datetime.now(UTC).date() + timedelta(days=7)
    beginner = facts(level="beginner", weekly_distance_meters=0, long_run_meters=0, pace_seconds_per_km=None)
    sessions, _ = program_forecast(PLAN_ID, {"type": "first_5k"}, DAYS, beginner, start, "UTC")
    peak = 0
    for session in sessions:
        dose = session["dose"]["running_seconds"]
        if peak:
            assert dose <= peak + min(60, peak * 0.08) + 8  # integer per-repetition rounding
        peak = max(peak, dose)
    assert peak == 1800


def test_forecast_does_not_mutate_supplied_ability_or_goal():
    import copy

    start = datetime.now(UTC).date() + timedelta(days=7)
    evidence = facts()
    goal = {"type": "general_fitness"}
    before = copy.deepcopy((evidence, goal, DAYS))
    program_forecast(PLAN_ID, goal, DAYS, evidence, start, "UTC")
    assert (evidence, goal, DAYS) == before


@pytest.mark.parametrize("terminal", ["completed", "retired"])
def test_retained_future_terminal_history_cannot_establish_remaining_preparation(terminal):
    profile, old, as_of, context = remaining_fixture()
    future_ids = [s["id"] for s in old if date.fromisoformat(s["date"]) > as_of.date()]
    context["protected_session_ids"] = future_ids
    context["retired_session_ids"] = future_ids
    if terminal == "completed":
        context["completed_session_ids"] = future_ids
    original = copy.deepcopy(old)
    result = generate_remaining_program(PLAN_ID, profile, old, as_of, context)
    assert result["status"] == "goal_infeasible"
    assert result["forecast"] is None
    assert result["preserved_forecast"] == original
    assert old == original
