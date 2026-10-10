"""Owned device receipts and semantic transitions in the caller's transaction.

All mutations lock the athlete before reading receipt/member/workout state.
No network, implicit commit, hard deletion, or inference is performed here.
"""
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.activity_observation import canonical_workout, digest, instant, observation_projection, query_exhaustion, semantic_inventory, semantic_observation
from app.coaching_policy import POLICY
from app.coaching_service import enqueue, lock_athlete
from app.models.activity_observation import ActivityObservation, ActivityObservationMember
from app.models.queue import WorkoutQueue
from app.models.user import User
from app.models.workout import Workout


def device_actor(request, user):
    if user.role != "user" or request.state.token_scope != "device":
        raise HTTPException(403, "Athlete device token required")
    return request.state.token_id


def owned_receipt(db, user, observation_id, token_id=None, mutable=False):
    query = select(ActivityObservation).where(ActivityObservation.id == observation_id,
                                             ActivityObservation.user_id == user.id)
    receipt = db.scalar(query.with_for_update() if mutable else query)
    if receipt is None or token_id is not None and receipt.token_id != token_id:
        raise HTTPException(404, "Observation not found")
    return receipt


def require_current(user, receipt):
    if (user.activity_observation_state or {}).get("currentReceiptId") != str(receipt.id):
        raise HTTPException(409, "Pending observation was superseded; begin a fresh scan")


def members(db, receipt):
    return db.scalars(select(ActivityObservationMember).where(
        ActivityObservationMember.observation_id == receipt.id).order_by(ActivityObservationMember.source_id)).all()


def declaration(member):
    return dict(sourceId=str(member.source_id), payloadDigest=member.payload_digest,
                disposition="pending" if member.disposition == "acknowledged" else member.disposition,
                aliasOf=str(member.alias_of) if member.alias_of else None,
                explicitSourceDeletion=member.disposition == "source_withdrawn")


def manifest_digest(rows):
    return digest(sorted([declaration(row) for row in rows], key=lambda value: value["sourceId"]))


def begin(db, user, token_id, payload, now=None):
    now = now or datetime.now(UTC)
    lock_athlete(db, user.id)
    body = payload.model_dump(mode="json", by_alias=True)
    # Normalize offsets to a single wire representation before idempotency checks.
    body["rangeStart"], body["rangeEnd"] = instant(payload.range_start).isoformat(), instant(payload.range_end).isoformat()
    if instant(payload.range_end) > now:
        raise HTTPException(422, "Observation cannot cover the future")
    prior = db.get(ActivityObservation, payload.observation_id)
    if prior:
        if prior.user_id != user.id or prior.token_id != token_id:
            raise HTTPException(404, "Observation not found")
        if prior.data["declaration"] != body:
            raise HTTPException(409, "Observation declaration is immutable")
        return prior
    latest = db.scalar(select(ActivityObservation).where(ActivityObservation.user_id == user.id,
                       ActivityObservation.token_id == token_id).order_by(ActivityObservation.inventory_revision.desc()).limit(1))
    if latest and payload.inventory_revision <= latest.inventory_revision:
        raise HTTPException(409, "Inventory revision must advance for this device identity")
    receipt = ActivityObservation(id=payload.observation_id, user_id=user.id, token_id=token_id,
                  inventory_revision=payload.inventory_revision, status="pending", received_at=now,
                  data={"declaration": body, "pages": {}, "query": None})
    db.add(receipt)
    state = dict(user.activity_observation_state or {})
    # New pending observation immediately withdraws stale completeness claims;
    # its terminal semantic transition, not each page/ACK, schedules the review.
    state["currentReceiptId"] = str(receipt.id)
    state["currentObservationStatus"] = "pending"
    user.activity_observation_state = state
    return receipt


def add_page(db, user, token_id, observation_id, page, payload):
    lock_athlete(db, user.id)
    receipt = owned_receipt(db, user, observation_id, token_id, mutable=True)
    body = [m.model_dump(mode="json", by_alias=True) for m in payload.members]
    page_hash = digest(sorted(body, key=lambda value: value["sourceId"]))
    prior = receipt.data["pages"].get(str(page))
    if prior:
        if prior != page_hash:
            raise HTTPException(409, "Manifest page is immutable")
        return receipt
    require_current(user, receipt)
    if receipt.status != "pending" or receipt.data["query"] is not None:
        raise HTTPException(409, "Manifest is sealed")
    existing = {m.source_id for m in members(db, receipt)}
    if existing.intersection(m.source_id for m in payload.members):
        raise HTTPException(409, "Source id already registered on another page")
    for value in payload.members:
        if value.disposition == "source_withdrawn":
            workout = db.get(Workout, value.source_id)
            if workout is None or workout.user_id != user.id:
                raise HTTPException(404, "Withdrawn activity not found")
        db.add(ActivityObservationMember(observation_id=receipt.id, source_id=value.source_id, page=page,
               payload_digest=value.payload_digest, disposition=value.disposition, alias_of=value.alias_of))
    receipt.data = {**receipt.data, "pages": {**receipt.data["pages"], str(page): page_hash}}
    return receipt


