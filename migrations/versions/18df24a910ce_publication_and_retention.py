"""Publication controls, cleanup coordination and session identity lifecycle."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "18df24a910ce"
down_revision = "6cf2b87d401a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("cleanup_started_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column(
                "cleanup_failed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("creator_profiles") as batch:
        batch.add_column(sa.Column("public_id", sa.String(), nullable=True))
        batch.add_column(
            sa.Column(
                "publicly_hidden",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("local_users") as batch:
        batch.add_column(sa.Column("session_key", sa.String(), nullable=True))
    connection = op.get_bind()
    for table, key, column in (
        ("creator_profiles", "application_id", "public_id"),
        ("local_users", "id", "session_key"),
    ):
        records = sa.table(
            table, sa.column(key, sa.Integer()), sa.column(column, sa.String())
        )
        for identifier in connection.scalars(sa.select(records.c[key])):
            connection.execute(
                records.update()
                .where(records.c[key] == identifier)
                .values({column: uuid4().hex})
            )
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.String(), nullable=False)
            batch.create_unique_constraint(f"uq_{table}_{column}", [column])


def downgrade():
    for table, column in (
        ("creator_profiles", "public_id"),
        ("local_users", "session_key"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"uq_{table}_{column}", type_="unique")
            batch.drop_column(column)
    with op.batch_alter_table("creator_profiles") as batch:
        batch.drop_column("publicly_hidden")
    with op.batch_alter_table("events") as batch:
        batch.drop_column("cleanup_failed")
        batch.drop_column("cleanup_started_at")
