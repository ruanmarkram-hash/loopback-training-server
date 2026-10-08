import uuid
from datetime import date, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class InventoryDate(BaseModel):
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)

    @model_validator(mode="after")
    def valid_calendar_date(self):
        if self.year is not None and self.month is not None and self.day is not None:
            date(self.year, self.month, self.day)
        return self


class ObservedDevice(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    device_plan_id: uuid.UUID = Field(alias="devicePlanId")
    prescription_revision: uuid.UUID | None = Field(default=None, alias="prescriptionRevision")
    content_hash: str | None = Field(default=None, alias="contentHash", pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime | None = Field(default=None, alias="observedAt")
    date: InventoryDate
    complete: bool = False


class InventoryItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    logical_workout_id: uuid.UUID | None = Field(default=None, alias="logicalWorkoutId")
    device_plan_id: uuid.UUID | None = Field(default=None, alias="devicePlanId")
    display_name: str = Field(alias="displayName")
    date: InventoryDate
    complete: bool = False
    observed_devices: list[ObservedDevice] = Field(default_factory=list, alias="observedDevices", max_length=10)
    prescription_revision: uuid.UUID | None = Field(default=None, alias="prescriptionRevision")
    content_hash: str | None = Field(default=None, alias="contentHash", min_length=64, max_length=64)
    observed_at: AwareDatetime | None = Field(default=None, alias="observedAt")


class InventoryItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    year: int | None
    month: int | None
    day: int | None
    hour: int | None
    minute: int | None
    complete: bool
    logical_workout_id: uuid.UUID | None = None
    device_plan_id: uuid.UUID | None = None
    prescription_revision: uuid.UUID | None = None
    content_hash: str | None = None
    observed_at: datetime | None = None
    observed_devices: list[ObservedDevice] = Field(default_factory=list)
    ambiguous: bool = False
    synced_at: datetime
