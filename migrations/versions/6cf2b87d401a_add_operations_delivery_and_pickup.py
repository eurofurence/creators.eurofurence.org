"""Add notification delivery metadata and badge pickup attribution.

Revision ID: 6cf2b87d401a
Revises: e73dc0ba90b6
"""

import sqlalchemy as sa
from alembic import op

revision = "6cf2b87d401a"
down_revision = "e73dc0ba90b6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("badges") as batch:
        batch.add_column(
            sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("picked_up_by", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_badge_pickup_actor",
            "local_users",
            ["picked_up_by"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("notification_outbox") as batch:
        for name in ("next_attempt_at", "claimed_until", "sent_at"):
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        for name in ("claim_id", "last_error"):
            batch.add_column(sa.Column(name, sa.String(), nullable=True))
        batch.create_check_constraint(
            "ck_outbox_state", "state IN ('PENDING','SENDING','SENT','FAILED')"
        )
        batch.create_check_constraint(
            "ck_outbox_attempts", "attempts >= 0 AND attempts <= 5"
        )


def downgrade():
    # Unfinished leases become pending again in the older outbox representation.
    op.execute(
        "UPDATE notification_outbox SET state = 'PENDING' WHERE state = 'SENDING'"
    )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_outbox_state", type_="check")
        batch.drop_constraint("ck_outbox_attempts", type_="check")
        for name in (
            "next_attempt_at",
            "claimed_until",
            "sent_at",
            "claim_id",
            "last_error",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("badges") as batch:
        batch.drop_constraint("fk_badge_pickup_actor", type_="foreignkey")
        batch.drop_column("picked_up_by")
        batch.drop_column("picked_up_at")
