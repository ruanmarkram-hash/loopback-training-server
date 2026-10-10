import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.activity_observation import canonical_workout, digest
from app import activity_observation_service as observations
from app.auth import CurrentUser
from app.database import DbSession
from app.models.feedback import WorkoutFeedback
from app.models.plan import Plan
from app.models.queue import WorkoutQueue
from app.models.workout import Workout
from app.routes.queue import _scheduled_date_from_data
from app.schemas.workout import (
    WorkoutContextQueueItem,
    WorkoutContextRead,
    WorkoutCreate,
    WorkoutList,
    WorkoutRead,
    WorkoutSummary,
)
from app.tenancy import get_owned
from app.workout_summary import downsample_timed, round_floats, strip_samples

router = APIRouter()


@router.post("", response_model=WorkoutRead, status_code=status.HTTP_201_CREATED)
def create_workout(payload: WorkoutCreate, db: DbSession, user: CurrentUser, request: Request):
    existing = db.get(Workout, payload.id)
    if existing and existing.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workout id belongs to another user")
    try:
        incoming_hash = digest(canonical_workout(payload))
    except ValueError as exc:
        raise HTTPException(422, "Workout evidence must contain finite canonical values") from exc
    member = None
    if payload.observation is not None:
        member = observations.upload_member(db, user, observations.device_actor(request, user),
                                            payload.observation, payload.id, incoming_hash)
    workout = existing or Workout(id=payload.id, user_id=user.id)
    # An exact retry must not churn updated_at or the claimed evidence checksum.
    if existing is None or digest(canonical_workout(existing)) != incoming_hash:
        for name in canonical_workout(payload):
            # Canonicalization serves the hash; typed values serve ORM persistence.
            setattr(workout, name, getattr(payload, name))
    if existing is None:
        db.add(workout)
    if payload.observation is not None:
        # A new explicit upload can supersede a withdrawal; unchanged ACK cannot.
        workout.source_withdrawn = False
    # before_flush preserves original-prescription normalization/assessment and
    # updates the server source revision. ACK only the actually persisted shape.
    if existing is not None and workout.source_evidence_hash is None:
        workout.source_evidence_hash = incoming_hash
        workout.source_evidence_revision = (workout.source_evidence_revision or 0)+1
    db.flush()
    if member is not None:
        stored_hash = digest(canonical_workout(workout))
        if workout.source_evidence_hash != stored_hash:
            raise HTTPException(409, "Stored content revision is not reconciled")
        member.disposition = "acknowledged"
        member.server_payload_digest = stored_hash
        member.workout_revision = workout.source_evidence_revision
        from datetime import UTC
        member.acknowledged_at = datetime.now(UTC)
    db.commit()
    db.refresh(workout)
    return workout


