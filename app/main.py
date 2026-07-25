"""FastAPI application: personal finance manager."""
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import _NotAuthenticated, require_user
from app.config import APP_ROOT, settings
from app.models import User
from app.routers import auth_routes, banks, expenses, investments
from app.security import get_csrf_token
from app.templating import templates

# In production the auto-generated docs are hidden: no need to publish the API
# schema of a single-user app.
_docs = None if settings.is_prod else "/docs"
_redoc = None if settings.is_prod else "/redoc"
_openapi = None if settings.is_prod else "/openapi.json"

app = FastAPI(
    title="Finance Manager",
    docs_url=_docs,
    redoc_url=_redoc,
    openapi_url=_openapi,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.cookie_secure,
    same_site="lax",
    session_cookie="fm_session",
    max_age=60 * 60 * 12,  # session expires after 12 hours
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # CSP: self plus the CDNs actually used (Tailwind is compiled locally;
    # htmx and Chart.js come from a CDN).
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.mount(
    "/static",
    StaticFiles(directory=str(APP_ROOT / "app" / "static")),
    name="static",
)


@app.exception_handler(_NotAuthenticated)
async def _auth_redirect(request: Request, exc: _NotAuthenticated):
    return RedirectResponse(url="/login", status_code=303)


app.include_router(auth_routes.router)
app.include_router(expenses.router)
app.include_router(investments.router)
app.include_router(banks.router)


@app.get("/")
def home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        "placeholder.html",
        {
            "request": request, "user": user, "active": "dashboard",
            "csrf_token": get_csrf_token(request),
            "title": "Dashboard", "msg": "Nothing here yet.",
        },
    )
