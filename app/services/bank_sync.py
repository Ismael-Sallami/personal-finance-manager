"""Bank movement sync (Enable Banking) -> Transaction.

Flow for every linked account (BankLink):
  1. Download movements since max(BANK_SYNC_FROM, last_sync - 5 days).
  2. Normalise (amount, sign, date, merchant, description) and drop pending ones.
  3. Deduplicate by external_id (the bank's id or a stable hash).
  4. Transfers between your own accounts -> kind "transfer" (not an expense).
  5. Categorise with rules (yours first, then built-in ones). No rule ->
     "Uncategorized" and review=True (the bot asks you).
  6. Update the balance of the matching BankAccount.

Normalisation and categorisation functions are pure so they can be tested.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BankAccount, BankLink, CategoryRule, Transaction

log = logging.getLogger("bank_sync")

UNCATEGORIZED = "Uncategorized"
TRANSFER = "Transfer"

# Names that, when they show up as merchant/counterparty, mean the money goes to
# another account of yours (e.g. topping up one bank with another's card).
# Edit this list to match the banks you use.
OWN_BANKS = ("revolut", "trade republic", "myinvestor", "imagin")

def investment_platforms() -> tuple[str, ...]:
    """Platforms used only to invest (setting INVESTMENT_BANKS, comma list)."""
    return tuple(p for p in (norm(x) for x in settings.investment_banks.split(",")) if p)


ROLES = {"main": "⭐ Main", "secondary": "💳 Secondary",
         "investment": "📈 Investment", "savings": "🐷 Savings"}


def initial_role(bank: str, db: Session) -> str:
    if any(p in norm(bank) for p in investment_platforms()):
        return "investment"
    has_main = db.scalar(select(func.count()).select_from(BankLink)
                         .where(BankLink.role == "main"))
    return "secondary" if has_main else "main"


_PLATFORMS_CACHE: set[str] = set()


def platforms(db: Session | None = None) -> set[str]:
    """Names (normalised) treated as investment platforms: banks with the
    investment role + the INVESTMENT_BANKS names that are not connected yet."""
    if db is not None:
        _PLATFORMS_CACHE.clear()
        _PLATFORMS_CACHE.update(norm(b) for b in db.scalars(
            select(BankLink.bank).where(BankLink.role == "investment")).all())
        other = {norm(b) for b in db.scalars(
            select(BankLink.bank).where(BankLink.role != "investment")).all()}
        _PLATFORMS_CACHE.update(p for p in investment_platforms()
                                if not any(p in o for o in other))
    return _PLATFORMS_CACHE or set(investment_platforms())


def is_platform(bank: str | None) -> bool:
    return any(p in norm(bank) for p in platforms())


def goes_to_investment(text: str, bank: str) -> bool:
    t = norm(text)
    return any(p in t for p in platforms() if p not in norm(bank))


PLATFORM_NAMES = {"trade republic": "Trade Republic", "myinvestor": "MyInvestor"}


def platform_in(text: str, bank: str) -> str | None:
    """Display name of the investment platform that appears in `text`."""
    t = norm(text)
    for p in sorted(platforms(), key=len, reverse=True):
        if p in t and p not in norm(bank):
            return PLATFORM_NAMES.get(p, p.title())
    return None


def investment_account(db: Session, platform: str) -> BankAccount:
    """The "invested capital" account of a platform (created if missing)."""
    from app.services.aggregation import INVESTMENT_KIND
    key = norm(platform).replace(" ", "")
    acc = next((a for a in db.scalars(select(BankAccount).where(
        BankAccount.kind == INVESTMENT_KIND)).all()
        if norm(a.name).replace(" ", "") == key), None)  # "TradeRepublic" == "Trade Republic"
    if acc is None:
        acc = BankAccount(name=platform, kind=INVESTMENT_KIND, balance=Decimal("0"))
        db.add(acc)
        db.flush()
    return acc


def move_capital(db: Session, platform: str, amount: Decimal) -> None:
    """Add (contribution) or subtract (withdrawal) invested capital; never below 0.
    It does not touch updated_at: that date is your last manual confirmation."""
    acc = investment_account(db, platform)
    acc.balance = max(Decimal("0"), (acc.balance or Decimal("0")) + amount)


# --- Destination by IBAN: the reliable way to know which of your accounts gets the money ---
IBAN_PREFIX = "iban:"  # learned rules: pattern "iban:ES12..." -> destination
# Hint table (Spanish bank code -> name) to help you recognise an unknown IBAN.
# Edit it for your country; it is only used to word the question.
BANK_CODES = {"1544": "MyInvestor", "1586": "Trade Republic", "2100": "CaixaBank"}


def clean_iban(iban: str | None) -> str:
    return (iban or "").replace(" ", "").upper()


def iban_destinations(db: Session) -> dict[str, str]:
    """IBAN -> destination ('investment:<Platform>' | 'transfer'): your connected
    accounts (by their role) and the IBANs you already answered about."""
    out = {}
    for link in db.scalars(select(BankLink)).all():
        if link.iban:
            name = PLATFORM_NAMES.get(norm(link.bank), link.bank)
            out[clean_iban(link.iban)] = (f"investment:{name}" if link.role == "investment"
                                          else "transfer")
    for r in db.scalars(select(CategoryRule).where(
            CategoryRule.pattern.like(IBAN_PREFIX + "%"))).all():
        out[clean_iban(r.pattern[len(IBAN_PREFIX):])] = r.category
    return out


def bank_of_iban(iban: str | None) -> str | None:
    i = clean_iban(iban)
    return BANK_CODES.get(i[4:8]) if i.startswith("ES") and len(i) >= 8 else None


def in_my_name(text: str) -> bool:
    """Two words of REPORT_OWNER appear in the text."""
    owner = norm(settings.report_owner)
    if not owner or owner == "account owner":
        return False
    words = [w for w in owner.split() if len(w) >= 3]
    t = norm(text)
    return len(words) >= 2 and sum(matches(w, t) for w in words) >= 2


def _is_outgoing(tx: Transaction) -> bool:
    n = norm(tx.note)
    return " to " in f" {n} " or "enviada" in n


def apply_destination(db: Session, tx: Transaction, destination: str,
                      existing: list[str]) -> None:
    """Classify a transfer according to its known destination."""
    if destination.startswith("investment:"):
        platform = destination.split(":", 1)[1]
        if tx.kind == "expense" or (tx.kind == "transfer" and tx.amount > 0 and _is_outgoing(tx)):
            tx.kind, tx.category, tx.review = "expense", investment_category(existing), False
            move_capital(db, platform, abs(tx.amount))
        else:  # money coming back from the platform
            tx.kind, tx.category, tx.review = "transfer", TRANSFER, False
            move_capital(db, platform, -abs(tx.amount))
    else:
        tx.kind, tx.category, tx.review = "transfer", TRANSFER, False


def learn_iban_destination(db: Session, tx: Transaction, destination: str) -> None:
    """The user said where that IBAN goes: it is remembered and applied."""
    iban = clean_iban(tx.counterparty_iban)
    if iban:
        pattern = IBAN_PREFIX + iban
        r = db.scalar(select(CategoryRule).where(CategoryRule.pattern == pattern))
        if r:
            r.category = destination
        else:
            db.add(CategoryRule(pattern=pattern, category=destination))
    apply_destination(db, tx, destination, existing_categories(db))
    db.commit()


def investment_category(existing: list[str]) -> str:
    from app.services.aggregation import INVESTMENT_CATEGORIES
    return next((c for c in existing if c.lower() in INVESTMENT_CATEGORIES), "Investment")


# Internal movements the bank does not flag as transfers: savings pots, interest
# accounts, currency exchange, top-ups (mostly Revolut).
TRANSFER_KEYS = (
    "cuenta remunerada", "savings vault", "to savings", "from savings",
    "to pocket", "from pocket", "exchanged to", "exchanged from",
    "top-up by", "topup by",
)

# Words that do not identify a merchant (cities, articles...).
_FILLER = {
    "los", "las", "del", "de", "la", "el", "en", "y", "the", "and", "sl", "sa", "sp",
    "cc", "es", "spain", "espana", "madrid", "barcelona", "sevilla",
    "malaga", "valencia", "centro", "online", "com", "www",
}

# Built-in rules (pattern -> category). Yours take priority.
DEFAULT_RULES = {
    "savings plan": "Investment",
    "sparplan": "Investment",
    "buy trade": "Investment",
    "kauf": "Investment",
    "suscripcion fondo": "Investment",
    "compra fondo": "Investment",
}

# Preference order of the balance type (ISO 20022).
_BALANCES = ("CLBD", "ITBD", "XPCD", "CLAV", "ITAV", "OTHR")


@dataclass
class BankEntry:
    external_id: str
    date: date
    kind: str              # expense | income
    amount: Decimal        # always positive
    merchant: str
    description: str
    counterparty_iban: str | None


@dataclass
class SyncResult:
    new: list[Transaction] = field(default_factory=list)
    transfers: int = 0
    errors: list[str] = field(default_factory=list)
    accounts: int = 0
    reclassified: int = 0
    skipped: list[str] = field(default_factory=list)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def norm(text: str | None) -> str:
    """Lower case, no accents, collapsed whitespace."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def _words(text: str | None) -> list[str]:
    """Useful words: letters only (no codes like P48FDBA77), >= 3 letters."""
    toks = [t.strip("-") for t in re.split(r"[\s*/.,:;_&]+", norm(text))]
    return [t for t in toks if t.replace("-", "").isalpha() and len(t) >= 3
            and t not in _FILLER]


