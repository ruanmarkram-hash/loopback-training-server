"""Approval previews for structural changes; immutable history stays append-only."""

import copy
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.coaching_forecast import require_forecast, require_program_metadata
from app.coaching_policy import POLICY
from app.coaching_service import checksum, prescription_snapshot, validate_composition
from app.models.action import WorkoutAction
from app.models.coaching import PlanRevision
from app.models.queue import WorkoutQueue


def future_eligible(item, now):
    return (
        item.status not in {"completed", "skipped"}
        and item.scheduled_date is not None
        and item.scheduled_date > now + timedelta(hours=POLICY["freeze_hours"])
    )


def prepare_forward_undo(db, user, plan, target_revision, now=None):
    now = now or datetime.now(UTC)
    if plan.activity_type != "running" or plan.status not in {"active", "upcoming"}:
        raise HTTPException(422, "Forward undo requires an active running plan")
    if target_revision >= plan.revision:
        raise HTTPException(422, "Undo target must be an earlier committed revision")
    target = db.scalar(
        select(PlanRevision).where(
            PlanRevision.user_id == user.id, PlanRevision.plan_id == plan.id, PlanRevision.revision == target_revision
        )
    )
    if target is None:
        raise HTTPException(404, "Committed target revision not found")
    history = {row["workout_id"]: row for row in target.snapshot.get("prescriptions", [])}
    changes, excluded = [], []
    for item in db.scalars(
        select(WorkoutQueue).where(WorkoutQueue.user_id == user.id, WorkoutQueue.plan_id == plan.id)
    ):
        previous = history.get(str(item.id))
        if previous is None:
            continue
        if not future_eligible(item, now):
            excluded.append(str(item.id))
            continue
        before = prescription_snapshot(item)
        if not all(key in previous for key in before):
            raise HTTPException(422, "Historical prescription is incomplete; no restoration inferred")
        after = {key: copy.deepcopy(previous[key]) for key in before}
        if after["workout_id"] != str(item.id) or after["plan_id"] != str(plan.id):
            raise HTTPException(422, "Historical workout does not belong to this plan")
        at = datetime.fromisoformat(after["scheduled_date"]) if after["scheduled_date"] else None
        if at is None or at.tzinfo is None or at <= now + timedelta(hours=POLICY["freeze_hours"]):
            excluded.append(str(item.id))
            continue
        if after["activity_type"] != "running":
            raise HTTPException(422, "Historical activity is not a running prescription")
        validate_composition(after["composition"])
        if before != after:
            changes.append(
                dict(
                    workoutId=str(item.id),
                    title=item.title,
                    basePrescriptionRevision=str(item.prescription_revision) if item.prescription_revision else None,
                    before=before,
                    after=after,
                )
            )
    if not changes:
        raise HTTPException(422, "No eligible future prescriptions differ from the target revision")
    return dict(
        type="future_undo",
        targetRevision=target_revision,
        changes=[],
        structuralChanges=sorted(changes, key=lambda x: x["workoutId"]),
        excludedWorkoutIds=sorted(excluded),
        reason="Explicit forward undo of eligible future prescriptions from committed history.",
        limitations=[
            "Past, completed, retired and near-start prescriptions remain unchanged.",
            "Missing/deleted workouts are not resurrected; history is retained.",
            "The event date and plan metadata are not rolled back. Approval creates a new forward revision.",
        ],
        policyVersion=POLICY["version"],
    )


def validate_forward_undo(db, user, plan, data):
    try:
        current = prepare_forward_undo(db, user, plan, data["targetRevision"])
    except HTTPException:
        raise HTTPException(409, "Undo target became unavailable or protected; preview again") from None
    if checksum(current["structuralChanges"]) != checksum(data["structuralChanges"]):
        raise HTTPException(409, "Future prescriptions changed or became protected; preview undo again")
    return current["structuralChanges"]


def apply_forward_undo(db, user, plan, changes):
    for change in changes:
        item = db.get(WorkoutQueue, uuid.UUID(change["workoutId"]))
        if item is None or item.user_id != user.id or item.plan_id != plan.id:
            raise HTTPException(409, "Restoration target is no longer owned by the plan")
        after = change["after"]
        item.title, item.description = after["title"], after["description"]
        item.activity_type = after["activity_type"]
        item.scheduled_date = datetime.fromisoformat(after["scheduled_date"])
        item.workout_data = copy.deepcopy(after["composition"])
        if item.status in {"fetched", "synced"}:
            db.add(
                WorkoutAction(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    workout_id=item.id,
                    action="edit",
                    composition=copy.deepcopy(item.workout_data),
                )
            )


