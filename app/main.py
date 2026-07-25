"""FastAPI application: personal finance manager."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import _NotAuthenticated
from app.config import APP_ROOT, settings
from app.routers import (
    auth_routes,
    banks,
    dashboard,
    expenses,
    investments,
    reports,
    tasks,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Never boot production with the default SECRET_KEY: sessions would be
    # forgeable by anyone who read this repository.
    if settings.is_prod and settings.secret_key == "dev-insecure-change-me":
        raise RuntimeError("SECRET_KEY is not configured in production.")
    # In-process scheduler: only on machines that stay awake. Where the app
    # sleeps, keep USE_SCHEDULER=false and let an external cron hit /tasks/*.
    scheduler = None
    if settings.use_scheduler:
        try:
            from app.scheduler import start_scheduler
            scheduler = start_scheduler()
        except Exception as exc:  # a broken scheduler must not take the app down
            print(f"[scheduler] not started: {exc}")
    if settings.telegram_enabled:
        try:
            from app.services.bot import setup_webhook
            await setup_webhook()
        except Exception as exc:
            print(f"[telegram] webhook not registered: {exc}")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


# In production the auto-generated docs are hidden: no need to publish the API
# schema of a single-user app.
_docs = None if settings.is_prod else "/docs"
_redoc = None if settings.is_prod else "/redoc"
_openapi = None if settings.is_prod else "/openapi.json"

app = FastAPI(
    title="Finance Manager",
    lifespan=lifespan,
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
app.include_router(dashboard.router)
app.include_router(expenses.router)
app.include_router(investments.router)
app.include_router(banks.router)
app.include_router(reports.router)
app.include_router(tasks.router)

# Telegram bot, only when a token is configured.
if settings.telegram_enabled:
    from app.routers import telegram
    app.include_router(telegram.router)


@app.get("/health")
def health():
    """Liveness: says the process answers. It does not touch the database.

    This is the host healthcheck. If it failed during a short database outage,
    the host would tear down the whole deployment, so nothing is queried here.
    """
    return {"status": "ok", "db": "sqlite" if settings.is_sqlite else "postgres"}


@app.get("/health/db")
def health_db():
    """Readiness: checks the database with a real SELECT 1.

    This is the endpoint an external monitor should ping. The same ping does two
    things: it keeps the app awake and it keeps the database alive, since free
    tiers pause a database that gets no queries. It answers 503 when the
    database does not reply, so a paused database raises an alert instead of
    going unnoticed.
    """
    from sqlalchemy import text

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        return JSONResponse(
            {"status": "error", "db": "unreachable", "detail": str(exc)[:200]},
            status_code=503,
        )
    finally:
        db.close()
    return {"status": "ok", "db": "sqlite" if settings.is_sqlite else "postgres"}
