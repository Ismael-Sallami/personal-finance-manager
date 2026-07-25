"""In-process scheduled jobs, for hosts that stay awake.

- Nightly price refresh (03:00).
- Monthly contributions on the 1st (06:00).
- Daily net worth snapshot (23:00).
- Month-end Telegram reminder (last day, 20:00).

Where the app sleeps, keep USE_SCHEDULER=false and drive the same jobs from an
external cron against /tasks/* (see app/routers/tasks.py).
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings

log = logging.getLogger("scheduler")


def _refresh_prices():
    from app.db import SessionLocal
    from app.services.prices import refresh_all
    db = SessionLocal()
    try:
        res = refresh_all(db)
        log.info("price refresh: %s", res)
    except Exception as exc:
        log.warning("price refresh failed: %s", exc)
    finally:
        db.close()


def _reminder():
    from app.services.bot import month_end_reminder
    month_end_reminder()


def _contributions():
    from app.db import SessionLocal
    from app.services.contributions import apply_monthly_contributions
    db = SessionLocal()
    try:
        n = apply_monthly_contributions(db)
        log.info("monthly contributions applied: %s", n)
    except Exception as exc:
        log.warning("monthly contributions failed: %s", exc)
    finally:
        db.close()


def _snapshot():
    from app.db import SessionLocal
    from app.services.aggregation import snapshot_today
    db = SessionLocal()
    try:
        snapshot_today(db)
    except Exception as exc:
        log.warning("net worth snapshot failed: %s", exc)
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=settings.timezone)

    sched.add_job(_refresh_prices, CronTrigger(hour=3, minute=0),
                  id="prices", replace_existing=True)

    sched.add_job(_contributions, CronTrigger(day=1, hour=6, minute=0),
                  id="contributions", replace_existing=True)

    # Daily snapshot, so the evolution chart keeps filling in.
    sched.add_job(_snapshot, CronTrigger(hour=23, minute=0),
                  id="snapshot", replace_existing=True)

    if settings.telegram_enabled:
        sched.add_job(_reminder, CronTrigger(day="last", hour=20, minute=0),
                      id="reminder", replace_existing=True)

    sched.start()
    log.info("scheduler started (jobs: %s)", [j.id for j in sched.get_jobs()])
    return sched
