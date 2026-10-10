"""Durable catch-up review triggers. Reads alone never enqueue jobs."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select

from app.coaching_forecast import validated_forecast, validated_program_metadata
from app.coaching_policy import POLICY
from app.coaching_service import checksum, enqueue, lock_athlete
from app.models.coaching import AthleteProfile, PrescriptionRevision
from app.models.feedback import WorkoutFeedback
from app.models.inventory import WorkoutInventory
from app.models.plan import Plan
from app.models.queue import WorkoutQueue
from app.models.user import User
from app.models.workout import Workout


def publication_review(db, user_id, plan, now=None):
    """Read-only decision; never rewrite an approved forecast or issued history."""
    now = now or datetime.now(UTC)
    projection = validated_program_metadata(plan.metadata_)
    sessions = validated_forecast((plan.metadata_ or {}).get("forecast"), projection.timezone) if projection else None
    if sessions is None:
        return {
            "status": "review_required",
            "reason": "Complete forecast facts are malformed or unavailable; request a fresh review before publication.",
            "calendarReconciled": False,
            "forecastComplete": False,
            "unrepresentedCommittedWorkoutIds": [],
            "restrictions": [],
            "availableDays": [],
        }
    profile = db.get(AthleteProfile, user_id)
    if profile is not None and inspect(profile).persistent:
        state = inspect(profile)
        unchanged = [
            column.key for column in state.mapper.column_attrs if not state.attrs[column.key].history.has_changes()
        ]
        if unchanged:
            with db.no_autoflush:
                db.refresh(profile, attribute_names=unchanged)
    data = profile.data if profile else {}
    metadata = plan.metadata_ or {}
    days = data.get("available_days", projection.facts.get("available_days"))
    reason = None
    if data.get("restrictions"):
        reason = "Current explicit restrictions require review before additional prescriptions."
    elif not days:
        reason = "Current availability is unknown; review is required before additional prescriptions."
    else:
        zone = ZoneInfo(data.get("timezone", metadata.get("timezone", "Australia/Brisbane")))
        weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        for session in sessions:
            at = datetime.fromisoformat(session["scheduledDate"])
            if at <= now or session.get("session_type") == "event":
                # The explicitly chosen fixed event date is separate from training-day availability.
                continue
            import uuid

            qid = uuid.UUID(session["id"])
            if db.get(WorkoutQueue, qid) or db.scalar(
                select(PrescriptionRevision.id).where(PrescriptionRevision.workout_id == qid)
            ):
                continue
            if weekdays[at.astimezone(zone).weekday()] not in days:
                reason = "Current availability no longer supports the uncommitted forecast."
                break
    forecast = {session["id"]: session for session in sessions}
    unrepresented = []
    for item in db.scalars(
        select(WorkoutQueue)
        .where(WorkoutQueue.user_id == user_id, WorkoutQueue.activity_type == "running")
        .order_by(WorkoutQueue.id)
    ):
        if item.status in {"completed", "skipped"} or (item.scheduled_date is not None and item.scheduled_date <= now):
            continue
        session = forecast.get(str(item.id))
        if (
            session is None
            or item.plan_id != plan.id
            or item.scheduled_date is None
            or item.scheduled_date != datetime.fromisoformat(session["scheduledDate"])
            or item.title != session["title"]
            or checksum(item.workout_data) != checksum(session["composition"])
        ):
            unrepresented.append(str(item.id))
    if unrepresented:
        reason = "Current owned running commitments are not reconciled with the approved forecast; review the complete calendar before additional prescriptions."
    return {
        "unrepresentedCommittedWorkoutIds": unrepresented,
        "calendarReconciled": not unrepresented,
        "status": "review_required" if reason else "compatible",
        "reason": reason,
        "restrictions": data.get("restrictions", []),
        "availableDays": days or [],
    }


def publish_forecast(db, user_id, plan, now=None):
    now = now or datetime.now(UTC)
    # Also protect direct approval/publication callers, not just the scanner.
    with db.no_autoflush:
        lock_athlete(db, user_id)
        state = inspect(plan)
        if state.persistent:
            unchanged = [
                column.key for column in state.mapper.column_attrs if not state.attrs[column.key].history.has_changes()
            ]
            if unchanged:
                db.refresh(plan, attribute_names=unchanged)
    if plan.user_id != user_id or plan.status != "active":
        return
    if not (plan.metadata_ or {}).get("forecastApproved"):
        return
    if publication_review(db, user_id, plan, now)["status"] != "compatible":
        return
    sessions = validated_forecast(
        (plan.metadata_ or {}).get("forecast"), (plan.metadata_ or {}).get("timezone", "Australia/Brisbane")
    )
    if sessions is None:
        return
    for f in sessions:
        at = datetime.fromisoformat(f["scheduledDate"])
        if not now + timedelta(hours=POLICY["freeze_hours"]) < at <= now + timedelta(days=14):
            continue
        import uuid

        qid = uuid.UUID(f["id"])
        if db.get(WorkoutQueue, qid) or db.scalar(
            select(PrescriptionRevision.id).where(PrescriptionRevision.workout_id == qid)
        ):
            continue
        db.add(
            WorkoutQueue(
                id=qid,
                user_id=user_id,
                plan_id=plan.id,
                activity_type="running",
                title=f["title"],
                workout_data=f["composition"],
                scheduled_date=at,
                status="pending",
            )
        )


def scan_reviews(db, now=None):
    now = now or datetime.now(UTC)
    owners = db.scalars(select(User).where(User.is_active.is_(True), User.role == "user").order_by(User.id)).all()
    for user in owners:
        # Serialize before active-plan and forecast existence reads.
        lock_athlete(db, user.id)
        db.refresh(user)
        if not user.is_active:
            continue
        if not db.scalar(
            select(Plan.id).where(Plan.user_id == user.id, Plan.status == "active", Plan.activity_type == "running")
        ):
            continue
        if "training" not in user.data_consent:
            continue
        from app.activity_observation_service import scan_staleness

        scan_staleness(db, user, now)
        for plan in db.scalars(select(Plan).where(Plan.user_id == user.id, Plan.status == "active")):
            publish_forecast(db, user.id, plan, now)
        lock_athlete(db, user.id)
        profile = db.get(AthleteProfile, user.id)
        zone = (profile.data if profile else {}).get("timezone", "Australia/Brisbane")
        try:
            local = now.astimezone(ZoneInfo(zone))
        except (ValueError, KeyError):
            local = now.astimezone(ZoneInfo("Australia/Brisbane"))
        # One weekly logical event, catches up once after restart instead of replaying timers.
        year, week, _ = local.date().isocalendar()
        enqueue(
            db,
            user.id,
            f"weekly:{year}:{week}",
            dict(trigger="weekly_load", policyVersion=POLICY["version"], sourceEventIds=[]),
        )
        skips = db.scalars(
            select(WorkoutFeedback).where(
                WorkoutFeedback.user_id == user.id,
                WorkoutFeedback.action == "skip",
                WorkoutFeedback.dismissed.is_(False),
                WorkoutFeedback.scheduled_date >= now - timedelta(days=POLICY["lookback_days"]),
            )
        ).all()
        if len(skips) >= POLICY["missed_session_trigger"]:
            ids = sorted(str(x.id) for x in skips)
            enqueue(
                db,
                user.id,
                "explicit_skips:" + checksum(ids),
                dict(trigger="explicit_skips", sourceEventIds=ids, policyVersion=POLICY["version"]),
            )
        last = db.scalar(
            select(func.max(Workout.start_date)).where(
                Workout.user_id == user.id, Workout.activity_type == "running", Workout.start_date <= now
            )
        )
        if last and last <= now - timedelta(days=POLICY["elapsed_gap_days"]):
            enqueue(
                db,
                user.id,
                "elapsed_gap:" + last.isoformat(),
                dict(
                    trigger="elapsed_gap",
                    lastRecordedRunAt=last.isoformat(),
                    coverage="unknown",
                    policyVersion=POLICY["version"],
                ),
            )
        latest = db.scalar(select(func.max(WorkoutInventory.synced_at)).where(WorkoutInventory.user_id == user.id))
        if latest is None or latest < now - timedelta(hours=48):
            continue
        overdue = db.scalars(
            select(WorkoutQueue).where(
                WorkoutQueue.user_id == user.id,
                WorkoutQueue.activity_type == "running",
                WorkoutQueue.status.in_(["pending", "fetched", "synced"]),
                WorkoutQueue.scheduled_date < now - timedelta(hours=24),
                WorkoutQueue.scheduled_date >= now - timedelta(days=POLICY["lookback_days"]),
            )
        ).all()
        if len(overdue) >= POLICY["missed_session_trigger"]:
            ids = sorted(str(q.id) for q in overdue)
            enqueue(
                db,
                user.id,
                "adherence:" + checksum(ids),
                dict(
                    trigger="overdue_schedule_review",
                    coverage="unknown",
                    sourceEventIds=ids,
                    syncObservedAt=latest.isoformat(),
                    policyVersion=POLICY["version"],
                ),
            )
    db.commit()


async def run_review_scheduler(session_factory):
    while True:
        try:
            # Synchronous PostgreSQL lock waits must never occupy the event loop.
            def scan_once():
                with session_factory() as db:
                    scan_reviews(db)

            await asyncio.to_thread(scan_once)
        except Exception:
            logging.getLogger("uvicorn.error").exception("coaching review catch-up failed")
        await asyncio.sleep(60)
