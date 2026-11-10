"""Bot: bank-sync and analysis commands, /pending, the bank callback and the job."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import BankLink, Budget, CategoryRule, Transaction
from app.services import bot, bot_banks
from tests.test_bot import _run, captured  # noqa: F401  (fixture reused)


def _tx(S, **kw):
    s = S()
    d = kw.pop("day", date.today())
    base = dict(date=d, year=d.year, month=d.month, category="Groceries", kind="expense",
                amount=Decimal("10"), origin="bank", merchant="MERCADONA")
    base.update(kw)
    t = Transaction(**base)
    s.add(t)
    s.commit()
    tid = t.id
    s.close()
    return tid


def _msg(text):
    return {"message": {"chat": {"id": 999}, "text": text}}


def _cb(data):
    return {"callback_query": {"id": "x", "data": data,
                               "message": {"message_id": 1, "chat": {"id": 999}}}}


def _last(captured):
    """(text, keyboard) of the last thing shown (new message or edited card)."""
    return captured["todo"][-1]


def _button(captured, text):
    kb = _last(captured)[1]
    return next(b["callback_data"] for row in kb["inline_keyboard"] for b in row
                if b["text"] == text)


@pytest.fixture
def bank_on(monkeypatch):
    monkeypatch.setattr(settings, "eb_app_id", "app", raising=False)
    monkeypatch.setattr(settings, "eb_private_key", "key", raising=False)
    monkeypatch.setattr(settings, "entry_mode", "auto", raising=False)


def test_pace_and_merchants(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, amount=Decimal("30"), merchant="BAR PEPE", category="Restaurants")
    _run(bot.handle_update(_msg("/pace")))
    assert "you have spent" in captured["msgs"][-1][1]
    _run(bot.handle_update(_msg("/merchants")))
    assert "BAR PEPE" in captured["msgs"][-1][1]


def test_summary_includes_pace(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, amount=Decimal("30"))
    _run(bot.handle_update(_msg("/summary")))
    assert "At this pace" in captured["msgs"][-1][1]


def test_pending_categorises_and_learns(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Restaurants", merchant="OTHER BAR")
    a = _tx(S, category="Uncategorized", review=True, merchant="NEWSSTAND PEPE 1")
    b = _tx(S, category="Uncategorized", review=True, merchant="NEWSSTAND PEPE 2")
    _run(bot.handle_update(_msg("/pending")))
    text, kb = _last(captured)
    assert "<b>2</b> left" in text
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    # every category, even if it does not exist yet, with an icon; your
    # "Restaurants" replaces the base "Eating out" (alias)
    assert "🛒 Groceries" in labels and "💪 Gym" in labels
    assert "🍽 Restaurants" in labels and "🍽 Eating out" not in labels
    _run(bot.handle_update(_cb(_button(captured, "🍽 Restaurants"))))
    final = _last(captured)[0]
    assert "and 1 more" in final and "All categorised" in final
    assert "Expenses by category" in final  # the month's breakdown at the end
    assert len(captured["msgs"]) == 1  # everything in the same card
    s = S()
    assert {s.get(Transaction, i).category for i in (a, b)} == {"Restaurants"}
    assert s.scalar(select(CategoryRule)).pattern == "newsstand"


def test_pending_transfer_and_skip(captured):
    S = sessionmaker(bind=captured["engine"])
    a = _tx(S, category="Uncategorized", review=True, merchant="A")
    _tx(S, category="Uncategorized", review=True, merchant="B",
        day=date.today() - timedelta(days=1))
    _run(bot.handle_update(_msg("/pending")))
    _run(bot.handle_update(_cb(_button(captured, "⏭ Skip"))))              # skips A
    _run(bot.handle_update(_cb(_button(captured, "🔄 My own transfer"))))  # B -> transfer
    s = S()
    assert s.get(Transaction, a).review is True
    assert s.scalar(select(Transaction).where(Transaction.merchant == "B")).kind == "transfer"


def test_budget_set_list_remove(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, amount=Decimal("50"))
    _run(bot.handle_update(_msg("/budget Groceries 100")))
    assert "Budget" in captured["msgs"][-1][1]
    _run(bot.handle_update(_msg("/budget")))
    assert "50 %" in captured["msgs"][-1][1]
    _run(bot.handle_update(_msg("/budget Groceries 0")))
    assert S().scalar(select(Budget)) is None


def test_bank_commands_say_it_is_off_in_manual_mode(captured, monkeypatch):
    monkeypatch.setattr(settings, "eb_app_id", "", raising=False)
    for cmd in ("/connect imagin", "/connect", "/sync", "/connections", "/disconnect"):
        _run(bot.handle_update(_msg(cmd)))
        assert "Bank sync is off" in captured["msgs"][-1][1], cmd
    assert "docs/BANK_SYNC.md" in captured["msgs"][-1][1]


def test_signed_state():
    st = bot_banks.sign_state("Revolut")
    assert bot_banks.read_state(st) == "Revolut"
    assert bot_banks.read_state(st + "x") is None


def test_sync_text_is_silent_when_nothing_to_report():
    r = {"new": 0, "transfers": 0, "accounts": 1, "errors": [], "alerts": [],
         "pending": 3}
    assert bot_banks.sync_text(r) is None
    assert "3 entries" in bot_banks.sync_text({**r, "new": 2})


# --- Bank callback and job (TestClient) ---
class FakeEB:
    def create_session(self, code):
        assert code == "abc"
        return {"session_id": "s1", "access": {"valid_until": "2027-05-01T00:00:00Z"},
                "accounts": [{"uid": "u1", "identification_hash": "Ww" * 200,
                              "account_id": {"iban": "ES0000000000000000001234"}}]}

    def transactions(self, uid, date_from):
        return [{"transaction_amount": {"amount": "4.50"}, "credit_debit_indicator": "DBIT",
                 "booking_date": date.today().isoformat(), "transaction_id": "t1",
                 "creditor": {"name": "CAFE SOL"}}]

    def balances(self, uid):
        return [{"balance_type": "CLBD", "balance_amount": {"amount": "321.00"}}]


@pytest.fixture
def app_client(captured, monkeypatch):
    from app.main import app
    from app.routers import bank_sync as route
    from app.services import bank_sync as svc
    from app.services import enablebanking
    fake = FakeEB()
    for name in ("create_session", "transactions", "balances"):
        monkeypatch.setattr(enablebanking, name, getattr(fake, name))
    # DB shared between threads: TestClient serves the request in another thread.
    Session = sessionmaker(bind=captured["engine"])
    monkeypatch.setattr(route, "SessionLocal", Session)
    monkeypatch.setattr(bot, "SessionLocal", Session)
    monkeypatch.setattr(settings, "eb_app_id", "app", raising=False)
    monkeypatch.setattr(settings, "eb_private_key", "key", raising=False)
    monkeypatch.setattr(settings, "entry_mode", "auto", raising=False)
    monkeypatch.setattr(settings, "telegram_bot_token", "tok", raising=False)
    monkeypatch.setattr(settings, "tasks_token", "secret", raising=False)
    monkeypatch.setattr(settings, "bank_sync_from", "2000-01-01", raising=False)
    sent = []
    monkeypatch.setattr(bot, "send_message_sync", lambda c, t: sent.append(t))
    return TestClient(app), Session, sent, svc


def test_callback_saves_session_and_syncs(app_client):
    c, Session, sent, _ = app_client
    r = c.get("/bank-sync/callback", params={"code": "abc",
                                             "state": bot_banks.sign_state("imagin")})
    assert r.status_code == 200 and "imagin connected" in r.text
    s = Session()
    assert s.scalar(select(BankLink)).uid == "u1"
    assert s.scalar(select(Transaction)).merchant == "CAFE SOL"
    assert "imagin" in sent[-1] and "1 movements" in sent[-1]


def test_callback_rejects_a_forged_state(app_client):
    c, *_ = app_client
    r = c.get("/bank-sync/callback", params={"code": "abc", "state": "made-up"})
    assert r.status_code == 400


def test_callback_is_404_when_bank_sync_is_off(app_client, monkeypatch):
    c, *_ = app_client
    monkeypatch.setattr(settings, "eb_app_id", "", raising=False)
    assert c.get("/bank-sync/callback", params={"code": "abc", "state": "x"}).status_code == 404


def test_banksync_job_and_expiry_warning(app_client, monkeypatch):
    c, Session, sent, _ = app_client
    s = Session()
    s.add(BankLink(bank="Revolut", session_id="s", uid="u1", identification_hash="h9",
                   valid_until=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3)))
    s.commit()
    r = c.post("/tasks/banksync", headers={"X-Tasks-Token": "secret"})
    assert r.status_code == 200 and r.json()["new"] == 1
    assert any("expires" in t and "/connect revolut" in t for t in sent)
    sent.clear()
    c.post("/tasks/banksync", headers={"X-Tasks-Token": "secret"})
    assert not any("expires" in t for t in sent)  # warns only once


def test_banksync_job_fails_loudly_in_manual_mode(app_client, monkeypatch):
    c, *_ = app_client
    monkeypatch.setattr(settings, "eb_app_id", "", raising=False)
    r = c.post("/tasks/banksync", headers={"X-Tasks-Token": "secret"})
    assert r.status_code == 500 and r.json()["ok"] is False


def test_month_end_with_banks(app_client, monkeypatch):
    c, Session, sent, _ = app_client
    photos = []
    monkeypatch.setattr(bot, "send_photo_sync", lambda c, d, caption="": photos.append(len(d)))
    s = Session()
    s.add(BankLink(bank="imagin", session_id="s", uid="u1", identification_hash="h1",
                   valid_until=datetime(2030, 1, 1)))
    s.commit()
    bot.month_end_reminder()
    assert "close" in sent[-1] and photos and photos[0] > 0


def test_views_tolerate_transfers(captured):
    """Sums by kind must ignore kind="transfer"."""
    from app.services.aggregation import build_dashboard, period_totals
    S = sessionmaker(bind=captured["engine"])
    _tx(S, amount=Decimal("10"))
    _tx(S, amount=Decimal("500"), kind="transfer", category="Transfer")
    s = S()
    today = date.today()
    assert period_totals(s, today.year, today.month) == (10.0, 0.0)
    cf = build_dashboard(s, today.year, today.month)["charts"]["cashflow"]
    assert cf["expenses"][today.month - 1] == 10.0 and cf["income"][today.month - 1] == 0.0


def test_commands_are_valid_for_telegram():
    import inspect
    import re
    names = [c for c, _ in bot.COMMANDS]
    assert len(names) == len(set(names)) <= 100
    for c, d in bot.COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c), c
        assert 3 <= len(d) <= 256
    # every suggested command exists in the dispatcher
    source = inspect.getsource(bot.handle_update) + str(bot._BANK_CMDS)
    for c in names:
        assert f'"/{c}"' in source or f"'/{c}'" in source, c


def test_menu_shows_bank_section_only_in_bank_mode(captured, monkeypatch):
    monkeypatch.setattr(settings, "eb_app_id", "", raising=False)
    _run(bot.handle_update(_msg("/menu")))
    kb = captured["msgs"][-1][2]
    texts = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "➕ Connect bank" not in texts and "📊 Summary" in texts

    monkeypatch.setattr(settings, "eb_app_id", "app", raising=False)
    monkeypatch.setattr(settings, "eb_private_key", "key", raising=False)
    monkeypatch.setattr(settings, "entry_mode", "auto", raising=False)
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Uncategorized", review=True)
    _run(bot.handle_update(_msg("/menu")))
    kb = captured["msgs"][-1][2]
    texts = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "🏷 Pending (1)" in texts and "➕ Connect bank" in texts

    _run(bot.handle_update(_msg("/connect")))
    kb = captured["msgs"][-1][2]
    data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "k:imagin" in data and "k:traderepublic" in data
    monkeypatch.setattr(settings, "eb_app_id", "", raising=False)
    _run(bot.handle_update(_cb("k:imagin")))
    assert "Bank sync is off" in captured["edits"][-1][2]


def test_command_with_bot_suffix(captured):
    _run(bot.handle_update(_msg("/pace@MyFinanceBot")))
    assert "Pace" in captured["msgs"][-1][1]


def test_typed_category_creates_and_learns(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Groceries", merchant="X")
    a = _tx(S, category="Uncategorized", review=True, merchant="VET LUNA")
    _run(bot.handle_update(_msg("/pending")))
    _run(bot.handle_update(_cb(_button(captured, "✏️ Other"))))
    assert "Type the category name" in _last(captured)[0]
    _run(bot.handle_update(_msg("Pets")))
    assert any("Pets</b> 🆕" in m[1] for m in captured["msgs"])
    s = S()
    assert s.get(Transaction, a).category == "Pets"
    assert s.scalar(select(CategoryRule)).category == "Pets"
    assert not bot_banks.waiting_for_category()


def test_typed_category_reuses_existing_spelling(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Groceries", merchant="X")
    a = _tx(S, category="Uncategorized", review=True, merchant="LIDL")
    _run(bot.handle_update(_msg("/pending")))
    _run(bot.handle_update(_cb(_button(captured, "✏️ Other"))))
    _run(bot.handle_update(_msg("groceries")))
    assert S().get(Transaction, a).category == "Groceries"


def test_entries_while_waiting_are_not_a_category(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Uncategorized", review=True, merchant="A")
    _run(bot.handle_update(_msg("/pending")))
    _run(bot.handle_update(_cb(_button(captured, "✏️ Other"))))
    _run(bot.handle_update(_msg("- Groceries: 12")))
    assert "Confirm?" in captured["msgs"][-1][1]  # goes through the normal entry flow
    assert not bot_banks.waiting_for_category()


def test_disconnect_with_confirmation(captured, monkeypatch, bank_on):
    from app.services import enablebanking
    revoked = []
    monkeypatch.setattr(enablebanking, "delete_session", lambda sid: revoked.append(sid))
    S = sessionmaker(bind=captured["engine"])
    s = S()
    s.add(BankLink(bank="Trade Republic", session_id="ses-tr", uid="u", identification_hash="h",
                   valid_until=datetime(2030, 1, 1)))
    s.commit()
    tx = _tx(S, merchant="SOMETHING")
    _run(bot.handle_update(_msg("/disconnect")))
    _run(bot.handle_update(_cb(_button(captured, "✂ Trade Republic"))))
    assert "Are you sure" in captured["edits"][-1][2]
    _run(bot.handle_update(_cb("dd:Trade Republic")))
    assert "disconnected" in captured["edits"][-1][2]
    s = S()
    assert s.scalar(select(BankLink)) is None and s.get(Transaction, tx) is not None
    assert revoked == ["ses-tr"]


def test_disconnect_cancel(captured):
    _run(bot.handle_update(_cb("dn")))
    assert "Cancelled" in captured["edits"][-1][2]


def test_manual_entry_of_the_month_gets_todays_date(captured):
    _run(bot.handle_update(_msg("- Groceries: 12")))
    token = captured["msgs"][-1][2]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
    _run(bot.handle_update(_cb(f"c:ok:{token}")))
    t = sessionmaker(bind=captured["engine"])().scalar(select(Transaction))
    assert t.date == date.today() and t.origin == "manual"


def test_pending_with_suggestion_and_visible_rule(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Groceries", merchant="LIDL")
    _tx(S, category="Uncategorized", review=True, merchant="Primaprix Norte Ii")
    _run(bot.handle_update(_msg("/pending")))
    text, kb = _last(captured)
    assert "“primaprix”" in text
    first = kb["inline_keyboard"][0][0]
    assert first["text"] == "⭐ Groceries"
    _run(bot.handle_update(_cb(first["callback_data"])))
    assert "Rule: “primaprix” → Groceries" in captured["edits"][-1][2]


def test_bizum_refund_subtracts_from_the_expense(captured):
    from app.services.aggregation import period_totals
    S = sessionmaker(bind=captured["engine"])
    today = date.today()
    _tx(S, category="Home", merchant="DIGI", amount=Decimal("20"))
    _tx(S, category="Salary", kind="income", merchant="ACME LTD", amount=Decimal("1000"))
    b = _tx(S, category="Uncategorized", review=True, kind="income",
            amount=Decimal("6.67"), merchant="Wifi Bizum payment from: A.B.C.",
            note="Wifi Bizum payment from: A.B.C.")
    _run(bot.handle_update(_msg("/pending")))
    _run(bot.handle_update(_cb(_button(captured, "↩ It is a refund (subtracts from an expense)"))))
    assert "Which expense" in _last(captured)[0]
    token = next(k for k, v in bot_banks._CATEG.items() if v[2])
    idx = bot_banks._CATEG[token][1].index("Home")
    _run(bot.handle_update(_cb(f"p:{token}:{idx}")))
    assert "refund" in captured["edits"][-1][2]
    s = S()
    t = s.get(Transaction, b)
    assert (t.kind, t.category, t.amount) == ("expense", "Home", Decimal("-6.67"))
    expense, income = period_totals(s, today.year, today.month)
    assert (round(expense, 2), income) == (13.33, 1000.0)  # same net, income not inflated
    assert s.scalar(select(CategoryRule)) is None  # Bizum: no rule is learned


def test_expense_rule_on_incoming_money_is_a_refund(db):
    from app.services import bank_sync as bs
    db.add(Transaction(date=date(2026, 11, 1), year=2026, month=11, kind="expense",
                       category="Shopping", amount=Decimal("30")))
    t = Transaction(date=date(2026, 11, 2), year=2026, month=11, kind="income",
                    category="Uncategorized", amount=Decimal("12"), review=True,
                    merchant="AMAZON EU REFUND", note="", origin="bank", account="Revolut")
    db.add(t)
    db.commit()
    assert bs.reclassify_pending(db) == 1
    assert (t.kind, t.category, t.amount) == ("expense", "Shopping", Decimal("-12"))


def test_categories_lists_yours_and_the_base_ones(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Food", amount=Decimal("40"))
    _run(bot.handle_update(_msg("/categories")))
    text = captured["msgs"][-1][1]
    assert "Food" in text and "Gym" in text
    assert "Groceries" not in text  # alias of your "Food": not duplicated


def test_refund_without_headroom_never_goes_negative(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Home", merchant="DIGI", amount=Decimal("5"))  # only 5 in Home
    b = _tx(S, category="Uncategorized", review=True, kind="income",
            amount=Decimal("20"), merchant="Wifi Bizum payment from: X", note="Wifi Bizum")
    _run(bot.handle_update(_msg("/pending")))
    _run(bot.handle_update(_cb(_button(captured, "↩ It is a refund (subtracts from an expense)"))))
    _run(bot.handle_update(_cb(_button(captured, "🏡 Home"))))
    text, kb = _last(captured)
    assert "negative" in text and kb is not None  # back to the card
    t = S().get(Transaction, b)
    assert (t.kind, t.amount, t.review) == ("income", Decimal("20"), True)
    _run(bot.handle_update(_cb(_button(captured, "💸 Bizum received"))))
    t = S().get(Transaction, b)
    assert (t.kind, t.category, t.review) == ("income", "Bizum received", False)


def test_existing_negatives_are_reopened_and_flagged(captured):
    S = sessionmaker(bind=captured["engine"])
    neg = _tx(S, category="Home", amount=Decimal("-6.67"), merchant="Wifi Bizum")
    _run(bot.handle_update(_msg("/expenses")))
    assert "Negative: Home" in captured["msgs"][-1][1]
    _run(bot.handle_update(_msg("/pending")))
    assert "subtracted from <b>Home</b>" in _last(captured)[0]
    t = S().get(Transaction, neg)
    assert (t.kind, t.amount, t.review) == ("income", Decimal("6.67"), True)


def test_automatic_refund_without_headroom_stays_pending(db):
    from app.services import bank_sync as bs
    d = date.today()
    db.add(Transaction(date=d, year=d.year, month=d.month, category="Home", kind="expense",
                       amount=Decimal("5"), origin="bank", merchant="DIGI"))
    tx = Transaction(date=d, year=d.year, month=d.month, category="Uncategorized",
                     kind="income", amount=Decimal("20"), origin="bank", review=True)
    db.add(tx)
    db.commit()
    assert bs.assign_category(tx, "Home", {"Home"}, db=db) is False
    assert (tx.kind, tx.review) == ("income", True)


def test_income_by_category(captured):
    S = sessionmaker(bind=captured["engine"])
    _tx(S, category="Salary", kind="income", amount=Decimal("1200"), merchant="ACME")
    _tx(S, category="Interest", kind="income", amount=Decimal("3.10"), merchant="TR")
    _run(bot.handle_update(_msg("/income")))
    text = captured["msgs"][-1][1]
    assert "💼 Salary" in text and "💰 Interest" in text and "1,203.10" in text or "1.203,10" in text


def test_every_category_has_an_icon():
    cases = {"Utilities": "💡", "Suministros": "💡", "Eating out": "🍽", "Comida": "🍽",
             "Salary": "💼", "Nomina": "💼", "Investments": "📈", "Car": "🚗",
             "Coche": "🚗", "Pets": "🐾", "Mascotas": "🐾", "Groceries": "🛒",
             "Rent": "🏠", "Weird thing": "🏷"}
    for cat, icon in cases.items():
        assert bot_banks._icon(cat) == icon, cat
    assert all(bot_banks._icon(c) != "•" for c in ("Xyz", "Bizums", "Education"))
