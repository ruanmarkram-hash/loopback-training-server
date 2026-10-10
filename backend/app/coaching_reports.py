"""Allowlisted athlete-supplied context, never physiological readiness or clearance.

Callers supply only the authenticated athlete's rows. Activity payloads, notes,
medical fields and declared provenance remain private; only this report subset
crosses the worker boundary. Free text remains untrusted data.
"""
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict
from app.coaching_policy import POLICY
from app.coaching_feedback import REASONS, eligible_skips, feedback_identity


class FatigueReport(TypedDict):
    fatigue: str
    reportedAt: str
    activityDates: list[str]
    associatedRecordCount: int
    omittedActivityDateCount: int
    independence: Literal["unknown"]
    source: Literal["athlete_workout_report"]
    untrusted: Literal[True]


class MissedWorkoutReport(TypedDict):
    reason: Literal["busy", "tired", "weather", "soreness", "motivation", "other"]
    action: Literal["move", "adjust", "skip"]
    scheduledDate: str
    source: Literal["athlete_missed_workout_feedback"]
    untrusted: Literal[True]


LIMIT = 25
ACTIVITY_DATE_LIMIT = 5
FATIGUE_TEXT_LIMIT = 500
ACTIONS = frozenset(("move", "adjust", "skip"))


def aware_date(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def athlete_reports(workouts, feedback, now=None, *, queues=()):
    now = now or datetime.now(UTC)
    start = now - timedelta(days=POLICY["lookback_days"])
    statements = {}
    missed: list[MissedWorkoutReport] = []
    omitted_fatigue = omitted_feedback = 0
    eligible = {id(row) for row in eligible_skips(feedback, queues, now, POLICY["lookback_days"])}
    projected_skips = set()
    for workout in workouts:
        if workout.activity_type != "running":
            continue
        data = workout.data if isinstance(workout.data, dict) else {}
        raw = data.get("athleteReportedContext")
        if not isinstance(raw, dict) or "fatigue" not in raw:
            continue
        text = raw["fatigue"]
        reported = aware_date(raw.get("reported_at"))
        activity = aware_date(workout.start_date)
        if (not isinstance(text, str) or not text.strip() or len(text) > FATIGUE_TEXT_LIMIT
                or reported is None or activity is None
                or not start <= reported <= now or not start <= activity <= now):
            omitted_fatigue += 1
            continue
        # One declared statement may be attached to many workouts. Associations
        # are not independent reports or corroborating physiological evidence.
        key = (text, reported.isoformat())
        group = statements.setdefault(key, dict(activityDates=set(), recordCount=0))
        group["activityDates"].add(activity.isoformat())
        group["recordCount"] += 1
    for row in feedback:
        scheduled = aware_date(row.scheduled_date)
        if row.dismissed:
            continue
        if (not isinstance(row.reason, str) or row.reason not in REASONS
                or not isinstance(row.action, str) or row.action not in ACTIONS
                or scheduled is None or not start <= scheduled <= now):
            omitted_feedback += 1
            continue
        if row.action == "skip":
            key = feedback_identity(row)
            if id(row) not in eligible or key in projected_skips:
                omitted_feedback += 1
                continue
            projected_skips.add(key)
        missed.append(dict(reason=row.reason, action=row.action, scheduledDate=scheduled.isoformat(),
                           source="athlete_missed_workout_feedback", untrusted=True))
    fatigue: list[FatigueReport] = []
    for (text, reported), group in statements.items():
        dates = sorted(group["activityDates"], reverse=True)
        fatigue.append(dict(fatigue=text, reportedAt=reported,
                            activityDates=dates[:ACTIVITY_DATE_LIMIT],
                            associatedRecordCount=group["recordCount"],
                            omittedActivityDateCount=max(0, len(dates)-ACTIVITY_DATE_LIMIT),
                            independence="unknown", source="athlete_workout_report", untrusted=True))
    fatigue.sort(key=lambda x: (x["reportedAt"], x["fatigue"]), reverse=True)
    missed.sort(key=lambda x: (x["scheduledDate"], x["reason"], x["action"]), reverse=True)
    return dict(
        fatigueReports=fatigue[:LIMIT], missedWorkoutFeedback=missed[:LIMIT],
        omittedFatigueRecordCount=omitted_fatigue + sum(x["associatedRecordCount"] for x in fatigue[LIMIT:]),
        omittedFatigueStatementCount=max(0, len(fatigue)-LIMIT),
        deduplicatedFatigueRecordCount=sum(x["associatedRecordCount"] for x in fatigue)-len(fatigue),
        omittedFeedbackCount=omitted_feedback + max(0, len(missed)-LIMIT),
        coverage="unknown", readiness="unknown",
    )
