"""Pure feedback eligibility; never infer capture coverage or physiological facts."""
from datetime import datetime, timedelta

REASONS = frozenset(("busy", "tired", "weather", "soreness", "motivation", "other"))


def feedback_identity(row):
    owner, workout = getattr(row, "user_id", None), getattr(row, "workout_id", None)
    return (owner, workout) if owner and workout else None


def eligible_skips(feedback, queues, now, lookback_days):
    start = now - timedelta(days=lookback_days)
    # Callers already query owned queues. Match explicit identity and current
    # producer-supported state; absent prescription provenance is ineligible.
    prescriptions = {(getattr(q, "user_id", None), q.id): q for q in queues}
    selected, seen = [], set()
    for row in feedback:
        key = feedback_identity(row)
        prescription = prescriptions.get(key)
        if (key is None or key in seen or row.action != "skip" or row.dismissed
                or not isinstance(getattr(row, "reason", None), str)
                or row.reason not in REASONS
                or not isinstance(row.scheduled_date, datetime)
                or row.scheduled_date.tzinfo is None or row.scheduled_date.utcoffset() is None
                or not start <= row.scheduled_date <= now
                or prescription is None
                or prescription.activity_type != "running"
                or prescription.status not in ("pending", "fetched", "synced", "skipped")
                or getattr(prescription, "completed_at", None) is not None
                or prescription.scheduled_date != row.scheduled_date
                or getattr(row, "new_date", None) is not None):
            continue
        seen.add(key)
        selected.append(row)
    return selected