def is_bizum(merchant: str | None, description: str | None) -> bool:
    return "bizum" in norm(f"{merchant} {description}")


def pattern_of(merchant: str | None, description: str | None = "") -> str:
    """Pattern to learn a rule from.

    - Merchant: the brand (first useful word): "Mercadona Central Street" ->
      "mercadona", "Spotify P48FDBA77" -> "spotify". It works for any shop of
      the chain and does not change with each charge's code.
    - Bizum: "" (never learned; you categorise those every time).
    """
    if is_bizum(merchant, description):
        return ""
    ws = _words(merchant)
    return ws[0][:120] if ws else ""


def matches(pattern: str, text: str) -> bool:
    """`pattern` appears as whole word(s) in `text` (already normalised)."""
    return bool(pattern) and re.search(
        rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])", text) is not None


def normalize(raw: dict, bank: str, uid: str) -> BankEntry | None:
    """Enable Banking movement -> BankEntry. None if pending or invalid."""
    if (raw.get("status") or "BOOK").upper() == "PDNG":
        return None
    try:
        amount = abs(Decimal(str(raw["transaction_amount"]["amount"])))
    except Exception:
        return None
    date_s = raw.get("booking_date") or raw.get("value_date") or raw.get("transaction_date")
    if not date_s or not amount:
        return None
    day = date.fromisoformat(date_s[:10])
    credit = (raw.get("credit_debit_indicator") or "").upper() == "CRDT"
    party = (raw.get("debtor") if credit else raw.get("creditor")) or {}
    party_account = (raw.get("debtor_account") if credit else raw.get("creditor_account")) or {}
    info = raw.get("remittance_information") or []
    description = " ".join(info) if isinstance(info, list) else str(info)
    merchant = (party.get("name") or description or "Unknown").strip()[:200]

    ref = raw.get("transaction_id") or raw.get("entry_reference")
    if ref and len(f"{bank}:{ref}") <= 128:
        external_id = f"{bank}:{ref}"
    elif ref:
        external_id = f"{bank}:r:{_sha(str(ref))}"
    else:
        base = f"{uid}|{day}|{amount}|{credit}|{description}|{merchant}"
        external_id = f"{bank}:h:{hashlib.sha256(base.encode()).hexdigest()[:32]}"
    return BankEntry(
        external_id=external_id, date=day,
        kind="income" if credit else "expense", amount=amount,
        merchant=merchant, description=description[:255],
        counterparty_iban=(party_account.get("iban") or None),
    )


