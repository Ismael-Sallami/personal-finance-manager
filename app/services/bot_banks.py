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
HOURS = 4  # = bank_sync.HOURS_BETWEEN_QUERIES (for the text)

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
EB_PANEL = "https://enablebanking.com/cp/applications"


def connect_txt(arg: str, country: str = "ES") -> str:
    """Authorisation link. `arg`: short key (imagin, revolut...) or the exact bank
    name as Enable Banking lists it for that country."""
    if not settings.bank_sync_enabled:
        return OFF_TEXT
    key = (arg or "").strip().lower().replace(" ", "")
    bank = BANKS.get(key) or (arg or "").strip()
    if not bank:
        return "Usage: <code>/connect</code> and pick the bank in the buttons."
    from app.services import enablebanking as eb
    try:
        url = eb.start_auth(bank, redirect_url(), sign_state(bank), country)
    except Exception as exc:
        log.warning("start_auth %s: %s", bank, exc)
        # Only the owner sees this (allowlist); the reason saves a trip to the logs.
        reason = html.escape(f"{type(exc).__name__}: {exc}"[:300])
        return (f"⚠ Could not start the connection with {html.escape(bank)}.\n"
                f"<code>{reason}</code>\n\n"
                f"If it is a new bank, link it first in the <a href=\"{EB_PANEL}\">Enable "
                "Banking panel</a> (Activate by linking accounts).")
    return (f"🔗 <b>Connect {html.escape(bank)}</b> (read-only)\n"
            f'<a href="{url}">Open this link</a>, press "Continue authentication" '
            "and approve at your bank. The link expires in a few minutes.")


_BANK_BUTTONS = [("🟦 imagin", "imagin"), ("⬛ Revolut", "revolut"),
                 ("🟩 Trade Republic", "traderepublic"), ("🟨 MyInvestor (beta)", "myinvestor")]


def choose_bank() -> tuple[str, dict]:
    """Buttons for /connect with no argument (connect or renew the consent)."""
    from app.services.bot import _kb
    rows = [[(t, f"k:{k}") for t, k in _BANK_BUTTONS[i:i + 2]]
            for i in range(0, len(_BANK_BUTTONS), 2)]
    rows.append([("➕ Other bank", "k:+")])
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
            from app.services.bank_sync import ROLES
            rows.append(f"{icon} <b>{html.escape(link.bank)}</b>{acct} · "
                        f"{ROLES.get(link.role, link.role)}\n"
                        f"     consent {state} · last sync {sync}")
    finally:
        db.close()
    if not rows:
        return "🏦 No banks connected. Use <code>/connect imagin</code>"
    return "🏦 <b>Connections</b>\n" + "\n".join(rows) + "\n\nTap a bank to change its role."


def connections_kb():
    from app.services.bot import _kb
    db = _db()
    try:
        banks = sorted(set(db.scalars(select(BankLink.bank)).all()))
    finally:
        db.close()
    return _kb([[(f"⚙ {b}", f"rl:{b}")] for b in banks]) if banks else None


def choose_role(bank: str) -> tuple[str, dict]:
    from app.services.bank_sync import ROLES
    from app.services.bot import _kb
    rows = [[(t, f"rs:{r}:{bank}")] for r, t in ROLES.items()]
    return (f"⚙ <b>{html.escape(bank)}</b>: what role does it have?\n"
            "⭐ Main / 💳 Secondary: spending accounts.\n"
            "📈 Investment: what you send there counts as Investment and its internal "
            "movements are ignored.\n🐷 Savings: what you move there is a transfer."), _kb(rows)


def set_role(role: str, bank: str) -> str:
    from app.services.bank_sync import ROLES
    if role not in ROLES:
        return "⚠ Invalid role."
    db = _db()
    try:
        if role == "main":  # only one main
            for link in db.scalars(select(BankLink).where(BankLink.role == "main")).all():
                link.role = "secondary"
        for link in db.scalars(select(BankLink).where(BankLink.bank == bank)).all():
            link.role = role
        db.commit()
    finally:
        db.close()
    return f"✅ {html.escape(bank)} → {ROLES[role]}\n\n" + connections_txt()


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
# Icons per category (normalised name). Your own without an icon get "•".
_ICONS = {
    "groceries": "🛒", "supermarket": "🛒", "eating out": "🍽", "restaurants": "🍽",
    "subscriptions": "🔁", "gym": "💪", "phone & internet": "📱", "transport": "🚌",
    "fuel": "⛽", "health": "💊", "clothes": "👕", "shopping": "🛍", "studies": "📚",
    "leisure": "🎬", "rent": "🏠", "home": "🏡", "gifts": "🎁", "travel": "✈️",
    "investment": "📈", "other": "📦", "salary": "💼", "interest": "💰",
    "bizum received": "💸", "other income": "➕",
}
# Besides the base ones: usual categories that do not come from a merchant.
_EXTRA_EXPENSE = ["Rent", "Home", "Gifts", "Travel", "Other"]
_INCOME = ["Salary", "Bizum received", "Interest", "Other income"]
_SESSION = {"done": 0}
# Refunds reopened because they left a category negative: id -> category.
_REOPENED: dict[int, str] = {}


