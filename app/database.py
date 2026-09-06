from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


if settings.database_url is None:
    raise RuntimeError("DATABASE_URL is not configured")


# Base for future models
class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url.get_secret_value(),
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