def text_is_transfer(text: str, bank: str) -> bool:
    t = norm(text)
    if any(c in t for c in TRANSFER_KEYS):
        return True
    owner = norm(settings.report_owner)
    if owner and owner != "account owner":
        if owner in t:
            return True
        # Two words of the name are enough ("JANE DOE" for "Jane Q Doe Smith").
        words = [w for w in owner.split() if len(w) >= 3]
        if len(words) >= 2 and sum(matches(w, t) for w in words) >= 2:
            return True
    return any(b in t for b in OWN_BANKS if b not in norm(bank))


def text_is_transfer_without_owner(text: str, bank: str) -> bool:
    """Transfer recognisable without looking at the owner's name (pots, another bank of yours)."""
    t = norm(text)
    return any(c in t for c in TRANSFER_KEYS) or \
        any(b in t for b in OWN_BANKS if b not in norm(bank))


def is_transfer(m: BankEntry, own_ibans: set[str], bank: str) -> bool:
    if m.counterparty_iban and m.counterparty_iban.replace(" ", "") in own_ibans:
        return True
    return text_is_transfer(f"{m.merchant} {m.description}", bank)


def categorize(text: str, rules: dict[str, str],
               existing: list[str] | None = None) -> str | None:
    """Your rules first (longest pattern), then investing and base categories.
    Bizum is never classified automatically: you do it."""
    t = norm(text)
    if "bizum" in t:
        return None
    for source in (rules, DEFAULT_RULES):
        hits = [p for p in source if matches(p, t)]
        if hits:
            return source[max(hits, key=len)]
    return base_category(t, existing)


def existing_categories(db: Session) -> list[str]:
    return [c for c in db.scalars(select(Transaction.category).distinct()).all()
            if c not in (UNCATEGORIZED, TRANSFER)]


def expense_only_categories(db: Session) -> set[str]:
    """Categories used as expense and never as income (Groceries, Home...)."""
    expenses = set(db.scalars(select(Transaction.category).distinct()
                              .where(Transaction.kind == "expense")).all())
    incomes = set(db.scalars(select(Transaction.category).distinct()
                             .where(Transaction.kind == "income")).all())
    return {c for c in expenses - incomes if c not in (UNCATEGORIZED, TRANSFER)}


class NoHeadroom(ValueError):
    """A refund would leave the category negative that month."""

    def __init__(self, category: str, headroom: Decimal, amount: Decimal):
        super().__init__(category)
        self.category, self.headroom, self.amount = category, headroom, amount


def month_expense(db: Session, category: str, year: int, month: int,
                  exclude_id: int | None = None) -> Decimal:
    cond = [Transaction.kind == "expense", Transaction.category == category,
            Transaction.year == year, Transaction.month == month]
    if exclude_id is not None:
        cond.append(Transaction.id != exclude_id)
    return Decimal(str(db.scalar(select(func.sum(Transaction.amount)).where(*cond)) or 0))


def assign_category(tx: Transaction, category: str, expense_only: set[str],
                    refund: bool = False, db: Session | None = None,
                    strict: bool = False) -> bool:
    """Set the category. Money coming IN under an expense category (a Bizum for
    the shared wifi, a shop refund...) is a refund: it is stored as a negative
    expense, so it subtracts from that category instead of inflating income.

    It never leaves a category negative in the month: if there is not enough
    expense, with `strict` it raises NoHeadroom (the user picks something else);
    otherwise it leaves the entry pending as income. Returns whether it applied."""
    incoming = tx.kind == "income" or (tx.kind == "expense" and tx.amount < 0)
    if incoming and (refund or category in expense_only):
        amount = abs(tx.amount)
        if db is not None:
            headroom = month_expense(db, category, tx.year, tx.month, tx.id)
            if headroom < amount:
                if strict:
                    raise NoHeadroom(category, headroom, amount)
                tx.kind, tx.amount = "income", amount
                tx.category, tx.review = UNCATEGORIZED, True
                return False
        tx.category, tx.review = category, False
        tx.kind, tx.amount = "expense", -amount
        return True
    tx.category, tx.review = category, False
    return True