# Word contained in the name -> icon (for your own categories). Spanish and
# English keywords: the first group that matches wins.
_ICON_WORDS = [
    (("peluquer", "belleza", "estetica", "barber", "hair", "beauty", "salon"), "💇"),
    (("super", "alimentacion", "compra", "mercado", "grocer", "food"), "🛒"),
    (("comida", "restaur", "comer", "cena", "bar", "salida", "eating", "dining", "lunch",
      "dinner", "takeaway"), "🍽"),
    (("cafe", "coffee"), "☕"),
    (("suscrip", "subscri"), "🔁"),
    (("gym", "gimnas", "deporte", "fitness", "sport"), "💪"),
    (("movil", "telefon", "internet", "fibra", "phone", "mobile", "broadband"), "📱"),
    (("transporte", "bus", "tren", "metro", "taxi", "uber", "transport", "train", "commut"),
     "🚌"),
    (("gasolina", "combustible", "diesel", "fuel", "petrol"), "⛽"),
    (("prestamo", "deuda", "tarjeta", "credito", "loan", "debt", "card", "credit"), "💳"),
    (("coche", "taller", "itv", "parking", "car", "garage", "vehicle"), "🚗"),
    (("salud", "medic", "farmacia", "dentista", "optica", "health", "pharmac", "dental",
      "doctor"), "💊"),
    (("ropa", "calzado", "zapat", "clothes", "clothing", "shoes", "fashion"), "👕"),
    (("compras", "amazon", "tienda", "shopping", "shop", "store"), "🛍"),
    (("uni", "estudio", "curso", "libro", "educacion", "material", "academia", "studies",
      "study", "course", "book", "education", "school", "tuition"), "📚"),
    (("ocio", "fiesta", "cine", "concierto", "juego", "leisure", "entertainment", "fun",
      "party", "cinema", "game"), "🎬"),
    (("alquiler", "piso", "vivienda", "hipoteca", "rent", "mortgage", "housing"), "🏠"),
    (("casa", "hogar", "mueble", "home", "house", "furniture"), "🏡"),
    (("luz", "agua", "gas", "suministro", "factura", "electric", "utilit", "water", "bill"),
     "💡"),
    (("regalo", "cumple", "gift", "birthday", "present"), "🎁"),
    (("viaje", "vuelo", "hotel", "vacacion", "travel", "trip", "flight", "holiday",
      "vacation"), "✈️"),
    (("mascota", "perro", "gato", "veterin", "pet", "dog", "cat", "vet"), "🐾"),
    (("bizum",), "💸"),
    (("inversion", "cripto", "fondo", "accion", "bolsa", "etf", "invest", "crypto", "stock",
      "fund"), "📈"),
    (("nomina", "sueldo", "salario", "trabajo", "salary", "payroll", "wage", "work"), "💼"),
    (("beca", "scholarship", "grant"), "🎓"),
    (("interes", "dividend", "interest"), "💰"),
    (("ahorro", "hucha", "saving"), "🐷"),
    (("seguro", "insurance"), "🛡"),
    (("efectivo", "cajero", "cash", "atm"), "🏧"),
    (("impuesto", "hacienda", "multa", "tasa", "tax", "fine", "fee"), "🏛"),
    (("donacion", "ong", "donation", "charity"), "🤝"),
    (("loteria", "apuesta", "lottery", "bet", "gambl"), "🎲"),
    (("tabaco", "tobacco", "cigar"), "🚬"),
    (("bebe", "nino", "hijo", "baby", "child", "kid"), "🧸"),
    (("otro", "other", "misc"), "📦"),
    (("ingreso", "venta", "reembolso", "devolucion", "income", "sale", "refund"), "➕"),
]


def _icon(cat: str) -> str:
    """Icon for any category: exact name, then a word contained in the name, then
    the alias of a base category and, if nothing fits, a label."""
    from app.services.bank_sync import BASE_CATEGORIES, norm
    n = norm(cat)
    if n in _ICONS:
        return _ICONS[n]
    for words, icon in _ICON_WORDS:
        if any(w in n for w in words):
            return icon
    for name, aliases, _ in BASE_CATEGORIES:
        if n in aliases and norm(name) in _ICONS:
            return _ICONS[norm(name)]
    return "🏷"


def _categories(db, kind: str) -> list[str]:
    """Options to classify: the base ones (with YOUR name if you already have an
    equivalent) + your categories + a few usual ones. No duplicates."""
    from app.services.bank_sync import BASE_CATEGORIES, norm
    rows = db.execute(
        select(Transaction.category, func.count()).where(
            Transaction.kind == kind,
            Transaction.category.notin_((UNCATEGORIZED, TRANSFER)),
        ).group_by(Transaction.category).order_by(func.count().desc())).all()
    yours = [c for c, _ in rows]
    by_norm = {norm(c): c for c in yours}
    out: list[str] = []

    def add(name, aliases=()):
        real = next((by_norm[a] for a in (norm(name),) + tuple(aliases) if a in by_norm),
                    name)
        if norm(real) not in {norm(x) for x in out}:
            out.append(real)

    if kind == "expense":
        for name, aliases, _ in BASE_CATEGORIES:
            if name != "Interest":
                add(name, aliases)
        for c in _EXTRA_EXPENSE:
            add(c)
    else:
        for c in _INCOME:
            add(c)
    for c in yours:  # your own categories that match no base one
        add(c)
    return out[:24]


