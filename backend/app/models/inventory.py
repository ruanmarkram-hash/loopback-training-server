import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkoutInventory(Base):
    __tablename__ = "workout_inventory"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    logical_workout_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    device_plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    prescription_revision: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_devices: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    @property
    def ambiguous(self):
        return len({item["devicePlanId"] for item in self.observed_devices or []}) > 1

    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
