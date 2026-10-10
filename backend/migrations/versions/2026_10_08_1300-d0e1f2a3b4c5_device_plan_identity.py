"""Add immutable device plan identity without changing legacy logical UUIDs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workout_inventory", sa.Column("logical_workout_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column("workout_inventory", sa.Column("device_plan_id", pg.UUID(as_uuid=True), nullable=True))


def downgrade():
    op.drop_column("workout_inventory", "device_plan_id")
    op.drop_column("workout_inventory", "logical_workout_id")