def _keyboard(token: str, cats: list[str], first: str | None = None) -> list:
    order = ([first] if first in cats else []) + [c for c in cats if c != first]
    buttons = [((f"⭐ {c}" if c == first else f"{_icon(c)} {c}"), f"p:{token}:{cats.index(c)}")
               for c in order]
    return [buttons[j:j + 2] for j in range(0, len(buttons), 2)]


def _bar(done: int, left: int) -> str:
    total = done + left
    full = round(10 * done / total) if total else 10
    return "▰" * full + "▱" * (10 - full)


def next_pending(previous: str = "", tx_id: int | None = None) -> tuple[str, dict | None]:
    """(text, keyboard) of the next entry to categorise. `previous` is the
    confirmation of the last one: shown on top, in the same card."""
    from app.services.bot import _expenses_txt, _kb
    header = f"{previous}\n\n" if previous else ""
    db = _db()
    try:
        q = select(Transaction).where(Transaction.review.is_(True))
        if _SKIPPED:
            q = q.where(Transaction.id.notin_(tuple(_SKIPPED)))
        if not tx_id:
            pair = _pair_card(db, header)
            if pair:
                return pair
        tx = db.get(Transaction, tx_id) if tx_id else None
        if tx is None or not tx.review:
            tx = db.scalar(q.order_by(Transaction.date.desc()))
        total = pending_count(db)
        if tx is None:
            _SKIPPED.clear()
            done, _SESSION["done"] = _SESSION["done"], 0
            if total:
                return (header + f"⏭ You skipped the {total} that remain. "
                        "/pending to see them again."), None
            today = _today()
            end = (f"🎉 <b>All categorised!</b>"
                   + (f" ({done} in this round)" if done else "") + "\n\n")
            return header + end + _expenses_txt(today.year, today.month), None
        from app.services.bank_sync import is_bizum, pattern_of, suggestion
        cats = _categories(db, tx.kind)
        sug = suggestion(tx.merchant, tx.note, cats)
        if sug and sug not in cats:
            cats.insert(0, sug)
        pattern = pattern_of(tx.merchant, tx.note)
        if pattern:
            hint = f"I will learn “{pattern}” for next time."
        elif is_bizum(tx.merchant, tx.note):
            hint = "Bizum: you choose every time."
        else:
            hint = "No recognisable name: I will not learn a rule."
        token = _store_token(tx.id, cats, False)
        incoming = tx.kind == "income"
        sign = "−" if tx.kind == "expense" else "+"
        bizum = "💸 Bizum · " if is_bizum(tx.merchant, tx.note) else ""
        note = (f"\n<i>{html.escape(tx.note)}</i>"
                if tx.note and tx.note != tx.merchant else "")
        if tx.id in _REOPENED:
            note += (f"\n⚠ It subtracted from <b>{html.escape(_REOPENED[tx.id])}</b> and left it "
                     "negative. Is it income or the refund of another expense?")
        text = (header +
                f"<code>{_bar(_SESSION['done'], total)}</code>  <b>{total}</b> left\n\n"
                f"{tx.date:%d/%m} · {html.escape(tx.account or '')}\n"
                f"{bizum}<b>{html.escape(tx.merchant or '')}</b>  {sign}{money(tx.amount)}"
                f"{note}\n\n<i>{hint}</i>")
    finally:
        db.close()
    rows = _keyboard(token, cats, sug)
    if incoming:
        rows.append([("↩ It is a refund (subtracts from an expense)", f"p:{token}:r")])
    rows.append([("✏️ Other", f"p:{token}:n"), ("🔄 My own transfer", f"p:{token}:t"),
                 ("⏭ Skip", f"p:{token}:s")])
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
    rows = _keyboard(token, cats)
    rows.append([("✏️ Other", f"p:{token}:n"), ("⏭ Skip", f"p:{token}:s")])
    return ("↩ Which expense is this refund from? It will subtract from that category "
            "(e.g. a Bizum for the shared wifi subtracts from Home)."), _kb(rows)


def answer_pending(data: str) -> tuple[str, dict | None, bool]:
    """Apply the pressed button -> (text, keyboard, go_to_next)."""
    result = _answer(data)
    if isinstance(result, tuple):  # asks for a second step (refund)
        return result[0], result[1], False
    return result, None, not waiting_for_category()


