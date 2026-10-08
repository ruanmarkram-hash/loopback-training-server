"""Athlete-owned adaptive surface; worker output remains an untrusted proposal."""

import copy
import hashlib
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.auth import CurrentUser
from app.coaching_forecast import (
    require_forecast,
    require_program_metadata,
    validated_forecast,
    validated_program_metadata,
)
from app.coaching_policy import (
    POLICY,
    deterministic_review,
    finite_positive,
    speed_meters_per_second,
)
from app.coaching_program import program_forecast
from app.coaching_service import checksum, enqueue, lock_athlete, normalize_job_key, validate_composition
from app.coaching_structural import (
    apply_forward_undo,
    apply_rebuild,
    prepare_forward_undo,
    prepare_rebuild,
    undo_explanation_context,
    validate_forward_undo,
    validate_rebuild,
)
from app.database import DbSession
from app.models.action import WorkoutAction
from app.models.coaching import (
    AthleteProfile,
    ExecutionAssessment,
    PlanRevision,
    PrescriptionRevision,
    ReviewJob,
    ReviewProposal,
)
from app.models.feedback import WorkoutFeedback
from app.models.plan import Plan
from app.models.queue import WorkoutQueue
from app.models.workout import Workout
from app.tenancy import get_owned
from app.validation_service import run_validation

router = APIRouter()


class BenchmarkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pace_seconds_per_km: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    weekly_distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    long_run_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source: str | None = Field(default=None, max_length=200)
    observed_at: AwareDatetime | None = None
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    level: Literal["beginner", "established"] | None = None


class GoalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["first_5k", "5k", "10k", "half_marathon", "marathon", "general_fitness"]
    race_date: date | None = None
    target_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    benchmark: BenchmarkInput | None = None
    goal: GoalInput | None = None
    available_days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=list, max_length=7
    )
    timezone: str = "Australia/Brisbane"
    units: Literal["metric"] = "metric"
    restrictions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, KeyError):
            raise ValueError("Unknown timezone")
        return value

    @field_validator("available_days")
    @classmethod
    def unique_days(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Availability weekdays must be unique")
        return value

    @field_validator("restrictions")
    @classmethod
    def bounded_restrictions(cls, value):
        if any(len(x) > 1000 for x in value):
            raise ValueError("Restriction is too long")
        return value


@router.get("/profile")
def get_profile(db: DbSession, user: CurrentUser):
    row = db.get(AthleteProfile, user.id)
    return row.data if row else None


@router.put("/profile")
def put_profile(body: ProfileInput, db: DbSession, user: CurrentUser):
    row = db.get(AthleteProfile, user.id)
    data = body.model_dump(mode="json")
    changed = row is None or row.data != data
    if row:
        row.data = data
    else:
        db.add(AthleteProfile(user_id=user.id, data=data))
    if changed:
        enqueue(
            db, user.id, "profile:" + checksum(data), dict(trigger="profile_changed", policyVersion=POLICY["version"])
        )
    db.commit()
    return body.model_dump(mode="json")


@router.get("/status")
def get_status(db: DbSession, user: CurrentUser):
    workouts = db.scalars(select(Workout).where(Workout.user_id == user.id).order_by(Workout.id)).all()
    return dict(
        schemaVersion="1",
        policyVersion=POLICY["version"],
        policy=POLICY,
        profile=get_profile(db, user),
        metrics=training_context(db, user)["metrics"],
        assessment=training_context(db, user)["assessment"],
        coverage={"history": "available" if workouts else "unknown"},
        jobs=list_jobs(db, user),
        proposals=list_proposals(db, user),
    )


# Wire names here are intentionally explicit, independent of legacy casing.


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotencyKey: str = Field(min_length=1, max_length=100)
    planId: uuid.UUID | None = None


def client_request_key_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def job_out(row):
    return dict(
        id=str(row.id),
        ownerId=str(row.user_id),
        status=row.status,
        attempts=row.attempts,
        clientRequestKeyHash=row.data.get("clientRequestKeyHash"),
        requestType=row.data.get("requestType"),
        requestPlanId=row.data.get("requestPlanId"),
        data=row.data,
        created_at=row.created_at,
    )


def proposal_out(row):
    return {
        **row.data,
        "id": str(row.id),
        "previewJobId": str(row.job_id),
        "ownerId": str(row.user_id),
        "clientRequestKeyHash": row.data.get("clientRequestKeyHash"),
        "requestType": row.data.get("requestType"),
        "requestPlanId": row.data.get("requestPlanId"),
        "plan_id": str(row.plan_id),
        "base_revision": row.base_revision,
        "status": row.status,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
    }


@router.post("/reviews", status_code=201)
def request_review(body: ReviewInput, db: DbSession, user: CurrentUser):
    if body.planId:
        get_owned(db, Plan, body.planId, user)
    lock_athlete(db, user.id)
    row = enqueue(
        db,
        user.id,
        "manual:" + body.idempotencyKey,
        dict(
            trigger="athlete_requested",
            clientRequestKeyHash=client_request_key_hash(body.idempotencyKey),
            requestType="athlete_review",
            requestPlanId=str(body.planId) if body.planId else None,
            planId=str(body.planId) if body.planId else None,
            policyVersion=POLICY["version"],
        ),
    )
    fingerprint = checksum(body.model_dump(mode="json"))
    if row.data.get("requestChecksum") not in (None, fingerprint):
        raise HTTPException(409, "Idempotency key reused with different review request")
    row.data = {**row.data, "requestChecksum": fingerprint}
    db.commit()
    db.refresh(row)
    return job_out(row)


@router.get("/jobs")
def list_jobs(db: DbSession, user: CurrentUser):
    return [
        job_out(x)
        for x in db.scalars(
            select(ReviewJob).where(ReviewJob.user_id == user.id).order_by(ReviewJob.created_at.desc()).limit(100)
        )
    ]


@router.get("/proposals")
def list_proposals(db: DbSession, user: CurrentUser):
    return [
        proposal_out(x)
        for x in db.scalars(
            select(ReviewProposal)
            .where(ReviewProposal.user_id == user.id)
            .order_by(ReviewProposal.created_at.desc())
            .limit(100)
        )
    ]


def training_context(db, user, requested_plan_id=None):
    workouts = db.scalars(select(Workout).where(Workout.user_id == user.id).order_by(Workout.id)).all()
    queues = db.scalars(select(WorkoutQueue).where(WorkoutQueue.user_id == user.id).order_by(WorkoutQueue.id)).all()
    plans = db.scalars(select(Plan).where(Plan.user_id == user.id, Plan.status.in_(["active", "upcoming"]))).all()
    assessments = db.scalars(
        select(ExecutionAssessment)
        .where(ExecutionAssessment.user_id == user.id)
        .order_by(ExecutionAssessment.workout_id)
    ).all()
    now = datetime.now(UTC)
    candidates = []
    for q in queues:
        if (
            q.scheduled_date
            and q.scheduled_date > now + timedelta(hours=POLICY["freeze_hours"])
            and q.status not in ("completed", "skipped")
        ):
            candidates.append(
                dict(
                    workoutId=str(q.id),
                    planId=str(q.plan_id) if q.plan_id else None,
                    title=q.title,
                    scheduledDate=q.scheduled_date.isoformat(),
                    composition=q.workout_data,
                )
            )
    # No location trails, raw medical/body/nutrition/recovery data or free-form notes.
    profile = get_profile(db, user) or {}
    feedback = db.scalars(
        select(WorkoutFeedback).where(WorkoutFeedback.user_id == user.id).order_by(WorkoutFeedback.id)
    ).all()
    review = deterministic_review(
        workouts, assessments, feedback, queues, now, profile.get("timezone", "Australia/Brisbane")
    )
    settled = db.scalars(
        select(ReviewProposal).where(
            ReviewProposal.user_id == user.id, ReviewProposal.status.in_(["applied", "dismissed"])
        )
    ).all()
    revisions = db.scalars(select(PlanRevision).where(PlanRevision.user_id == user.id)).all()
    times = [r.data.get("acceptedAt") or r.data.get("dismissedAt") for r in settled] + [
        r.snapshot.get("manualEditedAt") for r in revisions
    ]
    review["cooldownActive"] = any(
        t and datetime.fromisoformat(t) > now - timedelta(hours=POLICY["cooldown_hours"]) for t in times
    )
    # Bind policy inputs even when an update leaves derived aggregates unchanged.
    # Raw activity/feedback payloads stay private; only their digest enters CLI context.
    evidence_digest = checksum(
        dict(
            activities=[
                dict(
                    id=str(w.id),
                    date=w.start_date,
                    distance=w.total_distance,
                    duration=w.duration,
                    updated=w.updated_at,
                    payloadHash=checksum(w.data),
                )
                for w in workouts
            ],
            feedback=[
                dict(
                    id=str(f.id),
                    payloadHash=checksum({c.name: getattr(f, c.name) for c in WorkoutFeedback.__table__.columns}),
                )
                for f in feedback
            ],
            assessments=[
                dict(id=str(a.workout_id), evidenceHash=a.evidence_hash, payloadHash=checksum(a.data))
                for a in assessments
            ],
            queues=[
                dict(
                    id=str(q.id),
                    planId=str(q.plan_id) if q.plan_id else None,
                    status=q.status,
                    date=q.scheduled_date,
                    revision=str(q.prescription_revision),
                    compositionHash=checksum(q.workout_data),
                    title=q.title,
                )
                for q in queues
            ],
            profile=profile,
            sharedDomains=sorted(user.data_consent),
        )
    )
    return dict(
        evidenceDigest=evidence_digest,
        policy=POLICY,
        profile=profile,
        metrics=review["metrics"],
        assessment=review,
        assessments=[dict(workoutId=str(a.workout_id), **a.data) for a in assessments[-50:]],
        metricsScope="all_owned_recorded_running_activities",
        planRevisions={
            str(p.id): p.revision for p in plans if requested_plan_id is None or str(p.id) == str(requested_plan_id)
        },
        futureWorkouts=[c for c in candidates if requested_plan_id is None or c["planId"] == str(requested_plan_id)][
            :40
        ],
        coverage={"history": "available" if workouts else "unknown"},
    )


@router.post("/worker/claim")
def claim_job(request: Request, db: DbSession, user: CurrentUser):
    if request.state.token_scope != "coach_worker":
        raise HTTPException(403, "Restricted coach_worker token required")
    lock_athlete(db, user.id)
    now = datetime.now(UTC)
    if db.scalar(
        select(ReviewJob).where(
            ReviewJob.user_id == user.id, ReviewJob.status == "processing", ReviewJob.lease_until > now
        )
    ):
        return {"job": None}
    expired = db.scalars(
        select(ReviewJob)
        .where(ReviewJob.user_id == user.id, ReviewJob.status == "processing", ReviewJob.lease_until <= now)
        .with_for_update()
    ).all()
    for j in expired:
        j.status = "pending" if j.attempts < POLICY["maximum_attempts"] else "dead_letter"
        j.lease_hash = None
        j.lease_until = None
    db.flush()
    job = db.scalar(
        select(ReviewJob)
        .where(ReviewJob.user_id == user.id, ReviewJob.status == "pending", ReviewJob.available_at <= now)
        .order_by(ReviewJob.created_at)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        db.commit()
        return {"job": None}
    if "training" not in user.data_consent:
        job.status = "completed"
        job.data = {**job.data, "decision": "excluded", "reason": "Training domain not shared with coach"}
        db.commit()
        return {"job": None}
    requested_plan_id = job.data.get("requestPlanId") or job.data.get("planId")
    context = training_context(db, user, requested_plan_id)
    if job.data.get("trigger") == "structural_explanation":
        try:
            validate_explanation_binding(db, user, job)
        except HTTPException as exc:
            job.status = "dead_letter"
            job.data = {**job.data, "failure": "stale_explanation", "failureReason": str(exc.detail)[:1000]}
            db.commit()
            return {"job": None}
        context = {
            "task": "explanation_only",
            "instruction": "Explain the immutable deterministic preview and limitations. Never propose changes. proposedChanges must be empty. Athlete approval is separate.",
            "preview": job.data["summary"],
            "planRevisions": context["planRevisions"],
        }
    # Bounded context at both HTTP and CLI boundaries; preserve oldest job on failure.
    bounded_request = dict(
        version=1, correlationId=str(job.id), idempotencyKey="0" * max(64, len(job.idempotency_key)), context=context
    )
    if (
        len(
            __import__("json")
            .dumps(bounded_request, default=str, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            .encode("utf-8")
        )
        > 32768
    ):
        job.status = "dead_letter"
        job.data = {
            **job.data,
            "failure": "context_too_large",
            "failureReason": "Complete review context exceeds the 32KiB CLI limit. Choose a smaller review scope; no inference or prescription mutation occurred.",
        }
        db.commit()
        return {"job": None}
    lease = secrets.token_urlsafe(32)
    job.status = "processing"
    job.attempts += 1
    job.lease_hash = hashlib.sha256(lease.encode()).hexdigest()
    job.lease_until = now + timedelta(seconds=POLICY["lease_seconds"])
    job.data = {
        **job.data,
        "baselineRevisions": context["planRevisions"],
        "evidenceChecksum": checksum(context),
        "claimedContext": copy.deepcopy(context),
    }
    db.commit()
    return {
        "job": dict(
            id=str(job.id),
            leaseToken=lease,
            request=dict(version=1, correlationId=str(job.id), idempotencyKey=job.idempotency_key, context=context),
        )
    }


class ResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    leaseToken: str = Field(min_length=20, max_length=200)
    result: dict


_FAILURES = {
    "busy",
    "idempotency_conflict",
    "interrupted",
    "budget_exceeded",
    "auth_expired",
    "quota",
    "malformed",
    "policy_rejected",
    "transient",
    "timeout",
    "cancelled",
    "output_limit",
    "capability_violation",
    "termination_failed",
    "evidence_failed",
}


def validate_adaptive_evidence(db, user, job, status_code=422):
    requested_plan_id = job.data.get("requestPlanId") or job.data.get("planId")
    claimed = job.data.get("claimedContext")
    digest = job.data.get("evidenceChecksum")
    if (
        not isinstance(claimed, dict)
        or digest != checksum(claimed)
        or digest != checksum(training_context(db, user, requested_plan_id))
    ):
        raise HTTPException(status_code, "Reviewed evidence changed or is unavailable; request or retry a fresh review")
    return claimed


def validate_adaptive_proposal_evidence(db, user, row):
    job = get_owned(db, ReviewJob, row.job_id, user)
    requested_plan_id = job.data.get("requestPlanId") or job.data.get("planId")
    if (
        job.status != "completed"
        or not row.data.get("reviewedEvidenceChecksum")
        or row.data["reviewedEvidenceChecksum"] != job.data.get("evidenceChecksum")
        or row.data.get("reviewedRequestPlanId") != requested_plan_id
    ):
        raise HTTPException(409, "Reviewed evidence binding is unavailable; request a fresh review")
    return validate_adaptive_evidence(db, user, job, 409)


def _submit_result(job_id: uuid.UUID, body: ResultInput, request: Request, db: DbSession, user: CurrentUser):
    if request.state.token_scope != "coach_worker":
        raise HTTPException(403, "Restricted coach_worker token required")
    lock_athlete(db, user.id)
    job = get_owned(db, ReviewJob, job_id, user)
    now = datetime.now(UTC)
    if (
        job.status != "processing"
        or not job.lease_until
        or job.lease_until <= now
        or not secrets.compare_digest(job.lease_hash or "", hashlib.sha256(body.leaseToken.encode()).hexdigest())
    ):
        raise HTTPException(409, "Lease expired or invalid")
    result = body.result
    status = result.get("status")
    if status not in {"ok"} | _FAILURES:
        raise HTTPException(422, "Unknown worker result status")
    safe = {
        key: result[key]
        for key in ("status", "usage", "eventHash", "diagnosticHash", "exitCode", "exitSignal")
        if key in result
    }
    job.data = {**job.data, "inference": safe, "provider": "authenticated-cli", "promptVersion": "running-coach-v1"}
    if status != "ok":
        retryable = status in {"transient", "timeout", "busy", "interrupted"}
        job.status = "pending" if retryable and job.attempts < POLICY["maximum_attempts"] else "dead_letter"
        job.available_at = now + timedelta(seconds=min(300, 30 * 2**job.attempts))
        job.lease_hash = None
        job.lease_until = None
        db.commit()
        return job_out(job)
    output = result.get("output")
    if (
        not isinstance(output, dict)
        or output.get("status") not in ("monitoring", "propose", "insufficient_evidence")
        or not isinstance(output.get("reason"), str)
        or len(output["reason"]) > 2000
        or not isinstance(output.get("proposedChanges"), list)
        or len(output["proposedChanges"]) > 40
    ):
        raise HTTPException(422, "Malformed proposal output")
    changes = output["proposedChanges"]
    if job.data.get("trigger") == "structural_explanation":
        validate_explanation_binding(db, user, job)
        if changes:
            raise HTTPException(422, "Explanation-only job cannot propose changes")
        job.data = {
            **job.data,
            "explanation": {
                "source": "authenticated-cli",
                "reason": output["reason"],
                "status": output["status"],
                "untrusted_annotation": True,
                "receivedAt": now.isoformat(),
            },
        }
        job.status = "completed"
        job.lease_hash = None
        job.lease_until = None
        db.commit()
        return job_out(job)
    reviewed_context = validate_adaptive_evidence(db, user, job)
    job.data = {**job.data, "decision": output["status"], "reason": output["reason"]}
    if changes:
        if output["status"] != "propose":
            raise HTTPException(422, "Only propose may include changes")
        if training_context(db, user)["assessment"].get("cooldownActive"):
            raise HTTPException(422, "Coaching cooldown active")
        preview, plan = validate_changes(db, user, changes)
        requested_plan_id = job.data.get("requestPlanId") or job.data.get("planId")
        if requested_plan_id is not None and str(plan.id) != str(requested_plan_id):
            raise HTTPException(422, "Candidate belongs to a different plan than the requested review")
        baseline = job.data["baselineRevisions"].get(str(plan.id))
        if baseline != plan.revision:
            raise HTTPException(409, "Plan changed during review")
        require_current_policy(db, user, preview)
        proposal = ReviewProposal(
            id=uuid.uuid4(),
            user_id=user.id,
            job_id=job.id,
            plan_id=plan.id,
            base_revision=plan.revision,
            status="awaiting_approval",
            expires_at=now + timedelta(hours=POLICY["proposal_expiry_hours"]),
            data=dict(
                evidenceSummary={k: v for k, v in reviewed_context["assessment"].items() if k != "metrics"},
                coverage={"history": "recorded_only", "ingestionCompleteness": "unknown"},
                metrics=reviewed_context["metrics"],
                reviewedEvidenceChecksum=job.data["evidenceChecksum"],
                reviewedRequestPlanId=job.data.get("requestPlanId") or job.data.get("planId"),
                clientRequestKeyHash=job.data.get("clientRequestKeyHash"),
                requestType=job.data.get("requestType"),
                requestPlanId=job.data.get("requestPlanId"),
                type="pace" if all(c["field"] == "pace_seconds_per_km" for c in changes) else "volume",
                reason=output["reason"],
                changes=preview,
                policyVersion=POLICY["version"],
                validation=[],
            ),
        )
        db.add(proposal)
    job.status = "completed"
    job.lease_hash = None
    job.lease_until = None
    db.commit()
    return job_out(job)


@router.post("/worker/jobs/{job_id}/result")
def submit_result(job_id: uuid.UUID, body: ResultInput, request: Request, db: DbSession, user: CurrentUser):
    try:
        return _submit_result(job_id, body, request, db, user)
    except HTTPException as exc:
        if exc.status_code not in (409, 422) or exc.detail == "Lease expired or invalid":
            raise
        job = get_owned(db, ReviewJob, job_id, user)
        job.status = "dead_letter"
        job.lease_hash = None
        job.lease_until = None
        job.data = {**job.data, "failure": "policy_rejected", "failureReason": str(exc.detail)[:1000]}
        db.commit()
        return job_out(job)


def work_steps(composition):
    validate_composition(composition)
    if (composition or {}).get("singleGoal") and not (composition or {}).get("blocks"):
        return [({"purpose": "work", "goal": composition["singleGoal"]}, 1)]
    return [
        (step, block.get("iterations", 1))
        for block in (composition or {}).get("blocks", [])
        for step in block.get("steps", [])
        if step.get("purpose") == "work"
    ]


def changed_composition(item, change):
    composition = copy.deepcopy(item.workout_data or {})
    field = change.get("field")
    value = change.get("value")
    if field not in ("pace_seconds_per_km", "distance_meters", "duration_seconds") or not finite_positive(value):
        raise HTTPException(422, "Invalid proposed field/value")
    steps = work_steps(composition)
    selected = []
    if field == "pace_seconds_per_km":
        for step, n in steps:
            alert = step.get("alert") or {}
            if speed_meters_per_second(alert) is not None:
                selected.append((step, n))
        if not selected:
            raise HTTPException(422, "Original work pace targets unavailable")
        old = sum(1000 / (sum(speed_meters_per_second(s["alert"])) / 2) for s, n in selected) / len(selected)
        ratio = old / value
        for step, n in selected:
            step["alert"] = {**step["alert"], "min": step["alert"]["min"] * ratio, "max": step["alert"]["max"] * ratio}
    else:
        goal_type = "distance" if field == "distance_meters" else "time"
        selected = [
            (s, n)
            for s, n in steps
            if (s.get("goal") or {}).get("type") == goal_type and finite_positive((s.get("goal") or {}).get("value"))
        ]
        if not selected:
            raise HTTPException(422, "Original work quantity unavailable")

        def factor(goal):
            factors = {"meters": 1, "kilometers": 1000, "miles": 1609.344, "seconds": 1, "minutes": 60}
            if goal.get("unit") not in factors:
                raise HTTPException(422, "Unsupported goal unit")
            return factors[goal["unit"]]

        old = sum(s["goal"]["value"] * factor(s["goal"]) * n for s, n in selected)
        ratio = value / old
        for step, n in selected:
            step["goal"] = {**step["goal"], "value": step["goal"]["value"] * ratio}
        if composition.get("singleGoal") and not composition.get("blocks"):
            composition["singleGoal"] = selected[0][0]["goal"]
    if abs(value / old - 1) > POLICY["maximum_change_fraction"] + 1e-9:
        raise HTTPException(422, "Change exceeds provisional policy bound")
    validate_composition(composition)
    return composition, old


def validate_changes(db, user, changes):
    now = datetime.now(UTC)
    preview = []
    plan = None
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"workoutId", "field", "value", "reason", "oldValue"}:
            raise HTTPException(422, "Unknown proposal fields")
        try:
            qid = uuid.UUID(change["workoutId"])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "Invalid workout ID")
        key = (qid, change.get("field"))
        if key in seen:
            raise HTTPException(422, "Duplicate proposed change")
        seen.add(key)
        q = get_owned(db, WorkoutQueue, qid, user)
        if (
            q.status in ("completed", "skipped")
            or not q.scheduled_date
            or q.scheduled_date <= now + timedelta(hours=POLICY["freeze_hours"])
        ):
            raise HTTPException(409, "Workout is frozen or retired")
        if not q.plan_id:
            raise HTTPException(422, "Proposal requires an owned running plan")
        p = get_owned(db, Plan, q.plan_id, user)
        if p.activity_type != "running" or p.status not in ("active", "upcoming"):
            raise HTTPException(422, "Plan must be active running")
        if plan and p.id != plan.id:
            raise HTTPException(422, "Proposal must affect one plan")
        plan = p
        _composition, old = changed_composition(q, change)
        if "oldValue" in change and abs(change["oldValue"] - old) > 1e-6:
            raise HTTPException(409, "Original prescription changed")
        if change["field"] != "pace_seconds_per_km" and change["value"] > old:
            metrics = training_context(db, user)["metrics"]
            if metrics["session_count"] < POLICY["minimum_independent_sessions"]:
                raise HTTPException(422, "Volume increase requires established running history")
        preview.append(
            dict(
                workoutId=str(qid),
                field=change["field"],
                oldValue=old,
                value=change["value"],
                reason=str(change.get("reason", ""))[:1000],
            )
        )
    if not plan:
        raise HTTPException(422, "Empty proposal")
    return preview, plan


def require_no_unresolved_restrictions(db, user, status_code=422):
    profile = db.get(AthleteProfile, user.id)
    restrictions = (profile.data if profile else {}).get("restrictions", [])
    if restrictions:
        raise HTTPException(
            status_code,
            {
                "status": "review_required",
                "restrictions": restrictions,
                "reason": "Explicit current restrictions require athlete review before new coaching prescriptions. No clearance or interpretation is inferred.",
            },
        )


def require_current_policy(db, user, preview, status_code=422):
    require_no_unresolved_restrictions(db, user, status_code)
    decision = training_context(db, user)["assessment"]
    if decision.get("cooldownActive"):
        raise HTTPException(status_code, "Coaching cooldown active; request a new review")
    pace = [c for c in preview if c["field"] == "pace_seconds_per_km"]
    if pace:
        direction = decision["paceDirection"]
        if direction is None:
            raise HTTPException(status_code, "Current independent quality evidence does not support pace change")
        if any((c["value"] < c["oldValue"]) != (direction == "faster") for c in pace):
            raise HTTPException(status_code, "Current measured pace direction conflicts with proposal")


def require_initial_calendar(db, user, forecast, timezone, status_code=422):
    """A standalone forecast cannot silently merge or replace committed running work."""
    forecast = require_forecast(forecast, status_code, timezone=timezone)
    zone = ZoneInfo(timezone)
    last_day = max(datetime.fromisoformat(f["scheduledDate"]).astimezone(zone).date() for f in forecast)
    now = datetime.now(UTC)
    commitments = [
        item
        for item in db.scalars(
            select(WorkoutQueue)
            .where(
                WorkoutQueue.user_id == user.id,
                WorkoutQueue.activity_type == "running",
                WorkoutQueue.status.not_in(("completed", "skipped")),
            )
            .order_by(WorkoutQueue.id)
        )
        if item.scheduled_date is None
        or (item.scheduled_date > now and item.scheduled_date.astimezone(zone).date() <= last_day)
    ]
    if commitments:
        raise HTTPException(
            status_code,
            {
                "status": "review_required",
                "forecast": None,
                "proposal": None,
                "reason": "A new standalone program cannot safely reconcile already committed running work. Review the current calendar or explicitly retire/modify commitments first; no existing prescription is replaced or deleted.",
                "committedWorkoutIds": [str(q.id) for q in commitments],
                "calendarReconciled": False,
                "recordedRunningLoad": training_context(db, user)["metrics"],
                "ingestionCompleteness": "unknown",
            },
        )


def validate_initial_evidence(db, user, plan, data):
    from app.coaching_program import _validate

    facts = data.get("initialFacts")
    if not facts or data.get("initialEvidenceDigest") != explanation_evidence(db, user, plan):
        raise HTTPException(409, "Initial ability, availability or recorded evidence changed; preview again")
    try:
        _validate(
            facts["goal"],
            facts["available_days"],
            facts.get("benchmark"),
            date.fromisoformat(facts["start_date"]),
            facts["timezone"],
            reference_at=datetime.now(UTC),
        )
    except HTTPException:
        raise HTTPException(409, "Initial program evidence became stale or unavailable; preview again") from None
    require_initial_calendar(db, user, data.get("forecast", []), facts["timezone"], 409)


def require_initial_lead_time(forecast, status_code=422, timezone="Australia/Brisbane"):
    forecast = require_forecast(forecast, status_code, timezone=timezone)
    if min(datetime.fromisoformat(f["scheduledDate"]) for f in forecast) <= datetime.now(UTC) + timedelta(
        hours=POLICY["freeze_hours"]
    ):
        raise HTTPException(
            status_code,
            "Initial program first session must be more than 24 hours ahead; choose a later start date. Fixed event date is retained.",
        )


class AcceptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedRevision: int = Field(ge=1)


class DismissInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=1000)


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(proposal_id: uuid.UUID, body: AcceptInput, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    row = get_owned(db, ReviewProposal, proposal_id, user)
    if row.status == "applied":
        return row.data["application"]
    if row.status != "awaiting_approval":
        raise HTTPException(409, "Proposal is not awaiting approval")
    if row.expires_at <= datetime.now(UTC):
        row.status = "expired"
        db.commit()
        raise HTTPException(409, "Proposal expired")
    plan = get_owned(db, Plan, row.plan_id, user)
    if row.data.get("requestPlanId") is not None and str(plan.id) != str(row.data["requestPlanId"]):
        row.status = "superseded"
        db.commit()
        raise HTTPException(409, "Proposal does not belong to the requested review plan")
    db.refresh(plan)
    if body.expectedRevision != row.base_revision or plan.revision != row.base_revision:
        row.status = "superseded"
        db.commit()
        raise HTTPException(409, "Plan revision changed; request a new review")
    initial = row.data.get("type") == "initial_program"
    undo = row.data.get("type") == "future_undo"
    rebuild = row.data.get("type") == "program_rebuild"
    structural_changes = []
    if initial or undo or rebuild:
        preview = []
    else:
        preview, current_plan = validate_changes(db, user, row.data["changes"])
        if current_plan.id != row.plan_id:
            row.status = "superseded"
            db.commit()
            raise HTTPException(409, "Proposed workouts moved to a different plan; request a new review")

    try:
        require_no_unresolved_restrictions(db, user, 409)
        if initial:
            validate_initial_evidence(db, user, plan, row.data)
            require_initial_lead_time(
                (plan.metadata_ or {}).get("forecast", []),
                409,
                timezone=(plan.metadata_ or {}).get("timezone", "Australia/Brisbane"),
            )
        elif rebuild:
            validate_rebuild(db, user, plan, row.data)
        elif undo:
            source = get_owned(db, ReviewJob, row.job_id, user)
            validate_original_preview(db, user, plan, source, row)
            structural_changes = validate_forward_undo(db, user, plan, row.data)
        else:
            validate_adaptive_proposal_evidence(db, user, row)
            require_current_policy(db, user, preview, 409)
    except HTTPException:
        row.status = "superseded"
        db.commit()
        raise

    db.info["coaching_apply"] = True
    try:
        if initial:
            from app.coaching_scheduler import publish_forecast

            plan.status = "active"
            plan.metadata_ = {**plan.metadata_, "forecastApproved": True}
            publish_forecast(db, user.id, plan)
        if rebuild:
            apply_rebuild(db, user, plan, row.data)
        if undo:
            apply_forward_undo(db, user, plan, structural_changes)
        final_items = {}
        for change in preview:
            q = get_owned(db, WorkoutQueue, uuid.UUID(change["workoutId"]), user)
            q.workout_data, _ = changed_composition(q, change)
            final_items[q.id] = q
        for q in final_items.values():
            if q.status in ("fetched", "synced"):
                db.add(
                    WorkoutAction(
                        id=uuid.uuid4(),
                        user_id=user.id,
                        workout_id=q.id,
                        action="edit",
                        composition=copy.deepcopy(q.workout_data),
                    )
                )
        db.flush()
        warnings, _ = run_validation(db, user, plan)
        hard = [w for w in warnings if w.get("code") == "guardrail_breach"]
        if hard:
            db.rollback()
            raise HTTPException(422, {"reason": "Plan guardrail breach", "validation": hard})
        result = dict(status="applied", planRevision=plan.revision)
        row.status = "applied"
        row.data = {
            **row.data,
            "validation": warnings,
            "application": result,
            "acceptedAt": datetime.now(UTC).isoformat(),
        }
        others = db.scalars(
            select(ReviewProposal).where(
                ReviewProposal.plan_id == plan.id,
                ReviewProposal.user_id == user.id,
                ReviewProposal.status == "awaiting_approval",
                ReviewProposal.id != row.id,
            )
        ).all()
        for other in others:
            other.status = "superseded"
        db.commit()
        return result
    finally:
        db.info.pop("coaching_apply", None)


@router.post("/proposals/{proposal_id}/dismiss")
def dismiss_proposal(proposal_id: uuid.UUID, body: DismissInput, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    row = get_owned(db, ReviewProposal, proposal_id, user)
    if row.status == "dismissed":
        return {"status": "dismissed"}
    if row.status != "awaiting_approval":
        raise HTTPException(409, "Proposal is not awaiting approval")
    row.status = "dismissed"
    row.data = {**row.data, "dismissalReason": body.reason, "dismissedAt": datetime.now(UTC).isoformat()}
    db.commit()
    return {"status": "dismissed"}


@router.get("/plans/{plan_id}/revisions")
def plan_history(plan_id: uuid.UUID, db: DbSession, user: CurrentUser):
    get_owned(db, Plan, plan_id, user)
    return [
        dict(id=str(r.id), revision=r.revision, snapshot=r.snapshot, created_at=r.created_at)
        for r in db.scalars(
            select(PlanRevision)
            .where(PlanRevision.user_id == user.id, PlanRevision.plan_id == plan_id)
            .order_by(PlanRevision.revision)
        )
    ]


@router.get("/prescriptions/{workout_id}")
def prescription_history(workout_id: uuid.UUID, db: DbSession, user: CurrentUser):
    rows = db.scalars(
        select(PrescriptionRevision)
        .where(PrescriptionRevision.workout_id == workout_id, PrescriptionRevision.user_id == user.id)
        .order_by(PrescriptionRevision.created_at)
    ).all()
    if not rows:
        raise HTTPException(404, "Prescription not found")
    return [dict(id=str(r.id), content_hash=r.content_hash, snapshot=r.snapshot, created_at=r.created_at) for r in rows]


@router.get("/assessments")
def assessments(db: DbSession, user: CurrentUser):
    return [
        dict(workoutId=str(r.workout_id), evidenceHash=r.evidence_hash, **r.data)
        for r in db.scalars(select(ExecutionAssessment).where(ExecutionAssessment.user_id == user.id))
    ]


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: uuid.UUID, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    job = get_owned(db, ReviewJob, job_id, user)
    if job.status != "dead_letter":
        raise HTTPException(409, "Only dead-letter jobs may be retried")
    job.status = "pending"
    job.attempts = 0
    job.available_at = datetime.now(UTC)
    job.lease_hash = None
    job.lease_until = None
    db.commit()
    return job_out(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    job = get_owned(db, ReviewJob, job_id, user)
    if job.status not in ("pending", "processing"):
        raise HTTPException(409, "Job is no longer cancellable")
    job.status = "cancelled"
    job.lease_hash = None
    job.lease_until = None
    db.commit()
    return job_out(job)


class ProgramInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: GoalInput
    available_days: list[str] = Field(min_length=1, max_length=5)
    benchmark: BenchmarkInput | None = None
    start_date: date
    timezone: str = "Australia/Brisbane"
    idempotencyKey: str | None = Field(default=None, max_length=100)


@router.post("/program", status_code=201)
def preview_program(body: ProgramInput, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    require_no_unresolved_restrictions(db, user)
    key = normalize_job_key("program:" + (body.idempotencyKey or checksum(body.model_dump(mode="json"))))
    intent = checksum(body.model_dump(mode="json"))
    existing = db.scalar(select(ReviewJob).where(ReviewJob.user_id == user.id, ReviewJob.idempotency_key == key))
    if existing:
        if existing.data.get("intentChecksum") != intent:
            raise HTTPException(409, "Idempotency key reused for different program intent")
        proposal = db.scalar(
            select(ReviewProposal).where(ReviewProposal.job_id == existing.id, ReviewProposal.user_id == user.id)
        )
        if proposal:
            plan = get_owned(db, Plan, proposal.plan_id, user)
            return {
                "proposal": proposal_out(proposal),
                "plan": dict(id=str(plan.id), forecast=(plan.metadata_ or {}).get("forecast", [])),
                "previewJobId": str(existing.id),
            }
        raise HTTPException(409, "Existing program intent has no recoverable proposal")
    try:
        zone = ZoneInfo(body.timezone)
    except (ValueError, KeyError):
        raise HTTPException(422, "Unknown timezone")
    if body.start_date < datetime.now(zone).date():
        raise HTTPException(422, "Program must start in the future")
    plan_id = uuid.uuid4()
    forecast, end = program_forecast(
        plan_id,
        body.goal.model_dump(mode="json", exclude_none=True),
        body.available_days,
        body.benchmark.model_dump(mode="json", exclude_none=True) if body.benchmark else None,
        body.start_date,
        body.timezone,
    )
    require_initial_lead_time(forecast, timezone=body.timezone)
    require_initial_calendar(db, user, forecast, body.timezone)
    job = enqueue(
        db,
        user.id,
        key,
        dict(
            trigger="program_preview",
            policyVersion=POLICY["version"],
            intentChecksum=intent,
            clientRequestKeyHash=client_request_key_hash(body.idempotencyKey),
            requestType="initial_program",
            requestPlanId=str(plan_id),
        ),
    )
    plan = Plan(
        id=plan_id,
        user_id=user.id,
        name="Running program: " + body.goal.type,
        activity_type="running",
        status="draft",
        start_date=body.start_date,
        end_date=end,
        metadata_=dict(
            goals=[body.goal.model_dump(mode="json", exclude_none=True)],
            forecast=forecast,
            forecastApproved=False,
            programFacts=dict(available_days=body.available_days),
            timezone=body.timezone,
            programPolicyVersion=forecast[0].get("policy") if forecast else None,
            policyVersion=POLICY["version"],
        ),
    )
    db.add(plan)
    db.flush()
    job.status = "completed"
    row = ReviewProposal(
        id=uuid.uuid4(),
        user_id=user.id,
        job_id=job.id,
        plan_id=plan.id,
        base_revision=plan.revision,
        status="awaiting_approval",
        expires_at=datetime.now(UTC) + timedelta(hours=72),
        data=dict(
            programPolicyVersion=forecast[0].get("policy") if forecast else None,
            type="initial_program",
            clientRequestKeyHash=client_request_key_hash(body.idempotencyKey),
            requestType="initial_program",
            requestPlanId=str(plan_id),
            initialFacts=body.model_dump(mode="json", exclude_none=True),
            initialEvidenceDigest=explanation_evidence(db, user, plan),
            reason="Provisional conservative program from explicitly supplied current ability and availability. Review the full forecast before activation.",
            changes=[],
            forecast=forecast,
            validation=[],
            policyVersion=POLICY["version"],
        ),
    )
    db.add(row)
    bind_structural_preview(db, user, plan, job)
    db.commit()
    return {
        "proposal": proposal_out(row),
        "plan": dict(id=str(plan.id), forecast=forecast),
        "previewJobId": str(job.id),
    }


@router.get("/programs/{plan_id}")
def full_program(plan_id: uuid.UUID, db: DbSession, user: CurrentUser):
    plan = get_owned(db, Plan, plan_id, user)
    queued = {
        str(q.id): q
        for q in db.scalars(
            select(WorkoutQueue).where(WorkoutQueue.user_id == user.id, WorkoutQueue.plan_id == plan.id)
        )
    }
    from app.coaching_scheduler import publication_review

    publication = publication_review(db, user.id, plan)
    projection = validated_program_metadata(plan.metadata_)
    validated = validated_forecast((plan.metadata_ or {}).get("forecast"), projection.timezone) if projection else None
    forecasts = [] if validated is not None else None
    for f in validated or []:
        q = queued.get(f["id"])
        forecasts.append({**f, "status": q.status if q else "forecast", "prescription_committed": q is not None})
    return dict(
        id=str(plan.id),
        revision=plan.revision,
        status=plan.status,
        forecast=forecasts,
        forecastApproved=(plan.metadata_ or {}).get("forecastApproved", False),
        publicationReview=publication,
    )


class UndoPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targetRevision: int = Field(ge=1)
    idempotencyKey: str = Field(min_length=1, max_length=200)


@router.post("/plans/{plan_id}/undo-preview", status_code=201)
def preview_forward_undo(plan_id: uuid.UUID, body: UndoPreviewInput, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    require_no_unresolved_restrictions(db, user)
    plan = get_owned(db, Plan, plan_id, user)
    key = normalize_job_key("future-undo:" + str(plan.id) + ":" + body.idempotencyKey)
    intent = checksum(body.model_dump(mode="json"))
    job = db.scalar(select(ReviewJob).where(ReviewJob.user_id == user.id, ReviewJob.idempotency_key == key))
    if job:
        if job.data.get("intentChecksum") != intent:
            raise HTTPException(409, "Idempotency key reused for different undo intent")
        existing = db.scalar(
            select(ReviewProposal).where(ReviewProposal.job_id == job.id, ReviewProposal.user_id == user.id)
        )
        if existing is None:
            raise HTTPException(409, "Undo intent has no recoverable proposal")
        return {"proposal": proposal_out(existing), "previewJobId": str(job.id)}
    data = prepare_forward_undo(db, user, plan, body.targetRevision)
    binding = dict(
        clientRequestKeyHash=client_request_key_hash(body.idempotencyKey),
        requestType="future_undo",
        requestPlanId=str(plan.id),
    )
    data.update(binding)
    job = enqueue(
        db,
        user.id,
        key,
        dict(
            trigger="future_undo_preview",
            intentChecksum=intent,
            decision_source="deterministic_committed_history",
            **binding,
        ),
    )
    job.status = "completed"
    row = ReviewProposal(
        id=uuid.uuid4(),
        user_id=user.id,
        job_id=job.id,
        plan_id=plan.id,
        base_revision=plan.revision,
        status="awaiting_approval",
        expires_at=datetime.now(UTC) + timedelta(hours=72),
        data=data,
    )
    db.add(row)
    bind_structural_preview(db, user, plan, job)
    db.commit()
    return {"proposal": proposal_out(row), "previewJobId": str(job.id)}


class InterruptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    confirmed: bool = False


class CoverageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    complete: bool = False


class RebuildInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotencyKey: str = Field(min_length=1, max_length=200)
    interruption: InterruptionInput
    coverage: CoverageInput


@router.post("/programs/{plan_id}/rebuild", status_code=201)
def preview_rebuild(plan_id: uuid.UUID, body: RebuildInput, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    plan = get_owned(db, Plan, plan_id, user)
    intent = body.model_dump(mode="json")
    for field, flag in (("interruption", "confirmed"), ("coverage", "complete")):
        if intent[field][flag] and (not intent[field]["start_date"] or not intent[field]["end_date"]):
            raise HTTPException(422, "Confirmed interruption/coverage requires both dates")
    key = normalize_job_key("program-rebuild:" + str(plan.id) + ":" + body.idempotencyKey)
    previous = db.scalar(select(ReviewJob).where(ReviewJob.user_id == user.id, ReviewJob.idempotency_key == key))
    if previous:
        if previous.data.get("intentChecksum") != checksum(intent):
            raise HTTPException(409, "Idempotency key reused for different rebuild intent")
        proposal = db.scalar(
            select(ReviewProposal).where(ReviewProposal.user_id == user.id, ReviewProposal.job_id == previous.id)
        )
        return {**previous.data["preview"], "proposal": proposal_out(proposal) if proposal else None}
    result, digest, retired = prepare_rebuild(db, user, plan, intent)
    response = {**result, "proposal": None}
    job = enqueue(
        db,
        user.id,
        key,
        dict(
            trigger="program_rebuild_preview",
            clientRequestKeyHash=client_request_key_hash(body.idempotencyKey),
            requestType="program_rebuild",
            requestPlanId=str(plan.id),
            intentChecksum=checksum(intent),
            decision_source="deterministic_provisional_remaining_program",
            preview=result,
        ),
    )
    job.status = "completed"
    result = {**result, "previewJobId": str(job.id), "planId": str(plan.id)}
    job.data = {**job.data, "preview": result}
    response = {**result, "proposal": None}
    if result["status"] == "forecast_ready":
        protected = sorted({f["id"] for f in result["preserved_forecast"]} - set(result["replacement_session_ids"]))
        data = dict(
            type="program_rebuild",
            clientRequestKeyHash=client_request_key_hash(body.idempotencyKey),
            requestType="program_rebuild",
            requestPlanId=str(plan.id),
            changes=[],
            forecast=result["forecast"],
            structuralChanges=[
                dict(
                    workoutId=identifier,
                    before=next((f for f in result["preserved_forecast"] if f["id"] == identifier), None),
                    after=next((f for f in result["forecast"] if f["id"] == identifier), None),
                )
                for identifier in result["replacement_session_ids"]
            ],
            protectedSessionIds=protected,
            retiredWorkoutIds=retired,
            evidenceDigest=digest,
            intent=intent,
            evidence=result["evidence"],
            limitations=result["limitations"],
            options=result["options"],
            programPolicyVersion=result["policy_version"],
            policyVersion=POLICY["version"],
            reason="Provisional remaining-program preview from owned recorded load and explicit athlete attestations; no CLI explanation has been requested.",
        )
        row = ReviewProposal(
            id=uuid.uuid4(),
            user_id=user.id,
            job_id=job.id,
            plan_id=plan.id,
            base_revision=plan.revision,
            status="awaiting_approval",
            expires_at=datetime.now(UTC) + timedelta(hours=72),
            data=data,
        )
        db.add(row)
        db.flush()
        response["proposal"] = proposal_out(row)
    bind_structural_preview(db, user, plan, job)
    db.commit()
    return response


def explanation_evidence(db, user, plan):
    profile = db.get(AthleteProfile, user.id)
    activities = db.scalars(select(Workout).where(Workout.user_id == user.id).order_by(Workout.id)).all()
    queues = db.scalars(select(WorkoutQueue).where(WorkoutQueue.user_id == user.id).order_by(WorkoutQueue.id)).all()
    feedback = db.scalars(
        select(WorkoutFeedback).where(WorkoutFeedback.user_id == user.id).order_by(WorkoutFeedback.id)
    ).all()
    return checksum(
        dict(
            planRevision=plan.revision,
            sharedDomains=sorted(user.data_consent),
            profile=profile.data if profile else None,
            feedback=[
                dict(
                    id=str(f.id),
                    payloadHash=checksum({c.name: getattr(f, c.name) for c in WorkoutFeedback.__table__.columns}),
                )
                for f in feedback
            ],
            queueState=sorted(
                [
                    dict(id=str(q.id), status=q.status, date=q.scheduled_date, revision=str(q.prescription_revision))
                    for q in queues
                ],
                key=lambda x: x["id"],
            ),
            activities=sorted(
                [
                    dict(
                        id=str(w.id),
                        start=w.start_date,
                        updated=w.updated_at,
                        distance=w.total_distance,
                        duration=w.duration,
                    )
                    for w in activities
                ],
                key=lambda x: x["id"],
            ),
        )
    )


def bind_structural_preview(db, user, plan, source):
    """Bind deterministic facts at creation, before any explanation can refresh them."""
    db.flush()
    captured_at = datetime.now(UTC)
    source.data = {
        **source.data,
        "previewEvidenceBinding": {
            "planId": str(plan.id),
            "baseRevision": plan.revision,
            "evidenceDigest": explanation_evidence(db, user, plan),
            "capturedAt": captured_at.isoformat(),
            "expiresAt": (captured_at + timedelta(hours=72)).isoformat(),
        },
    }


def validate_original_preview(db, user, plan, source, proposal):
    binding = source.data.get("previewEvidenceBinding")
    if (
        not binding
        or binding.get("planId") != str(plan.id)
        or binding.get("baseRevision") != plan.revision
        or datetime.fromisoformat(binding["expiresAt"]) <= datetime.now(UTC)
        or binding.get("evidenceDigest") != explanation_evidence(db, user, plan)
    ):
        raise HTTPException(409, "Original preview evidence changed or expired; request a fresh preview")
    if proposal:
        if proposal.data.get("type") == "initial_program":
            validate_initial_evidence(db, user, plan, proposal.data)
        elif proposal.data.get("type") == "future_undo":
            validate_forward_undo(db, user, plan, proposal.data)
        elif proposal.data.get("type") == "program_rebuild":
            validate_rebuild(db, user, plan, proposal.data)


def validate_explanation_binding(db, user, job):
    binding = job.data["binding"]
    plan = get_owned(db, Plan, uuid.UUID(binding["planId"]), user)
    source = get_owned(db, ReviewJob, uuid.UUID(binding["previewJobId"]), user)
    proposal = db.scalar(
        select(ReviewProposal).where(ReviewProposal.user_id == user.id, ReviewProposal.job_id == source.id)
    )
    validate_original_preview(db, user, plan, source, proposal)
    if datetime.fromisoformat(binding["expiresAt"]) <= datetime.now(UTC):
        raise HTTPException(409, "Explanation preview expired")
    if plan.revision != binding["baseRevision"] or explanation_evidence(db, user, plan) != binding["evidenceDigest"]:
        raise HTTPException(409, "Explanation evidence changed; request a fresh preview")
    if checksum(source.data) != binding["previewChecksum"] or (
        proposal and (proposal.status != "awaiting_approval" or checksum(proposal.data) != binding["proposalChecksum"])
    ):
        raise HTTPException(409, "Explanation preview is no longer current")


class ExplanationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotencyKey: str = Field(min_length=1, max_length=200)


@router.post("/jobs/{preview_id}/explanation", status_code=201)
def request_explanation(preview_id: uuid.UUID, body: ExplanationInput, db: DbSession, user: CurrentUser):
    lock_athlete(db, user.id)
    source = get_owned(db, ReviewJob, preview_id, user)
    if source.status != "completed" or source.data.get("trigger") not in {
        "program_preview",
        "program_rebuild_preview",
        "future_undo_preview",
    }:
        raise HTTPException(422, "A completed deterministic structural preview is required")
    proposal = db.scalar(
        select(ReviewProposal).where(ReviewProposal.user_id == user.id, ReviewProposal.job_id == source.id)
    )
    plan_id = (
        proposal.plan_id
        if proposal
        else uuid.UUID(source.data.get("preview", {}).get("planId", "00000000-0000-0000-0000-000000000000"))
    )
    plan = get_owned(db, Plan, plan_id, user)
    if proposal and (proposal.status != "awaiting_approval" or proposal.base_revision != plan.revision):
        raise HTTPException(409, "Preview is no longer awaiting approval at the current revision")
    validate_original_preview(db, user, plan, source, proposal)
    key = normalize_job_key("structural-explanation:" + str(source.id) + ":" + body.idempotencyKey)
    existing = db.scalar(select(ReviewJob).where(ReviewJob.user_id == user.id, ReviewJob.idempotency_key == key))
    if existing:
        return job_out(existing)
    preview = proposal.data if proposal else source.data["preview"]
    projection = require_program_metadata(plan.metadata_, 409, require_goal=preview.get("type") != "future_undo")
    full_forecast = preview.get("forecast") or preview.get("preserved_forecast") or []
    if full_forecast:
        full_forecast = require_forecast(
            full_forecast, calendar_dates=True, timezone=(plan.metadata_ or {}).get("timezone", "Australia/Brisbane")
        )
    local_day = datetime.now(ZoneInfo((plan.metadata_ or {}).get("timezone", "Australia/Brisbane"))).date().isoformat()
    protected_ids = set(preview.get("protectedSessionIds", []))
    preview_evidence = preview.get("evidence", {})
    terminal_ids = set(preview_evidence.get("retiredSessionIds", []))
    calendar_reconciled = not (
        preview_evidence.get("unrepresentedCommittedWorkoutIds")
        or preview_evidence.get("protectedPrescriptionMismatches")
    )
    compact_remaining = [
        dict(
            id=f["id"],
            date=f["date"],
            scheduledDate=f["scheduledDate"],
            title=f["title"],
            phase=f.get("phase"),
            sessionType=f.get("session_type"),
            dose=f.get("dose"),
            protected=f["id"] in protected_ids,
            provenancePolicy=f.get("policy"),
        )
        for f in full_forecast
        if f["date"] >= local_day and f["id"] not in terminal_ids
    ]
    summary = dict(
        plannedNotRecorded=True,
        goalTargetIsAspiration=True,
        remainingCalendar=compact_remaining,
        remainingCalendarComplete=calendar_reconciled,
        remainingCalendarScope="reconciled_outstanding_forecast"
        if calendar_reconciled
        else "unreconciled_original_forecast_requires_review",
        retiredSessionIds=sorted(terminal_ids),
        retiredExcludedCount=sum(f["date"] >= local_day and f["id"] in terminal_ids for f in full_forecast),
        fullForecastSessionCount=len(full_forecast),
        pastExcludedCount=sum(f["date"] < local_day for f in full_forecast),
        localCalendarCutoff=local_day,
        availability=projection.facts.get("available_days", []),
        targetSummary=projection.goals,
        unknowns=[
            "No physiological readiness or execution completion inferred from planned dose.",
            "Absent recorded activity does not establish complete ingestion or a verified interruption.",
        ],
        type=preview.get("type", "program_rebuild"),
        status=source.data.get("preview", {}).get("status", "awaiting_approval"),
        reason=preview.get("reason"),
        limitations=preview.get("limitations", []),
        options=preview.get("options", []),
        evidence=preview.get("evidence", {}),
        policyVersion=preview.get("programPolicyVersion")
        or preview.get("policy_version")
        or preview.get("policyVersion"),
        proposedSessionCount=len(preview.get("forecast") or []),
        protectedSessionCount=len(preview.get("protectedSessionIds", [])),
        eventDate=projection.goals[0].get("race_date") if projection.goals else None,
    )
    if preview.get("type") == "future_undo":
        summary.update(undo_explanation_context(db, user, plan, preview))
    job = enqueue(
        db,
        user.id,
        key,
        dict(
            trigger="structural_explanation",
            clientRequestKeyHash=client_request_key_hash(body.idempotencyKey),
            requestType="structural_explanation",
            requestPlanId=str(plan.id),
            summary=summary,
            binding=dict(
                planId=str(plan.id),
                previewJobId=str(source.id),
                baseRevision=plan.revision,
                previewChecksum=checksum(source.data),
                proposalChecksum=checksum(proposal.data) if proposal else None,
                evidenceDigest=explanation_evidence(db, user, plan),
                expiresAt=(datetime.now(UTC) + timedelta(hours=72)).isoformat(),
            ),
        ),
    )
    db.commit()
    return job_out(job)
