"""The part of the bot dedicated to bank sync and to the analysis commands.

Kept apart from bot.py so it does not grow too much. The `*_txt` functions
return the text (Telegram HTML); sending reuses the functions in bot.py.
"""
from __future__ import annotations

import html
import logging
import secrets
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select

from app.config import settings
from app.format import money
from app.models import BankLink, Budget, Transaction
from app.services import insights
from app.services.bank_sync import TRANSFER, UNCATEGORIZED
from app.templating import MONTHS

log = logging.getLogger("bot")

# Keys accepted by /connect -> exact bank name at Enable Banking. Edit it for the
# banks of your country (list them with `enablebanking.aspsp`).
BANKS = {"imagin": "imagin", "revolut": "Revolut", "myinvestor": "MyInvestor Banco",
         "traderepublic": "Trade Republic", "tr": "Trade Republic"}
EXPIRY_WARNING_DAYS = 10

OFF_TEXT = "🏦 Bank sync is off. See docs/BANK_SYNC.md (manual mode: just paste your entries)."

# /pending buttons: token -> (tx_id, [categories], is_refund).
_CATEG: "OrderedDict[str, tuple[int, list[str], bool]]" = OrderedDict()
_SKIPPED: set[int] = set()
# Entry waiting for the user to type a new category name ("✏️ Other category").
_WAITING: dict[str, int] = {}


def _db():
    from app.services import bot  # SessionLocal can be patched in tests
    return bot.SessionLocal()


def _today() -> date:
    return date.today()


# --- Signed state for the authorisation callback ---
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="eb-auth")


def sign_state(bank: str) -> str:
    return _serializer().dumps({"b": bank, "n": secrets.token_hex(4)})


def read_state(state: str, max_age: int = 3600) -> str | None:
    try:
        return _serializer().loads(state, max_age=max_age)["b"]
    except (BadSignature, KeyError, TypeError):
        return None


def redirect_url() -> str:
    return settings.public_base_url.rstrip("/") + "/bank-sync/callback"


# --- /connect ---
def connect_txt(arg: str) -> str:
    if not settings.bank_sync_enabled:
        return OFF_TEXT
    key = (arg or "").strip().lower().replace(" ", "")
    bank = BANKS.get(key)
    if not bank:
        return ("Usage: <code>/connect bank</code>\n"
                "Banks: " + ", ".join(k for k in BANKS if k != "tr"))
    from app.services import enablebanking as eb
    try:
        url = eb.start_auth(bank, redirect_url(), sign_state(bank))
    except Exception as exc:
        log.warning("start_auth %s: %s", bank, exc)
        # Only the owner sees this (allowlist); the reason saves a trip to the logs.
        reason = html.escape(f"{type(exc).__name__}: {exc}"[:300])
        return (f"⚠ Could not start the connection with {bank}.\n"
                f"<code>{reason}</code>")
    return (f"🔗 <b>Connect {bank}</b> (read-only)\n"
            f'<a href="{url}">Open this link</a>, press "Continue authentication" '
            "and approve at your bank. The link expires in a few minutes.")


_BANK_BUTTONS = [("🟦 imagin", "imagin"), ("⬛ Revolut", "revolut"),
                 ("🟩 Trade Republic", "traderepublic"), ("🟨 MyInvestor (beta)", "myinvestor")]


def choose_bank() -> tuple[str, dict]:
    """Buttons for /connect with no argument (connect or renew the consent)."""
    from app.services.bot import _kb
    rows = [[(t, f"k:{k}") for t, k in _BANK_BUTTONS[i:i + 2]]
            for i in range(0, len(_BANK_BUTTONS), 2)]
    return ("🔗 <b>Which bank should I connect?</b>\nRead-only. If it was already "
            "connected, this renews the consent."), _kb(rows)