def reopen_negatives(db: Session) -> list[int]:
    """Expense categories with a negative total in a month (too many refunds):
    their bank refunds go back to pending, as income, until the category is
    >= 0. Returns the reopened ids."""
    neg = db.execute(
        select(Transaction.category, Transaction.year, Transaction.month,
               func.sum(Transaction.amount))
        .where(Transaction.kind == "expense")
        .group_by(Transaction.category, Transaction.year, Transaction.month)
        .having(func.sum(Transaction.amount) < 0)).all()
    ids = []
    for cat, y, m, total in neg:
        total = Decimal(str(total))
        for tx in db.scalars(select(Transaction).where(
                Transaction.kind == "expense", Transaction.category == cat,
                Transaction.year == y, Transaction.month == m,
                Transaction.amount < 0, Transaction.origin == "bank")
                .order_by(Transaction.amount)).all():
            if total >= 0:
                break
            total -= tx.amount
            tx.kind, tx.amount = "income", abs(tx.amount)
            tx.category, tx.review = UNCATEGORIZED, True
            ids.append(tx.id)
    if ids:
        db.commit()
    return ids


def user_rules(db: Session) -> dict[str, str]:
    return {r.pattern: r.category for r in db.scalars(select(CategoryRule)).all()
            if not r.pattern.startswith((IBAN_PREFIX, PREPAID_PREFIX))}


def start_date(link: BankLink, today: date) -> date:
    try:
        since = date.fromisoformat(settings.bank_sync_from.strip())
    except ValueError:
        since = today.replace(day=1)
    if link.last_sync:
        since = max(since, link.last_sync - timedelta(days=5))
    return since


def _balance(balances: list[dict]) -> Decimal | None:
    by_type = {b.get("balance_type"): b for b in balances}
    for t in _BALANCES:
        if t in by_type:
            return Decimal(str(by_type[t]["balance_amount"]["amount"]))
    if balances:
        return Decimal(str(balances[0]["balance_amount"]["amount"]))
    return None


def _account_for(db: Session, link: BankLink) -> BankAccount:
    if link.bank_account_id:
        acc = db.get(BankAccount, link.bank_account_id)
        if acc:
            return acc
    siblings = db.scalar(select(func.count()).select_from(BankLink).where(
        BankLink.bank == link.bank)) or 1
    if siblings <= 1:
        name = link.bank
    else:  # several accounts at the same bank: one each (no IBAN -> by uid)
        name = f"{link.bank} …{(link.iban or link.identification_hash)[-4:]}"
    acc = db.scalar(select(BankAccount).where(func.lower(BankAccount.name) == name.lower(),
                                              BankAccount.kind != "investment"))
    if acc is None:
        acc = BankAccount(name=name, kind="account", balance=Decimal("0"))
        db.add(acc)
        db.flush()
    link.bank_account_id = acc.id
    return acc


def save_movements(db: Session, link: BankLink, raws: list[dict],
                   own_ibans: set[str], res: SyncResult) -> None:
    """Normalise, deduplicate, classify and insert. Does not commit."""
    if link.role == "investment" or is_platform(link.bank):
        return  # investment platform: only its balance (sync_all sets it)
    rules = user_rules(db)
    expense_only = expense_only_categories(db)
    existing_cats = existing_categories(db)
    destinations = iban_destinations(db)
    plans = prepaid_plans(db)
    entries = [m for m in (normalize(r, link.bank, link.uid) for r in raws) if m]
    if not entries:
        return
    ids = [m.external_id for m in entries]
    known = set(db.scalars(
        select(Transaction.external_id).where(Transaction.external_id.in_(ids))).all())
    acc = _account_for(db, link)
    for m in entries:
        if m.external_id in known:
            continue
        known.add(m.external_id)
        target = platform_in(f"{m.merchant} {m.description}", link.bank)
        text = f"{m.merchant} {m.description}"
        pkey = (prepaid_key(f"{m.description} {m.merchant}", plans)
                if m.kind == "income" and plans and "bizum" in norm(text) else None)
        if pkey:  # Bizum for a prepaid plan: it is not income
            db.add(Transaction(
                date=m.date, year=m.date.year, month=m.date.month, category=f"Prepaid {pkey}",
                kind="prepaid", amount=m.amount, note=m.description or None, origin="bank",
                external_id=m.external_id, merchant=m.merchant, account=acc.name, review=False,
                pair_reviewed=True, counterparty_iban=clean_iban(m.counterparty_iban) or None))
            continue
        by_iban = destinations.get(clean_iban(m.counterparty_iban)) if m.counterparty_iban else None
        if by_iban and by_iban.startswith("investment:"):
            platform = by_iban.split(":", 1)[1]
            if m.kind == "expense":
                kind, cat, review = "expense", investment_category(existing_cats), False
                move_capital(db, platform, m.amount)
            else:
                kind, cat, review = "transfer", TRANSFER, False
                move_capital(db, platform, -m.amount)
                res.transfers += 1
        elif by_iban:
            kind, cat, review = "transfer", TRANSFER, False
            res.transfers += 1
        elif (m.counterparty_iban and in_my_name(text)
              and not text_is_transfer_without_owner(text, link.bank)):
            # In your name but to an account I do not know: ask (and remember).
            kind, cat, review = m.kind, UNCATEGORIZED, True
        elif m.kind == "expense" and target:
            kind, cat, review = "expense", investment_category(existing_cats), False
            move_capital(db, target, m.amount)       # contribution
        elif m.kind == "income" and target:
            kind, cat, review = "transfer", TRANSFER, False
            move_capital(db, target, -m.amount)      # withdrawal to your account
            res.transfers += 1
        elif is_transfer(m, own_ibans, link.bank):
            kind, cat, review = "transfer", TRANSFER, False
            res.transfers += 1
        else:
            cat = categorize(f"{m.merchant} {m.description}", rules, existing_cats)
            kind, review = m.kind, cat is None
            cat = cat or UNCATEGORIZED
        tx = Transaction(
            date=m.date, year=m.date.year, month=m.date.month,
            category=cat, kind=kind, amount=m.amount,
            note=m.description or None, origin="bank", external_id=m.external_id,
            merchant=m.merchant, account=acc.name, review=review,
            counterparty_iban=clean_iban(m.counterparty_iban) or None,
        )
        if kind != "transfer" and not review:
            assign_category(tx, cat, expense_only, db=db)
        db.add(tx)
        if kind != "transfer":
            res.new.append(tx)


