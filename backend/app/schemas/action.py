import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    workout_id: uuid.UUID = Field(alias="workoutId")
    action: Literal["edit", "delete"]
    composition: dict | None = None


class ActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    workout_id: uuid.UUID = Field(serialization_alias="workoutId")
    action: str
    composition: dict | None
    base_prescription_revision: uuid.UUID | None = Field(default=None,serialization_alias="basePrescriptionRevision")
    desired_prescription_revision: uuid.UUID | None = Field(default=None,alias="prescriptionRevision")
    created_at: datetime
