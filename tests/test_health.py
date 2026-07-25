"""The two health endpoints have different contracts on purpose.

/health     -> liveness for the host. Never fails because of the database.
/health/db  -> readiness for the external monitor. 503 when the DB is silent.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_answers_without_touching_the_db(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_db_is_ok_when_the_db_answers(client):
    r = client.get("/health/db")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_db_returns_503_when_the_db_fails(client, monkeypatch):
    """The 503 is what triggers the monitor alert. Without it a paused database
    would go unnoticed, because the process itself is still alive."""
    import app.db

    class BrokenSession:
        def execute(self, *_):
            raise RuntimeError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr(app.db, "SessionLocal", BrokenSession)
    r = client.get("/health/db")
    assert r.status_code == 503
    assert r.json()["db"] == "unreachable"


def test_health_survives_a_broken_db(client, monkeypatch):
    """The point: if /health failed with the database down, the host would tear
    the deployment down."""
    import app.db

    class BrokenSession:
        def execute(self, *_):
            raise RuntimeError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr(app.db, "SessionLocal", BrokenSession)
    assert client.get("/health").status_code == 200