def connections_txt() -> str:
    db = _db()
    try:
        links = db.scalars(select(BankLink).order_by(BankLink.bank)).all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = []
        for link in links:
            days = (link.valid_until - now).days
            icon = "🔴" if days < 0 else "🟠" if days <= EXPIRY_WARNING_DAYS else "🟢"
            acct = f" …{link.iban[-4:]}" if link.iban else ""
            sync = link.last_sync.strftime("%d/%m") if link.last_sync else "never"
            state = "expired" if days < 0 else f"{days} days"
            rows.append(f"{icon} {link.bank}{acct}: {state} · last sync {sync}")
    finally:
        db.close()
    if not rows:
        return "🏦 No banks connected. Use <code>/connect imagin</code>"
    return "🏦 <b>Connections</b>\n" + "\n".join(rows)


# --- Pace / comparison / merchants / subscriptions ---
def pace_line(r: insights.Pace) -> str:
    base = f"⏱ Day {r.day}: you have spent {money(r.spent)}. At this pace you end at {money(r.projection)}"
    if r.average:
        icon = "🔴" if r.projection > r.average * 11 / 10 else "🟢"
        return f"{base}; your average is {money(r.average)} {icon}"
    return base + "."


def pace_txt() -> str:
    today = _today()
    db = _db()
    try:
        r = insights.pace(db, today)
        comp = insights.comparison(db, today)[:5]
    finally:
        db.close()
    lines = [f"⏱ <b>Pace of {MONTHS[today.month]}</b>", pace_line(r)]
    if comp:
        lines.append("\n<b>Versus last month, same day</b>")
        for cat, cur, prev in comp:
            if prev:
                pct = (cur - prev) / prev * 100
                lines.append(f"• {cat}: {money(cur)} ({pct:+.0f} %)")
            else:
                lines.append(f"• {cat}: {money(cur)} (new)")
    return "\n".join(lines)


def merchants_txt(year: int, month: int | None) -> str:
    month = month or _today().month
    db = _db()
    try:
        top = insights.top_merchants(db, year, month, n=10)
    finally:
        db.close()
    if not top:
        return f"🏪 No purchases with a merchant in {MONTHS[month]} {year}."
    rows = "\n".join(f"{i}. {c}: {money(s)} ({k}×)" for i, (c, s, k) in enumerate(top, 1))
    return f"🏪 <b>Where the money goes · {MONTHS[month]} {year}</b>\n{rows}"


def subscriptions_txt() -> str:
    db = _db()
    try:
        subs = insights.subscriptions(db, _today())
    finally:
        db.close()
    if not subs:
        return "🔁 No subscriptions detected (it takes 3 monthly charges)."
    total = sum(s.amount for s in subs)
    rows = []
    for s in subs:
        up = f" ⬆ was {money(s.previous)}" if s.previous and s.amount > s.previous else ""
        rows.append(f"• {s.merchant}: {money(s.amount)}{up}")
    return ("🔁 <b>Subscriptions</b>\n" + "\n".join(rows) +
            f"\n\n<b>Total: {money(total)}/month · {money(total * 12)}/year</b>")


