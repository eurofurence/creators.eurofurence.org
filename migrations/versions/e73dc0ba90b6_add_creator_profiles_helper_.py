"""Add creator profiles helper relationships and shared badges

Revision ID: e73dc0ba90b6
Revises: 9db8f45a6ca7
Create Date: 2026-10-06 12:44:33.765309

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e73dc0ba90b6"
down_revision: str | Sequence[str] | None = "9db8f45a6ca7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("creator_applications") as batch:
        batch.add_column(
            sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_unique_constraint(
            "uq_application_owner", ["id", "event_id", "user_id"]
        )
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("helper_limit", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_event_helper_limit", "helper_limit IS NULL OR helper_limit >= 0"
        )
    op.create_table(
        "banned_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("normalized_account", sa.String(), nullable=False),
        sa.Column("original_reference", sa.String(), nullable=True),
        sa.Column("private_reason", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "normalized_account", name="uq_banned_account"),
    )
    op.create_table(
        "profile_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deletion_failed", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "state IN ('STAGED','ACTIVE','DELETE')", name="ck_image_state"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_table(
        "redemption_throttles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["local_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "creator_profiles",
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("channel_name", sa.String(), nullable=False),
        sa.Column("image_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "length(channel_name) <= 200", name="ck_profile_name_length"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["creator_applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["image_id"], ["profile_images.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("application_id"),
        sa.UniqueConstraint("image_id"),
    )
    op.create_table(
        "helper_invitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("token_digest", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["application_id", "event_id"],
            ["creator_applications.id", "creator_applications.event_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_by"], ["local_users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "application_id", "event_id", name="uq_invitation_context"
        ),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_table(
        "helper_registrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("creator_user_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("invitation_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reg_id", sa.String(), nullable=True),
        sa.Column("nickname", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("eligibility_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','CONFIRMED','DECLINED')", name="ck_helper_status"
        ),
        sa.CheckConstraint("user_id <> creator_user_id", name="ck_helper_not_self"),
        sa.CheckConstraint("version > 0", name="ck_helper_version"),
        sa.ForeignKeyConstraint(
            ["application_id", "event_id", "creator_user_id"],
            [
                "creator_applications.id",
                "creator_applications.event_id",
                "creator_applications.user_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invitation_id", "application_id", "event_id"],
            [
                "helper_invitations.id",
                "helper_invitations.application_id",
                "helper_invitations.event_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["local_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "user_id", name="uq_helper_relationship"),
        sa.UniqueConstraint("id", "event_id", name="uq_helper_event"),
        sa.UniqueConstraint("invitation_id"),
    )
    with op.batch_alter_table("badges") as batch:
        batch.add_column(sa.Column("helper_id", sa.Integer(), nullable=True))
        batch.alter_column("application_id", existing_type=sa.Integer(), nullable=True)
        batch.create_unique_constraint("uq_badge_helper", ["helper_id"])
        batch.create_foreign_key(
            "fk_badge_helper_event",
            "helper_registrations",
            ["helper_id", "event_id"],
            ["id", "event_id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_badge_owner",
            "(application_id IS NOT NULL AND helper_id IS NULL) OR (application_id IS NULL AND helper_id IS NOT NULL)",
        )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("uq_outbox_application_version", type_="unique")
        batch.create_unique_constraint(
            "uq_outbox_change_recipient_type",
            [
                "application_id",
                "application_version",
                "recipient_id",
                "notification_type",
            ],
        )
    op.execute(
        "INSERT INTO creator_profiles (application_id, channel_name) SELECT id, '' FROM creator_applications WHERE status = 'APPROVED'"
    )


def downgrade() -> None:
    # Development downgrade discards M3-only badges and notification intents.
    op.execute("DELETE FROM badges WHERE helper_id IS NOT NULL")
    op.execute(
        "DELETE FROM notification_outbox WHERE notification_type LIKE 'HELPER_%' OR notification_type IN ('CREATOR_RESTORED', 'CREATOR_INACTIVE')"
    )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("uq_outbox_change_recipient_type", type_="unique")
        batch.create_unique_constraint(
            "uq_outbox_application_version", ["application_id", "application_version"]
        )
    with op.batch_alter_table("badges") as batch:
        batch.drop_constraint("ck_badge_owner", type_="check")
        batch.drop_constraint("fk_badge_helper_event", type_="foreignkey")
        batch.drop_constraint("uq_badge_helper", type_="unique")
        batch.alter_column("application_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("helper_id")
    op.drop_table("helper_registrations")
    op.drop_table("helper_invitations")
    op.drop_table("creator_profiles")
    op.drop_table("redemption_throttles")
    op.drop_table("profile_images")
    op.drop_table("banned_channels")
    with op.batch_alter_table("creator_applications") as batch:
        batch.drop_constraint("uq_application_owner", type_="unique")
        batch.drop_column("withdrawn_at")
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("ck_event_helper_limit", type_="check")
        batch.drop_column("helper_limit")
