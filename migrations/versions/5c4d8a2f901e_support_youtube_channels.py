"""Allow YouTube publication channels without rewriting retained accounts."""

import sqlalchemy as sa
from alembic import op

revision = "5c4d8a2f901e"
down_revision = "18df24a910ce"
branch_labels = None
depends_on = None

PLATFORMS = (
    "'Bluesky','Facebook','Instagram','Mastodon','Threads','TikTok','Twitch','X'"
)


def upgrade():
    with op.batch_alter_table("creator_channels") as batch:
        batch.drop_constraint("ck_channel_platform", type_="check")
        batch.create_check_constraint(
            "ck_channel_platform", f"platform IN ({PLATFORMS},'YouTube')"
        )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM creator_channels WHERE platform = 'YouTube'")
    ):
        raise RuntimeError(
            "Cannot downgrade while YouTube channels exist; retained data was not removed."
        )
    with op.batch_alter_table("creator_channels") as batch:
        batch.drop_constraint("ck_channel_platform", type_="check")
        batch.create_check_constraint(
            "ck_channel_platform", f"platform IN ({PLATFORMS})"
        )
