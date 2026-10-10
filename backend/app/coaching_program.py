"""Deterministic, provisional goal forecasts, never an athlete readiness guarantee.

Aggregate benchmark pace is used only for estimated time/distance accounting.
It cannot establish an easy/threshold/race pace, so no speed alerts are generated.
Approval, immutable revisions and the 14-day committed window belong to the caller.
"""

import math
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from app.coaching_policy import finite_positive

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MIN_WEEKS = {"first_5k": 8, "5k": 8, "10k": 8, "half_marathon": 12, "marathon": 16, "general_fitness": 8}
EVENT_METERS = {"first_5k": 5000, "5k": 5000, "10k": 10000, "half_marathon": 21097.5, "marathon": 42195}
EVENT_NAMES = {
    "first_5k": "First 5 km event",
    "5k": "5 km event",
    "10k": "10 km event",
    "half_marathon": "Half marathon event",
    "marathon": "Marathon event",
}
PROGRAM_POLICY = {
    "version": "loopback-goal-forecast-v2",
    "evidence_max_age_days": 42,
    "growth_per_build_week": 0.03,
    "maximum_growth_fraction": 0.5,
    "recovery_every_weeks": 4,
    "recovery_fraction": 0.8,
    "minimum_c25k_sessions": 36,
    "maximum_quality_sessions_per_week": 1,
    "beginner_growth_per_exposure": 0.08,
    "beginner_maximum_added_seconds": 60,
    "minimum_demanding_session_gap_days": 2,
    "pre_event_rest_days": 2,
    "taper_weeks": {"first_5k": 1, "5k": 1, "10k": 1, "half_marathon": 2, "marathon": 3},
    "long_run_share_by_frequency": {1: 1.0, 2: 0.55, 3: 0.4, 4: 0.35, 5: 0.3},
    "minimum_established_load": {
        "10k": (10000, 4000, 2),
        "half_marathon": (20000, 8000, 3),
        "marathon": (25000, 10000, 3),
    },
    "projected_preparation_screen_meters": {
        "10k": (12000, 6000),
        "half_marathon": (25000, 14000),
        "marathon": (40000, 28000),
    },
}

REMAINING_PROGRAM_POLICY = {
    "version": "loopback-remaining-forecast-v1",
    "evidence_max_age_days": 7,
    "minimum_supported_load_days": 14,
    "minimum_confidence": 0.5,
    "recovery_days": 14,
    "recovery_fraction": 0.7,
    "new_prescription_freeze_hours": 24,
    "growth_per_build_week": 0.03,
    "maximum_growth_fraction": 0.5,
    "minimum_rebuild_weeks": {
        "first_5k": 8,
        "5k": 4,
        "10k": 6,
        "half_marathon": 8,
        "marathon": 12,
        "general_fitness": 4,
    },
}


def _onboarding(fields):
    raise HTTPException(422, {"status": "onboarding_required", "required_fields": fields})


def _event_constraint(reason):
    raise HTTPException(
        422,
        {
            "status": "goal_constraints",
            "reason": reason,
            "fixed_event_unchanged": True,
            "policy": PROGRAM_POLICY["version"],
        },
    )


