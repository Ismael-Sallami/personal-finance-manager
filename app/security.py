"""Password hashing, CSRF tokens and login rate limiting."""
import secrets
import threading
import time

import bcrypt
from starlette.requests import Request


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --- CSRF (double submit, token kept in the session) ---
CSRF_SESSION_KEY = "csrf_token"


def get_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf(request: Request, submitted: str | None) -> bool:
    expected = request.session.get(CSRF_SESSION_KEY)
    return bool(expected) and bool(submitted) and secrets.compare_digest(expected, submitted)


# --- Login rate limiting (in memory; this app runs on one machine) ---
class LoginRateLimiter:
    """Sliding window per IP. Slows down brute force without extra services."""

    def __init__(self, max_attempts: int = 8, window_seconds: int = 900):
        self.max = max_attempts
        self.window = window_seconds
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(ip, []) if now - t < self.window]
        self._fails[ip] = recent
        return recent

    def is_blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            return len(self._prune(ip, now)) >= self.max

    def record_fail(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(ip, now).append(now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


login_limiter = LoginRateLimiter()


def client_ip(request: Request) -> str:
    # Behind a proxy the socket address is the proxy, so trust X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
