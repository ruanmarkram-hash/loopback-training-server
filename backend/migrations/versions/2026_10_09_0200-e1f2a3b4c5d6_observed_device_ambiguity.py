"""Preserve multiple actual device plan observations per logical workout."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workout_inventory", sa.Column("observed_devices", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade():
    op.drop_column("workout_inventory", "observed_devices")
