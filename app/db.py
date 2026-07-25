"""SQLAlchemy engine and session. Works with Postgres or SQLite (dev)."""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if settings.is_sqlite:
        # check_same_thread False: FastAPI serves requests from a thread pool.
        return {"connect_args": {"check_same_thread": False}}
    # Remote Postgres: recycle pooled connections so the pooler does not hand
    # back a socket the server already closed.
    return {"pool_pre_ping": True, "pool_recycle": 1800}


engine = create_engine(settings.sqlalchemy_url, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
