"""Security tests: login rate limit, security headers, CSRF."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (fills Base.metadata before create_all)
from app.db import Base, get_session
from app.main import app
from app.security import LoginRateLimiter, login_limiter


@pytest.fixture
def empty_db():
    """In-memory DB with the tables created, injected into the routes.

    Without it the test depends on a dev.db already existing on the machine: on
    a fresh clone the login route fails with "no such table: users" and the rate
    limit is never exercised.
    """
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    app.dependency_overrides[get_session] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_rate_limiter_blocks():
    rl = LoginRateLimiter(max_attempts=3, window_seconds=900)
    ip = "1.2.3.4"
    assert not rl.is_blocked(ip)
    for _ in range(3):
        rl.record_fail(ip)
    assert rl.is_blocked(ip)
    rl.reset(ip)
    assert not rl.is_blocked(ip)


def test_security_headers_present():
    c = TestClient(app)
    h = c.get("/login").headers
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in h


def test_login_rejects_invalid_csrf():
    c = TestClient(app)
    r = c.post("/login", data={"email": "x@x.com", "password": "y", "csrf_token": "bad"})
    assert r.status_code == 400


def test_login_brute_force_is_cut_off(empty_db):
    login_limiter.reset("testclient")
    c = TestClient(app)
    # Valid CSRF but wrong credentials, repeated -> ends in 429.
    codes = []
    for _ in range(10):
        html = c.get("/login").text
        tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        r = c.post("/login", data={"email": "no@no.com", "password": "wrong", "csrf_token": tok})
        codes.append(r.status_code)
    assert 429 in codes
    login_limiter.reset("testclient")