def record_query(db, user, token_id, observation_id, payload):
    lock_athlete(db, user.id)
    receipt = owned_receipt(db, user, observation_id, token_id, mutable=True)
    body = payload.model_dump(mode="json", by_alias=True)
    if receipt.data["query"] is not None:
        if receipt.data["query"] != body:
            raise HTTPException(409, "Query outcome is immutable")
        return receipt
    if receipt.status != "pending":
        raise HTTPException(409, "Observation is terminal")
    require_current(user, receipt)
    rows = members(db, receipt)
    if sum(m.disposition != "source_withdrawn" for m in rows) != payload.observed_count or manifest_digest(rows) != payload.manifest_digest:
        raise HTTPException(409, "Manifest count or digest mismatch")
    if receipt.data["declaration"]["queryMethod"] == "sample_capped200" and payload.observed_count > 200:
        raise HTTPException(422, "Capped query cannot enumerate more than 200")
    # An alias requires an actual imported member, never a missing/cyclic alias.
    by_id = {m.source_id: m for m in rows}
    for member in rows:
        if member.disposition == "duplicate_source_not_uploaded":
            target = by_id.get(member.alias_of)
            if target is None or target.disposition not in {"pending", "acknowledged"}:
                raise HTTPException(409, "Duplicate source requires a registered imported target")
    receipt.data = {**receipt.data, "query": body}
    return receipt


def upload_member(db, user, token_id, binding, workout_id, expected_hash):
    lock_athlete(db, user.id)
    receipt = owned_receipt(db, user, binding.observation_id, token_id, mutable=True)
    require_current(user, receipt)
    member = db.get(ActivityObservationMember, (receipt.id, binding.source_id))
    if member is None or member.source_id != workout_id:
        raise HTTPException(404, "Observation member not found")
    if receipt.status != "pending" or member.disposition not in {"pending", "acknowledged"}:
        raise HTTPException(409, "Observation cannot accept this upload")
    if member.payload_digest != binding.payload_digest or binding.payload_digest != expected_hash:
        raise HTTPException(409, "Upload differs from registered canonical payload")
    return member


def acknowledge(db, user, token_id, observation_id, source_id, payload, now=None):
    """Bind already stored content without rewriting a workout or its arrival stamp."""
    lock_athlete(db, user.id)
    receipt = owned_receipt(db, user, observation_id, token_id, mutable=True)
    member = db.get(ActivityObservationMember, (receipt.id, source_id))
    workout = db.get(Workout, source_id)
    if member is None or workout is None or workout.user_id != user.id:
        raise HTTPException(404, "Observation activity not found")
    if member.disposition not in {"pending", "acknowledged"}:
        raise HTTPException(409, "Member cannot be acknowledged")
    current_hash = digest(canonical_workout(workout))
    if payload.server_payload_digest != current_hash or workout.source_withdrawn:
        raise HTTPException(409, "Stored activity differs from observation")
    if receipt.status != "pending":
        if (member.disposition == "acknowledged" and member.workout_revision == workout.source_evidence_revision
            and member.server_payload_digest == current_hash and payload.workout_revision == workout.source_evidence_revision):
            return receipt
        raise HTTPException(409, "Observation is terminal")
    require_current(user, receipt)
    if workout.source_evidence_hash is None:
        # Additive lazy legacy provenance. Existing activity job keys remain unchanged.
        workout.source_evidence_hash = current_hash
        workout.source_evidence_revision = (workout.source_evidence_revision or 0)+1
        db.flush()
    if workout.source_evidence_hash != current_hash or payload.workout_revision != workout.source_evidence_revision:
        raise HTTPException(409, "Stored activity revision is not reconciled")
    member.disposition, member.workout_revision = "acknowledged", workout.source_evidence_revision
    member.server_payload_digest = current_hash
    member.acknowledged_at = now or datetime.now(UTC)
    return receipt


def snapshot(db, receipt):
    if receipt is None:
        return None
    body = receipt.data["declaration"]
    rows = members(db, receipt)
    query = receipt.data.get("query") or {}
    counts = {name: sum(m.disposition == state for m in rows) for name, state in
              (("acknowledgedCount", "acknowledged"), ("pendingCount", "pending"),
               ("failedCount", "extraction_failed"), ("duplicateCount", "duplicate_source_not_uploaded"),
               ("withdrawnCount", "source_withdrawn"))}
    invalidated = 0
    for member in rows:
        if member.disposition != "acknowledged":
            continue
        workout = db.get(Workout, member.source_id)
        if (workout is None or workout.user_id != receipt.user_id or workout.source_withdrawn
            or workout.source_evidence_hash != member.server_payload_digest
            or workout.source_evidence_revision != member.workout_revision
            or digest(canonical_workout(workout)) != member.server_payload_digest):
            invalidated += 1
    return {**body, **counts, "invalidatedCount": invalidated, "status": receipt.status, "queryStatus": query.get("status", "pending"),
            "observedCount": query.get("observedCount"), "exhaustion": query_exhaustion(body["queryMethod"], query.get("status"), query.get("observedCount", 0)),
            "finalizedAt": receipt.finalized_at.isoformat() if receipt.finalized_at else None,
            "lastSuccessfulReconciliationAt": receipt.data.get("lastSuccessfulReconciliationAt"),
            "contentDigest": semantic_inventory([{**declaration(m), "serverPayloadDigest": m.server_payload_digest, "revision": m.workout_revision} for m in rows])}