# PSD2: without the user present, the bank may cap queries at ~4 per day per
# account (a 429 error). Each sync spends 2 (movements + balance), so the daily
# job queries each bank once a day and a manual /sync only does it if
# HOURS_BETWEEN_QUERIES have passed since that bank's last query.
HOURS_BETWEEN_QUERIES = 4
_LAST_QUERY: dict[int, datetime] = {}


def sync_all(db: Session, today: date | None = None, client=None,
             force: bool = False, only: list[str] | None = None) -> SyncResult:
    """Sync the linked accounts whose consent is still valid.

    force: query even if it was already done today (if a few hours have passed).
    only: limit to those banks (e.g. the one just connected)."""
    from app.services import enablebanking as eb_default
    eb = client or eb_default
    today = today or date.today()
    res = SyncResult()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    links = db.scalars(select(BankLink)).all()
    ibans = {link.iban.replace(" ", "") for link in links if link.iban}
    platforms(db)
    for link in links:
        if only and link.bank not in only:
            continue
        if link.valid_until and link.valid_until < now:
            res.errors.append(f"{link.bank}: consent expired")
            continue
        last = _LAST_QUERY.get(link.id)
        if force and last and now - last < timedelta(hours=HOURS_BETWEEN_QUERIES):
            res.skipped.append(link.bank)
            continue
        if not force and link.last_sync == today:
            res.skipped.append(link.bank)
            continue
        _LAST_QUERY[link.id] = now
        try:
            raws = eb.transactions(link.uid, start_date(link, today))
            save_movements(db, link, raws, ibans, res)
            balance = _balance(eb.balances(link.uid))
            if balance is not None:
                acc = _account_for(db, link)
                acc.balance = balance
                acc.updated_at = now
            link.last_sync = today
            db.commit()
            res.accounts += 1
        except Exception as exc:  # one broken account does not stop the others
            db.rollback()
            log.warning("sync %s failed: %s", link.bank, exc)
            if "429" in str(exc):
                res.errors.append(f"{link.bank}: the bank has reached its query limit for "
                                  "today (PSD2: ~4/day). It retries on its own tomorrow.")
            else:
                res.errors.append(f"{link.bank}: {str(exc)[:120]}")
    res.reclassified = reclassify_pending(db)
    apply_prepaid(db)
    if res.accounts:
        from app.services.aggregation import snapshot_today
        snapshot_today(db)
    return res


def reclassify_pending(db: Session) -> int:
    """Run transfers and rules again over what is pending (e.g. after improving
    the rules or learning a new one). Returns how many stop being pending."""
    reopen_negatives(db)
    platforms(db)
    rules = user_rules(db)
    expense_only = expense_only_categories(db)
    existing_cats = existing_categories(db)
    destinations = iban_destinations(db)
    n = 0
    for tx in db.scalars(select(Transaction).where(Transaction.review.is_(True),
                                                   Transaction.origin == "bank")).all():
        text = f"{tx.merchant} {tx.note}"
        if tx.counterparty_iban:
            destination = destinations.get(clean_iban(tx.counterparty_iban))
            if destination:  # known IBAN (connected account or already answered)
                apply_destination(db, tx, destination, existing_cats)
                n += 1
                continue
            if in_my_name(text):
                continue  # in your name to an account I do not know: you decide
        if is_platform(tx.account):  # e.g. the "Unknown" rows of an investment platform
            tx.kind, tx.category, tx.review = "transfer", TRANSFER, False
            n += 1
            continue
        if text_is_transfer(text, tx.account or ""):
            tx.kind, tx.category, tx.review = "transfer", TRANSFER, False
            n += 1
            continue
        cat = categorize(text, rules, existing_cats)
        if cat and assign_category(tx, cat, expense_only, db=db):
            n += 1
    db.commit()
    return n


