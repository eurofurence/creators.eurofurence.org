"""Persist explicit one-time Registration evidence without backfilling assumptions."""

import sqlalchemy as sa
from alembic import op

revision = "7d3e91a4b620"
down_revision = "5c4d8a2f901e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registration_verifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("local_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_year", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reg_id", sa.String(), nullable=False),
        sa.Column("nickname", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id", "event_id", "purpose", name="uq_registration_verification"
        ),
        sa.CheckConstraint(
            "purpose IN ('creator','helper')", name="ck_verification_purpose"
        ),
        sa.CheckConstraint(
            "status IN ('PAID','CHECKED_IN')", name="ck_verification_status"
        ),
        sa.CheckConstraint("length(trim(reg_id)) > 0", name="ck_verification_reg_id"),
        sa.CheckConstraint(
            "length(issuer) > 0 AND length(subject) > 0",
            name="ck_verification_identity",
        ),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM registration_verifications")):
        raise RuntimeError(
            "Cannot downgrade while Registration verification evidence exists."
        )
    op.drop_table("registration_verifications")
