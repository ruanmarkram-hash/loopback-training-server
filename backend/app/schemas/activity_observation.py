"""Device-reported enumeration facts; acknowledgement and coverage are server-derived."""
import uuid
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.activity_observation import MAX_MEMBERS



class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ObservationBegin(Input):
    observation_id: uuid.UUID = Field(alias="observationId")
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    producer: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._/-]+$")
    source: Literal["healthkit"]
    inventory_revision: int = Field(alias="inventoryRevision", ge=1, le=2**53-1, strict=True)
    range_start: AwareDatetime = Field(alias="rangeStart")
    range_end: AwareDatetime = Field(alias="rangeEnd")
    timezone: str = Field(max_length=100)
    boundary: Literal["strict_start_date"]
    query_method: Literal["sample_no_limit", "sample_capped200"] = Field(alias="queryMethod")

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, KeyError) as exc:
            raise ValueError("Unknown timezone") from exc
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if not self.range_start < self.range_end or self.range_end-self.range_start > timedelta(days=190):
            raise ValueError("Observation range must be ordered and at most six calendar months")
        return self


class ObservationMember(Input):
    source_id: uuid.UUID = Field(alias="sourceId")
    payload_digest: str | None = Field(default=None, alias="payloadDigest", pattern=r"^[a-f0-9]{64}$")
    disposition: Literal["pending", "extraction_failed", "duplicate_source_not_uploaded", "source_withdrawn"] = "pending"
    alias_of: uuid.UUID | None = Field(default=None, alias="aliasOf")
    explicit_source_deletion: bool = Field(default=False, alias="explicitSourceDeletion", strict=True)

    @model_validator(mode="after")
    def valid_disposition(self):
        if self.disposition == "pending" and self.payload_digest is None:
            raise ValueError("Pending member requires its canonical payload digest")
        if (self.alias_of is not None) != (self.disposition == "duplicate_source_not_uploaded"):
            raise ValueError("Only duplicate dispositions require aliasOf")
        if self.alias_of == self.source_id:
            raise ValueError("A source cannot alias itself")
        if self.explicit_source_deletion != (self.disposition == "source_withdrawn"):
            raise ValueError("Withdrawal requires explicit source deletion, never scan absence")
        return self


class ManifestPage(Input):
    members: list[ObservationMember] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_members(self):
        if len({m.source_id for m in self.members}) != len(self.members):
            raise ValueError("Duplicate source ids in manifest page")
        return self


class QueryOutcome(Input):
    status: Literal["succeeded", "failed"]
    observed_count: int = Field(alias="observedCount", ge=0, le=MAX_MEMBERS, strict=True)
    manifest_digest: str = Field(alias="manifestDigest", pattern=r"^[a-f0-9]{64}$")
    error_code: Literal["query_failed", "permission_unknown", "cancelled", "capacity_exceeded"] | None = Field(default=None, alias="errorCode")

    @model_validator(mode="after")
    def truthful_error(self):
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("Failed query requires an opaque error code; success cannot carry one")
        return self


class ObservationBinding(Input):
    observation_id: uuid.UUID = Field(alias="observationId")
    source_id: uuid.UUID = Field(alias="sourceId")
    payload_digest: str = Field(alias="payloadDigest", pattern=r"^[a-f0-9]{64}$")


class ExistingActivityAck(Input):
    server_payload_digest: str = Field(alias="serverPayloadDigest", pattern=r"^[a-f0-9]{64}$")
    workout_revision: int = Field(alias="workoutRevision", ge=1, strict=True)
