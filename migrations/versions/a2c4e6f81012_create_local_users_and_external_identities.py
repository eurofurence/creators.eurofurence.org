"""Create local users and external identities.

Revision ID: a2c4e6f81012
Revises: f1de09a2a1d9
"""

import sqlalchemy as sa
from alembic import op

revision = "a2c4e6f81012"
down_revision = "f1de09a2a1d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("local_users", sa.Column("id", sa.Integer(), primary_key=True))
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("local_users.id"), nullable=False
        ),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.UniqueConstraint(
            "issuer", "subject", name="uq_external_identity_issuer_subject"
        ),
    )
    op.create_index(
        "ix_external_identities_user_id", "external_identities", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_external_identities_user_id", table_name="external_identities")
    op.drop_table("external_identities")
    op.drop_table("local_users")