# --- Budgets ---
def budget_txt(arg: str) -> str:
    from decimal import Decimal, InvalidOperation
    parts = (arg or "").rsplit(" ", 1)
    db = _db()
    try:
        if len(parts) == 2 and parts[0].strip():
            cat = parts[0].strip()
            try:
                limit = Decimal(parts[1].replace(",", "."))
            except InvalidOperation:
                return "Usage: <code>/budget Eating out 150</code> (0 removes it)"
            b = db.scalar(select(Budget).where(func.lower(Budget.category) == cat.lower()))
            if limit <= 0:
                if b:
                    db.delete(b)
                    db.commit()
                return f"🗑 Budget for <b>{html.escape(cat)}</b> removed."
            if b is None:
                b = Budget(category=cat, monthly_limit=limit)
                db.add(b)
            else:
                b.monthly_limit, b.last_alert = limit, None
            db.commit()
            return (f"✅ Budget <b>{html.escape(cat)}</b>: {money(limit)}/month. "
                    "I will warn you at 80 % and 100 %.")
        status = insights.budget_status(db, _today())
    finally:
        db.close()
    if not status:
        return "🎯 No budgets. Create one: <code>/budget Eating out 150</code>"
    rows = []
    for b, spent in status:
        pct = spent / b.monthly_limit * 100 if b.monthly_limit else 0
        bar = "█" * min(10, int(pct // 10)) + "░" * max(0, 10 - int(pct // 10))
        rows.append(f"{html.escape(b.category)}: {money(spent)} / {money(b.monthly_limit)}\n"
                    f"<code>{bar}</code> {pct:.0f} %")
    return "🎯 <b>Budgets this month</b>\n" + "\n".join(rows)


# --- /pending: categorise with buttons ---
def _categories(db, kind: str) -> list[str]:
    rows = db.execute(
        select(Transaction.category, func.count()).where(
            Transaction.kind == kind,
            Transaction.category.notin_((UNCATEGORIZED, TRANSFER)),
        ).group_by(Transaction.category).order_by(func.count().desc()).limit(12)).all()
    return [c for c, _ in rows]


def next_pending() -> tuple[str, dict | None]:
    """(text, keyboard) of the next entry to categorise."""
    from app.services.bot import _kb
    db = _db()
    try:
        q = select(Transaction).where(Transaction.review.is_(True))
        if _SKIPPED:
            q = q.where(Transaction.id.notin_(tuple(_SKIPPED)))
        tx = db.scalar(q.order_by(Transaction.date.desc()))
        total = db.scalar(select(func.count()).select_from(Transaction)
                          .where(Transaction.review.is_(True))) or 0
        if tx is None:
            _SKIPPED.clear()
            return ("✅ Nothing left to categorise." if not total else
                    f"⏭ You skipped the {total} that remain. /pending to see them again."), None
        from app.services.bank_sync import is_bizum, pattern_of, suggestion
        cats = _categories(db, tx.kind)
        every = db.scalars(select(Transaction.category).distinct().where(
            Transaction.category.notin_((UNCATEGORIZED, TRANSFER)))).all()
        sug = suggestion(tx.merchant, tx.note, list(every))
        if sug and sug not in cats:
            cats.append(sug)
        pattern = pattern_of(tx.merchant, tx.note)
        if pattern:
            hint = f"I will remember it for everything that says “{pattern}”."
        elif is_bizum(tx.merchant, tx.note):
            hint = "Bizum with no useful description: I will not learn a rule, I will ask every time."
        else:
            hint = "I will not learn a rule for this one (no recognisable name)."
        token = _store_token(tx.id, cats, False)
        incoming = tx.kind == "income"
        sign = "−" if tx.kind == "expense" else "+"
        bizum = "💸 Bizum · " if is_bizum(tx.merchant, tx.note) else ""
        text = (f"❓ <b>{total} to categorise</b>\n\n"
                f"{tx.date:%d/%m} · {html.escape(tx.account or '')}\n"
                f"{bizum}<b>{html.escape(tx.merchant or '')}</b> {sign}{money(tx.amount)}"
                + (f"\n<i>{html.escape(tx.note)}</i>" if tx.note and tx.note != tx.merchant else "")
                + f"\n\nWhat is it? {hint}")
    finally:
        db.close()
    rows = []
    if sug:
        rows.append([(f"💡 {sug}", f"p:{token}:{cats.index(sug)}")])
    rest = [(c, i) for i, c in enumerate(cats) if c != sug]
    rows += [[(c, f"p:{token}:{i}") for c, i in rest[j:j + 3]]
             for j in range(0, len(rest), 3)]
    if incoming:
        rows.append([("↩ It is a refund (subtracts from an expense)", f"p:{token}:r")])
    rows.append([("✏️ Other category", f"p:{token}:n")])
    rows.append([("🔄 My own transfer", f"p:{token}:t"), ("⏭ Skip", f"p:{token}:s")])
    return text, _kb(rows)


def _store_token(tx_id: int, cats: list[str], refund: bool) -> str:
    token = secrets.token_urlsafe(6)
    _CATEG[token] = (tx_id, cats, refund)
    while len(_CATEG) > 50:
        _CATEG.popitem(last=False)
    return token


def _refund_keyboard(tx_id: int) -> tuple[str, dict]:
    """Second step of "↩ Refund": which expense is it from?"""
    from app.services.bot import _kb
    db = _db()
    try:
        cats = _categories(db, "expense")
    finally:
        db.close()
    token = _store_token(tx_id, cats, True)
    rows = [[(c, f"p:{token}:{i}") for i, c in enumerate(cats[j:j + 3], j)]
            for j in range(0, len(cats), 3)]
    rows.append([("✏️ Other category", f"p:{token}:n"), ("⏭ Skip", f"p:{token}:s")])
    return ("↩ Which expense is this refund from? It will subtract from that category "
            "(e.g. a Bizum for the shared wifi subtracts from Home)."), _kb(rows)


def answer_pending(data: str) -> tuple[str, dict | None, bool]:
    """Apply the pressed button -> (text, keyboard, go_to_next)."""
    result = _answer(data)
    if isinstance(result, tuple):  # asks for a second step (refund)
        return result[0], result[1], False
    return result, None, not waiting_for_category()


def _answer(data: str):
    from app.services.bank_sync import learn_rule, mark_transfer
    try:
        _, token, choice = data.split(":", 2)
    except ValueError:
        return "⚠ Invalid button."
    pending = _CATEG.pop(token, None)
    if not pending:
        return "⚠ Expired. Use /pending again."
    tx_id, cats, refund = pending
    if choice == "r":
        return _refund_keyboard(tx_id)
    if choice == "s":
        _SKIPPED.add(tx_id)
        return "⏭ Skipped."
    if choice == "n":
        _WAITING["tx"] = tx_id
        _WAITING["refund"] = int(refund)
        return ("✏️ Type the category name (new or existing). "
                "I will remember it for next time.")
    db = _db()
    try:
        tx = db.get(Transaction, tx_id)
        if tx is None:
            return "⚠ That entry no longer exists."
        if choice == "t":
            mark_transfer(db, tx)
            return f"🔄 {html.escape(tx.merchant or '')}: transfer between your accounts (not an expense)."
        try:
            cat = cats[int(choice)]
        except (ValueError, IndexError):
            return "⚠ Invalid button."
        from app.services.bank_sync import pattern_of
        pattern = pattern_of(tx.merchant, tx.note)
        others = learn_rule(db, tx, cat, refund=refund)
        extra = f" (and {others} more like it)" if others else ""
        rule = f"\n<i>Rule: “{pattern}” → {html.escape(cat)}</i>" if pattern else ""
        ref = " (refund: subtracts from the expense)" if refund else ""
        return (f"✅ {html.escape(tx.merchant or '')} → <b>{html.escape(cat)}</b>{ref}"
                f"{extra}{rule}")
    finally:
        db.close()


def pending_count(db) -> int:
    return db.scalar(select(func.count()).select_from(Transaction)
                     .where(Transaction.review.is_(True))) or 0


# --- Daily job ---
def run_sync(db) -> dict:
    """Sync and build the alerts. Sends nothing (the caller does)."""
    from app.services.bank_sync import sync_all
    today = _today()
    res = sync_all(db, today)
    alerts: list[str] = []
    for tx in res.new:
        for f in (insights.unusual_alert, insights.subscription_alert):
            try:
                a = f(db, tx)
            except Exception as exc:
                log.warning("alert %s failed: %s", f.__name__, exc)
                a = None
            if a:
                alerts.append(a)
    alerts.extend(insights.budget_alerts(db, today))

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for link in db.scalars(select(BankLink)).all():
        if (not link.expiry_notified
                and link.valid_until - now <= timedelta(days=EXPIRY_WARNING_DAYS)):
            key = next((k for k, v in BANKS.items() if v == link.bank), link.bank.lower())
            alerts.append(f"🔑 The consent for <b>{link.bank}</b> expires on "
                          f"{link.valid_until:%d/%m}. Renew it with <code>/connect {key}</code>")
            link.expiry_notified = True
    db.commit()
    return {"new": len(res.new), "transfers": res.transfers, "accounts": res.accounts,
            "errors": res.errors, "alerts": alerts, "pending": pending_count(db)}


def sync_text(r: dict, force: bool = False) -> str | None:
    """The sync message. None if there is nothing to say (so it does not nag)."""
    parts = list(r["alerts"])
    if r["pending"] and (r["new"] or force):
        parts.append(f"❓ You have {r['pending']} entries to categorise: /pending")
    if r["errors"]:
        parts.append("⚠ " + "; ".join(r["errors"]))
    if force:
        parts.insert(0, f"🔄 Sync: {r['new']} new, {r['transfers']} transfers, "
                        f"{r['accounts']} accounts.")
    return "\n\n".join(parts) if parts else None


# --- Month-end summary ---
def month_end_summary(db, today: date) -> tuple[str, bytes | None]:
    from app.services.aggregation import build_dashboard, period_totals
    from app.services.reports.charts import donut
    expense, income = period_totals(db, today.year, today.month)
    r = insights.pace(db, today)
    comp = insights.comparison(db, today)[:3]
    top = insights.top_merchants(db, today.year, today.month, n=3)
    pend = pending_count(db)
    cats = build_dashboard(db, today.year, today.month)["charts"]["categories"]
    saved = income - expense
    lines = [f"📅 <b>{MONTHS[today.month]} close</b>",
             f"Income: {money(income)}", f"Expenses: {money(expense)}",
             f"{'🟢' if saved >= 0 else '🔴'} Saved: <b>{money(saved)}</b>"]
    if r.average:
        diff = r.spent - r.average
        lines.append(f"Consumption {money(r.spent)} vs your average {money(r.average)} ({money(diff)})")
    if comp:
        lines.append("\n<b>What changed most</b>")
        lines += [f"• {c}: {money(a)} (was {money(p)})" for c, a, p in comp]
    if top:
        lines.append("\n<b>Top merchants</b>")
        lines += [f"• {c}: {money(s)}" for c, s, _ in top]
    if pend:
        lines.append(f"\n❓ {pend} left to categorise: /pending")
    img = None
    if cats["labels"]:
        try:
            img = donut(cats["labels"], cats["data"], f"Expenses {MONTHS[today.month]}")
        except Exception as exc:
            log.warning("month-end donut failed: %s", exc)
    return "\n".join(lines), img


def has_banks(db) -> bool:
    return bool(db.scalar(select(func.count()).select_from(BankLink)))


def waiting_for_category() -> bool:
    return "tx" in _WAITING


def typed_category(text: str) -> str:
    """The user typed the name after pressing "✏️ Other category"."""
    from app.services.bank_sync import learn_rule, norm
    tx_id = _WAITING.pop("tx", None)
    refund = bool(_WAITING.pop("refund", 0))
    name = " ".join((text or "").split())[:60]
    if tx_id is None:
        return "⚠ No entry was waiting for a category. /pending"
    if not name or name.startswith("/") or norm(name) in (norm(UNCATEGORIZED), norm(TRANSFER)):
        _WAITING["tx"] = tx_id
        _WAITING["refund"] = int(refund)
        return "✏️ That name will not do. Type another one (for example: <code>Pets</code>)."
    db = _db()
    try:
        tx = db.get(Transaction, tx_id)
        if tx is None:
            return "⚠ That entry no longer exists."
        # If it exists with another spelling ("groceries" vs "Groceries"), reuse it.
        existing = db.scalars(select(Transaction.category).distinct()).all()
        name = next((c for c in existing if norm(c) == norm(name)), name)
        is_new = name not in existing
        others = learn_rule(db, tx, name, refund=refund)
        extra = f" (and {others} more like it)" if others else ""
        tag = " 🆕" if is_new else ""
        return f"✅ {html.escape(tx.merchant or '')} → <b>{html.escape(name)}</b>{tag}{extra}"
    finally:
        db.close()


# --- /disconnect ---
def disconnect_options() -> tuple[str, dict | None]:
    from app.services.bot import _kb
    db = _db()
    try:
        banks = sorted(set(db.scalars(select(BankLink.bank)).all()))
    finally:
        db.close()
    if not banks:
        return "🏦 No bank is connected.", None
    rows = [[(f"✂ {b}", f"d:{b}")] for b in banks] + [[("Cancel", "dn")]]
    return ("✂ <b>Which bank should I disconnect?</b>\nThe consent is revoked and syncing "
            "stops. Entries already imported and the balance are kept."), _kb(rows)


def confirm_disconnect(bank: str) -> tuple[str, dict]:
    from app.services.bot import _kb
    return (f"Are you sure you want to disconnect <b>{html.escape(bank)}</b>?",
            _kb([[("Yes, disconnect", f"dd:{bank}"), ("Cancel", "dn")]]))


def disconnect(bank: str) -> str:
    from app.services import enablebanking as eb
    db = _db()
    try:
        links = db.scalars(select(BankLink).where(BankLink.bank == bank)).all()
        if not links:
            return f"{html.escape(bank)} was no longer connected."
        warnings = []
        for sid in {link.session_id for link in links}:
            try:
                eb.delete_session(sid)
            except Exception as exc:  # the consent expires on its own anyway
                log.warning("delete_session %s: %s", bank, exc)
                warnings.append("could not revoke it at Enable Banking (it will expire on its own)")
        for link in links:
            db.delete(link)
        db.commit()
    finally:
        db.close()
    extra = f"\n⚠ {warnings[0]}" if warnings else ""
    return (f"✂ <b>{html.escape(bank)}</b> disconnected. It no longer syncs; "
            f"what was imported stays. To come back: /connect{extra}")


# --- /categories ---
def categories_txt() -> str:
    """Your categories (with usage and spending this month) + the base ones
    that will be created on their own."""
    from app.services.bank_sync import BASE_CATEGORIES, norm
    today = _today()
    db = _db()
    try:
        rows = db.execute(
            select(Transaction.category, Transaction.kind, func.count(),
                   func.sum(Transaction.amount)
                   .filter(Transaction.year == today.year, Transaction.month == today.month))
            .where(Transaction.category.notin_((UNCATEGORIZED, TRANSFER)))
            .group_by(Transaction.category, Transaction.kind)
            .order_by(func.count().desc())).all()
    finally:
        db.close()
    expenses = [(c, n, m) for c, k, n, m in rows if k == "expense"]
    incomes = [(c, n, m) for c, k, n, m in rows if k == "income"]
    lines = [f"🗂 <b>Your categories</b> (spending in {MONTHS[today.month]})"]
    lines += [f"• {html.escape(c)}: {money(m or 0)} · {n} entries" for c, n, m in expenses] or ["—"]
    if incomes:
        lines.append("\n<b>Income</b>")
        lines += [f"• {html.escape(c)}: {money(m or 0)}" for c, n, m in incomes]
    yours = {norm(c) for c, _, _ in expenses + incomes}
    new = [n for n, aliases, _ in BASE_CATEGORIES
           if norm(n) not in yours and not any(a in yours for a in aliases)]
    if new:
        lines.append("\n<b>Created on their own when they show up</b>\n" + ", ".join(new))
    return "\n".join(lines)
