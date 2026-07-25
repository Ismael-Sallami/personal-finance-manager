"""Login and logout routes."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import authenticate, current_user, login_user, logout_user
from app.db import get_session
from app.security import (
    client_ip,
    get_csrf_token,
    login_limiter,
    validate_csrf,
)
from app.templating import templates

router = APIRouter()


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_session)):
    if current_user(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "csrf_token": get_csrf_token(request), "error": None},
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_session),
):
    ip = client_ip(request)
    if login_limiter.is_blocked(ip):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "csrf_token": get_csrf_token(request),
             "error": "Too many attempts. Wait a few minutes."},
            status_code=429,
        )
    if not validate_csrf(request, csrf_token):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "csrf_token": get_csrf_token(request),
             "error": "Invalid security token. Try again."},
            status_code=400,
        )
    user = authenticate(db, email.strip().lower(), password)
    if not user:
        login_limiter.record_fail(ip)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "csrf_token": get_csrf_token(request),
             "error": "Wrong credentials."},
            status_code=401,
        )
    login_limiter.reset(ip)
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=303)