def _answer(data: str):
    from app.services.bank_sync import NoHeadroom, learn_rule, mark_transfer
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
            _SESSION["done"] += 1
            return f"🔄 {html.escape(tx.merchant or '')}: transfer between your accounts (not an expense)."
        try:
            cat = cats[int(choice)]
        except (ValueError, IndexError):
            return "⚠ Invalid button."
        from app.services.bank_sync import pattern_of
        pattern = pattern_of(tx.merchant, tx.note)
        try:
            others = learn_rule(db, tx, cat, refund=refund)
        except NoHeadroom as e:
            db.rollback()
            return next_pending(previous=_headroom_notice(e), tx_id=tx_id)
        _REOPENED.pop(tx_id, None)
        _SESSION["done"] += 1 + others
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
def run_sync(db, force: bool = False, only: list[str] | None = None) -> dict:
    """Sync and build the alerts. Sends nothing (the caller does)."""
    from app.services.bank_sync import sync_all
    today = _today()
    res = sync_all(db, today, force=force, only=only)
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
    if today.day == 1:  # once a month, not daily
        alerts.extend(investment_notices(db))
    from app.services.bank_sync import detect_pairs
    pairs = len(detect_pairs(db))
    if pairs:
        alerts.append(f"🔁 {pairs} possible transfer(s) between your accounts (salary moved to "
                      "another bank?). Confirm in /pending")
    return {"new": len(res.new), "transfers": res.transfers, "accounts": res.accounts,
            "errors": res.errors, "alerts": alerts, "pending": pending_count(db),
            "skipped": res.skipped}


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
        if r.get("skipped"):
            parts.insert(1, "⏳ Not querying " + ", ".join(sorted(set(r["skipped"])))
                         + f" again: I did it less than {HOURS} h ago and banks only allow "
                           "about 4 queries a day.")
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
    inv = investment_notices(db)
    if inv:
        lines.append("\n" + "\n".join(inv))
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
        from app.services.bank_sync import NoHeadroom
        try:
            others = learn_rule(db, tx, name, refund=refund)
        except NoHeadroom as e:
            db.rollback()
            return _headroom_notice(e)
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


def _headroom_notice(e) -> str:
    return (f"⚠ <b>{html.escape(e.category)}</b> only has {money(e.headroom)} of expense that "
            f"month: subtracting {money(e.amount)} would leave it negative.\n"
            "Choose an income (💸 Bizum received, ➕ Other income) or the refund of another expense.")


def reopen_negatives_bot() -> int:
    """When /pending opens: send back to pending whatever leaves expenses negative."""
    from app.services.bank_sync import reopen_negatives
    db = _db()
    try:
        cats = {t.id: t.category for t in db.scalars(select(Transaction).where(
            Transaction.kind == "expense", Transaction.amount < 0)).all()}
        ids = reopen_negatives(db)
    finally:
        db.close()
    for i in ids:
        _REOPENED[i] = cats.get(i, "?")
    return len(ids)


def income_txt(year: int, month: int | None) -> str:
    db = _db()
    try:
        cond = [Transaction.kind == "income", Transaction.year == year]
        if month:
            cond.append(Transaction.month == month)
        rows = db.execute(select(Transaction.category, func.sum(Transaction.amount),
                                 func.count()).where(*cond)
                          .group_by(Transaction.category)
                          .order_by(func.sum(Transaction.amount).desc())).all()
    finally:
        db.close()
    period = f"{MONTHS[month]} {year}" if month else str(year)
    if not rows:
        return f"💶 No income in {period}."
    total = sum(v for _, v, _ in rows)
    lines = "\n".join(f"{_icon(c)} {html.escape(c)}: {money(v)}" + (f" ({n}×)" if n > 1 else "")
                      for c, v, n in rows)
    return f"💶 <b>Income · {period}</b>\n{lines}\n\n<b>Total: {money(total)}</b>"


def negatives_notice(year: int, month: int | None) -> str:
    """Warning line if some expense category ends up negative."""
    db = _db()
    try:
        cond = [Transaction.kind == "expense", Transaction.year == year]
        if month:
            cond.append(Transaction.month == month)
        neg = db.execute(select(Transaction.category, func.sum(Transaction.amount))
                         .where(*cond).group_by(Transaction.category)
                         .having(func.sum(Transaction.amount) < 0)).all()
    finally:
        db.close()
    if not neg:
        return ""
    names = ", ".join(f"{html.escape(c)} ({money(v)})" for c, v in neg)
    return f"\n\n⚠ Negative: {names}. Reassign them in /pending."


# --- /move: fix a wrongly classified entry ---
# token -> ("list", ids, filter, page) | ("cat", tx_id, cats)
_MOVE: "OrderedDict[str, tuple]" = OrderedDict()
_PER_PAGE = 8


def _move_token(value: tuple) -> str:
    token = secrets.token_urlsafe(6)
    _MOVE[token] = value
    while len(_MOVE) > 50:
        _MOVE.popitem(last=False)
    return token