def prepare_rebuild(db, user, plan, intent, now=None):
    """Recorded activity supplies load; athlete coverage attestation stays labelled."""
    import math
    from zoneinfo import ZoneInfo

    from app.coaching_program import generate_remaining_program
    from app.models.coaching import AthleteProfile, PrescriptionRevision
    from app.models.workout import Workout

    now = now or datetime.now(UTC)
    if plan.status != "active" or plan.activity_type != "running":
        raise HTTPException(422, "Rebuild requires an active running program")
    version = db.scalar(
        select(PlanRevision).where(
            PlanRevision.user_id == user.id, PlanRevision.plan_id == plan.id, PlanRevision.revision == plan.revision
        )
    )
    metadata = (version.snapshot if version else {}).get("metadata", {})
    projection = require_program_metadata(metadata, require_goal=True)
    previous = copy.deepcopy(
        require_forecast(
            metadata.get("forecast"), calendar_dates=True, timezone=metadata.get("timezone", "Australia/Brisbane")
        )
    )
    if not previous or not metadata.get("forecastApproved"):
        raise HTTPException(422, "An approved immutable full program is required")
    profile_row = db.get(AthleteProfile, user.id)
    facts = profile_row.data if profile_row else {}
    profile = dict(
        goal=projection.goals[0],
        available_days=facts.get("available_days", projection.facts.get("available_days", [])),
        timezone=projection.timezone,
    )
    zone = ZoneInfo(profile["timezone"])
    today = now.astimezone(zone).date()
    start, end = today - timedelta(days=14), today - timedelta(days=1)
    workouts = db.scalars(
        select(Workout).where(Workout.user_id == user.id, Workout.activity_type == "running", Workout.start_date <= now)
    ).all()
    rows = [w for w in workouts if start <= w.start_date.astimezone(zone).date() <= end]
    usable = [
        w
        for w in rows
        if isinstance(w.total_distance, (int, float))
        and math.isfinite(w.total_distance)
        and w.total_distance > 0
        and isinstance(w.duration, (int, float))
        and math.isfinite(w.duration)
        and w.duration > 0
    ]
    supported = len({w.start_date.astimezone(zone).date() for w in usable}) >= 3 and len(usable) == len(rows)
    observed = now.isoformat()
    coverage = intent["coverage"]
    interruption = intent["interruption"]
    context = dict(
        interruption={
            **interruption,
            "verified": interruption.get("confirmed") is True,
            "source": "explicit_athlete_interruption_attestation",
            "observed_at": observed,
            "confidence": 0.5,
        },
        coverage={
            **coverage,
            "status": "complete" if coverage.get("complete") is True else "unknown",
            "source": "explicit_athlete_coverage_attestation_not_device_verification",
            "observed_at": observed,
            "confidence": 0.5,
        },
        load=dict(
            source="owned_recorded_running_activities",
            observed_at=observed,
            confidence=0.5 if supported else 0,
            level="established",
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            weekly_distance_meters=sum(w.total_distance for w in usable) / 2,
            long_run_meters=max((w.total_distance for w in usable), default=0),
            pace_seconds_per_km=(sum(w.duration for w in usable) * 1000 / sum(w.total_distance for w in usable))
            if usable
            else None,
        ),
        protected_session_ids=[],
        completed_session_ids=[],
        retired_session_ids=[],
    )
    forecast_ids = {f["id"] for f in previous}
    queues = {
        str(q.id): q
        for q in db.scalars(
            select(WorkoutQueue).where(WorkoutQueue.user_id == user.id, WorkoutQueue.activity_type == "running")
        )
    }
    unrepresented = sorted(
        str(q.id)
        for q in queues.values()
        if (str(q.id) not in forecast_ids or q.plan_id != plan.id)
        and q.status not in {"completed", "skipped"}
        and (q.scheduled_date is None or q.scheduled_date > now)
    )
    mismatches = []
    for f in previous:
        q = queues.get(f["id"])
        at = datetime.fromisoformat(f["scheduledDate"])
        retired = q is None and db.scalar(
            select(PrescriptionRevision.id).where(
                PrescriptionRevision.user_id == user.id, PrescriptionRevision.workout_id == uuid.UUID(f["id"])
            )
        )
        if (q and q.status in {"completed", "skipped"}) or retired:
            context["retired_session_ids"].append(f["id"])
            if q and q.status == "completed":
                context["completed_session_ids"].append(f["id"])
        if at <= now + timedelta(hours=POLICY["freeze_hours"]) or (q and not future_eligible(q, now)) or retired:
            context["protected_session_ids"].append(f["id"])
            if q and (q.scheduled_date != at or q.workout_data != f["composition"] or q.title != f["title"]):
                version = db.get(PrescriptionRevision, q.prescription_revision) if q.prescription_revision else None
                if version and (version.user_id != user.id or version.workout_id != q.id):
                    version = None
                mismatches.append(
                    dict(
                        workoutId=f["id"],
                        forecastDate=f["scheduledDate"],
                        forecastDose=f.get("dose"),
                        currentPrescription=compact_prescription(prescription_snapshot(q), version),
                        prescriptionRevision=str(version.id) if version else None,
                    )
                )
    evidence_digest = checksum(
        dict(
            activities=sorted(
                [
                    dict(
                        id=str(w.id),
                        date=w.start_date.isoformat(),
                        distance=w.total_distance,
                        duration=w.duration,
                        updated=w.updated_at.isoformat(),
                    )
                    for w in rows
                ],
                key=lambda x: x["id"],
            ),
            profile=facts,
            protected=sorted(context["protected_session_ids"]),
            retired=sorted(context["retired_session_ids"]),
            unrepresented=sorted(
                (prescription_snapshot(queues[i]) for i in unrepresented), key=lambda x: x["workout_id"]
            ),
        )
    )
    if not profile["available_days"] or facts.get("restrictions") or mismatches or unrepresented:
        context["load"]["confidence"] = 0
    result = generate_remaining_program(plan.id, profile, previous, now, context)
    result["evidence"]["protectedPrescriptionMismatches"] = mismatches
    result["evidence"]["unrepresentedCommittedWorkoutIds"] = unrepresented
    result["evidence"]["retiredSessionIds"] = sorted(context["retired_session_ids"])
    result["evidence"]["completedSessionIds"] = sorted(context["completed_session_ids"])
    if unrepresented:
        result.update(status="review_required", forecast=None, replacement_session_ids=[])
        result["limitations"].append(
            "Current committed workouts are absent from the immutable forecast. Their load and demanding-session spacing require review; no incomplete calendar is treated as complete."
        )
    if mismatches:
        result.update(status="review_required", forecast=None, replacement_session_ids=[])
        result["limitations"].append(
            "Protected current immutable prescriptions differ from the original forecast. No stale dose, schedule or spacing budget is inferred; review actual committed prescriptions before rebuilding."
        )
    result["limitations"].append(
        "Coverage and interruption are athlete attestations; recorded activities do not prove absent activity or device coverage."
    )
    if not profile["available_days"] or facts.get("restrictions"):
        result.update(status="review_required", forecast=None, replacement_session_ids=[])
        result["limitations"].append(
            "Current availability is unknown or restrictions require an athlete review; no replacement is inferred."
        )
    retired_ids = sorted(
        forecast_ids - set(context["protected_session_ids"]) - {f["id"] for f in (result["forecast"] or [])}
    )
    return result, evidence_digest, retired_ids


