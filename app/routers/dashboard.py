"""Dashboard: net worth, month KPIs and the charts."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import require_user
from app.db import get_session
from app.models import User
from app.security import get_csrf_token
from app.services.aggregation import build_dashboard
from app.templating import templates

router = APIRouter()


@router.get("/")
def home(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    data = build_dashboard(db, year, month)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "active": "dashboard",
            "csrf_token": get_csrf_token(request),
            **data,
        },
    )
