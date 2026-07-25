"""Authentication: login, logout and the require_user dependency."""
from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import User
from app.security import verify_password

SESSION_USER_KEY = "uid"


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email))
    if user and verify_password(password, user.password_hash):
        return user
    return None


def login_user(request: Request, user: User) -> None:
    request.session[SESSION_USER_KEY] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def current_user(
    request: Request, db: Session = Depends(get_session)
) -> User | None:
    uid = request.session.get(SESSION_USER_KEY)
    if not uid:
        return None
    return db.get(User, uid)


def require_user(
    request: Request, db: Session = Depends(get_session)
) -> User:
    """Route guard. Raises so the handler in main.py can redirect to /login."""
    user = current_user(request, db)
    if user is None:
        raise _NotAuthenticated()
    return user


class _NotAuthenticated(Exception):
    pass


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)
