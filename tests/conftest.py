import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401


def make_engine():
    """In-memory SQLite (default) or the Postgres in TEST_DATABASE_URL.

    Postgres exposes failures SQLite hides (e.g. text longer than its column):
    TEST_DATABASE_URL=postgresql+psycopg2://... pytest
    """
    import os

    from sqlalchemy.pool import StaticPool
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        from sqlalchemy import text
        engine = create_engine(url)
        # Sessions a test left open would block the DROP (test database only).
        with engine.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
            c.commit()
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db():
    engine = make_engine()
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clear_query_limit():
    """The bank query limit is in-memory state: isolate every test."""
    from app.services import bank_sync
    bank_sync._LAST_QUERY.clear()
    bank_sync._PLATFORMS_CACHE.clear()
    yield
    bank_sync._LAST_QUERY.clear()
    bank_sync._PLATFORMS_CACHE.clear()
