from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select

from app.auth import CurrentUser
from app.database import DbSession
from app.models.coaching import PrescriptionRevision
from app.models.inventory import WorkoutInventory
from app.models.queue import WorkoutQueue
from app.schemas.inventory import InventoryDate, InventoryItem, InventoryItemRead

router = APIRouter()


@router.put("")
def sync_inventory(items: list[InventoryItem], db: DbSession, user: CurrentUser):
    """Replace this user's stored inventory with their full on-device snapshot."""
    incoming_ids = {item.id for item in items}
    # One timestamp for the whole snapshot: synced_at means "last time the
    # device reported this item", so it must move on update too — an in-place
    # `complete` flip with a stale synced_at made the snapshot look older
    # than the data it carried.
    now = datetime.now(UTC)

    # Delete THIS USER's items no longer on device (never touch other users' rows).
    db.execute(
        delete(WorkoutInventory).where(
            WorkoutInventory.user_id == user.id,
            WorkoutInventory.id.notin_(incoming_ids),
        )
    )

    # Upsert each item
    for item in items:
        known = db.get(WorkoutQueue, item.id)
        if known and known.user_id != user.id:
            raise HTTPException(422, "Logical workout is not owned")
        device_ids = [x.device_plan_id for x in item.observed_devices]
        if len(device_ids) != len(set(device_ids)):
            raise HTTPException(422, "Duplicate device observations")
        for observed in item.observed_devices:
            device_revision = db.get(PrescriptionRevision, observed.device_plan_id)
            if device_revision and (device_revision.user_id != user.id or device_revision.workout_id != item.id):
                raise HTTPException(422, "Device observation is not owned")
            if observed.prescription_revision:
                revision = db.get(PrescriptionRevision, observed.prescription_revision)
                if (
                    not revision
                    or revision.user_id != user.id
                    or revision.workout_id != item.id
                    or revision.id != observed.device_plan_id
                    or revision.content_hash != observed.content_hash
                    or observed.observed_at is None
                ):
                    raise HTTPException(422, "Device observation does not match owned revision")
            elif observed.content_hash:
                raise HTTPException(422, "Unknown device revision cannot assert a content hash")
        if item.observed_devices:
            if len(item.observed_devices) > 1:
                if any(
                    x is not None
                    for x in (item.device_plan_id, item.prescription_revision, item.content_hash, item.observed_at)
                ):
                    raise HTTPException(422, "Ambiguous inventory cannot assert a single prescription")
                item.date = InventoryDate()
            else:
                observed = item.observed_devices[0]
                if any(
                    a is not None and a != b
                    for a, b in [
                        (item.device_plan_id, observed.device_plan_id),
                        (item.prescription_revision, observed.prescription_revision),
                        (item.content_hash, observed.content_hash),
                        (item.observed_at, observed.observed_at if observed.prescription_revision else None),
                    ]
                ):
                    raise HTTPException(422, "Flat evidence differs from device observation")
                item.device_plan_id = observed.device_plan_id
                item.prescription_revision = observed.prescription_revision
                item.content_hash = observed.content_hash
                item.observed_at = observed.observed_at if observed.prescription_revision else None
                item.date = observed.date
            item.complete = all(x.complete for x in item.observed_devices)
        if item.logical_workout_id not in (None, item.id):
            raise HTTPException(422, "Logical workout identity mismatch")
        if not item.observed_devices and item.device_plan_id not in (None, item.id, item.prescription_revision):
            raise HTTPException(422, "Device plan identity mismatch")
        if item.prescription_revision:
            revision = db.get(PrescriptionRevision, item.prescription_revision)
            if (
                not revision
                or revision.user_id != user.id
                or revision.workout_id != item.id
                or revision.content_hash != item.content_hash
                or item.observed_at is None
            ):
                raise HTTPException(422, "Observed prescription evidence does not match owned revision")
        elif item.content_hash or item.observed_at:
            raise HTTPException(422, "Observed evidence requires prescription revision")
        existing = db.get(WorkoutInventory, item.id)
        if existing and existing.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Inventory id belongs to another user")
        if existing:
            existing.display_name = item.display_name
            existing.year = item.date.year
            existing.month = item.date.month
            existing.day = item.date.day
            existing.hour = item.date.hour
            existing.minute = item.date.minute
            existing.complete = item.complete
            existing.synced_at = now
            existing.logical_workout_id = item.logical_workout_id
            existing.device_plan_id = item.device_plan_id
            existing.prescription_revision = item.prescription_revision
            existing.content_hash = item.content_hash
            existing.observed_at = item.observed_at
            existing.observed_devices = [x.model_dump(mode="json", by_alias=True) for x in item.observed_devices]
        else:
            db.add(
                WorkoutInventory(
                    id=item.id,
                    user_id=user.id,
                    display_name=item.display_name,
                    year=item.date.year,
                    month=item.date.month,
                    day=item.date.day,
                    hour=item.date.hour,
                    minute=item.date.minute,
                    complete=item.complete,
                    synced_at=now,
                    logical_workout_id=item.logical_workout_id,
                    device_plan_id=item.device_plan_id,
                    prescription_revision=item.prescription_revision,
                    content_hash=item.content_hash,
                    observed_at=item.observed_at,
                    observed_devices=[x.model_dump(mode="json", by_alias=True) for x in item.observed_devices],
                )
            )

    db.commit()
    return {"ok": True, "count": len(items)}


@router.get("", response_model=list[InventoryItemRead])
def get_inventory(db: DbSession, user: CurrentUser):
    """Return the current on-device workout inventory."""
    q = (
        select(WorkoutInventory)
        .where(WorkoutInventory.user_id == user.id)
        .order_by(
            WorkoutInventory.year,
            WorkoutInventory.month,
            WorkoutInventory.day,
            WorkoutInventory.hour,
            WorkoutInventory.minute,
        )
    )
    return db.scalars(q).all()