def move_list(text_filter: str = "", page: int = 0, previous: str = "") -> tuple[str, dict | None]:
    """Latest classified entries (optionally filtered) as buttons."""
    from app.services.bot import _kb
    db = _db()
    try:
        q = select(Transaction).where(Transaction.review.is_(False),
                                      Transaction.category != UNCATEGORIZED)
        f = (text_filter or "").strip()
        if f:
            like = f"%{f.lower()}%"
            q = q.where(func.lower(Transaction.merchant).like(like)
                        | func.lower(Transaction.category).like(like))
        txs = db.scalars(q.order_by(Transaction.date.desc(), Transaction.id.desc())
                         .offset(page * _PER_PAGE).limit(_PER_PAGE + 1)).all()
        has_more = len(txs) > _PER_PAGE
        txs = txs[:_PER_PAGE]
        info = [(t.id, t.date, t.merchant or t.note or t.category, t.amount, t.kind,
                 t.category) for t in txs]
    finally:
        db.close()
    head = f"{previous}\n\n" if previous else ""
    if not info:
        return head + ("✏️ No entries found" + (f" with “{html.escape(f)}”." if f else ".")), None
    token = _move_token(("list", [i for i, *_ in info], f, page))
    rows = []
    for n, (_, day, name, amount, kind, cat) in enumerate(info):
        sign = "+" if kind == "income" else ("⇄" if kind == "transfer" else "−")
        label = f"{day:%d/%m} {name[:18]} {sign}{abs(amount):.2f} → {_icon(cat)}{cat[:12]}"
        rows.append([(label, f"mv:{token}:{n}")])
    nav = []
    if page:
        nav.append(("◀ Newer", f"mv:{token}:prev"))
    if has_more:
        nav.append(("Older ▶", f"mv:{token}:next"))
    if nav:
        rows.append(nav)
    title = "✏️ <b>Which one do you want to move?</b>" + (f" · “{html.escape(f)}”" if f else "")
    return head + title + "\nTap it and pick the right category.", _kb(rows)


def move_choose(tx_id: int) -> tuple[str, dict | None]:
    from app.services.bot import _kb
    db = _db()
    try:
        tx = db.get(Transaction, tx_id)
        if tx is None:
            return "⚠ That entry no longer exists.", None
        incoming = tx.kind == "income" or (tx.kind == "expense" and tx.amount < 0)
        cats = (_categories(db, "expense") + _categories(db, "income") if incoming
                else _categories(db, "expense"))
        cats = list(dict.fromkeys(cats))
        text = (f"✏️ <b>{html.escape(tx.merchant or tx.note or '')}</b> "
                f"{money(abs(tx.amount))} · {tx.date:%d/%m}\n"
                f"Now: {_icon(tx.category)} <b>{html.escape(tx.category)}</b>\n\n"
                "Where do I move it?")
    finally:
        db.close()
    token = _move_token(("cat", tx_id, cats))
    rows = _keyboard(token, cats)
    rows = [[(t, d.replace("p:", "mc:", 1)) for t, d in row] for row in rows]
    rows.append([("🔄 My own transfer", f"mc:{token}:t"), ("❓ To pending", f"mc:{token}:p")])
    return text, _kb(rows)


def move_apply(tx_id: int, choice: str, cats: list[str]) -> str:
    """Move the entry and update its merchant's rule. Returns the text."""
    from app.models import CategoryRule
    from app.services.bank_sync import (NoHeadroom, assign_category, mark_transfer,
                                        pattern_of, reopen_negatives)
    db = _db()
    try:
        tx = db.get(Transaction, tx_id)
        if tx is None:
            return "⚠ That entry no longer exists."
        name = html.escape(tx.merchant or tx.note or "")
        incoming = tx.kind == "income" or (tx.kind == "expense" and tx.amount < 0)
        if choice == "t":
            mark_transfer(db, tx)
            return f"🔄 {name}: now a transfer between your accounts."
        if choice == "p":
            if incoming:
                tx.kind, tx.amount = "income", abs(tx.amount)
            elif tx.kind == "transfer":
                tx.kind = "expense"
            tx.category, tx.review = UNCATEGORIZED, True
            db.commit()
            return f"❓ {name} goes back to /pending."
        try:
            cat = cats[int(choice)]
        except (ValueError, IndexError):
            return "⚠ Invalid button."
        is_income = cat in _categories(db, "income") or cat in _INCOME
        if is_income:
            tx.kind, tx.amount, tx.category, tx.review = "income", abs(tx.amount), cat, False
        elif incoming:
            tx.kind, tx.amount = "income", abs(tx.amount)
            try:
                assign_category(tx, cat, set(), refund=True, db=db, strict=True)
            except NoHeadroom as e:
                db.rollback()
                return _headroom_notice(e)
        else:
            tx.kind, tx.amount, tx.category, tx.review = "expense", abs(tx.amount), cat, False
        pattern = pattern_of(tx.merchant, tx.note)
        rule_txt = ""
        if pattern:
            r = db.scalar(select(CategoryRule).where(CategoryRule.pattern == pattern))
            if r:
                r.category = cat
            else:
                db.add(CategoryRule(pattern=pattern, category=cat))
            rule_txt = f"\n<i>Next ones from “{pattern}” will go to {html.escape(cat)}.</i>"
        db.commit()
        reopened = reopen_negatives(db)
        extra = (f"\n⚠ {len(reopened)} refund(s) were left negative: back to /pending."
                 if reopened else "")
        return f"✅ {name} → {_icon(cat)} <b>{html.escape(cat)}</b>{rule_txt}{extra}"
    finally:
        db.close()


