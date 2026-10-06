"""Add creator applications and review transactions

Revision ID: 9db8f45a6ca7
Revises: a2c4e6f81012
Create Date: 2026-10-06 12:17:39.102595

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9db8f45a6ca7"
down_revision: Union[str, Sequence[str], None] = "a2c4e6f81012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "badge_counters",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("next_number", sa.Integer(), nullable=False),
        sa.CheckConstraint("next_number > 0", name="ck_counter_positive"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_table(
        "business_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["local_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_business_audit_event_id"), "business_audit", ["event_id"], unique=False
    )
    op.create_table(
        "creator_applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("livestream", sa.Boolean(), nullable=False),
        sa.Column("shorts", sa.Boolean(), nullable=False),
        sa.Column("vlogs", sa.Boolean(), nullable=False),
        sa.Column("outcome_reason", sa.String(), nullable=False),
        sa.Column("staff_notes", sa.String(), nullable=False),
        sa.Column("reg_id", sa.String(), nullable=True),
        sa.Column("nickname", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("eligibility_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('NEW','ON_REVIEW','APPROVED','NOT_APPROVED','NOT_ACCEPTED')",
            name="ck_application_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('NOT_APPROVED','NOT_ACCEPTED') OR length(trim(outcome_reason)) > 0",
            name="ck_application_reason",
        ),
        sa.CheckConstraint(
            "livestream OR shorts OR vlogs", name="ck_application_content"
        ),
        sa.CheckConstraint("version > 0", name="ck_application_version"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["local_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "event_id", name="uq_application_id_event"),
        sa.UniqueConstraint("user_id", "event_id", name="uq_application_user_event"),
    )
    op.create_table(
        "local_role_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.CheckConstraint(
            "(role = 'ADMIN' AND event_id IS NULL) OR (role = 'BADGE_STAFF' AND event_id IS NOT NULL)",
            name="ck_role_scope",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["local_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "event_id", "role", name="uq_event_role"),
    )
    op.create_index(
        "uq_global_admin",
        "local_role_assignments",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("role = 'ADMIN'"),
        sqlite_where=sa.text("role = 'ADMIN'"),
    )
    op.create_table(
        "badges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("badge_number", sa.Integer(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("badge_number > 0", name="ck_badge_positive"),
        sa.ForeignKeyConstraint(
            ["application_id", "event_id"],
            ["creator_applications.id", "creator_applications.event_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id"),
        sa.UniqueConstraint("event_id", "badge_number", name="uq_badge_event_number"),
    )
    op.create_table(
        "convention_videos",
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.CheckConstraint("position >= 0 AND position < 10", name="ck_video_position"),
        sa.ForeignKeyConstraint(
            ["application_id"], ["creator_applications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("application_id", "position"),
    )
    op.create_table(
        "creator_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("original_representation", sa.String(), nullable=False),
        sa.Column("normalized_account", sa.String(), nullable=False),
        sa.Column("canonical_url", sa.String(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("publicly_hidden", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "platform IN ('Bluesky','Facebook','Instagram','Mastodon','Threads','TikTok','Twitch','X')",
            name="ck_channel_platform",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["creator_applications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id",
            "platform",
            "normalized_account",
            name="uq_channel_account",
        ),
    )
    op.create_index(
        op.f("ix_creator_channels_application_id"),
        "creator_channels",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        "uq_channel_primary",
        "creator_channels",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
        sqlite_where=sa.text("is_primary = 1"),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("application_version", sa.Integer(), nullable=False),
        sa.Column("recipient_id", sa.Integer(), nullable=False),
        sa.Column("notification_type", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"], ["creator_applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["local_users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id",
            "application_version",
            name="uq_outbox_application_version",
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "badge_change_deadline_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION check_application_primary() RETURNS trigger AS $$
            DECLARE application_key integer;
            BEGIN
                IF TG_TABLE_NAME = 'creator_applications' THEN
                    application_key := NEW.id;
                ELSIF TG_OP = 'DELETE' THEN
                    application_key := OLD.application_id;
                ELSE
                    application_key := NEW.application_id;
                END IF;
                IF EXISTS (SELECT 1 FROM creator_applications WHERE id = application_key)
                   AND (SELECT count(*) FROM creator_channels WHERE application_id = application_key AND is_primary) <> 1 THEN
                    RAISE EXCEPTION 'Application must have exactly one primary channel' USING ERRCODE = '23514';
                END IF;
                IF TG_TABLE_NAME = 'creator_channels' AND TG_OP = 'UPDATE' THEN
                    IF OLD.application_id <> NEW.application_id
                       AND EXISTS (SELECT 1 FROM creator_applications WHERE id = OLD.application_id)
                       AND (SELECT count(*) FROM creator_channels WHERE application_id = OLD.application_id AND is_primary) <> 1 THEN
                        RAISE EXCEPTION 'Application must have exactly one primary channel' USING ERRCODE = '23514';
                    END IF;
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute(
            "CREATE CONSTRAINT TRIGGER application_primary_required AFTER INSERT OR UPDATE ON creator_applications DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_application_primary()"
        )
        op.execute(
            "CREATE CONSTRAINT TRIGGER channel_primary_required AFTER INSERT OR UPDATE OR DELETE ON creator_channels DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_application_primary()"
        )


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER channel_primary_required ON creator_channels")
        op.execute("DROP TRIGGER application_primary_required ON creator_applications")
        op.execute("DROP FUNCTION check_application_primary()")
    op.drop_column("events", "badge_change_deadline_at")
    op.drop_table("notification_outbox")
    op.drop_index(
        "uq_channel_primary",
        table_name="creator_channels",
        postgresql_where=sa.text("is_primary"),
        sqlite_where=sa.text("is_primary = 1"),
    )
    op.drop_index(
        op.f("ix_creator_channels_application_id"), table_name="creator_channels"
    )
    op.drop_table("creator_channels")
    op.drop_table("convention_videos")
    op.drop_table("badges")
    op.drop_index(
        "uq_global_admin",
        table_name="local_role_assignments",
        postgresql_where=sa.text("role = 'ADMIN'"),
        sqlite_where=sa.text("role = 'ADMIN'"),
    )
    op.drop_table("local_role_assignments")
    op.drop_table("creator_applications")
    op.drop_index(op.f("ix_business_audit_event_id"), table_name="business_audit")
    op.drop_table("business_audit")
    op.drop_table("badge_counters")