def current_snapshot(db, user):
    state = user.activity_observation_state or {}
    receipt_id = state.get("currentReceiptId")
    receipt = db.get(ActivityObservation, uuid.UUID(receipt_id)) if receipt_id else None
    if receipt is not None and receipt.user_id != user.id:
        raise HTTPException(409, "Invalid owned observation pointer")
    value = snapshot(db, receipt)
    if value is not None:
        value["lastSuccessfulReconciliationAt"] = state.get("lastSuccessfulReconciliationAt")
    return value


def semantic_context(db, user, now, queues=None):
    queues = queues if queues is not None else db.scalars(select(WorkoutQueue).where(WorkoutQueue.user_id == user.id)).all()
    horizon = [q.scheduled_date for q in queues if q.scheduled_date is not None
               and now-timedelta(days=POLICY["lookback_days"]) <= q.scheduled_date <= now]
    return semantic_observation(current_snapshot(db, user), now, horizon)


def record_transition(db, user, now, queues=None):
    effective = semantic_context(db, user, now, queues)
    key = digest(effective)
    state = dict(user.activity_observation_state or {})
    if state.get("semanticDigest") == key:
        return False
    sequence = state.get("transitionSequence", 0)+1
    state.update(semanticDigest=key, transitionSequence=sequence)
    user.activity_observation_state = state
    if "training" in user.data_consent:
        enqueue(db, user.id, f"observation:{sequence}:{key}",
                dict(trigger="device_observation_changed", semanticDigest=key, observation=effective,
                     policyVersion=POLICY["version"]))
    return True


def finalize(db, user, token_id, observation_id, now=None):
    now = now or datetime.now(UTC)
    lock_athlete(db, user.id)
    receipt = owned_receipt(db, user, observation_id, token_id, mutable=True)
    if receipt.status != "pending":
        return receipt
    require_current(user, receipt)
    query = receipt.data.get("query")
    if query is None:
        raise HTTPException(409, "Query outcome required")
    rows = members(db, receipt)
    if sum(m.disposition != "source_withdrawn" for m in rows) != query["observedCount"] or manifest_digest(rows) != query["manifestDigest"]:
        raise HTTPException(409, "Manifest no longer matches sealed query")
    if any(m.disposition == "pending" for m in rows):
        raise HTTPException(409, "Member acknowledgements are pending")
    for member in rows:
        if member.disposition == "acknowledged":
            workout = db.get(Workout, member.source_id)
            if (workout is None or workout.user_id != user.id or workout.source_withdrawn
                or workout.source_evidence_hash != member.server_payload_digest
                or digest(canonical_workout(workout)) != member.server_payload_digest
                or workout.source_evidence_revision != member.workout_revision):
                raise HTTPException(409, "Acknowledged activity changed before finalization")
        elif member.disposition == "source_withdrawn":
            workout = db.get(Workout, member.source_id)
            if workout is None or workout.user_id != user.id:
                raise HTTPException(404, "Withdrawn activity not found")
            # Explicit source-reported uncertainty, never removal of actual recorded load.
            workout.source_withdrawn = True
    receipt.status = "failed" if query["status"] == "failed" or any(m.disposition == "extraction_failed" for m in rows) else "finalized"
    receipt.finalized_at = now
    db.flush()
    state = dict(user.activity_observation_state or {})
    if state.get("currentReceiptId") == str(receipt.id):
        state["currentObservationStatus"] = receipt.status
        user.activity_observation_state = state
        value = snapshot(db, receipt)
        if observation_projection(value, now)["sourceCoverage"] == "within_window_observed":
            # Receipt-local immutable audit proof for the device checkpoint. Never
            # borrow another receipt's historical success for a partial/empty scan.
            receipt.data = {**receipt.data, "lastSuccessfulReconciliationAt": now.isoformat()}
            state["lastSuccessfulReconciliationAt"] = now.isoformat()
            user.activity_observation_state = state
        record_transition(db, user, now)
    return receipt


def scan_staleness(db, user, now):
    if user.activity_observation_state is None:
        return False
    lock_athlete(db, user.id)
    receipt_id = (user.activity_observation_state or {}).get("currentReceiptId")
    receipt = db.get(ActivityObservation, uuid.UUID(receipt_id)) if receipt_id else None
    if receipt is None or receipt.status == "pending":
        return False
    return record_transition(db, user, now)