def move_callback(data: str) -> tuple[str, dict | None]:
    prefix, token, choice = (data.split(":", 2) + ["", ""])[:3]
    value = _MOVE.get(token)
    if not value:
        return "⚠ Expired. Use /move again.", None
    if prefix == "mv":
        _, ids, text_filter, page = value
        if choice in ("next", "prev"):
            return move_list(text_filter, page + (1 if choice == "next" else -1))
        try:
            return move_choose(ids[int(choice)])
        except (ValueError, IndexError):
            return "⚠ Invalid button.", None
    _, tx_id, cats = _MOVE.pop(token)
    return move_list(previous=move_apply(tx_id, choice, cats))


# --- /breakdown: the entries one by one ---
_BREAKDOWN: "OrderedDict[str, tuple]" = OrderedDict()  # token -> (year, month, [cats])
_MAX_MSG = 3800  # Telegram cuts at 4096


def _period_cond(year: int, month: int | None):
    cond = [Transaction.year == year, Transaction.kind.in_(("expense", "income"))]
    if month:
        cond.append(Transaction.month == month)
    return cond


def breakdown_menu(year: int, month: int | None) -> tuple[str, dict | None]:
    from app.services.bot import _kb
    db = _db()
    try:
        rows = db.execute(select(Transaction.category, Transaction.kind,
                                 func.sum(Transaction.amount), func.count())
                          .where(*_period_cond(year, month))
                          .group_by(Transaction.category, Transaction.kind)
                          .order_by(Transaction.kind, func.sum(Transaction.amount).desc())).all()
    finally:
        db.close()
    period = f"{MONTHS[month]} {year}" if month else str(year)
    if not rows:
        return f"📋 No entries in {period}.", None
    cats = [c for c, *_ in rows]
    token = secrets.token_urlsafe(6)
    _BREAKDOWN[token] = (year, month, cats)
    while len(_BREAKDOWN) > 30:
        _BREAKDOWN.popitem(last=False)
    buttons = [(f"{'💶' if k == 'income' else _icon(c)} {c} · {money(v)} ({n})",
                f"dg:{token}:{i}") for i, (c, k, v, n) in enumerate(rows)]
    keyboard = [[b] for b in buttons] + [[("📋 Whole period", f"dg:{token}:all")]]
    return (f"📋 <b>Breakdown · {period}</b>\nPick a category to see its entries.",
            _kb(keyboard))


def breakdown_txt(year: int, month: int | None, category: str | None = None) -> list[str]:
    """Entries one by one (date, merchant, amount), in chunks for Telegram."""
    from app.services.bank_sync import norm
    db = _db()
    try:
        q = select(Transaction).where(*_period_cond(year, month))
        txs = db.scalars(q.order_by(Transaction.date, Transaction.id)).all()
        if category:
            c = norm(category)
            txs = [t for t in txs if c in norm(t.category) or c in norm(t.merchant)]
        data = [(t.date, t.merchant or t.note or t.category, t.amount, t.kind, t.category,
                 t.account, t.origin) for t in txs]
    finally:
        db.close()
    period = f"{MONTHS[month]} {year}" if month else str(year)
    title = f"📋 <b>{html.escape(category) if category else 'Everything'} · {period}</b>"
    if not data:
        return [title + "\nNo entries."]
    spent = sum(a for _, _, a, k, *_ in data if k == "expense")
    earned = sum(a for _, _, a, k, *_ in data if k == "income")
    lines = []
    for day, name, amount, kind, cat, account, origin in data:
        sign = "+" if kind == "income" else ("↩" if amount < 0 else "−")
        where = f" · {html.escape(account)}" if account else (" · manual" if origin == "manual" else "")
        icon = "" if category else f"{_icon(cat)} "
        lines.append(f"<code>{day:%d/%m}</code> {icon}{html.escape(name[:30])}  "
                     f"<b>{sign}{money(abs(amount))}</b>{where}")
    foot = (f"\n<b>{len(data)} entries</b> · expenses {money(spent)}"
            + (f" · income {money(earned)}" if earned else ""))
    chunks, current = [], title
    for ln in lines:
        if len(current) + len(ln) + 1 > _MAX_MSG:
            chunks.append(current)
            current = ""
        current += "\n" + ln
    chunks.append(current + "\n" + foot)
    return chunks


def breakdown_callback(data: str) -> list[str]:
    _, token, choice = (data.split(":", 2) + ["", ""])[:3]
    value = _BREAKDOWN.get(token)
    if not value:
        return ["⚠ Expired. Use /breakdown again."]
    year, month, cats = value
    if choice == "all":
        return breakdown_txt(year, month)
    try:
        return breakdown_txt(year, month, cats[int(choice)])
    except (ValueError, IndexError):
        return ["⚠ Invalid button."]


