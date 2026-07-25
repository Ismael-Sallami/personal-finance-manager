"""Scheduled task endpoints, driven by an external cron (cron-job.org, GitHub
Actions, ...). Meant for hosts that put the app to sleep, where an in-process
scheduler is not reliable. Every call is protected by TASKS_TOKEN.

Jobs (POST /tasks/{job}):
  - prices        -> revalue the investments (yfinance)
  - contributions -> apply the monthly contributions   [run on the 1st]
  - snapshot      -> today's net worth snapshot
  - reminder      -> month-end Telegram reminder       [run on the last day]
  - backup        -> database copy sent to your chat   [run weekly]

The cron must send the token in the `X-Tasks-Token` header or as `?token=`.

Do not drop the `?token=` route: cron-job.org does not forward custom headers,
so the job would answer 403 with no further hint. It is the only thing that
works there. With curl or GitHub Actions the header does arrive and is
preferable, because the token never ends up written in a URL.
"""
import secrets

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session

router = APIRouter(prefix="/tasks")

JOBS = {"prices", "contributions", "snapshot", "reminder", "backup"}


def _authorised(header_token: str | None, query_token: str | None) -> bool:
    expected = settings.tasks_token
    if not expected:  # no token configured: the endpoints stay disabled
        return False
    submitted = header_token or query_token or ""
    return secrets.compare_digest(submitted, expected)


@router.post("/{job}")
def run_task(
    job: str,
    x_tasks_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: Session = Depends(get_session),
):
    if not _authorised(x_tasks_token, token):
        return JSONResponse({"ok": False, "error": "not authorised"}, status_code=403)
    if job not in JOBS:
        return JSONResponse({"ok": False, "error": "unknown job"}, status_code=404)

    if job == "prices":
        from app.services.prices import refresh_all
        return {"ok": True, "job": job, "result": refresh_all(db)}

    if job == "contributions":
        from app.services.contributions import apply_monthly_contributions
        return {"ok": True, "job": job, "applied": apply_monthly_contributions(db)}

    if job == "snapshot":
        from app.services.aggregation import snapshot_today
        snap = snapshot_today(db)
        return {"ok": True, "job": job, "total": float(snap.total)}

    if job == "reminder":
        from app.services.bot import month_end_reminder
        month_end_reminder()
        return {"ok": True, "job": job}

    if job == "backup":
        from app.services.backup import backup_to_telegram
        result = backup_to_telegram(db)
        # 500 when it could not be delivered, so the cron shows it in red and
        # a silent backup failure does not go unnoticed.
        if not result["sent"]:
            return JSONResponse({"ok": False, "job": job, **result}, status_code=500)
        return {"ok": True, "job": job, **result}
