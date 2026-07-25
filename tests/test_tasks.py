"""Task endpoint tests (external cron), with the token and an in-memory DB."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_session
from app.main import app


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    app.dependency_overrides[get_session] = lambda: db
    monkeypatch.setattr(settings, "tasks_token", "secret", raising=False)
    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_wrong_token_is_403(client):
    c, _ = client
    r = c.post("/tasks/snapshot", headers={"X-Tasks-Token": "bad"})
    assert r.status_code == 403


def test_snapshot_runs(client):
    c, _ = client
    r = c.post("/tasks/snapshot", headers={"X-Tasks-Token": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["job"] == "snapshot"
    assert "total" in body


def test_unknown_job_is_404(client):
    c, _ = client
    r = c.post("/tasks/made-up", headers={"X-Tasks-Token": "secret"})
    assert r.status_code == 404


def test_token_in_the_query_string(client):
    c, _ = client
    r = c.post("/tasks/snapshot?token=secret")
    assert r.status_code == 200


def test_no_token_configured_disables_the_endpoints(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(settings, "tasks_token", "", raising=False)
    r = c.post("/tasks/snapshot", headers={"X-Tasks-Token": ""})
    assert r.status_code == 403