# --- Connect any bank: country -> search/paginate -> link ---
COUNTRIES = [("🇪🇸 Spain", "ES"), ("🇵🇹 Portugal", "PT"), ("🇫🇷 France", "FR"),
             ("🇩🇪 Germany", "DE"), ("🇮🇹 Italy", "IT"), ("🇳🇱 Netherlands", "NL"),
             ("🇧🇪 Belgium", "BE"), ("🇦🇹 Austria", "AT"), ("🇮🇪 Ireland", "IE"),
             ("🇫🇮 Finland", "FI"), ("🇱🇹 Lithuania", "LT"), ("🇵🇱 Poland", "PL")]
_BANKS_TOKEN: "OrderedDict[str, tuple[str, list[str]]]" = OrderedDict()
_WAITING_BANK: dict[str, str] = {}  # "country" -> country to search in with what you type
_BANKS_PER_PAGE = 8


def choose_country() -> tuple[str, dict]:
    from app.services.bot import _kb
    rows = [[(t, f"kp:{c}") for t, c in COUNTRIES[i:i + 3]] for i in range(0, len(COUNTRIES), 3)]
    return ("🌍 <b>Which country is the bank in?</b>\n"
            "Another country? Type <code>/connect country XX</code> (2-letter code)."), _kb(rows)


def _bank_page(token: str, country: str, names: list[str], page: int,
               total_note: str = "") -> tuple[str, dict]:
    from app.services.bot import _kb
    start = page * _BANKS_PER_PAGE
    rows = [[(n[:40], f"kl:{token}:{start + i}")]
            for i, n in enumerate(names[start:start + _BANKS_PER_PAGE])]
    nav = []
    if page:
        nav.append(("◀", f"kn:{token}:{page - 1}"))
    if start + _BANKS_PER_PAGE < len(names):
        nav.append(("▶", f"kn:{token}:{page + 1}"))
    if nav:
        rows.append(nav)
    pages = (len(names) - 1) // _BANKS_PER_PAGE + 1
    return (f"🏦 <b>Banks in {country}</b> ({len(names)}) · page {page + 1}/{pages}\n"
            "Tap yours or <b>type part of the name</b> to search.\n"
            f"<i>First link it in the <a href=\"{EB_PANEL}\">Enable Banking panel</a>.</i>"
            ), _kb(rows)


def banks_of(country: str, text_filter: str = "", page: int = 0) -> tuple[str, dict | None]:
    """Paginated list of a country's banks, filterable by typing part of the name."""
    from app.services import enablebanking as eb
    from app.services.bank_sync import norm
    try:
        names = sorted(b["name"] for b in eb.aspsps(country))
    except Exception as exc:
        return (f"⚠ Could not list the banks of {html.escape(country)}: "
                f"{html.escape(str(exc)[:150])}"), None
    if text_filter:
        names = [n for n in names if norm(text_filter) in norm(n)]
    _WAITING_BANK["country"] = country
    if not names:
        return (f"🔎 No bank in {country} contains “{html.escape(text_filter)}”. "
                "Type another part of the name."), None
    token = secrets.token_urlsafe(6)
    _BANKS_TOKEN[token] = (country, names)
    while len(_BANKS_TOKEN) > 30:
        _BANKS_TOKEN.popitem(last=False)
    return _bank_page(token, country, names, page)


def banks_callback(data: str) -> tuple[str, dict | None]:
    prefix, token, value = (data.split(":", 2) + ["", ""])[:3]
    if prefix == "kp":
        return banks_of(token)  # here token = country code
    saved = _BANKS_TOKEN.get(token)
    if not saved:
        return "⚠ Expired. Use /connect again.", None
    country, names = saved
    try:
        n = int(value)
    except ValueError:
        return "⚠ Invalid button.", None
    if prefix == "kn":  # the saved list is already filtered: paginate over it
        return _bank_page(token, country, names, n)
    try:
        name = names[n]
    except IndexError:
        return "⚠ Invalid button.", None
    _WAITING_BANK.clear()
    return connect_txt(name, country), None


def waiting_for_bank() -> bool:
    return "country" in _WAITING_BANK


# --- Mirror pairs (transfers between your accounts that were not recognised) ---
_PAIRS: "OrderedDict[str, tuple[int, int]]" = OrderedDict()


def _pair_card(db, header: str = "") -> tuple[str, dict] | None:
    from app.services.bank_sync import detect_pairs
    from app.services.bot import _kb
    pairs = detect_pairs(db)
    if not pairs:
        return None
    out_id, in_id = pairs[0]
    out, inc = db.get(Transaction, out_id), db.get(Transaction, in_id)
    token = secrets.token_urlsafe(6)
    _PAIRS[token] = (out_id, in_id)
    while len(_PAIRS) > 30:
        _PAIRS.popitem(last=False)

    def line(t, sign):
        return (f"{t.date:%d/%m} · {html.escape(t.account or '')}: "
                f"<b>{sign}{money(t.amount)}</b> {html.escape((t.merchant or '')[:30])}")

    text = (header + "🔁 <b>Is this the same money moving between your accounts?</b>"
            + (f"  ({len(pairs)})" if len(pairs) > 1 else "") + "\n\n"
            + line(out, "−") + "\n" + line(inc, "+") +
            "\n\nIf it is your own transfer (e.g. moving your salary from one bank to "
            "another), it counts neither as expense nor as income.")
    return text, _kb([[("✅ Yes, my own transfer", f"par:{token}:yes")],
                      [("❌ No, they are different", f"par:{token}:no")]])


