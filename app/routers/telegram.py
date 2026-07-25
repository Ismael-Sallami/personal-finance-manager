"""Telegram webhook. Only mounted when TELEGRAM_BOT_TOKEN is set."""
import logging
import secrets

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from app.config import settings
from app.services import bot

log = logging.getLogger("bot")
router = APIRouter(prefix="/tg")


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Check the webhook secret, so forged POSTs are dropped.
    secret = settings.telegram_webhook_secret
    if secret and not secrets.compare_digest(
        x_telegram_bot_api_secret_token or "", secret
    ):
        return Response(status_code=403)
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    try:
        await bot.handle_update(update)
    except Exception as exc:  # never answer 5xx: Telegram would retry forever
        log.exception("error handling update: %s", exc)
    return {"ok": True}