def _validate(goal, days, benchmark, start, zone, reference_at=None):
    from app.coaching_forecast import valid_program_goal

    if not valid_program_goal(goal):
        raise HTTPException(422, "Unsupported running goal")
    kind = goal["type"]
    if (
        not isinstance(days, (list, tuple))
        or not 1 <= len(days) <= 5
        or any(not isinstance(d, str) or d not in DAYS for d in days)
        or len(set(days)) != len(days)
    ):
        _onboarding(["available_days (1 to 5 distinct weekdays)"])
    try:
        tz = ZoneInfo(zone)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Unknown timezone")
    if not isinstance(start, date) or isinstance(start, datetime):
        raise HTTPException(422, "Start must be a local calendar date")
    reference_at = reference_at or datetime.now(UTC)
    if start < reference_at.astimezone(tz).date():
        raise HTTPException(422, "Program start is in the past in the athlete timezone")
    try:
        event = date.fromisoformat(goal["race_date"]) if goal.get("race_date") else None
    except (TypeError, ValueError):
        raise HTTPException(422, "Invalid race date")
    if kind not in ("first_5k", "general_fitness") and event is None:
        _onboarding(["goal.race_date"])
    if kind == "general_fitness" and event:
        raise HTTPException(422, "General fitness has no race event; select a race goal")
    if goal.get("target_seconds") is not None and not finite_positive(goal["target_seconds"]):
        raise HTTPException(422, "Goal result must be finite and positive")
    if benchmark is not None and not isinstance(benchmark, dict):
        raise HTTPException(422, "Benchmark must be an object")
    benchmark = benchmark or {}
    beginner = kind == "first_5k" and benchmark.get("level") == "beginner"
    required = []
    keys = ["weekly_distance_meters", "long_run_meters", "source", "observed_at"]
    if not beginner:
        keys.append("pace_seconds_per_km")
    for key in keys:
        if benchmark.get(key) is None or isinstance(benchmark.get(key), str) and not benchmark[key].strip():
            required.append("benchmark." + key)
    if required:
        _onboarding(required)
    if benchmark.get("level") not in (None, "beginner", "established"):
        raise HTTPException(422, "Unknown ability level")
    if benchmark.get("level") == "beginner" and not beginner:
        _event_constraint("Beginner evidence supports a first_5k walk/run forecast, not established race training")
    if not isinstance(benchmark["source"], str) or len(benchmark["source"].strip()) > 200:
        raise HTTPException(422, "Benchmark evidence source required (up to 200 characters)")
    for key in ("weekly_distance_meters", "long_run_meters"):
        value = benchmark[key]
        if not finite_positive(value) and not (
            beginner and isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0
        ):
            raise HTTPException(422, "Benchmark load must be finite and positive; explicit beginner zero is supported")
    pace = benchmark.get("pace_seconds_per_km")
    if pace is not None and (not finite_positive(pace) or not 120 <= pace <= 1200):
        raise HTTPException(422, "Aggregate benchmark pace is outside the supported 120 to 1200 seconds/km range")
    confidence = benchmark.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise HTTPException(422, "Benchmark confidence must be between zero and one")
    try:
        observed = datetime.fromisoformat(benchmark["observed_at"])
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Benchmark evidence timestamp required")
    if observed.tzinfo is None:
        raise HTTPException(422, "Benchmark evidence timestamp must include timezone")
    age = reference_at - observed
    if age < timedelta(minutes=-5) or age > timedelta(days=PROGRAM_POLICY["evidence_max_age_days"]):
        _onboarding(["benchmark.observed_at (current evidence within 42 days, not future dated)"])
    weekly = benchmark["weekly_distance_meters"]
    longest = benchmark["long_run_meters"]
    if longest > weekly or (weekly > 0 and longest == 0):
        raise HTTPException(422, "Long run must be consistent with the supplied weekly load")
    if weekly > 200000 or longest > 60000:
        _event_constraint("Supplied load is outside this provisional forecast policy")
    if not beginner and (weekly < 1000 or longest < 500):
        _event_constraint(
            "Established forecast requires at least 1000 m/week and 500 m longest-run evidence; review beginner onboarding"
        )
    if beginner and (weekly > 15000 or longest > 5000):
        _event_constraint("Supplied running load and beginner level disagree; review current ability")
    if kind in PROGRAM_POLICY["minimum_established_load"]:
        min_weekly, min_long, min_days = PROGRAM_POLICY["minimum_established_load"][kind]
        if weekly < min_weekly or longest < min_long or len(days) < min_days:
            _event_constraint(
                f"{kind} requires established evidence of at least {min_weekly} m/week, {min_long} m longest run and {min_days} available days under this provisional policy"
            )
    return kind, tz, event, beginner, benchmark


def _spaced_days(available):
    """Largest subset up to three, respecting gaps across the week boundary."""
    for count in range(min(3, len(available)), 0, -1):
        for candidate in combinations(sorted(available), count):
            if count == 1 or all(
                min((a - b) % 7, (b - a) % 7) >= PROGRAM_POLICY["minimum_demanding_session_gap_days"]
                for a, b in combinations(candidate, 2)
            ):
                return set(candidate)


def _step(purpose, value, unit="meters"):
    return {
        "purpose": purpose,
        "goal": {"type": "distance" if unit == "meters" else "time", "unit": unit, "value": value},
    }


def _distance_composition(distance, session_type):
    # Warmup/cooldown are INCLUDED in the distance budget, never extra mileage.
    edge = round(min(800, distance * 0.15), 1)
    body = round(distance - 2 * edge, 1)
    if session_type == "quality":
        # Deliberately no pace alert. Relaxed work/recovery effort is described below.
        steps = [_step("work", round(body * 0.6 / 4, 1)), _step("rest", round(body * 0.4 / 4, 1))]
        blocks = [{"iterations": 4, "steps": steps}]
    else:
        blocks = [{"iterations": 1, "steps": [_step("work", body)]}]
    return {"warmup": _step("warmup", edge), "cooldown": _step("cooldown", edge), "blocks": blocks}


