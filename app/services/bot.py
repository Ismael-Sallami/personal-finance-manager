"""Telegram bot on top of the Bot API (plain HTTP, no extra dependency).

It runs as a webhook: Telegram POSTs every update to /tg/webhook, and this
module validates and dispatches it. Security:
  - The webhook is registered with a `secret_token`. Telegram sends it back in
    the `X-Telegram-Bot-Api-Secret-Token` header, which the router checks.
  - Only chats in `settings.allowed_chat_ids` are served. When the allowlist is
    empty (setup mode), the bot replies with the chat_id so the owner can put it
    in TELEGRAM_CHAT_ID.

It supports a button menu (inline keyboards and callback queries) and asks for
confirmation before saving anything. Commands:
  /start /menu /help · /summary [mm/yyyy] · /networth · /report [mm/yyyy]
  /expenses [mm/yyyy] · /cashflow [yyyy] · /savings [mm/yyyy] · /banks
  /bank <name> <balance>  (creates or updates an account)
  Analysis: /pace /merchants /subscriptions /pending /budget /categories
  Bank mode: /sync /connections /connect /disconnect (see bot_banks.py)
  Free text with expense lines -> preview with Confirm/Cancel.
"""
from __future__ import annotations

import logging
import secrets
from collections import OrderedDict
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.format import money
from app.services.expenses_parse import parse_text
from app.templating import MONTHS

log = logging.getLogger("bot")

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20

# Entries waiting for confirmation: token -> (chat_id, ParsedExpenses).
_PENDING: "OrderedDict[str, tuple]" = OrderedDict()
_MAX_PENDING = 50


def _url(method: str) -> str:
    return API.format(token=settings.telegram_bot_token, method=method)


# --- Inline keyboards ---
def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    """Build an inline_keyboard from rows of (text, callback_data)."""
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row] for row in rows
    ]}


def menu_kb(pending: int = 0, bank_mode: bool | None = None) -> dict:
    """Menu by sections: this month · analysis · money · banks · reports.
    The bank section only shows in bank mode."""
    if bank_mode is None:
        bank_mode = settings.bank_sync_enabled
    pend = f"🏷 Pending ({pending})" if pending else "🏷 Pending"
    rows = [
        [("📊 Summary", "m:summary"), ("⏱ Pace", "m:pace"), ("📐 Net", "m:net")],
        [("🧾 Expenses", "m:cats"), ("💶 Income", "m:income"), ("📋 Breakdown", "m:breakdown")],
        [("🏪 Merchants", "m:merchants"), ("🗂 Categories", "m:categories")],
        [("🔁 Subscriptions", "m:subs"), ("🎯 Budgets", "m:budget")],
        [("💼 Net worth", "m:networth"), ("🏦 Balances", "m:banks"), ("📈 Cashflow", "m:cashflow")],
    ]
    if bank_mode:
        rows += [
            [(pend, "m:pending"), ("✏️ Move", "m:move"), ("🔄 Sync", "m:sync")],
            [("🔗 Connections", "m:connections"), ("➕ Connect bank", "m:connect")],
        ]
    rows += [
        [("📄 PDF report", "m:report"), ("📋 Detailed PDF", "m:detailed")],
        [("❓ Help", "m:help")],
    ]
    return _kb(rows)


MENU_KB = menu_kb(bank_mode=False)  # backwards compatible

# Commands Telegram suggests when typing "/" (setMyCommands). Order = order in
# the list. Names: lower case, no accents, <= 32 characters.
COMMANDS = [
    ("menu", "Menu with buttons"),
    ("summary", "Income, expenses, savings and pace of the month [mm/yyyy]"),
    ("pace", "Spent so far, projection and comparison with last month"),
    ("expenses", "Expenses by category [mm/yyyy]"),
    ("income", "Income by category [mm/yyyy]"),
    ("merchants", "Where the money goes [mm/yyyy]"),
    ("subscriptions", "Detected monthly charges and their yearly cost"),
    ("pending", "Categorise new movements (it learns)"),
    ("categories", "Your categories and the ones created on their own"),
    ("move", "Fix the category of an entry [text]"),
    ("breakdown", "Entries one by one [mm/yyyy] [category]"),
    ("budget", "See budgets or set one: Category amount"),
    ("net", "Income - expenses [mm/yyyy | yyyy | range]"),
    ("savings", "Savings of the period [mm/yyyy]"),
    ("cashflow", "Income vs expenses of the year [yyyy]"),
    ("networth", "Investments + banks"),
    ("balance", "Balances by kind; set one: name amount (savings, cash...)"),
    ("invested", "Invested capital per platform [name amount]"),
    ("banks", "Balance of each account"),
    ("bank", "Set a balance by hand: name balance"),
    ("sync", "Fetch the bank movements now"),
    ("connections", "Connected banks and days of consent left"),
    ("connect", "Connect or renew a bank (read-only)"),
    ("disconnect", "Disconnect a bank (revokes the consent)"),
    ("report", "PDF with charts [mm/yyyy | yyyy | range]"),
    ("detailed", "Detailed PDF [period]"),
    ("help", "All the commands"),
]