def learn_rule(db: Session, tx: Transaction, category: str, refund: bool = False) -> int:
    """Give `tx` a category, store the rule for its merchant and apply it to the
    other pending entries that match. Returns how many others were recategorised.
    `refund`: money that comes in but subtracts from an expense category."""
    expense_only = expense_only_categories(db) | ({category} if refund else set())
    assign_category(tx, category, expense_only, refund, db=db, strict=True)
    pattern = pattern_of(tx.merchant, tx.note)
    others = 0
    if pattern:
        rule = db.scalar(select(CategoryRule).where(CategoryRule.pattern == pattern))
        if rule:
            rule.category = category
        else:
            db.add(CategoryRule(pattern=pattern, category=category))
        for p in db.scalars(select(Transaction).where(
                Transaction.review.is_(True), Transaction.id != tx.id)).all():
            if matches(pattern, norm(f"{p.merchant} {p.note}")):
                if assign_category(p, category, expense_only, db=db):
                    others += 1
    db.commit()
    return others


def mark_transfer(db: Session, tx: Transaction) -> None:
    tx.kind, tx.category, tx.review = "transfer", TRANSFER, False
    db.commit()


def save_session(db: Session, session: dict, bank: str) -> list[BankLink]:
    """After authorising: create/update one BankLink per account (re-mapped by hash)."""
    valid = (session.get("access") or {}).get("valid_until")
    valid_until = (datetime.fromisoformat(valid.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).replace(tzinfo=None)
                   if valid else datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=90))
    links = []
    for acc in session.get("accounts") or []:
        # The app adds everything up in one currency: a GBP/USD account would be summed wrong.
        if (acc.get("currency") or "EUR").upper() != "EUR":
            log.info("account %s in %s ignored (EUR only)", bank, acc.get("currency"))
            continue
        # Enable Banking's hash can be long (base64 of a JSON): store its sha256,
        # which is stable and fixed-length.
        h = _sha(acc.get("identification_hash") or acc["uid"])
        link = db.scalar(select(BankLink).where(BankLink.identification_hash == h))
        if link is None:
            link = BankLink(identification_hash=h, bank=bank, role=initial_role(bank, db))
            db.add(link)
        link.bank = bank
        link.session_id = session["session_id"]
        link.uid = acc["uid"]
        link.iban = (acc.get("account_id") or {}).get("iban")
        link.valid_until = valid_until
        link.expiry_notified = False
        links.append(link)
    db.commit()
    return links


# Base categories: (name, aliases you may already use, merchant keywords).
# The keywords are Spanish chains and words (the sync reads Spanish bank
# statements): edit them for your country. Whatever matches is classified ON ITS
# OWN. If you already have a category with one of the aliases ("Food",
# "Restaurants"...), yours is used so your history is not split.
BASE_CATEGORIES = [
    ("Fuel", ("fuel", "petrol", "gas station"),
     ("estacion de servicio", "gasolinera", "repsol", "cepsa", "galp", "shell",
      "petroprix", "ballenoil", "plenoil", "bonarea")),
    ("Groceries", ("groceries", "supermarket", "food", "shopping list"),
     ("mercadona", "dia", "lidl", "aldi", "primaprix", "alcampo", "carrefour", "consum",
      "coviran", "supermercado", "spar", "ahorramas", "hipercor", "eroski", "supersol")),
    ("Sport", ("sport", "gym", "gimnasio", "deporte", "fitness"),
     ("basic-fit", "basic fit", "altafit", "mcfit", "vivagym", "fitness", "gimnasio",
      "crossfit", "padel")),
    ("Subscriptions", ("subscriptions", "subscription"),
     ("spotify", "netflix", "hbo", "disney", "icloud", "apple com bill", "youtube",
      "google one", "google", "prime video", "amazon prime", "twitch", "chatgpt", "openai",
      "audible", "dazn", "filmin", "crunchyroll")),
    ("Phone", ("phone", "phone & internet", "internet", "mobile", "movil"),
     ("vodafone", "digimobil", "digi", "movistar", "orange", "yoigo", "pepephone", "lowi",
      "simyo", "masmovil", "jazztel", "o2", "finetwork")),
    ("Eating out", ("eating out", "restaurants", "restaurant", "dining", "bars"),
     ("restaurante", "bar", "cafeteria", "cafe", "comedor", "burger", "mcdonalds",
      "telepizza", "kebab", "pizzeria", "tapas", "cerveceria", "panaderia",
      "pasteleria", "churreria", "glovo", "just eat", "uber eats",
      "starbucks", "foster", "goiko", "taberna", "meson")),
    ("Transport", ("transport", "transportation"),
     ("renfe", "alsa", "uber", "cabify", "bolt", "metro", "taxi", "blablacar",
      "autobus", "transportes", "parking")),
    ("Health", ("health", "pharmacy", "medical"),
     ("farmacia", "parafarmacia", "clinica", "dentista", "optica", "hospital", "fisio")),
    ("Clothes", ("clothes", "clothing"),
     ("zara", "bershka", "pull & bear", "decathlon", "primark", "stradivarius", "mango",
      "springfield", "lefties", "kiabi", "shein", "h&m", "jd sports", "foot locker")),
    ("Shopping & tech", ("shopping & tech", "shopping", "tech", "misc"),
     ("amazon", "aliexpress", "ale-hop", "ikea", "leroy merlin", "action", "tiger",
      "miniso", "druni", "primor", "media markt", "pccomponentes", "el corte ingles",
      "temu")),
    ("Utilities", ("utilities", "bills", "electricity", "water"),
     ("iberdrola", "endesa", "naturgy", "emasagra", "aguas", "totalenergies", "holaluz",
      "repsol luz", "digi fibra", "fibra")),
    ("Hairdresser", ("hairdresser", "barber", "peluqueria"),
     ("peluqueria", "barberia", "barber")),
    ("Car", ("car", "auto", "coche"),
     ("itv", "taller", "neumaticos", "mapfre", "mutua madrilena", "linea directa",
      "parking")),
    ("Travel", ("travel", "trips", "holidays", "viajes"),
     ("ryanair", "vueling", "iberia", "booking", "airbnb", "hotel", "hostel", "easyjet",
      "aer lingus")),
    ("Gifts", ("gifts", "gift", "regalos"), ("regalo",)),
    ("Studies", ("studies", "university", "school", "supplies"),
     ("fotocopias", "copisteria", "papeleria", "libreria", "matricula")),
    ("Leisure", ("leisure", "entertainment", "fun"),
     ("cine", "cinesa", "yelmo", "entradas", "ticketmaster", "steam", "playstation",
      "nintendo", "bolera", "discoteca", "concierto")),
    # Income
    ("Interest", ("interest",), ("interest", "intereses", "zinsen", "interes")),
    ("Salary", ("salary", "payroll", "work", "nomina", "salario", "trabajo"),
     ("nomina", "abono nomina", "salario", "payroll")),
    ("Grants", ("grants", "grant", "scholarship", "becas", "beca"), ("beca", "becas", "grant")),
]


