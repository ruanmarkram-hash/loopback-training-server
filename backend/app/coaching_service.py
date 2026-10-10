"""Transactional snapshots, deduplicated review jobs and deterministic validation."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.activity_observation import canonical_workout, digest
from app.coaching_policy import POLICY, assess
from app.models.action import WorkoutAction
from app.models.coaching import ExecutionAssessment, PlanRevision, PrescriptionRevision, ReviewJob
from app.models.feedback import WorkoutFeedback
from app.models.plan import Plan
from app.models.plan_note import PlanNote
from app.models.queue import WorkoutQueue
from app.models.user import User
from app.models.workout import Workout


def checksum(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()
    ).hexdigest()


def lock_athlete(db, user_id):
    db.scalar(select(User).where(User.id == user_id).with_for_update())


def normalize_job_key(key):
    # Preserve existing persisted short keys and namespace long intents by their complete hash.
    return key if len(key) <= 128 else "bounded:" + checksum(key)


def enqueue(db, user_id, key, data):
    key = normalize_job_key(key)
    existing = db.scalar(select(ReviewJob).where(ReviewJob.user_id == user_id, ReviewJob.idempotency_key == key))
    if existing:
        return existing
    for row in db.new:
        if isinstance(row, ReviewJob) and row.user_id == user_id and row.idempotency_key == key:
            return row
    row = ReviewJob(id=uuid.uuid4(), user_id=user_id, idempotency_key=key, status="pending", attempts=0, data=data)
    db.add(row)
    return row


def owned_reference(db, model, value, user_id):
    if value is None:
        return None
    row = db.get(model, value)
    if row is not None and row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Linked resource not found")
    return row


def prescription_snapshot(item):
    return dict(
        workout_id=str(item.id),
        plan_id=str(item.plan_id) if item.plan_id else None,
        title=item.title,
        description=item.description,
        activity_type=item.activity_type,
        scheduled_date=(
            item.scheduled_date.replace(tzinfo=timezone.utc)
            if item.scheduled_date.tzinfo is None
            else item.scheduled_date
        )
        .astimezone(timezone.utc)
        .isoformat()
        if item.scheduled_date
        else None,
        composition=copy.deepcopy(item.workout_data),
    )


def validate_composition(composition):
    """Hard composition shape/number constraints, separate from coaching warnings."""
    if composition is None:
        return
    if not isinstance(composition, dict):
        raise HTTPException(422, "Composition must be an object")

    from app.composition_shape import composition_shape_valid, finite_nonnegative

    if not composition_shape_valid(composition):
        raise HTTPException(
            422, "Composition blocks, steps, goals, alerts and repetitions must have supported structural types"
        )

    def walk(value, depth=0):
        if depth > 12:
            raise HTTPException(422, "Composition nesting too deep")
        if isinstance(value, dict):
            for slot in ("goal", "singleGoal"):
                goal = value.get(slot)
                if isinstance(goal, dict) and goal.get("type") in ("distance", "time"):
                    from app.coaching_policy import finite_positive

                    if not finite_positive(goal.get("value")):
                        raise HTTPException(422, "Goal quantity must be finite and positive")
                    allowed = (
                        ("meters", "kilometers", "miles") if goal["type"] == "distance" else ("seconds", "minutes")
                    )
                    if goal.get("unit") not in allowed:
                        raise HTTPException(422, "Unsupported goal unit")
            alert = value.get("alert")
            if isinstance(alert, dict) and alert.get("type") == "pace":
                raise HTTPException(422, "Unsupported pace alert; use native speed units")
            if isinstance(alert, dict) and alert.get("type") == "speed":
                if alert.get("unit", "metersPerSecond") not in ("metersPerSecond", "kilometersPerHour"):
                    raise HTTPException(422, "Unsupported speed unit")
                from app.coaching_policy import finite_positive

                if not all(finite_positive(alert.get(k)) for k in ("min", "max")) or alert["min"] > alert["max"]:
                    raise HTTPException(422, "Invalid pace/speed range")
            for key, item in value.items():
                if (
                    key
                    in (
                        "distance",
                        "duration",
                        "distanceMeters",
                        "durationSeconds",
                        "repeatCount",
                        "iterations",
                    )
                    and isinstance(item, (int, float))
                    and (not finite_nonnegative(item) or item <= 0)
                ):
                    raise HTTPException(422, "Invalid composition quantity")
                walk(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 200:
                raise HTTPException(422, "Composition is too large")
            for item in value:
                walk(item, depth + 1)

    walk(composition)


@event.listens_for(Session, "before_flush")
def capture_changes(db, flush_context, instances):
    relevant = [
        x
        for x in list(db.new) + list(db.dirty) + list(db.deleted)
        if isinstance(x, (Plan, WorkoutQueue, Workout, WorkoutAction, WorkoutFeedback, PlanNote))
    ]
    changed = [x for x in relevant if x in db.new or x in db.deleted or db.is_modified(x, include_collections=False)]
    if not changed:
        return
    for user_id in sorted({x.user_id for x in changed if x.user_id}, key=str):
        lock_athlete(db, user_id)
    # Direct ORM writers can have read before another transaction committed.
    # Keep their explicit changes, but refresh untouched columns under the lock
    # so immutable snapshots describe the row that will actually be persisted.
    locked_users = {x.user_id for x in changed if x.user_id}
    related = [
        row for row in db.identity_map.values() if isinstance(row, (Plan, WorkoutQueue)) and row.user_id in locked_users
    ]
    with db.no_autoflush:
        for row in list(dict.fromkeys([*changed, *related])):
            state = inspect(row)
            if not state.persistent:
                continue
            unchanged = [
                column.key for column in state.mapper.column_attrs if not state.attrs[column.key].history.has_changes()
            ]
            if unchanged:
                db.refresh(row, attribute_names=unchanged)
    touched_plans = {}
    for row in changed:
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        plan_id = getattr(row, "plan_id", None)
        if plan_id:
            plan = owned_reference(db, Plan, plan_id, row.user_id)
            if plan is None:
                plan = next(
                    (p for p in db.new if isinstance(p, Plan) and p.id == plan_id and p.user_id == row.user_id), None
                )
            if plan is None:
                raise HTTPException(404, "Linked plan not found")
        if isinstance(row, Workout) and row.plan_workout_id:
            exact = db.get(PrescriptionRevision, row.plan_workout_id)
            if exact:
                if exact.user_id != row.user_id:
                    raise HTTPException(404, "Linked prescription not found")
                supplied = (row.data or {}).get("prescriptionRevision")
                if supplied and supplied != str(exact.id):
                    raise HTTPException(422, "Executed prescription identity mismatch")
                row.plan_workout_id = exact.workout_id
                row.data = {**(row.data or {}), "prescriptionRevision": str(exact.id)}
        if isinstance(row, Workout):
            owned_reference(db, WorkoutQueue, row.plan_workout_id, row.user_id)
        if isinstance(row, (WorkoutAction, WorkoutFeedback)):
            owned_reference(db, WorkoutQueue, row.workout_id, row.user_id)
        if isinstance(row, WorkoutQueue):
            if row in db.new or inspect(row).attrs.workout_data.history.has_changes():
                validate_composition(row.workout_data)
            material_fields = any(
                inspect(row).attrs[name].history.has_changes()
                for name in ("workout_data", "scheduled_date", "title", "description", "activity_type", "plan_id")
            )

            if row in db.deleted:
                snapshot = {**prescription_snapshot(row), "state": "deleted"}
                db.add(
                    PrescriptionRevision(
                        id=uuid.uuid4(),
                        user_id=row.user_id,
                        workout_id=row.id,
                        content_hash=checksum(snapshot),
                        snapshot=snapshot,
                    )
                )
            if row not in db.deleted:
                snapshot = prescription_snapshot(row)
                h = checksum(snapshot)
                previous = (
                    db.get(PrescriptionRevision, row.prescription_revision) if row.prescription_revision else None
                )
                if previous is None or previous.content_hash != h:
                    version = PrescriptionRevision(
                        id=uuid.uuid4(), user_id=row.user_id, workout_id=row.id, content_hash=h, snapshot=snapshot
                    )
                    db.add(version)
                    row.prescription_revision = version.id
                    if previous is None and row not in db.new and not material_fields:
                        legacy = db.scalars(
                            select(WorkoutAction).where(
                                WorkoutAction.user_id == row.user_id,
                                WorkoutAction.workout_id == row.id,
                                WorkoutAction.base_prescription_revision.is_(None),
                                WorkoutAction.desired_prescription_revision.is_(None),
                            )
                        ).all()
                        for action in legacy:
                            if action in db.deleted:
                                continue
                            action.base_prescription_revision = version.id
                            if action not in changed:
                                changed.append(action)

                elif row.prescription_revision is None:
                    row.prescription_revision = previous.id
            material = (
                row in db.new
                or row in db.deleted
                or any(
                    inspect(row).attrs[name].history.has_changes()
                    for name in ("workout_data", "scheduled_date", "title", "description", "activity_type", "plan_id")
                )
            )
            if plan_id and material:
                touched_plans[plan_id] = plan
            old_links = inspect(row).attrs.plan_id.history.deleted
            for old_id in old_links:
                if old_id and old_id != plan_id:
                    old_plan = db.get(Plan, old_id)
                    if old_plan and old_plan.user_id == row.user_id:
                        touched_plans[old_id] = old_plan

        if isinstance(row, Plan) and row not in db.deleted:
            touched_plans[row.id] = row
        if isinstance(row, Workout) and row in db.deleted:
            assessment = db.get(ExecutionAssessment, row.id)
            if assessment:
                db.delete(assessment)
            if not db.info.get("coaching_apply"):
                enqueue(
                    db,
                    row.user_id,
                    "activity-deleted:" + str(row.id) + ":" + checksum(row.data),
                    dict(trigger="activity_deleted", sourceEventIds=[str(row.id)], policyVersion=POLICY["version"]),
                )
        if isinstance(row, Workout) and row not in db.deleted:
            prescription = (
                db.scalar(
                    select(PrescriptionRevision)
                    .where(
                        PrescriptionRevision.workout_id == row.plan_workout_id,
                        PrescriptionRevision.user_id == row.user_id,
                    )
                    .order_by(PrescriptionRevision.created_at.desc())
                )
                if row.plan_workout_id
                else None
            )
            # Explicit reported revision selects an immutable historical prescription.
            observed = (row.data or {}).get("prescriptionRevision")
            if observed:
                try:
                    specified = db.get(PrescriptionRevision, uuid.UUID(observed))
                except (ValueError, TypeError):
                    specified = None
                if not specified or specified.user_id != row.user_id or specified.workout_id != row.plan_workout_id:
                    raise HTTPException(404, "Linked prescription not found")
                prescription = specified
            canonical_hash = digest(canonical_workout(row))
            if row.source_evidence_hash != canonical_hash:
                row.source_evidence_hash = canonical_hash
                row.source_evidence_revision = (row.source_evidence_revision or 0) + 1
            data = assess(row, prescription)
            h = checksum(
                dict(data=row.data, distance=row.total_distance, duration=row.duration, link=row.plan_workout_id,
                     **({"sourceWithdrawn": True} if row.source_withdrawn else {}))
            )
            assessment = db.get(ExecutionAssessment, row.id)
            if assessment:
                assessment.data = data
                assessment.evidence_hash = h
            else:
                db.add(ExecutionAssessment(workout_id=row.id, user_id=row.user_id, evidence_hash=h, data=data))
            if not db.info.get("coaching_apply"):
                enqueue(
                    db,
                    row.user_id,
                    "activity:" + str(row.id) + ":" + h,
                    dict(trigger="activity_changed", sourceEventIds=[str(row.id)], policyVersion=POLICY["version"]),
                )
        if isinstance(row, WorkoutFeedback) and not db.info.get("coaching_apply"):
            evidence = {
                c.key: str(getattr(row, c.key))
                for c in inspect(row).mapper.column_attrs
                if c.key not in ("created_at", "updated_at")
            }
            enqueue(
                db,
                row.user_id,
                "feedback:" + checksum(evidence),
                dict(trigger="feedback_changed", sourceEventIds=[str(row.id)], policyVersion=POLICY["version"]),
            )
    for action in [
        x
        for x in changed
        if isinstance(x, WorkoutAction)
        and x not in db.deleted
        and (x in db.new or x.base_prescription_revision is not None and x.desired_prescription_revision is None)
    ]:
        queue = owned_reference(db, WorkoutQueue, action.workout_id, action.user_id)
        if queue and queue.prescription_revision is None:
            snapshot = prescription_snapshot(queue)
            revision = PrescriptionRevision(
                id=uuid.uuid4(),
                user_id=queue.user_id,
                workout_id=queue.id,
                content_hash=checksum(snapshot),
                snapshot=snapshot,
            )
            db.add(revision)
            queue.prescription_revision = revision.id
        action.base_prescription_revision = queue.prescription_revision if queue else None
        if action.action == "edit" and action.composition:
            validate_composition(action.composition)
            snapshot = (
                prescription_snapshot(queue)
                if queue
                else dict(
                    workout_id=str(action.workout_id),
                    plan_id=None,
                    title=action.composition.get("displayName", "Workout"),
                    activity_type="running",
                    description=None,
                    scheduled_date=action.composition.get("scheduledDate"),
                )
            )
            if queue:
                old_name = (queue.workout_data or {}).get("displayName")
                new_name = action.composition.get("displayName")
                if new_name and queue.title == old_name:
                    snapshot["title"] = new_name
                raw = action.composition.get("scheduledDate")
                if isinstance(raw, str):
                    try:
                        intended = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        if intended.tzinfo is None:
                            intended = intended.replace(tzinfo=timezone.utc)
                        snapshot["scheduled_date"] = intended.astimezone(timezone.utc).isoformat()
                    except ValueError:
                        pass
            snapshot["composition"] = copy.deepcopy(action.composition)
            if queue and snapshot == prescription_snapshot(queue):
                action.desired_prescription_revision = queue.prescription_revision
            else:
                desired = PrescriptionRevision(
                    id=uuid.uuid4(),
                    user_id=action.user_id,
                    workout_id=action.workout_id,
                    content_hash=checksum(snapshot),
                    snapshot=snapshot,
                )
                db.add(desired)
                action.desired_prescription_revision = desired.id
    revisions = db.info.setdefault("coaching_transaction_revisions", {})
    for plan_id, plan in touched_plans.items():
        if plan in db.deleted:
            continue
        record = revisions.get(plan_id)
        if record is None:
            if plan in db.new:
                plan.revision = 1
            else:
                current = db.scalar(select(Plan.revision).where(Plan.id == plan_id)) or 1
                plan.revision = current + 1
            record = PlanRevision(
                id=uuid.uuid4(), user_id=plan.user_id, plan_id=plan_id, revision=plan.revision, snapshot={}
            )
            revisions[plan_id] = record
            db.add(record)
        else:
            plan.revision = record.revision
        snap = dict(
            name=plan.name,
            status=plan.status,
            start_date=str(plan.start_date),
            end_date=str(plan.end_date),
            metadata=copy.deepcopy(plan.metadata_),
        )
        prescribed = {
            q.id: q
            for q in db.scalars(
                select(WorkoutQueue).where(WorkoutQueue.user_id == plan.user_id, WorkoutQueue.plan_id == plan_id)
            )
        }
        prescribed = {key: q for key, q in prescribed.items() if q.plan_id == plan_id}
        prescribed.update(
            {
                q.id: q
                for q in list(db.new) + list(db.dirty)
                if isinstance(q, WorkoutQueue) and q.user_id == plan.user_id and q.plan_id == plan_id
            }
        )
        snap["prescriptions"] = [
            {
                **prescription_snapshot(q),
                "prescription_revision": str(q.prescription_revision) if q.prescription_revision else None,
            }
            for _, q in sorted(prescribed.items(), key=lambda pair: str(pair[0]))
            if q not in db.deleted
        ]
        manual = not db.info.get("coaching_apply") and any(
            x in db.dirty
            and (
                getattr(x, "plan_id", getattr(x, "id", None)) == plan_id
                or isinstance(x, WorkoutQueue)
                and plan_id in inspect(x).attrs.plan_id.history.deleted
            )
            for x in changed
        )
        if manual:
            snap["manualEditedAt"] = datetime.now(timezone.utc).isoformat()
        elif record.snapshot.get("manualEditedAt"):
            snap["manualEditedAt"] = record.snapshot["manualEditedAt"]
        record.snapshot = snap


@event.listens_for(Session, "after_transaction_end")
def clear_transaction_revisions(db, transaction):
    if transaction.parent is None:
        db.info.pop("coaching_transaction_revisions", None)
