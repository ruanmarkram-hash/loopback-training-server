import uuid

from fastapi import APIRouter, status
from sqlalchemy import select

from app.auth import CurrentUser
from app.coaching_service import lock_athlete
from app.database import DbSession
from app.models.action import WorkoutAction
from app.models.coaching import PrescriptionRevision
from app.models.queue import WorkoutQueue
from app.routes.queue import _scheduled_date_from_data
from app.schemas.action import ActionCreate, ActionRead
from app.tenancy import get_owned

router = APIRouter()


def action_current(db, action):
    item = db.get(WorkoutQueue, action.workout_id)
    if item is None:
        last = db.scalar(
            select(PrescriptionRevision)
            .where(PrescriptionRevision.workout_id == action.workout_id, PrescriptionRevision.user_id == action.user_id)
            .order_by(PrescriptionRevision.created_at.desc())
        )
        return action.action == "delete" or last is None or last.snapshot.get("state") != "deleted"
    return (
        item.user_id == action.user_id
        and item.status != "completed"
        and (item.status != "skipped" or action.action == "delete")
        and item.prescription_revision in (action.base_prescription_revision, action.desired_prescription_revision)
    )


def action_wire(db, action):
    composition = dict(action.composition or {}) if action.composition is not None else None
    if composition is not None:
        composition["id"] = str(action.workout_id)
        revision = (
            db.get(PrescriptionRevision, action.desired_prescription_revision)
            if action.desired_prescription_revision
            else None
        )
        if revision:
            composition["prescriptionRevision"] = str(revision.id)
            composition["contentHash"] = revision.content_hash
    return dict(
        id=action.id,
        workout_id=action.workout_id,
        action=action.action,
        composition=composition,
        created_at=action.created_at,
        base_prescription_revision=action.base_prescription_revision,
        desired_prescription_revision=action.desired_prescription_revision,
    )


@router.get("", response_model=list[ActionRead])
def get_pending_actions(db: DbSession, user: CurrentUser):
    q = select(WorkoutAction).where(WorkoutAction.user_id == user.id).order_by(WorkoutAction.created_at)
    rows = db.scalars(q).all()
    return [action_wire(db, a) for a in rows if action_current(db, a)]


@router.post("", response_model=ActionRead, status_code=status.HTTP_201_CREATED)
def create_action(payload: ActionCreate, db: DbSession, user: CurrentUser):
    action = WorkoutAction(
        user_id=user.id,
        workout_id=payload.workout_id,
        action=payload.action,
        composition=payload.composition,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action_wire(db, action)


@router.post("/batch", response_model=list[ActionRead], status_code=status.HTTP_201_CREATED)
def create_actions_batch(payload: list[ActionCreate], db: DbSession, user: CurrentUser):
    actions = []
    for item in payload:
        action = WorkoutAction(
            user_id=user.id,
            workout_id=item.workout_id,
            action=item.action,
            composition=item.composition,
        )
        db.add(action)
        actions.append(action)
    db.commit()
    for action in actions:
        db.refresh(action)
    return [action_wire(db, a) for a in actions]


@router.delete("/{action_id}", status_code=status.HTTP_200_OK)
def acknowledge_action(action_id: uuid.UUID, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    action = get_owned(db, WorkoutAction, action_id, user)
    # The app acks an action only after applying it on the watch — mirror the
    # confirmed change onto the queue item so validation, /context, and future
    # edits don't keep reading pre-action state.
    item = db.get(WorkoutQueue, action.workout_id)
    applicable = action_current(db, action)
    if item is not None and item.user_id == user.id and applicable:
        if (
            action.action == "edit"
            and action.composition is not None
            and (
                action.desired_prescription_revision is None
                or item.prescription_revision != action.desired_prescription_revision
            )
        ):
            # `title` is a column, `displayName` a key inside workout_data —
            # writing only the blob left the list title describing the *old*
            # session ("Easy 35 min" on a 30-minute run) with no way to tell
            # which one was stale.
            #
            # Only re-sync a title that was *tracking* the composition. A title
            # that already diverged is a deliberate label — coaches annotate
            # them ("… (cap 140) — OPTIONAL") where the watch name stays terse —
            # and blindly overwriting it destroys that on every edit ack. The
            # old blob is still in hand here, so "was it tracking?" is an exact
            # question, not a guess.
            old_name = (item.workout_data or {}).get("displayName")
            new_name = action.composition.get("displayName")
            if new_name and item.title == old_name:
                item.title = new_name
            item.workout_data = action.composition
            if action.desired_prescription_revision is not None:
                item.prescription_revision = action.desired_prescription_revision
            item.scheduled_date = _scheduled_date_from_data(action.composition) or item.scheduled_date
        elif action.action == "delete" and item.status != "completed":
            # Removed from the watch: retire the item like a skipped session
            # so it stops counting as a scheduled run or schedule collision.
            item.status = "skipped"
    db.delete(action)
    db.commit()
    return {"ok": True, "applied": applicable, "reason": "confirmed" if applicable else "superseded_or_retired"}