def base_category(text: str, existing: list[str] | None = None) -> str | None:
    """The base category that matches (most specific word first) or None.
    Returns your own name if you already have a category with an equivalent alias."""
    t = norm(text)
    # "E S <brand>" / "E.S." = service station: fuel even if the brand is a
    # supermarket (E S Alcampo, E S Carrefour).
    if t.startswith("e s ") or "estacion de servicio" in t:
        hits = [(99, *BASE_CATEGORIES[0][:2])]
    else:
        hits = []
    hits += [(len(k), name, aliases) for name, aliases, keys in BASE_CATEGORIES
             for k in keys if matches(k, t)]
    if not hits:
        return None
    _, name, aliases = max(hits, key=lambda h: h[0])
    by_norm = {norm(c): c for c in existing or []}
    for a in (norm(name),) + aliases:
        if a in by_norm:
            return by_norm[a]
    return name


def suggestion(merchant: str | None, description: str | None,
               categories: list[str]) -> str | None:
    """For /pending: the base category (even if it does not exist yet)."""
    return base_category(f"{merchant} {description}", categories)


# --- Mirror entries: the same money leaving one account and entering another ---
PAIR_DAYS = 3


def detect_pairs(db: Session, since: date | None = None) -> list[tuple[int, int]]:
    """(out_id, in_id) with the same amount, in different accounts and <= PAIR_DAYS
    days apart, that the user has not dismissed yet."""
    since = since or (date.today() - timedelta(days=45))
    base = [Transaction.origin == "bank", Transaction.pair_reviewed.is_(False),
            Transaction.date >= since, Transaction.amount > 0]
    outs = db.scalars(select(Transaction).where(*base, Transaction.kind == "expense")).all()
    ins = db.scalars(select(Transaction).where(*base, Transaction.kind == "income")).all()
    used, pairs = set(), []
    for o in sorted(outs, key=lambda t: t.date):
        for i in ins:
            if (i.id not in used and i.amount == o.amount and i.account != o.account
                    and abs((i.date - o.date).days) <= PAIR_DAYS):
                pairs.append((o.id, i.id))
                used.add(i.id)
                break
    return pairs


def resolve_pair(db: Session, out_id: int, in_id: int, is_transfer: bool) -> None:
    for tid in (out_id, in_id):
        tx = db.get(Transaction, tid)
        if tx is None:
            continue
        if is_transfer:
            tx.kind, tx.category, tx.review = "transfer", TRANSFER, False
        tx.pair_reviewed = True
    db.commit()


# --- Advances: Bizums that paid for a charge BEFORE it arrived (family plan...) ---
ADVANCE_DAYS = 75
ADVANCE_CATS = ("Refunds", "Bizum received")


def _common_key(pattern: str, text: str) -> bool:
    """'spotify' matches 'spoty' or 'spotify' in the description (4-letter prefix)."""
    if len(pattern) < 4:
        return False
    return any(w[:4] == pattern[:4] for w in _words(text))


def detect_advances(db: Session, today: date | None = None) -> list[tuple[int, list[int], Decimal]]:
    """(expense_id, [income ids], total) for recent charges that have earlier Bizums
    with the same key not reviewed yet."""
    today = today or date.today()
    out = []
    charges = db.scalars(select(Transaction).where(
        Transaction.origin == "bank", Transaction.kind == "expense", Transaction.amount > 0,
        Transaction.pair_reviewed.is_(False), Transaction.review.is_(False),
        Transaction.date >= today - timedelta(days=45))).all()
    for g in charges:
        pattern = pattern_of(g.merchant, g.note)
        if not pattern:
            continue
        cands = db.scalars(select(Transaction).where(
            Transaction.kind == "income", Transaction.category.in_(ADVANCE_CATS),
            Transaction.pair_reviewed.is_(False),
            Transaction.date >= g.date - timedelta(days=ADVANCE_DAYS),
            Transaction.date <= g.date + timedelta(days=15)).order_by(Transaction.date)).all()
        chosen, total = [], Decimal("0")
        for c in cands:
            before = norm(c.note or c.merchant).split("bizum")[0]
            if _common_key(pattern, before) and total + c.amount <= g.amount:
                chosen.append(c.id)
                total += c.amount
        if chosen:
            out.append((g.id, chosen, total))
    return out


