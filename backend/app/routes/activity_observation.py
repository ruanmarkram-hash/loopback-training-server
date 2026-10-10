"""Athlete-device-only activity query receipts. Never mutate scheduled inventory."""
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Path, Request

from app import activity_observation_service as service
from app.activity_observation import observation_projection
from app.auth import CurrentUser
from app.database import DbSession
from app.schemas.activity_observation import ExistingActivityAck, ManifestPage, ObservationBegin, QueryOutcome

router = APIRouter()


def response(db, receipt):
    # Exact owned receipt is for its device, not projected to the analyst/admin.
    return {"observationId": str(receipt.id), "inventoryRevision": receipt.inventory_revision,
            **observation_projection(service.snapshot(db, receipt), datetime.now(UTC))}


@router.post("", status_code=201)
@router.post("/", status_code=201, include_in_schema=False)
def begin(payload: ObservationBegin, request: Request, db: DbSession, user: CurrentUser):
    receipt = service.begin(db, user, service.device_actor(request, user), payload)
    db.commit()
    return response(db, receipt)


@router.get("/{observation_id}")
@router.get("/{observation_id}/", include_in_schema=False)
def get(observation_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    token_id = service.device_actor(request, user)
    return response(db, service.owned_receipt(db, user, observation_id, token_id))


@router.put("/{observation_id}/manifest/{page}")
@router.put("/{observation_id}/manifest/{page}/", include_in_schema=False)
def page(observation_id: uuid.UUID, payload: ManifestPage, request: Request, db: DbSession,
         user: CurrentUser, page: int = Path(ge=0, le=99)):
    receipt = service.add_page(db, user, service.device_actor(request, user), observation_id, page, payload)
    db.commit()
    return response(db, receipt)


@router.put("/{observation_id}/query")
@router.put("/{observation_id}/query/", include_in_schema=False)
def query(observation_id: uuid.UUID, payload: QueryOutcome, request: Request, db: DbSession, user: CurrentUser):
    receipt = service.record_query(db, user, service.device_actor(request, user), observation_id, payload)
    db.commit()
    return response(db, receipt)


@router.post("/{observation_id}/members/{source_id}/ack")
@router.post("/{observation_id}/members/{source_id}/ack/", include_in_schema=False)
def ack(observation_id: uuid.UUID, source_id: uuid.UUID, payload: ExistingActivityAck, request: Request, db: DbSession, user: CurrentUser):
    receipt = service.acknowledge(db, user, service.device_actor(request, user), observation_id, source_id, payload)
    db.commit()
    member = db.get(service.ActivityObservationMember, (receipt.id, source_id))
    return {**response(db, receipt), "memberAck": {"sourceId": str(source_id),
            "payloadDigest": member.payload_digest, "serverPayloadDigest": member.server_payload_digest, "workoutRevision": member.workout_revision,
            "acknowledgedAt": member.acknowledged_at.isoformat()}}


@router.post("/{observation_id}/finalize")
@router.post("/{observation_id}/finalize/", include_in_schema=False)
def finalize(observation_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser):
    receipt = service.finalize(db, user, service.device_actor(request, user), observation_id)
    db.commit()
    return response(db, receipt)
