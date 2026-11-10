"""Return from the bank after authorising access (Enable Banking).

The bank redirects here with `code` (single use) and `state`. The `state` is
signed with SECRET_KEY and expires after 1 h, so nobody can inject someone
else's session. No web login is needed: the authorisation starts from the bot
(/connect).
"""
import html
import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import SessionLocal

log = logging.getLogger("bank_sync")
router = APIRouter(prefix="/bank-sync")

_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finance Manager</title>
<body style="font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem">
<h2>{title}</h2><p>{text}</p></body></html>"""


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), text=html.escape(text)),
                        status_code=status)


@router.get("/callback")
def callback(code: str | None = Query(default=None), state: str | None = Query(default=None),
             error: str | None = Query(default=None)):
    from app.services import bot, bot_banks
    from app.services import enablebanking as eb
    from app.services.bank_sync import save_session

    if not settings.bank_sync_enabled:
        return _page("Not configured", "Bank sync is off. See docs/BANK_SYNC.md.", 404)
    bank = bot_banks.read_state(state or "")
    if not bank:
        return _page("Invalid link", "Ask for a new one with /connect in the bot.", 400)
    if error or not code:
        return _page("Cancelled", f"{bank} was not authorised. You can retry with /connect.")
    try:
        session = eb.create_session(code)
    except Exception as exc:
        log.warning("create_session %s: %s", bank, exc)
        return _page("Error", "The bank did not confirm the session. Try /connect again.", 502)

    db = SessionLocal()
    try:
        try:
            links = save_session(db, session, bank)
        except Exception:
            log.exception("save_session %s", bank)
            db.rollback()
            return _page("Error", f"Could not save the connection with {bank}. "
                                  "Check the log and retry with /connect.", 500)
        n = len(links)
        # The connection is saved: if the import fails, /sync retries it.
        try:
            r = bot_banks.run_sync(db)
            summary = f"Imported {r['new']} movements, {r['transfers']} transfers."
            if r["errors"]:
                summary += "\n⚠ " + "; ".join(r["errors"])
            if r["pending"]:
                summary += f"\n❓ {r['pending']} to categorise: /pending"
        except Exception as exc:
            log.exception("sync after connect %s", bank)
            db.rollback()
            summary = (f"⚠ The import failed ({type(exc).__name__}: {str(exc)[:200]}). "
                       "The connection is saved; retry with /sync.")
    finally:
        db.close()
    if settings.telegram_enabled:
        msg = (f"✅ <b>{html.escape(bank)}</b> connected ({n} account{'s' if n != 1 else ''}, "
               "read-only).\n" + html.escape(summary, quote=False))
        for chat_id in settings.allowed_chat_ids:
            bot.send_message_sync(chat_id, msg)
    return _page(f"{bank} connected",
                 f"{n} account(s) authorised read-only. You can close this tab "
                 "and go back to Telegram.")
