"""Owned activity observation receipts; legacy coverage remains unknown."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("activity_observation_state", JSONB, nullable=True))
    op.add_column("workout", sa.Column("source_evidence_hash", sa.String(64), nullable=True))
    op.add_column("workout", sa.Column("source_evidence_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("workout", sa.Column("source_withdrawn", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("device_activity_observations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", UUID(as_uuid=True), nullable=False),
        sa.Column("inventory_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("data", JSONB, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "token_id", "inventory_revision"))
    op.create_index("ix_device_activity_observations_user_id", "device_activity_observations", ["user_id"])
    op.create_table("device_activity_observation_members",
        sa.Column("observation_id", UUID(as_uuid=True), sa.ForeignKey("device_activity_observations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=True),
        sa.Column("server_payload_digest", sa.String(64), nullable=True),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("alias_of", UUID(as_uuid=True), nullable=True),
        sa.Column("workout_revision", sa.Integer(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_table("device_activity_observation_members")
    op.drop_table("device_activity_observations")
    op.drop_column("workout", "source_withdrawn")
    op.drop_column("workout", "source_evidence_revision")
    op.drop_column("workout", "source_evidence_hash")
    op.drop_column("users", "activity_observation_state")