def validate_rebuild(db, user, plan, data):
    result, digest, retired = prepare_rebuild(db, user, plan, data["intent"])
    if result["status"] != "forecast_ready" or digest != data["evidenceDigest"] or retired != data["retiredWorkoutIds"]:
        raise HTTPException(409, "Rebuild evidence or protected prescriptions changed; preview again")
    # The approved forecast is fixed. Re-running the generator cannot authorize a new forecast.
    for f in require_forecast(
        data.get("forecast"),
        409,
        calendar_dates=True,
        timezone=(plan.metadata_ or {}).get("timezone", "Australia/Brisbane"),
    ):
        if f["id"] not in data["protectedSessionIds"]:
            at = datetime.fromisoformat(f["scheduledDate"])
            if at <= datetime.now(UTC) + timedelta(hours=POLICY["freeze_hours"]):
                raise HTTPException(409, "Rebuild prescription became protected; preview again")
            validate_composition(f["composition"])


def apply_rebuild(db, user, plan, data):
    from app.coaching_scheduler import publish_forecast

    for identifier in data["retiredWorkoutIds"]:
        item = db.get(WorkoutQueue, uuid.UUID(identifier))
        if item is not None:
            if item.user_id != user.id or item.plan_id != plan.id or not future_eligible(item, datetime.now(UTC)):
                raise HTTPException(409, "Rebuild retirement target changed")
            db.add(WorkoutAction(id=uuid.uuid4(), user_id=user.id, workout_id=item.id, action="delete"))
            db.delete(item)
    for forecast in require_forecast(
        data.get("forecast"),
        409,
        calendar_dates=True,
        timezone=(plan.metadata_ or {}).get("timezone", "Australia/Brisbane"),
    ):
        if forecast["id"] in data["protectedSessionIds"]:
            continue
        item = db.get(WorkoutQueue, uuid.UUID(forecast["id"]))
        if item is not None:
            if item.user_id != user.id or item.plan_id != plan.id or not future_eligible(item, datetime.now(UTC)):
                raise HTTPException(409, "Rebuild replacement target changed")
            item.title = forecast["title"]
            item.workout_data = copy.deepcopy(forecast["composition"])
            item.scheduled_date = datetime.fromisoformat(forecast["scheduledDate"])
            if item.status in {"fetched", "synced"}:
                db.add(
                    WorkoutAction(
                        id=uuid.uuid4(),
                        user_id=user.id,
                        workout_id=item.id,
                        action="edit",
                        composition=copy.deepcopy(item.workout_data),
                    )
                )
    plan.metadata_ = {
        **plan.metadata_,
        "forecast": copy.deepcopy(data["forecast"]),
        "programPolicyVersion": data["programPolicyVersion"],
        "forecastApproved": True,
    }
    publish_forecast(db, user.id, plan)