def answer_pair(data: str) -> tuple[str, dict | None]:
    from app.services.bank_sync import resolve_pair
    _, token, answer = (data.split(":", 2) + ["", ""])[:3]
    pair = _PAIRS.pop(token, None)
    if not pair:
        return "⚠ Expired. Use /pending again.", None
    db = _db()
    try:
        resolve_pair(db, *pair, is_transfer=(answer == "yes"))
    finally:
        db.close()
    previous = ("🔄 Done: transfer between your accounts, not counted twice." if answer == "yes"
                else "👍 Understood, they are different. I will not ask again.")
    _SESSION["done"] += 1
    return next_pending(previous=previous)


# --- Invested capital per platform (/invested) ---
_BETA_CACHE: dict[str, tuple[date, bool]] = {}
DAYS_TO_CONFIRM_INVESTMENT = 30


def _is_beta(platform: str) -> bool:
    """Is its Enable Banking connector still beta? (daily cache; if it fails, yes)."""
    from app.services import enablebanking as eb
    from app.services.bank_sync import norm
    today = _today()
    cached = _BETA_CACHE.get(platform)
    if cached and cached[0] == today:
        return cached[1]
    beta = True
    try:
        for a in eb.aspsps(settings.bank_country):
            if norm(platform) in norm(a["name"]):
                beta = bool(a.get("beta"))
                break
    except Exception as exc:
        log.info("beta %s: %s", platform, exc)
    _BETA_CACHE[platform] = (today, beta)
    return beta


def investment_notices(db) -> list[str]:
    """Ask for the amount of the platforms that cannot be read (beta connector or
    not connected) if you have not confirmed it for 30 days; say when a
    connector stops being beta."""
    from app.services.aggregation import invested_capital
    from app.services.bank_sync import PLATFORM_NAMES, norm, platforms
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    connected = {norm(b) for b in db.scalars(select(BankLink.bank)).all()}
    caps = {norm(c.name): c for c in invested_capital(db)}
    notices = []
    for p in sorted(platforms()):
        name = PLATFORM_NAMES.get(p, p.title())
        cap = caps.get(norm(name))
        is_connected = any(p in c for c in connected)
        if not is_connected and settings.bank_sync_enabled and not _is_beta(name):
            notices.append(f"✨ {name} is no longer in beta: connect it with /connect "
                           "(and it stops asking you for the amount).")
            continue
        old = cap is None or not cap.updated_at or \
            (now - cap.updated_at).days >= DAYS_TO_CONFIRM_INVESTMENT
        if not is_connected and old:
            notices.append(f"📈 How much do you have invested in <b>{name}</b>? "
                           f"<code>/invested {name} 1234</code>")
        elif is_connected and cap is None:
            notices.append(f"📈 How much did you have invested in <b>{name}</b> before connecting "
                           f"it? <code>/invested {name} 1234</code> (from then on it adds only "
                           "what you send)")
    return notices


def _parse_amount(raw: str):
    from decimal import Decimal, InvalidOperation
    raw = raw.strip()
    try:
        return Decimal(raw.replace(".", "").replace(",", ".") if "," in raw else raw)
    except InvalidOperation:
        return None


def invested_txt(arg: str) -> str:
    """/invested [platform amount]: show or set the invested capital."""
    from decimal import Decimal
    from app.services.aggregation import invested_capital, snapshot_today
    from app.services.bank_sync import PLATFORM_NAMES, investment_account, norm
    db = _db()
    try:
        parts = (arg or "").rsplit(" ", 1)
        if len(parts) == 2 and parts[0].strip():
            amount = _parse_amount(parts[1])
            if amount is None:
                return "Usage: <code>/invested MyInvestor 1500</code>"
            name = parts[0].strip()
            name = next((v for k, v in PLATFORM_NAMES.items() if k == norm(name)), name)
            acc = investment_account(db, name)
            acc.balance = max(Decimal("0"), amount)
            acc.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            snapshot_today(db)
            header = f"✅ {html.escape(acc.name)}: {money(acc.balance)} invested.\n\n"
        else:
            header = ""
        caps = invested_capital(db)
        notices = investment_notices(db)
    finally:
        db.close()
    if not caps:
        body = "📈 No invested capital recorded yet."
    else:
        total = sum(c.balance for c in caps)
        rows = "\n".join(f"• {html.escape(c.name)}: {money(c.balance)}"
                         + (f"  <i>(confirmed {c.updated_at:%d/%m})</i>" if c.updated_at else "")
                         for c in caps)
        body = f"📈 <b>Invested capital</b>\n{rows}\n\n<b>Total: {money(total)}</b>"
    return header + body + ("\n\n" + "\n".join(notices) if notices else "")