def _beginner_composition(index, recovery=False, walking=False, taper=False):
    if walking:
        return {"blocks": [{"iterations": 1, "steps": [_step("work", 1200, "seconds")]}]}, 0, 1200
    # Add at most 8% or 60 s running per exposure. Holds at 30 min; no pace/5 km promise.
    running = 480.0
    for _ in range(min(index, 35)):
        running = min(
            1800,
            running
            + min(
                PROGRAM_POLICY["beginner_maximum_added_seconds"],
                running * PROGRAM_POLICY["beginner_growth_per_exposure"],
            ),
        )
    if recovery or taper:
        running *= 0.5 if taper else PROGRAM_POLICY["recovery_fraction"]
    repetitions = 1 if running >= 1800 else max(1, 8 - index // 4)
    work = math.floor(running / repetitions)
    steps = [_step("work", work, "seconds")]
    rest = max(30, 90 - index * 2) if repetitions > 1 else 0
    if rest:
        steps.append(_step("rest", rest, "seconds"))
    comp = {
        "warmup": _step("warmup", 300, "seconds"),
        "cooldown": _step("cooldown", 300, "seconds"),
        "blocks": [{"iterations": repetitions, "steps": steps}],
    }
    return comp, work * repetitions, work * repetitions + rest * repetitions + 600


def program_forecast(plan_id, goal, days, benchmark, start, zone):
    """Return (dated forecast sessions, fixed event/end date) without persistence."""
    return _forecast(plan_id, goal, days, benchmark, start, zone)


def _forecast(plan_id, goal, days, benchmark, start, zone, remaining=None):
    kind, tz, event, beginner, benchmark = _validate(
        goal, days, benchmark, start, zone, remaining["as_of"] if remaining else None
    )
    try:
        namespace = uuid.UUID(str(plan_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(422, "Plan identity must be a UUID")
    available = sorted(DAYS.index(d) for d in days)
    run_days = _spaced_days(available) if beginner else set(available)
    minimum_weeks = (
        max(MIN_WEEKS[kind], math.ceil(PROGRAM_POLICY["minimum_c25k_sessions"] / len(run_days)))
        if beginner
        else MIN_WEEKS[kind]
    )
    end = event or (remaining["end"] if remaining else start + timedelta(weeks=minimum_weeks) - timedelta(days=1))
    if not remaining and ((end - start).days + 1 < minimum_weeks * 7 or (end - start).days > 364):
        _event_constraint(
            f"Fixed event leaves insufficient or unsupported preparation window (minimum {minimum_weeks} weeks, maximum 52 weeks)"
        )
    if beginner and event and not remaining:
        exposures = sum(
            (start + timedelta(days=i)).weekday() in run_days
            for i in range(max(0, (event - start).days - PROGRAM_POLICY["pre_event_rest_days"]))
        )
        if exposures < PROGRAM_POLICY["minimum_c25k_sessions"]:
            _event_constraint(
                "Fixed event and availability leave fewer than 36 graded walk/run exposures, including pre-event rest"
            )
    anchor = start - timedelta(days=start.weekday())
    long_day = available[-1]
    quality_days = [
        d
        for d in available
        if min((d - long_day) % 7, (long_day - d) % 7) >= PROGRAM_POLICY["minimum_demanding_session_gap_days"]
    ]
    quality_day = (
        quality_days[0]
        if len(available) >= 3 and quality_days and PROGRAM_POLICY["maximum_quality_sessions_per_week"]
        else None
    )
    limits = [
        "Forecast only; athlete approval and current evidence required before prescriptions are committed.",
        "No readiness, finish time or clinical safety guarantee; no catch-up workload is added.",
    ]
    if goal.get("target_seconds"):
        limits.append("Desired finish time is an aspiration, not a training pace or predicted result.")
    if benchmark.get("confidence") is None:
        limits.append("Ability evidence confidence is unknown.")
    if len(days) < 3:
        limits.append(
            "Limited frequency constrains progression; a high long-run share is unavoidable and is disclosed."
        )
    if not beginner and len(days) >= 3 and quality_day is None:
        limits.append("Available days do not separate quality and long sessions; quality omitted.")
    if event and event.weekday() not in available:
        limits.append("Fixed event is outside normal training availability; confirm event participation separately.")
    if beginner:
        limits.append("Walk/run progression estimates a continuous 30-minute run; 5 km completion is unverified.")
    # Roles are stable across weeks; partial first/last weeks never redistribute omitted work.
    long_share = PROGRAM_POLICY["long_run_share_by_frequency"][len(available)]
    quality_share = {1: 0, 2: 0, 3: 0.27, 4: 0.22, 5: 0.2}[len(available)]
    if quality_day is None:
        quality_share = 0
    other_count = len(available) - 1 - (quality_day is not None)
    easy_share = (1 - long_share - quality_share) / other_count if other_count else 0
    base_weekly = benchmark["weekly_distance_meters"]
    base_long = benchmark["long_run_meters"]
    # Changing frequency never makes a single run exceed established longest-run evidence.
    budget = min(base_weekly, base_long / long_share) if not beginner else 0
    forecast = []
    day = start
    run_index = 0
    build_weeks = 0
    week = -1
    factor = 1.0
    while day <= end:
        current_week = (day - anchor).days // 7
        if current_week != week:
            week = current_week
            recovery = (week + 1) % PROGRAM_POLICY["recovery_every_weeks"] == 0
            if week and not recovery:
                build_weeks += 1
            factor = min(
                1 + PROGRAM_POLICY["maximum_growth_fraction"],
                (1 + PROGRAM_POLICY["growth_per_build_week"]) ** build_weeks,
            )
            if recovery:
                factor *= PROGRAM_POLICY["recovery_fraction"]
        days_left = (event - day).days if event else None
        taper_days = PROGRAM_POLICY["taper_weeks"].get(kind, 0) * 7
        phase = (
            "recovery"
            if (week + 1) % PROGRAM_POLICY["recovery_every_weeks"] == 0
            else "foundation"
            if week < 2
            else "build"
        )
        if not event and (end - day).days < 14:
            phase = "consolidation"
        if event and 0 <= days_left < taper_days:
            phase = "taper"
        if remaining:
            elapsed = (day - start).days
            rebuild_week = max(0, (elapsed - REMAINING_PROGRAM_POLICY["recovery_days"]) // 7)
            factor = min(
                1 + REMAINING_PROGRAM_POLICY["maximum_growth_fraction"],
                (1 + REMAINING_PROGRAM_POLICY["growth_per_build_week"]) ** rebuild_week,
            )
            if elapsed < REMAINING_PROGRAM_POLICY["recovery_days"]:
                factor = REMAINING_PROGRAM_POLICY["recovery_fraction"]
                if phase != "taper":
                    phase = "recovery"
            elif phase != "taper":
                phase = "recovery" if (rebuild_week + 1) % PROGRAM_POLICY["recovery_every_weeks"] == 0 else "build"
                if phase == "recovery":
                    factor *= PROGRAM_POLICY["recovery_fraction"]
        if event and day == event:
            session_type = "event"
            title = EVENT_NAMES[kind]
            composition = {"blocks": [{"iterations": 1, "steps": [_step("work", EVENT_METERS[kind])]}]}
            distance = EVENT_METERS[kind]
            duration = None  # A race prediction cannot be inferred from aggregate pace.
            run_seconds = None
            effort = "Event marker only. Review readiness and participation; no goal pace is prescribed."
            phase = "event"
        elif day.weekday() in available and (not event or days_left > PROGRAM_POLICY["pre_event_rest_days"]):
            if beginner:
                walking = day.weekday() not in run_days
                session_type = "walk" if walking else "walk_run"
                composition, run_seconds, duration = _beginner_composition(
                    run_index, phase == "recovery", walking, phase == "taper"
                )
                title = (
                    "Easy walking"
                    if walking
                    else "Continuous easy foundation"
                    if composition["blocks"][0]["iterations"] == 1
                    else "Walk/run foundation"
                )
                if not walking and phase not in ("recovery", "taper"):
                    run_index += 1
                distance = None  # Zero history must never acquire an invented running speed.
                effort = "Walk comfortably. During running, keep a conversational effort. Repeat or reduce with a reviewed proposal if this feels too much."
            else:
                session_type = (
                    "long"
                    if day.weekday() == long_day and len(days) > 1
                    else "quality"
                    if day.weekday() == quality_day and phase == "build"
                    else "easy"
                )
                share = (
                    long_share
                    if day.weekday() == long_day
                    else quality_share
                    if day.weekday() == quality_day
                    else easy_share
                )
                taper_factor = 1.0
                if phase == "taper":
                    taper_factor = 0.5 if days_left < 7 else 0.65 if days_left < 14 else 0.8
                    if session_type == "long":
                        session_type = "easy"
                distance = math.floor(budget * factor * share * taper_factor / 10) * 10
                composition = _distance_composition(distance, session_type)
                duration = round(distance * benchmark["pace_seconds_per_km"] / 1000)
                run_seconds = None
                title = {
                    "long": "Long easy run",
                    "quality": "Controlled effort intervals",
                    "easy": "Easy endurance run" if len(days) == 1 else "Easy run",
                }[session_type]
                effort = (
                    "Four controlled, comfortably challenging repeats with easy walk/jog recoveries; avoid maximal effort. No pace target is established."
                    if session_type == "quality"
                    else "Conversational easy effort. Slow down or walk as needed; no pace target is established."
                )
        else:
            day += timedelta(days=1)
            continue
        at = datetime(day.year, day.month, day.day, 7, tzinfo=tz)
        composition.update(
            activityType="running",
            displayName=title,
            location="outdoor",
            scheduledDate=at.isoformat(),
            trainingPurpose="quality" if session_type == "quality" else "easy" if session_type != "event" else "event",
        )
        forecast.append(
            {
                "id": str(uuid.uuid5(namespace, "forecast:" + day.isoformat())),
                "date": day.isoformat(),
                "scheduledDate": at.isoformat(),
                "title": title,
                "status": "forecast",
                "composition": composition,
                "policy": PROGRAM_POLICY["version"],
                "estimated": True,
                "phase": phase,
                "session_type": session_type,
                "effort": effort,
                "constraints": list(limits),
                "dose": {
                    "distance_meters": distance,
                    "estimated_duration_seconds": duration,
                    "running_seconds": run_seconds,
                    "distance_estimated": False,
                    "duration_estimated": not beginner and session_type != "event",
                    "event_excluded_from_training_load": session_type == "event",
                },
                "benchmark_evidence": {
                    "source": benchmark["source"],
                    "observed_at": benchmark["observed_at"],
                    "confidence": benchmark.get("confidence"),
                },
            }
        )
        day += timedelta(days=1)
    summaries = {}
    for session in forecast:
        local_day = date.fromisoformat(session["date"])
        monday = (local_day - timedelta(days=local_day.weekday())).isoformat()
        summary = summaries.setdefault(
            monday,
            {
                "week_start": monday,
                "planned_distance_meters": 0,
                "estimated_duration_seconds": 0,
                "session_count": 0,
                "quality_session_count": 0,
                "hard_session_count": 0,
                "hard_session_evidence": "prescribed_interval_structure_only",
                "duration_estimated": False,
                "longest_run_meters": 0,
                "long_run_share": None,
                "distance_unknown": False,
                "event_excluded": True,
                "partial_week": local_day - timedelta(days=local_day.weekday()) < start
                or local_day + timedelta(days=6 - local_day.weekday()) > end,
            },
        )
        if session["session_type"] != "event":
            distance = session["dose"]["distance_meters"]
            summary["distance_unknown"] |= distance is None
            summary["planned_distance_meters"] += distance or 0
            summary["estimated_duration_seconds"] += session["dose"]["estimated_duration_seconds"] or 0
            summary["session_count"] += 1
            summary["quality_session_count"] += session["session_type"] == "quality"
            summary["hard_session_count"] += session["session_type"] == "quality" or any(
                b["iterations"] > 1 and any(s["purpose"] == "rest" for s in b["steps"])
                for b in session["composition"]["blocks"]
            )
            summary["duration_estimated"] |= session["dose"]["duration_estimated"]
            summary["longest_run_meters"] = max(summary["longest_run_meters"], distance or 0)
        session["week_summary"] = summary
    for summary in summaries.values():
        if summary["distance_unknown"]:
            summary["planned_distance_meters"] = None
            summary["longest_run_meters"] = None
        elif summary["planned_distance_meters"]:
            summary["long_run_share"] = summary["longest_run_meters"] / summary["planned_distance_meters"]
    if kind in PROGRAM_POLICY["projected_preparation_screen_meters"]:
        min_peak_weekly, min_peak_long = PROGRAM_POLICY["projected_preparation_screen_meters"][kind]
        peak_weekly = max((s["planned_distance_meters"] or 0 for s in summaries.values()), default=0)
        peak_long = max((s["longest_run_meters"] or 0 for s in summaries.values()), default=0)
        if peak_weekly < min_peak_weekly or peak_long < min_peak_long:
            _event_constraint(
                f"Projected preparation reaches {peak_weekly:g} m/week and {peak_long:g} m longest run; this provisional {kind} screen requires {min_peak_weekly} and {min_peak_long} m. Review goal feasibility instead of increasing load or moving the event automatically"
            )
    return forecast, end


def _context_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be an ISO calendar date")


def _evidence_problem(evidence, as_of, label):
    if not isinstance(evidence, dict):
        return f"{label} evidence is missing."
    source, confidence = evidence.get("source"), evidence.get("confidence")
    if not isinstance(source, str) or not source.strip() or len(source) > 200:
        return f"{label} requires an explicit evidence source."
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not REMAINING_PROGRAM_POLICY["minimum_confidence"] <= confidence <= 1
    ):
        return f"{label} confidence is missing or insufficient under this provisional policy."
    try:
        observed = datetime.fromisoformat(evidence.get("observed_at"))
    except (ValueError, TypeError):
        return f"{label} requires a dated evidence observation."
    if observed.tzinfo is None:
        return f"{label} observation requires a timezone."
    age = as_of - observed
    if age < timedelta(0) or age > timedelta(days=REMAINING_PROGRAM_POLICY["evidence_max_age_days"]):
        return f"{label} evidence is future dated or older than the provisional seven-day limit."
    return None


def _training_weeks(sessions):
    """Group actual dated doses, not cached summaries or relative week indices."""
    weeks = {}
    for session in sessions:
        if session.get("session_type") != "event":
            day = date.fromisoformat(session["date"])
            weeks.setdefault(day - timedelta(days=day.weekday()), []).append(session)
    return weeks


def generate_remaining_program(plan_id, profile, previous_forecast, as_of, context):
    """Build a proposal from caller-verified facts, without persistence or publication.

    as_of is an aware datetime. Context coverage is a verified inventory statement,
    not an inference from absent rows. Blocking results have forecast=None; retained
    historical prescriptions are informational and must not be published as a rebuild.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise HTTPException(422, "as_of must be a timezone-aware datetime")
    if not isinstance(profile, dict) or not isinstance(context, dict):
        raise HTTPException(422, "Profile and verified context must be objects")
    try:
        uuid.UUID(str(plan_id))
        tz = ZoneInfo(profile.get("timezone"))
    except (TypeError, ValueError, AttributeError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Plan UUID and athlete timezone are required")
    today = as_of.astimezone(tz).date()
    freeze_until = as_of.astimezone(UTC) + timedelta(hours=REMAINING_PROGRAM_POLICY["new_prescription_freeze_hours"])
    goal = profile.get("goal")
    from app.coaching_forecast import valid_program_goal

    if not valid_program_goal(goal):
        raise HTTPException(422, "Supported goal required")
    if not isinstance(previous_forecast, list) or not previous_forecast:
        raise HTTPException(422, "Previous immutable full forecast required")
    from app.coaching_forecast import require_forecast

    previous = deepcopy(require_forecast(previous_forecast, calendar_dates=True, timezone=profile["timezone"]))
    dates, ids, near_term_ids = {}, set(), set()
    for session in previous:
        if not isinstance(session, dict) or not isinstance(session.get("id"), str):
            raise HTTPException(422, "Previous forecast session identity required")
        day = _context_date(session.get("date"), "forecast.date")
        if (
            session["id"] in ids
            or day in dates
            or not isinstance(session.get("composition"), dict)
            or ("dose" in session and not isinstance(session["dose"], dict))
        ):
            raise HTTPException(422, "Previous forecast requires unique dated sessions and compositions")
        try:
            scheduled = datetime.fromisoformat(session.get("scheduledDate"))
        except (TypeError, ValueError):
            raise HTTPException(422, "Previous forecast requires an immutable scheduled timestamp")
        if scheduled.tzinfo is None:
            raise HTTPException(422, "Previous scheduled timestamp must include a timezone")
        if scheduled <= freeze_until:
            near_term_ids.add(session["id"])
        ids.add(session["id"])
        dates[day] = session
    events = [d for d, s in dates.items() if s.get("session_type") == "event"]
    fixed_event = _context_date(goal["race_date"], "goal.race_date") if goal.get("race_date") else None
    if (fixed_event and events != [fixed_event]) or (not fixed_event and events):
        raise HTTPException(422, "Goal event must match the immutable previous forecast exactly")
    end = fixed_event or max(dates)
    protection = context.get("protected_session_ids")
    completed = context.get("completed_session_ids", [])
    retired = context.get("retired_session_ids", [])
    if (
        not isinstance(protection, list)
        or not isinstance(completed, list)
        or not isinstance(retired, list)
        or any(not isinstance(i, str) or i not in ids for i in protection + completed + retired)
    ):
        raise HTTPException(422, "Explicit protected session identities must belong to the previous forecast")
    non_outstanding_ids = set(completed + retired)
    frozen_ids = (
        set(protection + completed + retired) | near_term_ids | {s["id"] for d, s in dates.items() if d <= today}
    )
    limits = [
        "Provisional remaining-program policy; no clinical, readiness or finish-time guarantee.",
        "Verified recent load and coverage are supplied by the caller; missing records do not prove an interruption.",
        "Missed workload is not repaid. Past, started, completed and caller-protected prescriptions remain unchanged.",
        "Full forecast is a proposal only; approval and bounded near-term publication are separate.",
        "No new prescription within 24 hours of as_of; omitted work is never redistributed.",
        "Existing immutable prescriptions through the exact 24-hour boundary are automatically protected, independent of caller hints.",
        "Seven-day evidence freshness, confidence >=0.5, fourteen supported days and conservative rebuild bounds are engineering screens, not validated physiology.",
    ]
    result = {
        "status": "review_required",
        "forecast": None,
        "preserved_forecast": previous,
        "end_date": end.isoformat(),
        "replacement_session_ids": [],
        "limitations": limits,
        "options": [],
        "policy_version": REMAINING_PROGRAM_POLICY["version"],
        "evidence": deepcopy({k: context.get(k) for k in ("interruption", "coverage", "load")}),
    }

    def blocked(reason, infeasible=False):
        result["status"] = "goal_infeasible" if infeasible else "review_required"
        result["limitations"].append(reason)
        result["options"] = (
            [
                "Review participation or a less demanding event outcome with the athlete.",
                "Changing the fixed event date requires a separate explicit athlete decision.",
                "Do not compress missed training into the remaining weeks.",
            ]
            if infeasible
            else [
                "Obtain current sourced ability/load and verified coverage before proposing a rebuild.",
                "Review the existing future prescriptions with the athlete; no replacement is authorized by this result.",
            ]
        )
        return result

    for label in ("interruption", "coverage", "load"):
        problem = _evidence_problem(context.get(label), as_of, label)
        if problem:
            return blocked(problem)
    interruption, coverage, load = (context[k] for k in ("interruption", "coverage", "load"))
    if interruption.get("verified") is not True or coverage.get("status") != "complete":
        return blocked("Interruption or complete running-data coverage has not been verified.")
    interrupted_start = _context_date(interruption.get("start_date"), "interruption.start_date")
    interrupted_end = _context_date(interruption.get("end_date"), "interruption.end_date")
    coverage_start = _context_date(coverage.get("start_date"), "coverage.start_date")
    coverage_end = _context_date(coverage.get("end_date"), "coverage.end_date")
    load_start = _context_date(load.get("window_start"), "load.window_start")
    load_end = _context_date(load.get("window_end"), "load.window_end")
    if not interrupted_start <= interrupted_end < today:
        raise HTTPException(422, "Interruption dates must be ordered and entirely before as_of")
    if not coverage_start <= coverage_end < today or not load_start <= load_end < today:
        raise HTTPException(422, "Coverage/load windows must be ordered and entirely before as_of")
    for evidence, window_end, label in (
        (interruption, interrupted_end, "interruption"),
        (coverage, coverage_end, "coverage"),
        (load, load_end, "load"),
    ):
        if datetime.fromisoformat(evidence["observed_at"]).astimezone(tz).date() < window_end:
            raise HTTPException(422, f"{label} observation cannot predate its reported window")
    if (
        load_start <= interrupted_end
        or (load_end - load_start).days + 1 < REMAINING_PROGRAM_POLICY["minimum_supported_load_days"]
        or coverage_start > load_start
        or coverage_end < load_end
        or coverage_end < today - timedelta(days=1)
        or load_end < today - timedelta(days=1)
    ):
        return blocked("A complete recent fourteen-day running baseline after the interruption is not supported.")
    start = today + timedelta(days=1)
    kind = goal["type"]
    if (end - start).days + 1 < REMAINING_PROGRAM_POLICY["minimum_rebuild_weeks"][kind] * 7:
        return blocked(
            "The unchanged event/end leaves insufficient recovery and rebuild time under this provisional policy.", True
        )
    if load.get("level") == "beginner":
        return blocked(
            "Aggregate beginner load cannot establish the current completed walk/run exposure; obtain a reviewed progression baseline."
        )
    # Both recent supported ability and the athlete's already planned ceiling bound
    # future work. New availability never redistributes missed/protected mileage.
    benchmark = deepcopy(load)
    for key in ("weekly_distance_meters", "long_run_meters"):
        if not finite_positive(benchmark.get(key)):
            return blocked("Current established running load is unknown or zero; no fitness is inferred.")
    previous_weeks = _training_weeks(previous)
    planned_long = [s.get("dose", {}).get("distance_meters") for week in previous_weeks.values() for s in week]
    if not planned_long or any(not finite_positive(v) for v in planned_long):
        return blocked("The immutable forecast lacks a supported distance ceiling for a remaining rebuild.")
    weekly_ceiling = max(sum(s["dose"]["distance_meters"] for s in week) for week in previous_weeks.values())
    session_ceiling = max(planned_long)
    benchmark["weekly_distance_meters"] = min(benchmark["weekly_distance_meters"], weekly_ceiling)
    benchmark["long_run_meters"] = min(benchmark["long_run_meters"], session_ceiling)
    try:
        forecast, _ = _forecast(
            plan_id,
            goal,
            profile.get("available_days"),
            benchmark,
            start,
            profile["timezone"],
            {"as_of": as_of, "end": end},
        )
    except HTTPException as error:
        if isinstance(error.detail, dict) and error.detail.get("status") == "goal_constraints":
            return blocked(error.detail.get("reason", "Remaining goal requires feasibility review."), True)
        if isinstance(error.detail, dict) and error.detail.get("status") == "onboarding_required":
            return blocked("Current ability facts are incomplete: " + ", ".join(error.detail["required_fields"]))
        raise
    frozen = [s for s in previous if s["id"] in frozen_ids]
    outstanding_frozen = [s for s in frozen if s["id"] not in non_outstanding_ids]
    frozen_weeks = _training_weeks(outstanding_frozen)
    generated_weeks = _training_weeks(forecast)
    quality_counts = {
        monday: sum(s["session_type"] == "quality" for s in week) for monday, week in frozen_weeks.items()
    }
    if any(count > PROGRAM_POLICY["maximum_quality_sessions_per_week"] for count in quality_counts.values()):
        return blocked("Protected prescriptions already exceed the weekly quality limit; they require separate review.")
    proposed = [
        s
        for s in forecast
        if s["id"] not in frozen_ids
        and date.fromisoformat(s["date"]) not in {date.fromisoformat(f["date"]) for f in frozen}
        and datetime.fromisoformat(s["scheduledDate"]) > freeze_until
    ]
    for s in proposed:
        s["policy"] = REMAINING_PROGRAM_POLICY["version"]
        s["constraints"].extend(limits)
        # Protected nearby hard sessions never gain a stacked new quality/long run.
        day = date.fromisoformat(s["date"])
        monday = day - timedelta(days=day.weekday())
        quality_limit = (
            s["session_type"] == "quality"
            and quality_counts.get(monday, 0) >= PROGRAM_POLICY["maximum_quality_sessions_per_week"]
        )
        nearby_hard = s["session_type"] in ("quality", "long") and any(
            f.get("session_type") in ("quality", "long")
            and abs((date.fromisoformat(s["date"]) - date.fromisoformat(f["date"])).days)
            < PROGRAM_POLICY["minimum_demanding_session_gap_days"]
            for f in outstanding_frozen
        )
        if quality_limit or nearby_hard:
            s["session_type"], s["title"] = "easy", "Easy recovery run"
            s["composition"] = {
                **s["composition"],
                **_distance_composition(s["dose"]["distance_meters"], "easy"),
                "displayName": s["title"],
                "trainingPurpose": "easy",
            }
            s["effort"] = (
                "Conversational easy effort; protected work limits additional quality or nearby demanding work. No pace target."
            )
        if s["session_type"] == "quality":
            quality_counts[monday] = quality_counts.get(monday, 0) + 1
    # A protected prescription consumes its week's planned budget; it is never
    # silently reduced. If it uses the budget, new training is omitted, not deferred.
    weeks = _training_weeks(proposed)
    omitted = set()
    for monday, sessions in weeks.items():
        # Include generated protected slots before subtracting immutable work.
        # New omitted near-term work is removed from this budget, never reassigned.
        generated = generated_weeks[monday]
        near_term_omitted = sum(
            s["dose"]["distance_meters"]
            for s in generated
            if s["id"] not in frozen_ids and datetime.fromisoformat(s["scheduledDate"]) <= freeze_until
        )
        budget = min(weekly_ceiling, sum(s["dose"]["distance_meters"] for s in generated) - near_term_omitted)
        protected = frozen_weeks.get(monday, [])
        if any(not finite_positive(f.get("dose", {}).get("distance_meters")) for f in protected):
            return blocked("A protected prescription's load is unknown; its contribution cannot be treated as zero.")
        protected_load = sum(f["dose"]["distance_meters"] for f in protected)
        if protected_load > budget:
            result["limitations"].append(
                f"Protected prescriptions in week {monday.isoformat()} already exceed the reduced budget; new training is omitted. Their separate review remains necessary."
            )
        proposed_load = sum(s["dose"]["distance_meters"] for s in sessions)
        fraction = min(1, max(0, budget - protected_load) / proposed_load) if proposed_load else 0
        for s in sessions:
            distance = math.floor(min(session_ceiling, s["dose"]["distance_meters"] * fraction) / 10) * 10
            if distance < 100:
                omitted.add(s["id"])
                continue
            s["dose"]["distance_meters"] = distance
            s["dose"]["estimated_duration_seconds"] = round(distance * benchmark["pace_seconds_per_km"] / 1000)
            s["composition"] = {**s["composition"], **_distance_composition(distance, s["session_type"])}
        retained = [s for s in sessions if s["id"] not in omitted]
        summary = {
            **sessions[0]["week_summary"],
            "planned_distance_meters": protected_load + sum(s["dose"]["distance_meters"] for s in retained),
            "protected_distance_meters": protected_load,
            "session_count": len(retained) + len(protected),
            "quality_session_count": sum(s["session_type"] == "quality" for s in retained + protected),
            "hard_session_count": sum(s["session_type"] == "quality" for s in retained + protected),
            "estimated_duration_seconds": sum(
                s["dose"].get("estimated_duration_seconds") or 0 for s in retained + protected
            ),
            "protected_duration_unknown": any(s["dose"].get("estimated_duration_seconds") is None for s in protected),
            "longest_run_meters": max([s["dose"]["distance_meters"] for s in retained + protected] or [0]),
        }
        summary["long_run_share"] = (
            summary["longest_run_meters"] / summary["planned_distance_meters"]
            if summary["planned_distance_meters"]
            else None
        )
        for s in retained:
            s["week_summary"] = summary
    proposed = [s for s in proposed if s["id"] not in omitted]
    merged = sorted(frozen + proposed, key=lambda s: s["date"])
    # Earlier preparation is not evidence that a post-interruption proposal meets
    # its remaining screen. Only final future doses can establish these peaks.
    remaining_weeks = _training_weeks(
        [s for s in merged if date.fromisoformat(s["date"]) > today and s["id"] not in non_outstanding_ids]
    )
    if kind in PROGRAM_POLICY["projected_preparation_screen_meters"]:
        minimum_weekly, minimum_long = PROGRAM_POLICY["projected_preparation_screen_meters"][kind]
        peak_weekly = max(
            (sum(s["dose"]["distance_meters"] for s in week) for week in remaining_weeks.values()), default=0
        )
        peak_long = max((s["dose"]["distance_meters"] for week in remaining_weeks.values() for s in week), default=0)
        if peak_weekly < minimum_weekly or peak_long < minimum_long:
            return blocked(
                f"Final merged remaining preparation reaches {peak_weekly:g} m/week and {peak_long:g} m longest run; the provisional {kind} screen requires {minimum_weekly} and {minimum_long} m. Protected work, ceilings and omissions cannot be compensated with catch-up training.",
                True,
            )
    result.update(
        status="forecast_ready",
        forecast=merged,
        replacement_session_ids=sorted(({s["id"] for s in previous} | {s["id"] for s in proposed}) - frozen_ids),
        options=["Review the full remaining forecast, then approve only appropriate future prescriptions."],
    )
    return result
