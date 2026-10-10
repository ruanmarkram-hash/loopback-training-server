"""Calendar provenance independent of the server process timezone."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.coaching import AthleteProfile
from app.models.plan import Plan

DEFAULT_TIMEZONE = "Australia/Brisbane"


def calendar_zone(db, user_id, plan=None):
    """Explicit plan zone, then strength schedule, athlete profile, product default.

    A malformed explicit value is unknown, never replaced by another zone.
    """
    metadata = (plan.metadata_ or {}) if plan is not None else {}
    if not isinstance(metadata, dict):
        return None
    schedule = metadata.get("schedule")
    if "timezone" in metadata:
        value = metadata["timezone"]
    elif isinstance(schedule, dict) and "timezone" in schedule:
        value = schedule["timezone"]
    else:
        profile = db.get(AthleteProfile, user_id) if user_id is not None else None
        data = profile.data if profile is not None else {}
        if not isinstance(data, dict):
            return None
        value = data.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(value, str) or not value:
        return None
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def calendar_date(at, zone):
    if zone is None or not isinstance(at, datetime) or at.tzinfo is None:
        return None
    try:
        return at.astimezone(zone).date()
    except (ValueError, OverflowError):
        return None


def utc_day_bounds(first, last, zone):
    if zone is None:
        return None
    try:
        return (
            datetime.combine(first, time.min, zone).astimezone(UTC),
            datetime.combine(last, time.max, zone).astimezone(UTC),
        )
    except (ValueError, OverflowError):
        return None


def widened_utc_bounds(first, last):
    """Cover every zone's candidate timestamps; callers filter by local day."""
    lo = datetime.combine(first, time.min, UTC)
    hi = datetime.combine(last, time.max, UTC)
    return (
        lo - timedelta(days=1) if first > date.min else lo,
        hi + timedelta(days=1) if last < date.max else hi,
    )


def owned_item_zone(db, item):
    """Never inherit a foreign plan calendar from a legacy queue reference."""
    plan = db.get(Plan, item.plan_id) if item.plan_id is not None else None
    if plan is not None and plan.user_id != item.user_id:
        return None
    if item.plan_id is not None and plan is None:
        return None
    return calendar_zone(db, item.user_id, plan)