async def setup_commands() -> None:
    """Register the command list for Telegram's autocomplete."""
    payload = {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(_url("setMyCommands"), json=payload)
    log.info("setMyCommands -> %s", r.status_code)


def _menu_text() -> tuple[str, dict]:
    from app.services import bot_banks as bb
    pending = 0
    if settings.bank_sync_enabled:
        db = SessionLocal()
        try:
            pending = bb.pending_count(db)
        except Exception:
            pending = 0
        finally:
            db.close()
    text = ("🤖 <b>Finance Manager</b>\nWhat do you want to see? "
            "Type <code>/</code> to list every command.")
    return text, menu_kb(pending)


# --- Sending (async, used by the webhook) ---
async def send_message(chat_id: str | int, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        await c.post(_url("sendMessage"), json=payload)


async def edit_message_text(chat_id: str | int, message_id: int, text: str,
                            reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        await c.post(_url("editMessageText"), json=payload)


async def answer_callback_query(callback_id: str, text: str = "") -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        await c.post(_url("answerCallbackQuery"),
                     json={"callback_query_id": callback_id, "text": text})


async def send_document(chat_id: str | int, data: bytes, filename: str,
                        caption: str = "", mime: str = "application/pdf") -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        await c.post(_url("sendDocument"),
                     data={"chat_id": str(chat_id), "caption": caption},
                     files={"document": (filename, data, mime)})


# --- Synchronous sending, for the scheduler running in its own thread ---
def send_document_sync(chat_id: str | int, data: bytes, filename: str,
                       caption: str = "", mime: str = "application/pdf") -> None:
    """Send a file without async. Raises if Telegram answers with an error.

    Unlike send_message_sync, the failure does propagate here: the backup uses
    it, and a backup that fails silently is worse than no backup at all.
    """
    r = httpx.post(_url("sendDocument"),
                   data={"chat_id": str(chat_id), "caption": caption},
                   files={"document": (filename, data, mime)},
                   timeout=60)
    r.raise_for_status()


def send_message_sync(chat_id: str | int, text: str) -> None:
    try:
        httpx.post(_url("sendMessage"), json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        }, timeout=TIMEOUT)
    except Exception as exc:
        log.warning("send_message_sync failed: %s", exc)


async def setup_webhook() -> None:
    """Register the webhook with Telegram. Idempotent."""
    if not settings.public_base_url:
        log.info("PUBLIC_BASE_URL is empty: the webhook is not registered.")
        return
    url = settings.public_base_url.rstrip("/") + "/tg/webhook"
    payload = {"url": url, "drop_pending_updates": True,
               "allowed_updates": ["message", "callback_query"]}
    if settings.telegram_webhook_secret:
        payload["secret_token"] = settings.telegram_webhook_secret
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(_url("setWebhook"), json=payload)
    log.info("setWebhook %s -> %s", url, r.status_code)


# --- Authorisation ---
def is_allowed(chat_id: str | int) -> bool:
    allow = settings.allowed_chat_ids
    return (not allow) or (str(chat_id) in allow)


# --- Period parsing, "mm/yyyy" ---
def _parse_period(arg: str) -> tuple[int, int | None]:
    today = date.today()
    arg = (arg or "").strip()
    if not arg:
        return today.year, today.month
    arg = arg.replace("-", "/")
    if "/" in arg:
        mm, yy = arg.split("/", 1)
        try:
            return int(yy), max(1, min(12, int(mm)))
        except ValueError:
            return today.year, today.month
    # A year on its own.
    try:
        return int(arg), None
    except ValueError:
        return today.year, today.month


# Words ignored when parsing a range ("01/2026 to 03/2026", "... - ...").
_CONNECTORS = {"to", "until", "-", "and"}


def _parse_range(arg: str) -> tuple[int, int, int, int, str]:
    """Parse a period into (y1, m1, y2, m2, label).

    Accepts: empty (current month), 'mm/yyyy' (that month), 'yyyy' (whole year)
    and ranges 'mm/yyyy mm/yyyy' (also 'yyyy yyyy' or a mix).
    """
    toks = [t for t in (arg or "").split() if t.lower() not in _CONNECTORS]
    if not toks:
        y, m = _parse_period("")
        return y, m, y, m, f"{MONTHS[m]} {y}"
    if len(toks) == 1:
        y, m = _parse_period(toks[0])
        if m:
            return y, m, y, m, f"{MONTHS[m]} {y}"
        return y, 1, y, 12, str(y)  # a year alone means the whole year
    # Range: first and last token. A year with no month starts in January and
    # ends in December.
    ya, ma = _parse_period(toks[0])
    yb, mb = _parse_period(toks[-1])
    m1, m2 = (ma or 1), (mb or 12)
    return ya, m1, yb, m2, f"{MONTHS[m1][:3]} {ya} – {MONTHS[m2][:3]} {yb}"


def _net_txt(arg: str) -> str:
    from app.services.aggregation import range_totals
    y1, m1, y2, m2, label = _parse_range(arg)
    db = SessionLocal()
    try:
        expense, income = range_totals(db, y1, m1, y2, m2)
    finally:
        db.close()
    net = income - expense
    icon = "🟢" if net >= 0 else "🔴"
    return (
        f"📐 <b>Net · {label}</b>\n"
        f"Income: {money(income)}\n"
        f"Expenses: {money(expense)}\n"
        f"{icon} <b>Net: {money(net)}</b>"
    )


HELP = (
    "🤖 <b>Finance Manager</b>\n\n"
    "Tap a button or write to me. To add entries, send me lines:\n"
    "<code>- Groceries: 15-1+3\n- Expense: Fuel: 50\n- Income: Salary: 1200</code>\n\n"
    "Commands:\n"
    "/menu — buttons\n"
    "/summary [mm/yyyy] — income, expenses, savings, net worth\n"
    "/net [mm/yyyy | yyyy | mm/yyyy mm/yyyy] — income, expenses and net (ranges allowed)\n"
    "/networth — investments and banks\n"
    "/balance [name amount] — balances by kind; accounts the app cannot read (savings, cash)\n"
    "/invested [platform amount] — invested capital (Trade Republic, MyInvestor...)\n"
    "/banks — balance per account\n"
    "/bank &lt;name&gt; &lt;balance&gt; — update an account\n"
    "/expenses [mm/yyyy] — expenses by category\n"
    "/income [mm/yyyy] — income by category\n"
    "/cashflow [yyyy] — income vs expenses of the year\n"
    "/savings [mm/yyyy] — savings of the period\n"
    "/report [mm/yyyy | yyyy | mm/yyyy mm/yyyy] — PDF with charts (ranges allowed)\n"
    "/detailed [period] — full PDF: net worth, categories and cashflow\n\n"
    "<b>Analysis</b>\n"
    "/pace — spent so far, projection and comparison with last month\n"
    "/merchants [mm/yyyy] — where the money goes\n"
    "/subscriptions — detected monthly charges\n"
    "/pending — categorise new movements (it learns)\n"
    "/categories — your categories and the base ones\n"
    "/move [text] — fix a wrongly classified entry\n"
    "/breakdown [mm/yyyy] [category] — entries one by one\n"
    "/budget [category amount] — see or set budgets\n\n"
    "<b>Bank sync</b> (bank mode only, see docs/BANK_SYNC.md)\n"
    "/sync — fetch the movements now\n"
    "/connections — connected banks and days of consent left\n"
    "/connect — connect or renew a bank (buttons)\n"
    "/disconnect — remove a bank (what was imported stays)\n\n"
    "💡 Type <code>/</code> and Telegram suggests the commands."
)


# --- Main dispatcher (async, called from the webhook) ---
async def handle_update(update: dict) -> None:
    if update.get("callback_query"):
        await _handle_callback(update["callback_query"])
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return

    if not is_allowed(chat_id):
        # Setup mode: with no allowlist, help the owner register their chat.
        if not settings.allowed_chat_ids:
            await send_message(chat_id,
                               f"Your chat_id is <code>{chat_id}</code>.\n"
                               "Add it to TELEGRAM_CHAT_ID to switch me on.")
        else:
            log.warning("update from an unauthorised chat: %s", chat_id)
        return

    cmd = text.split()[0].lower()
    arg = text[len(cmd):].strip()

    # "/command@MyBot" (groups or autocomplete) -> "/command"
    cmd = cmd.split("@", 1)[0]
    if cmd.startswith("/") and cmd != "/connect":
        from app.services import bot_banks as bb
        bb._WAITING_BANK.clear()  # another command cancels the bank search

    if cmd in ("/start", "/menu"):
        text_, kb = _menu_text()
        await send_message(chat_id, text_, reply_markup=kb)
    elif cmd == "/help":
        await send_message(chat_id, HELP)
    elif cmd == "/summary":
        await send_message(chat_id, _summary(*_parse_period(arg)))
    elif cmd == "/net":
        await send_message(chat_id, _net_txt(arg))
    elif cmd == "/networth":
        await send_message(chat_id, _net_worth_txt())
    elif cmd == "/banks":
        await send_message(chat_id, _banks_txt())
    elif cmd == "/bank":
        await send_message(chat_id, _bank_set(arg))
    elif cmd == "/expenses":
        await send_message(chat_id, _expenses_txt(*_parse_period(arg)))
    elif cmd == "/income":
        from app.services import bot_banks as bb
        await send_message(chat_id, bb.income_txt(*_parse_period(arg)))
    elif cmd == "/cashflow":
        year, _ = _parse_period(arg)
        await send_message(chat_id, _cashflow_txt(year))
    elif cmd == "/savings":
        await send_message(chat_id, _savings_txt(*_parse_period(arg)))
    elif cmd == "/report":
        await _send_report(chat_id, arg)
    elif cmd == "/detailed":
        await _send_detailed(chat_id, arg)
    elif cmd in _BANK_CMDS:
        await _banks_cmd(chat_id, cmd, arg)
    elif cmd.startswith("/"):
        await send_message(chat_id, "Command not recognised. /help")
    else:
        from app.services import bot_banks as bb
        if bb.waiting_for_bank() and ":" not in text and "\n" not in text \
                and not bb.waiting_for_category():  # search in "➕ Other bank"
            text_, kb = bb.banks_of(bb._WAITING_BANK["country"], text)
            await send_message(chat_id, text_, reply_markup=kb)
            return
        if bb.waiting_for_category() and (":" in text or "\n" in text):
            bb._WAITING.clear()  # these are entries, not a name: forget the wait
        if bb.waiting_for_category():  # reply to "✏️ Other category"
            await send_message(chat_id, bb.typed_category(text))
            if not bb.waiting_for_category():
                text_, kb = bb.next_pending()
                await send_message(chat_id, text_, reply_markup=kb)
            return
        await _preview_entries(chat_id, text)


async def _handle_callback(cb: dict) -> None:
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    message_id = cb.get("message", {}).get("message_id")
    data = cb.get("data") or ""
    cb_id = cb.get("id")
    if chat_id is None or not is_allowed(chat_id):
        if cb_id:
            await answer_callback_query(cb_id)
        return
    await answer_callback_query(cb_id or "")

    if data.startswith("m:"):
        action = data[2:]
        if action == "summary":
            await send_message(chat_id, _summary(*_parse_period("")))
        elif action == "networth":
            await send_message(chat_id, _net_worth_txt())
        elif action == "banks":
            await send_message(chat_id, _banks_txt())
        elif action == "cats":
            await send_message(chat_id, _expenses_txt(*_parse_period("")))
        elif action == "income":
            from app.services import bot_banks as bb
            await send_message(chat_id, bb.income_txt(*_parse_period("")))
        elif action == "cashflow":
            await send_message(chat_id, _cashflow_txt(date.today().year))
        elif action == "net":
            await send_message(chat_id, _net_txt(""))
        elif action == "report":
            await _send_report(chat_id, "")
        elif action == "detailed":
            await _send_detailed(chat_id, "")
        elif f"/{action}" in _MENU_BANK:
            await _banks_cmd(chat_id, _MENU_BANK[f"/{action}"], "")
        else:
            await send_message(chat_id, HELP)
        return

    if data.startswith("st:"):  # /balance: kind of the new account
        from app.services import bot_banks as bb
        await edit_message_text(chat_id, message_id, bb.create_account_kind(data[3:]))
        return
    if data.startswith(("par:", "rl:", "rs:")):  # mirror pairs and bank roles
        from app.services import bot_banks as bb
        if data.startswith("par:"):
            text_, kb = bb.answer_pair(data)
        elif data.startswith("rl:"):
            text_, kb = bb.choose_role(data[3:])
        else:
            _, role, bank = data.split(":", 2)
            text_, kb = bb.set_role(role, bank), bb.connections_kb()
        await edit_message_text(chat_id, message_id, text_, reply_markup=kb)
        return
    if data.startswith("dg:"):  # /breakdown: first part in the card, the rest apart
        from app.services import bot_banks as bb
        chunks = bb.breakdown_callback(data)
        await edit_message_text(chat_id, message_id, chunks[0])
        for chunk in chunks[1:]:
            await send_message(chat_id, chunk)
        return
    if data.startswith(("mv:", "mc:")):  # /move
        from app.services import bot_banks as bb
        text_, kb = bb.move_callback(data)
        await edit_message_text(chat_id, message_id, text_, reply_markup=kb)
        return
    if data == "dn":
        await edit_message_text(chat_id, message_id, "Cancelled, nothing was disconnected.")
        return
    if data.startswith("dd:"):
        from app.services import bot_banks as bb
        await edit_message_text(chat_id, message_id, bb.disconnect(data[3:]))
        return
    if data.startswith("d:"):
        from app.services import bot_banks as bb
        text_, kb = bb.confirm_disconnect(data[2:])
        await edit_message_text(chat_id, message_id, text_, reply_markup=kb)
        return

    if data == "k:+":  # /connect -> ➕ Other bank
        from app.services import bot_banks as bb
        text_, kb = bb.choose_country()
        await edit_message_text(chat_id, message_id, text_, reply_markup=kb)
        return
    if data.startswith(("kp:", "kl:", "kn:")):
        from app.services import bot_banks as bb
        text_, kb = bb.banks_callback(data)
        await edit_message_text(chat_id, message_id, text_, reply_markup=kb)
        return
    if data.startswith("k:"):  # bank button in /connect
        from app.services import bot_banks as bb
        await edit_message_text(chat_id, message_id, bb.connect_txt(data[2:]))
        return

    if data.startswith("p:"):
        from app.services import bot_banks as bb
        text_, kb, go_on = bb.answer_pending(data)
        if go_on:  # same card: confirmation on top + next entry
            text_, kb = bb.next_pending(previous=text_)
        await edit_message_text(chat_id, message_id, text_, reply_markup=kb)
        return

    if data.startswith("c:ok:"):
        token = data[5:]
        pending = _PENDING.pop(token, None)
        if not pending:
            await edit_message_text(chat_id, message_id, "⚠ Expired. Paste the lines again.")
            return
        await edit_message_text(chat_id, message_id, _insert_entries(pending[1]))
        return

    if data.startswith("c:no:"):
        _PENDING.pop(data[5:], None)
        await edit_message_text(chat_id, message_id, "❌ Cancelled, nothing saved.")
        return


# --- Bank sync / analysis commands (bot_banks.py) ---
_BANK_CMDS = {"/pace", "/merchants", "/subscriptions", "/pending", "/budget",
              "/connect", "/connections", "/sync", "/disconnect", "/categories", "/move",
              "/breakdown", "/invested", "/balance"}
# Commands that only make sense with the bank connected (else: "bank sync is off").
_NEEDS_BANK = {"/connect", "/connections", "/sync", "/disconnect"}
# menu callback -> command
_MENU_BANK = {"/pace": "/pace", "/merchants": "/merchants", "/subs": "/subscriptions",
              "/pending": "/pending", "/budget": "/budget", "/sync": "/sync",
              "/connections": "/connections", "/connect": "/connect",
              "/move": "/move", "/breakdown": "/breakdown", "/categories": "/categories"}


async def _banks_cmd(chat_id, cmd: str, arg: str) -> None:
    import asyncio

    from app.services import bot_banks as bb
    if cmd in _NEEDS_BANK and not settings.bank_sync_enabled:
        await send_message(chat_id, bb.OFF_TEXT)
        return
    if cmd == "/pace":
        await send_message(chat_id, bb.pace_txt())
    elif cmd == "/merchants":
        await send_message(chat_id, bb.merchants_txt(*_parse_period(arg)))
    elif cmd == "/subscriptions":
        await send_message(chat_id, bb.subscriptions_txt())
    elif cmd == "/pending":
        bb._SKIPPED.clear()
        bb._WAITING.clear()
        bb._SESSION["done"] = 0
        bb.reopen_negatives_bot()
        text, kb = bb.next_pending()
        await send_message(chat_id, text, reply_markup=kb)
    elif cmd == "/budget":
        await send_message(chat_id, bb.budget_txt(arg))
    elif cmd == "/connect":
        parts = arg.split()
        if len(parts) == 2 and parts[0].lower() == "country" and len(parts[1]) == 2:
            text, kb = bb.banks_of(parts[1].upper())
            await send_message(chat_id, text, reply_markup=kb)
        elif arg.strip():
            await send_message(chat_id, bb.connect_txt(arg))
        else:
            text, kb = bb.choose_bank()
            await send_message(chat_id, text, reply_markup=kb)
    elif cmd == "/balance":
        text, kb = bb.balance_txt(arg)
        await send_message(chat_id, text, reply_markup=kb)
    elif cmd == "/invested":
        await send_message(chat_id, bb.invested_txt(arg))
    elif cmd == "/connections":
        await send_message(chat_id, bb.connections_txt(), reply_markup=bb.connections_kb())
    elif cmd == "/breakdown":
        # /breakdown [mm/yyyy | yyyy] [category or merchant]
        toks = arg.split()
        period = toks[0] if toks and toks[0][:1].isdigit() else ""
        rest = " ".join(toks[1:] if period else toks)
        y, m = _parse_period(period)
        if rest:
            for chunk in bb.breakdown_txt(y, m, rest):
                await send_message(chat_id, chunk)
        else:
            text, kb = bb.breakdown_menu(y, m)
            await send_message(chat_id, text, reply_markup=kb)
    elif cmd == "/move":
        text, kb = bb.move_list(arg)
        await send_message(chat_id, text, reply_markup=kb)
    elif cmd == "/categories":
        await send_message(chat_id, bb.categories_txt())
    elif cmd == "/disconnect":
        text, kb = bb.disconnect_options()
        await send_message(chat_id, text, reply_markup=kb)
    elif cmd == "/sync":
        await send_message(chat_id, "🔄 Syncing…")

        def _run():
            db = SessionLocal()
            try:
                return bb.run_sync(db, force=True)
            finally:
                db.close()
        r = await asyncio.to_thread(_run)
        await send_message(chat_id, bb.sync_text(r, force=True))


# --- Command implementations (sync DB calls inside async funcs; they are quick) ---
def _summary(year: int, month: int | None) -> str:
    from app.services.aggregation import (
        build_dashboard,
        invested_in_period,
        period_totals,
    )
    db = SessionLocal()
    try:
        expense, income = period_totals(db, year, month)
        invested = invested_in_period(db, year, month)
        k = build_dashboard(db, year, month)["kpis"]
        today = date.today()
        pace = ""
        if (year, month) == (today.year, today.month) and expense:
            from app.services import bot_banks, insights
            pace = "\n" + bot_banks.pace_line(insights.pace(db, today))
    finally:
        db.close()
    period = f"{MONTHS[month]} {year}" if month else str(year)
    note = f"\nℹ Of which {money(invested)} was invested" if invested > 0 else ""
    return (
        f"📊 <b>Summary {period}</b>\n"
        f"Income: {money(income)}\n"
        f"Expenses: {money(expense)}\n"
        f"Saved: {money(income - expense)}{note}{pace}\n"
        f"💼 Net worth: {money(k['net_worth'])}\n"
        f"   📈 {money(k['inv_value'])} · 🏦 {money(k['bank_balance'])}"
    )


def _net_worth_txt() -> str:
    from app.services.aggregation import build_dashboard
    db = SessionLocal()
    try:
        k = build_dashboard(db)["kpis"]
    finally:
        db.close()
    from app.services import bot_banks as bb
    from app.services.aggregation import invested_capital
    db = SessionLocal()
    try:
        caps = [(c.name, c.balance) for c in invested_capital(db)]
        notices = bb.investment_notices(db)
    finally:
        db.close()
    if caps:  # invested capital: same view as /balance, grouped by kind
        return bb.balance_summary() + ("\n\n" + "\n".join(notices) if notices else "")
    else:
        block = (f"📈 <b>Investments</b>: {money(k['inv_value'])}\n"
                 f"   Invested: {money(k['inv_invested'])}\n"
                 f"   Profit: {money(k['inv_profit'])} ({k['return_pct']} %)\n")
    return (
        f"💼 <b>Net worth: {money(k['net_worth'])}</b>\n\n" + block +
        f"🏦 <b>Banks and cash</b>: {money(k['bank_balance'])}"
        + ("\n\n" + "\n".join(notices) if notices else "")
    )


def _banks_txt() -> str:
    from app.models import BankAccount
    db = SessionLocal()
    try:
        from app.services import bot_banks as bb
        from app.services.aggregation import INVESTMENT_KIND
        accounts = db.scalars(select(BankAccount).where(BankAccount.kind != INVESTMENT_KIND)
                              .order_by(BankAccount.balance.desc())).all()
        total = sum((a.balance for a in accounts), Decimal("0"))
        lines = "\n".join(f"• {a.name}: {money(a.balance)}" for a in accounts)
        notices = bb.investment_notices(db)
    finally:
        db.close()
    extra = ("\n\n" + "\n".join(notices)) if notices else ""
    if not lines:
        return "🏦 No accounts yet. Add one with <code>/bank Savings 1500</code>" + extra
    return f"🏦 <b>Banks</b>\n{lines}\n\n<b>Total: {money(total)}</b>\n📈 /invested{extra}"


def _bank_set(arg: str) -> str:
    """Create or update an account: '/bank &lt;name&gt; &lt;balance&gt;'."""
    from app.models import BankAccount
    from app.services.aggregation import snapshot_today
    parts = (arg or "").rsplit(" ", 1)
    if len(parts) != 2 or not parts[0].strip():
        return "Usage: <code>/bank Savings 1500</code>"
    name = parts[0].strip()
    try:
        balance = Decimal(parts[1].strip().replace(",", "."))
    except Exception:
        return "That balance is not a number. Example: <code>/bank Cash 200</code>"
    db = SessionLocal()
    try:
        account = db.scalar(
            select(BankAccount).where(func.lower(BankAccount.name) == name.lower())
        )
        if account is None:
            account = BankAccount(name=name, kind="account", balance=balance)
            db.add(account)
            verb = "Created"
        else:
            account.balance = balance
            account.updated_at = datetime.now(timezone.utc)
            verb = "Updated"
        db.commit()
        snapshot_today(db)
        total = db.scalar(select(func.sum(BankAccount.balance))) or Decimal("0")
    finally:
        db.close()
    return f"✅ {verb} <b>{name}</b>: {money(balance)}\n🏦 Total in banks: {money(total)}"


def _expenses_txt(year: int, month: int | None) -> str:
    from app.services.aggregation import build_dashboard
    db = SessionLocal()
    try:
        cats = build_dashboard(db, year, month)["charts"]["categories"]
    finally:
        db.close()
    period = f"{MONTHS[month]} {year}" if month else str(year)
    if not cats["labels"]:
        return f"🧾 No expenses in {period}."
    from app.services import bot_banks as bb
    rows = "\n".join(
        f"{bb._icon(lbl)} {lbl}: {money(val)}" for lbl, val in zip(cats["labels"], cats["data"])
    )
    total = sum(cats["data"])
    return (f"🧾 <b>Expenses by category · {period}</b>\n{rows}\n\n<b>Total: {money(total)}</b>"
            + bb.negatives_notice(year, month))


def _cashflow_txt(year: int) -> str:
    from app.services.aggregation import build_dashboard
    db = SessionLocal()
    try:
        cf = build_dashboard(db, year)["charts"]["cashflow"]
    finally:
        db.close()
    rows = []
    for i in range(12):
        inc, exp = cf["income"][i], cf["expenses"][i]
        if inc or exp:
            rows.append(f"• {MONTHS[i + 1][:3]}: +{money(inc)} / -{money(exp)}")
    if not rows:
        return f"📈 Nothing recorded in {year}."
    return f"📈 <b>Cashflow {year}</b> (income / expenses)\n" + "\n".join(rows)


def _savings_txt(year: int, month: int | None) -> str:
    from app.services.aggregation import invested_in_period, period_totals
    db = SessionLocal()
    try:
        expense, income = period_totals(db, year, month)
        invested = invested_in_period(db, year, month)
    finally:
        db.close()
    period = f"{MONTHS[month]} {year}" if month else str(year)
    saved = income - expense
    icon = "🟢" if saved >= 0 else "🔴"
    note = f"\nℹ Of which {money(invested)} was invested" if invested > 0 else ""
    return (f"{icon} <b>Saved in {period}</b>: {money(saved)}\n"
            f"Income {money(income)} − Expenses {money(expense)}{note}")


async def _send_report(chat_id, arg: str) -> None:
    from app.services.reports import generate
    y1, m1, y2, m2, label = _parse_range(arg)
    try:
        db = SessionLocal()
        try:
            if y1 == y2 and m1 == m2:                  # one month
                data = generate.monthly_pdf(db, y1, m1)
                name = f"Report_{MONTHS[m1]}_{y1}.pdf"
            elif y1 == y2 and m1 == 1 and m2 == 12:    # a whole year
                data = generate.yearly_pdf(db, y1)
                name = f"Yearly_summary_{y1}.pdf"
            else:                                      # any range
                data = generate.period_pdf(db, y1, m1, y2, m2, label)
                name = f"Report_{m1:02d}{y1}-{m2:02d}{y2}.pdf"
        finally:
            db.close()
    except Exception:
        log.exception("report failed (%s)", arg)
        await send_message(chat_id, "⚠ Could not build the report. Check the period.")
        return
    await send_document(chat_id, data, name, caption=f"Report {label}")


async def _send_detailed(chat_id, arg: str) -> None:
    from app.services.reports import generate
    y1, m1, y2, m2, label = _parse_range(arg)
    try:
        db = SessionLocal()
        try:
            data = generate.detailed_pdf(db, y1, m1, y2, m2, label)
        finally:
            db.close()
    except Exception:
        log.exception("detailed report failed (%s)", arg)
        await send_message(chat_id, "⚠ Could not build the detailed report. Check the period.")
        return
    await send_document(chat_id, data,
                        f"Detailed_report_{m1:02d}{y1}-{m2:02d}{y2}.pdf",
                        caption=f"Detailed report {label}")


# --- Adding entries, with confirmation ---
async def _preview_entries(chat_id, text: str) -> None:
    parsed = parse_text(text)
    if not parsed.items:
        await send_message(chat_id, "No entries found. Format: <code>Category: 12+3</code>\n/help")
        return
    token = secrets.token_urlsafe(6)
    _PENDING[token] = (chat_id, parsed)
    while len(_PENDING) > _MAX_PENDING:
        _PENDING.popitem(last=False)  # drop the oldest

    month = parsed.month or date.today().month
    lines = "\n".join(
        f"• {it.category}: {money(it.amount)} ({it.kind})" for it in parsed.items
    )
    warn = f"\n⚠ {len(parsed.ignored)} lines skipped." if parsed.ignored else ""
    body = (f"👀 <b>About to add {len(parsed.items)} entries to {MONTHS[month]}</b>:\n"
            f"{lines}{warn}\n\nConfirm?")
    await send_message(chat_id, body, reply_markup=_kb([
        [("✅ Confirm", f"c:ok:{token}"), ("❌ Cancel", f"c:no:{token}")]
    ]))


def _insert_entries(parsed) -> str:
    """Insert the confirmed entries and return the summary (sync)."""
    from app.models import Transaction
    today = date.today()
    month = parsed.month or today.month
    db = SessionLocal()
    total_e = total_i = 0.0
    try:
        for it in parsed.items:
            # Current month: today's date (it lines up with the bank movements in
            # /pace and the comparisons). Another month: day 1, as always.
            day = today if month == today.month else date(today.year, month, 1)
            db.add(Transaction(
                date=day, year=today.year, month=month,
                category=it.category, kind=it.kind, amount=it.amount,
                note="telegram",
            ))
            if it.kind == "expense":
                total_e += float(it.amount)
            else:
                total_i += float(it.amount)
        db.commit()
    finally:
        db.close()
    lines = "\n".join(
        f"• {it.category}: {money(it.amount)} ({it.kind})" for it in parsed.items
    )
    return (
        f"✅ Added {len(parsed.items)} entries to {MONTHS[month]}:\n{lines}\n\n"
        f"Expenses +{money(total_e)} · Income +{money(total_i)}"
    )


# --- Month-end reminder (called by the scheduler or the cron, sync) ---
def send_photo_sync(chat_id: str | int, data: bytes, caption: str = "") -> None:
    try:
        httpx.post(_url("sendPhoto"), data={"chat_id": str(chat_id), "caption": caption},
                   files={"photo": ("chart.png", data, "image/png")}, timeout=60)
    except Exception as exc:
        log.warning("send_photo_sync failed: %s", exc)


def daily_sync_notice() -> dict:
    """Daily job: sync the banks and send the alerts (only if there is news)."""
    from app.services import bot_banks as bb
    if not settings.bank_sync_enabled:
        return {"ok": False, "reason": "bank sync is off (manual mode)"}
    db = SessionLocal()
    try:
        r = bb.run_sync(db)
    finally:
        db.close()
    text = bb.sync_text(r)
    if text and settings.telegram_enabled:
        for chat_id in settings.allowed_chat_ids:
            send_message_sync(chat_id, text)
    return r


def month_end_reminder() -> None:
    if not settings.telegram_enabled:
        return
    today = date.today()
    from app.services import bot_banks as bb
    db = SessionLocal()
    try:
        if bb.has_banks(db):
            if settings.bank_sync_enabled:
                try:
                    bb.run_sync(db, force=True)  # so the close includes today's movements
                except Exception as exc:
                    log.warning("sync before the close failed: %s", exc)
            text, img = bb.month_end_summary(db, today)
            for chat_id in settings.allowed_chat_ids:
                send_message_sync(chat_id, text)
                if img:
                    send_photo_sync(chat_id, img)
            return
    finally:
        db.close()
    for chat_id in (settings.allowed_chat_ids or set()):
        send_message_sync(chat_id, (
            f"📅 End of <b>{MONTHS[today.month]}</b>. "
            "Send me the expenses and income of the month to record them.\n"
            "Example: <code>- Groceries: 40+12\n- Income: Salary: 1200</code>"
        ))
