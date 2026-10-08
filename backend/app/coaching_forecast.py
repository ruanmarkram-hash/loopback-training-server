"""Validate persisted forecast facts before any parser or publication consumer."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

SESSION_TYPES = ("easy", "long", "quality", "walk", "walk_run", "event")


GOAL_TYPES = ("first_5k", "5k", "10k", "half_marathon", "marathon", "general_fitness")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def valid_program_goal(goal):
    if not isinstance(goal, dict) or not isinstance(goal.get("type"), str) or goal["type"] not in GOAL_TYPES:
        return False
    if goal.get("race_date") is not None:
        try:
            if date.fromisoformat(goal["race_date"]).isoformat() != goal["race_date"]:
                return False
        except (TypeError, ValueError):
            return False
    if goal.get("target_seconds") is not None:
        from app.composition_shape import finite_nonnegative

        if not finite_nonnegative(goal["target_seconds"]) or goal["target_seconds"] <= 0:
            return False
    return True


@dataclass(frozen=True)
class ProgramMetadata:
    facts: dict
    goals: list
    timezone: str


def validated_program_metadata(value, require_goal=False):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        return None
    facts, goals = value.get("programFacts", {}), value.get("goals", [])
    if not isinstance(facts, dict) or not isinstance(goals, list) or any(not valid_program_goal(g) for g in goals):
        return None
    if "available_days" in facts:
        days = facts["available_days"]
        if (
            not isinstance(days, list)
            or len(days) > 7
            or any(not isinstance(d, str) or d not in WEEKDAYS for d in days)
            or len(set(days)) != len(days)
        ):
            return None
    zone = value.get("timezone", "Australia/Brisbane")
    try:
        ZoneInfo(zone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    if require_goal and (
        not goals or (goals[0]["type"] not in ("first_5k", "general_fitness") and not goals[0].get("race_date"))
    ):
        return None
    return ProgramMetadata(facts=facts, goals=goals, timezone=zone)


def require_program_metadata(value, status_code=422, require_goal=False):
    metadata = validated_program_metadata(value, require_goal)
    if metadata is None:
        raise HTTPException(status_code, "Program facts or goals are malformed or unavailable; request a fresh review")
    return metadata


def validated_forecast(value, timezone="Australia/Brisbane"):
    try:
        zone = ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    if not isinstance(value, list) or not value:
        return None
    seen = {}
    for session in value:
        if not isinstance(session, dict) or not isinstance(session.get("id"), str):
            return None
        try:
            identifier = uuid.UUID(session["id"])
            at = datetime.fromisoformat(session.get("scheduledDate"))
            if at.tzinfo is None:
                return None
            utc = at.astimezone(UTC)
            # Leave conversion headroom for changed athlete zones as well as the program zone.
            utc + timedelta(days=1)
            utc - timedelta(days=1)
            local = at.astimezone(zone)
            if "date" in session and (
                date.fromisoformat(session["date"]).isoformat() != session["date"]
                or local.date().isoformat() != session["date"]
            ):
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            at.tzinfo is None
            or not isinstance(session.get("title"), str)
            or not session["title"]
            or not isinstance(session.get("composition"), dict)
            or not session["composition"]
            or ("dose" in session and not isinstance(session["dose"], dict))
            or ("session_type" in session and session["session_type"] not in SESSION_TYPES)
            or (identifier in seen and seen[identifier] != session)
        ):
            return None
        dose = session.get("dose", {})
        for key in ("distance_meters", "estimated_duration_seconds", "running_seconds"):
            if key not in dose:
                continue
            quantity = dose[key]
            if quantity is None:
                continue
            from app.composition_shape import finite_nonnegative

            if not finite_nonnegative(quantity):
                return None
        from app.coaching_service import validate_composition

        try:
            validate_composition(session["composition"])
        except HTTPException:
            return None
        seen[identifier] = session
    return value


def require_forecast(value, status_code=422, calendar_dates=False, timezone="Australia/Brisbane"):
    forecast = validated_forecast(value, timezone)
    if forecast is None or (
        calendar_dates
        and any("date" not in session or session.get("session_type") not in SESSION_TYPES for session in forecast)
    ):
        raise HTTPException(status_code, "Complete forecast facts are malformed or unavailable; request a fresh review")
    return forecast