def compact_prescription(snapshot, version):
    composition = snapshot.get("composition") or {}
    return dict(
        workoutId=snapshot["workout_id"],
        title=snapshot["title"],
        scheduledDate=snapshot["scheduled_date"],
        activityType=snapshot["activity_type"],
        prescriptionRevision=str(version.id) if version else None,
        contentHash=version.content_hash if version else None,
        targets={
            key: copy.deepcopy(composition[key])
            for key in ("singleGoal", "blocks", "warmup", "cooldown", "trainingPurpose")
            if key in composition
        },
    )


def undo_explanation_context(db, user, plan, preview, now=None):
    from app.models.coaching import PrescriptionRevision

    now = now or datetime.now(UTC)
    target = db.scalar(
        select(PlanRevision).where(
            PlanRevision.user_id == user.id,
            PlanRevision.plan_id == plan.id,
            PlanRevision.revision == preview["targetRevision"],
        )
    )
    current = db.scalar(
        select(PlanRevision).where(
            PlanRevision.user_id == user.id, PlanRevision.plan_id == plan.id, PlanRevision.revision == plan.revision
        )
    )
    if target is None or current is None:
        raise HTTPException(422, "Immutable current and target plan history required for undo explanation")
    old = {s["workout_id"]: s for s in target.snapshot.get("prescriptions", [])}

    def owned_version(identifier, workout_id, snapshot):
        version = db.get(PrescriptionRevision, uuid.UUID(identifier)) if identifier else None
        if (
            version is None
            or version.user_id != user.id
            or str(version.workout_id) != workout_id
            or checksum(version.snapshot) != checksum(snapshot)
        ):
            raise HTTPException(
                422, "Exact owned immutable prescription facts unavailable; no undo explanation inferred"
            )
        return version

    changes = []
    for change in preview["structuralChanges"]:
        previous = old.get(change["workoutId"])
        if previous is None:
            raise HTTPException(422, "Immutable undo target unavailable")
        before = owned_version(change["basePrescriptionRevision"], change["workoutId"], change["before"])
        after = owned_version(previous.get("prescription_revision"), change["workoutId"], change["after"])
        changes.append(
            dict(
                workoutId=change["workoutId"],
                before=compact_prescription(change["before"], before),
                after=compact_prescription(change["after"], after),
            )
        )
    calendar = []
    for item in db.scalars(select(WorkoutQueue).where(WorkoutQueue.user_id == user.id).order_by(WorkoutQueue.id)):
        if item.status in {"completed", "skipped"}:
            continue
        if item.scheduled_date is None:
            raise HTTPException(
                422,
                "Committed prescription schedule is unknown; reconcile its date before requesting a complete undo explanation",
            )
        if item.scheduled_date < now:
            continue
        snap = prescription_snapshot(item)
        version = owned_version(
            str(item.prescription_revision) if item.prescription_revision else None, str(item.id), snap
        )
        calendar.append(compact_prescription(snap, version))
    return dict(
        targetRevision=preview["targetRevision"],
        targetPlanRevisionId=str(target.id),
        currentPlanRevisionId=str(current.id),
        undoChanges=changes,
        remainingCalendar=sorted(calendar, key=lambda x: (x["scheduledDate"], x["workoutId"])),
        remainingCalendarScope="complete_owned_committed_remaining_prescriptions",
        remainingCalendarComplete=True,
        unpublishedForecastIncluded=False,
        unknowns=[
            "Calendar covers all owned committed future prescriptions, not an unissued full goal forecast.",
            "Immutable target structure is supplied; no distance from default speed, fitness or actual completion is inferred.",
        ],
    )
