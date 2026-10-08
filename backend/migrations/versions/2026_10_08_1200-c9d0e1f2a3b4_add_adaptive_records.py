"""Add immutable prescriptions/revisions and durable athlete-owned coaching records.

Existing workouts are deliberately not backfilled with invented executed versions.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def user(primary=False):
    return sa.Column(
        "user_id",
        pg.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=primary,
    )


def ident(name="id"):
    return sa.Column(name, pg.UUID(as_uuid=True), primary_key=True)


def stamp(name="created_at"):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def data(name="data"):
    return sa.Column(name, pg.JSONB(), nullable=False)


def upgrade():
    op.add_column("workout_queue", sa.Column("prescription_revision", pg.UUID(as_uuid=True)))
    op.add_column("workout_action", sa.Column("base_prescription_revision", pg.UUID(as_uuid=True)))
    op.add_column("workout_action", sa.Column("desired_prescription_revision", pg.UUID(as_uuid=True)))
    op.add_column("plans", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("workout_inventory", sa.Column("prescription_revision", pg.UUID(as_uuid=True)))
    op.add_column("workout_inventory", sa.Column("content_hash", sa.Text()))
    op.add_column("workout_inventory", sa.Column("observed_at", sa.DateTime(timezone=True)))
    op.add_column("api_tokens", sa.Column("scope", sa.String(32), nullable=False, server_default="device"))
    op.create_table("athlete_profiles", user(True), data(), stamp("updated_at"))
    op.create_table(
        "plan_revisions",
        ident(),
        user(),
        sa.Column("plan_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        data("snapshot"),
        stamp(),
        sa.UniqueConstraint("plan_id", "revision"),
    )
    op.create_table(
        "prescription_revisions",
        ident(),
        user(),
        sa.Column("workout_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        data("snapshot"),
        stamp(),
    )
    op.create_table(
        "execution_assessments",
        ident("workout_id"),
        user(),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        data(),
        stamp("updated_at"),
    )
    op.create_table(
        "review_jobs",
        ident(),
        user(),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_hash", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        stamp("available_at"),
        data(),
        stamp(),
        sa.UniqueConstraint("user_id", "idempotency_key"),
    )
    op.create_table(
        "review_proposals",
        ident(),
        user(),
        sa.Column("job_id", pg.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("plan_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        data(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        stamp(),
    )
    for table in (
        "plan_revisions",
        "prescription_revisions",
        "execution_assessments",
        "review_jobs",
        "review_proposals",
    ):
        op.create_index("ix_" + table + "_user_id", table, ["user_id"])
    for table, column in (
        ("plan_revisions", "plan_id"),
        ("prescription_revisions", "workout_id"),
        ("review_jobs", "status"),
        ("review_proposals", "plan_id"),
    ):
        op.create_index("ix_" + table + "_" + column, table, [column])


def downgrade():
    for table in (
        "review_proposals",
        "review_jobs",
        "execution_assessments",
        "prescription_revisions",
        "plan_revisions",
        "athlete_profiles",
    ):
        op.drop_table(table)
    for col in ("observed_at", "content_hash", "prescription_revision"):
        op.drop_column("workout_inventory", col)
    op.drop_column("api_tokens", "scope")
    op.drop_column("plans", "revision")
    op.drop_column("workout_queue", "prescription_revision")
    for col in ("desired_prescription_revision", "base_prescription_revision"):
        op.drop_column("workout_action", col)