@router.get("", response_model=list[WorkoutList])
def list_workouts(
    db: DbSession,
    user: CurrentUser,
    activity_type: str | None = None,
    start_after: datetime | None = None,
    start_before: datetime | None = None,
    plan_workout_id: uuid.UUID | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    q = select(Workout).where(Workout.user_id == user.id).order_by(Workout.start_date.desc())

    if activity_type:
        q = q.where(Workout.activity_type == activity_type)
    if start_after:
        q = q.where(Workout.start_date >= start_after)
    if start_before:
        q = q.where(Workout.start_date <= start_before)
    if plan_workout_id:
        q = q.where(Workout.plan_workout_id == plan_workout_id)

    q = q.offset(offset).limit(limit)
    return db.scalars(q).all()


@router.get("/summary", response_model=list[WorkoutSummary])
def workout_summary(
    db: DbSession,
    user: CurrentUser,
    activity_type: str | None = None,
    period: str = Query(default="month", pattern="^(week|month|year)$"),
    start_after: datetime | None = None,
    start_before: datetime | None = None,
    limit: int | None = Query(default=None, ge=1, le=500),
):
    trunc = func.date_trunc(period, Workout.start_date)

    q = (
        select(
            trunc.label("period"),
            Workout.activity_type,
            func.count().label("count"),
            func.sum(Workout.total_distance).label("total_distance"),
            func.sum(Workout.duration).label("total_duration"),
            func.avg(Workout.total_distance).label("avg_distance"),
            func.avg(Workout.duration).label("avg_duration"),
            func.sum(Workout.total_energy_burned).label("total_energy_burned"),
        )
        .where(Workout.user_id == user.id)
        .group_by(trunc, Workout.activity_type)
        .order_by(trunc.desc())
    )

    if activity_type:
        q = q.where(Workout.activity_type == activity_type)
    if start_after:
        q = q.where(Workout.start_date >= start_after)
    if start_before:
        q = q.where(Workout.start_date <= start_before)
    if limit:
        # Rows are ordered newest-first, so a limit keeps the most recent
        # (period × activity_type) rows and history stays reachable via
        # start_before. Without it the response grows forever.
        q = q.limit(limit)

    rows = db.execute(q).all()
    return [
        WorkoutSummary(
            period=str(row.period),
            activity_type=row.activity_type,
            count=row.count,
            total_distance=round(row.total_distance, 1) if row.total_distance is not None else None,
            total_duration=round(row.total_duration, 1) if row.total_duration is not None else None,
            avg_distance=round(float(row.avg_distance), 1) if row.avg_distance else None,
            avg_duration=round(float(row.avg_duration), 1) if row.avg_duration else None,
            total_energy_burned=round(row.total_energy_burned, 1) if row.total_energy_burned is not None else None,
        )
        for row in rows
    ]


@router.get("/{workout_id}", response_model=WorkoutRead)
def get_workout(
    workout_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    include_samples: bool = True,
):
    """Full workout detail. With `include_samples=false` the raw sample
    arrays in `data` (route GPS points, cadence, heart rate — ~600 kB for a
    GPS run) are replaced by a compact `data.samplesSummary`, and float noise
    is rounded away — root columns included, not just the blob."""
    workout = get_owned(db, Workout, workout_id, user)
    if include_samples:
        return workout
    read = WorkoutRead.model_validate(workout)
    read.data = strip_samples(read.data)
    # The ORM float columns carry the same double-precision noise as the blob.
    for name, value in list(read):
        if isinstance(value, float):
            setattr(read, name, round(value, 2))
    return read


@router.get("/{workout_id}/splits")
def get_workout_splits(workout_id: uuid.UUID, db: DbSession, user: CurrentUser):
    workout = get_owned(db, Workout, workout_id, user)
    return round_floats(workout.data.get("splits", workout.data.get("events", [])))


@router.get("/{workout_id}/heartrate")
def get_workout_heartrate(
    workout_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    max_samples: int | None = Query(default=None, ge=16, le=5000),
):
    """Heart rate series. `max_samples` caps the series length by averaging
    over evenly-sized buckets — a workout's sample count is otherwise bounded
    only by its duration (a 3 h run is ~2000 samples)."""
    workout = get_owned(db, Workout, workout_id, user)
    samples = workout.data.get("heartRate", workout.data.get("heartRateSamples", []))
    if max_samples and isinstance(samples, list):
        samples = downsample_timed(samples, max_samples)
    return samples


@router.get("/{workout_id}/context", response_model=WorkoutContextRead)
def get_workout_context(workout_id: uuid.UUID, db: DbSession, user: CurrentUser):
    """Server-held linkage for a recorded workout: the queue item it fulfilled
    (via plan_workout_id), the plan behind that item, and any feedback filed
    against it. Every key is null for a workout that never matched a queued
    session, so clients can call this unconditionally."""
    workout = get_owned(db, Workout, workout_id, user)

    queue_item = plan = feedback = None
    if workout.plan_workout_id is not None:
        queue_row = db.scalars(
            select(WorkoutQueue).where(
                WorkoutQueue.id == workout.plan_workout_id,
                WorkoutQueue.user_id == user.id,
            )
        ).first()
        if queue_row is not None:
            queue_item = WorkoutContextQueueItem.model_validate(queue_row)
            if queue_item.scheduled_date is None:
                queue_item.scheduled_date = _scheduled_date_from_data(queue_row.workout_data)
            if queue_row.plan_id is not None:
                plan = db.scalars(
                    select(Plan).where(Plan.id == queue_row.plan_id, Plan.user_id == user.id)
                ).first()
        # Feedback is keyed by the queue item id, so it survives queue deletion.
        feedback = db.scalars(
            select(WorkoutFeedback).where(
                WorkoutFeedback.workout_id == workout.plan_workout_id,
                WorkoutFeedback.user_id == user.id,
            )
        ).first()

    return WorkoutContextRead(
        workout_id=workout.id,
        plan_workout_id=workout.plan_workout_id,
        queue_item=queue_item,
        plan=plan,
        feedback=feedback,
    )


@router.delete("/{workout_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workout(workout_id: uuid.UUID, db: DbSession, user: CurrentUser):
    workout = get_owned(db, Workout, workout_id, user)
    db.delete(workout)
    db.commit()