def resolve_advance(db: Session, charge_id: int, income_ids: list[int], accept: bool) -> None:
    """Yes: the incomes subtract from the charge's category, in the charge's month
    (when they are offset); the original date stays in the note. No: not asked again."""
    g = db.get(Transaction, charge_id)
    g.pair_reviewed = True
    for i in income_ids:
        tx = db.get(Transaction, i)
        if tx is None:
            continue
        tx.pair_reviewed = True
        if accept:
            original = tx.date
            tx.date, tx.year, tx.month = g.date, g.date.year, g.date.month
            tx.note = f"{(tx.note or '')[:200]} (advance of {original:%d/%m/%Y})"
            tx.kind, tx.amount, tx.category, tx.review = "expense", -abs(tx.amount), g.category, False
    db.commit()


# --- Prepaid plans (/prepaid): a family subscription paid up front, etc. ---
# Rule "prepaid:<key>" -> "<months>|<yyyy-mm start>|<category>|<fixed total>". Bizums
# with the key become kind "prepaid" (not income) and each charge of the plan gets
# a refund = total / months (recomputed always; late payers even out every month).
PREPAID_PREFIX = "prepaid:"


def prepaid_plans(db: Session) -> dict[str, tuple[int, date, str, Decimal | None]]:
    """key -> (months, start, category, fixed_total). fixed_total: what was paid in
    an earlier cycle that is not in the app (e.g. last year's Bizums)."""
    out = {}
    for r in db.scalars(select(CategoryRule).where(
            CategoryRule.pattern.like(PREPAID_PREFIX + "%"))).all():
        parts = r.category.split("|")
        months, start, cat = parts[0], parts[1], parts[2]
        fixed = Decimal(parts[3]) if len(parts) > 3 and parts[3] else None
        y, m = map(int, start.split("-"))
        out[r.pattern[len(PREPAID_PREFIX):]] = (int(months), date(y, m, 1), cat, fixed)
    return out


def _month_n(start: date, d: date) -> int:
    return (d.year - start.year) * 12 + d.month - start.month + 1


def prepaid_key(text: str, plans: dict) -> str | None:
    """Plan (without a fixed total) an incoming Bizum belongs to."""
    before = norm(text).split("bizum")[0]
    return next((k for k, v in plans.items() if v[3] is None and _common_key(k, before)), None)


def create_prepaid_plan(db: Session, key: str, months: int, start: date, category: str,
                        fixed_total: Decimal | None = None) -> int:
    """Create/update the plan and mark the Bizums already received with that key
    (a plan with a fixed total marks nothing: that money is not in the app)."""
    key = norm(key).split()[0]
    pattern = PREPAID_PREFIX + key
    value = f"{months}|{start:%Y-%m}|{category}|{fixed_total if fixed_total is not None else ''}"
    r = db.scalar(select(CategoryRule).where(CategoryRule.pattern == pattern))
    if r:
        r.category = value
    else:
        db.add(CategoryRule(pattern=pattern, category=value))
    if fixed_total is not None:
        db.commit()
        return 0
    plans = {key: (months, start, category, fixed_total)}
    n = 0
    for tx in db.scalars(select(Transaction).where(
            Transaction.date >= start - timedelta(days=150),
            Transaction.kind.in_(("income", "expense")))).all():
        text = f"{tx.note or ''} {tx.merchant or ''}"
        incoming_bizum = "bizum" in norm(text) and (tx.kind == "income" or tx.amount < 0)
        if incoming_bizum and prepaid_key(text, plans):
            tx.kind, tx.amount = "prepaid", abs(tx.amount)
            tx.category, tx.review, tx.pair_reviewed = f"Prepaid {key}", False, True
            n += 1
    db.commit()
    return n


def apply_prepaid(db: Session) -> int:
    """Each charge of the plan, in its months, gets the month's share: total / months."""
    done = 0
    for key, (months, start, cat, fixed) in prepaid_plans(db).items():
        received = fixed if fixed is not None else Decimal(str(db.scalar(
            select(func.sum(Transaction.amount)).where(
                Transaction.kind == "prepaid", Transaction.category == f"Prepaid {key}")) or 0))
        share = (received / months).quantize(Decimal("0.01"))
        charges = [t for t in db.scalars(select(Transaction).where(
            Transaction.kind == "expense", Transaction.amount > 0, Transaction.origin == "bank",
            Transaction.date >= start)).all()
            if pattern_of(t.merchant, t.note)[:4] == key[:4] and 1 <= _month_n(start, t.date) <= months]
        seen = set()
        for c in sorted(charges, key=lambda t: t.date):
            n = _month_n(start, c.date)
            if n in seen:
                continue
            seen.add(n)
            ext = f"prepaid:{key}:{c.date:%Y-%m}"
            tx = db.scalar(select(Transaction).where(Transaction.external_id == ext))
            if tx is None:
                tx = Transaction(date=c.date, year=c.date.year, month=c.date.month, kind="expense",
                                 category=cat, origin="prepaid", external_id=ext,
                                 merchant=f"Paid in advance ({key})", amount=Decimal("0"))
                db.add(tx)
            tx.amount = -min(share, c.amount)
            tx.note = f"{n}/{months} · {received} € / {months}"
            done += 1
    db.commit()
    return done
