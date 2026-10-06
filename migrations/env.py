from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.applications import models  # noqa: F401
from app.config import settings
from app.creators import models as creator_models  # noqa: F401
from app.database import Base
from app.events.models import Event  # noqa: F401
from app.helpers import models as helper_models  # noqa: F401
from app.identity.models import ExternalIdentity, LocalUser  # noqa: F401

config = context.config

if settings.database_url is None:
    raise RuntimeError("DATABASE_URL is not configured")

config.set_main_option(
    "sqlalchemy.url",
    settings.database_url.get_secret_value().replace("%", "%%"),
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without connecting to the database."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations through a database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
