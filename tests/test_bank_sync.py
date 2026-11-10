"""Bank sync: normalisation, dedup, transfers, rules (no network)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import BankAccount, BankLink, CategoryRule, Transaction
from app.services import bank_sync as bs


def raw(amount, ind="DBIT", name="MERCADONA 1234 MADRID", day="2026-11-05",
        tid="t1", status="BOOK", iban=None, info=None):
    party = {"name": name} if name else None
    r = {"transaction_amount": {"amount": str(amount), "currency": "EUR"},
         "credit_debit_indicator": ind, "status": status, "booking_date": day,
         "transaction_id": tid, "remittance_information": info or []}
    if ind == "DBIT":
        r["creditor"] = party
        r["creditor_account"] = {"iban": iban} if iban else None
    else:
        r["debtor"] = party
        r["debtor_account"] = {"iban": iban} if iban else None
    return r


class FakeEB:
    """`txs` is a list (the same for every account) or {uid: list}."""

    def __init__(self, txs, balance="1234.56"):
        self.txs, self.balance, self.since = txs, balance, None

    def transactions(self, uid, date_from):
        self.since = date_from
        return self.txs.get(uid, []) if isinstance(self.txs, dict) else self.txs

    def balances(self, uid):
        return [{"balance_type": "CLBD", "balance_amount": {"amount": self.balance}}]


@pytest.fixture
def link(db):
    row = BankLink(bank="imagin", session_id="s", uid="u1", identification_hash=bs._sha("h1"),
                   iban="ES1100000000000000000001",
                   valid_until=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=100))
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "bank_sync_from", "2026-11-01", raising=False)
    monkeypatch.setattr(settings, "report_owner", "Jane Doe", raising=False)


def test_normalize_expense_and_income():
    g = bs.normalize(raw("12.50"), "imagin", "u1")
    assert (g.kind, g.amount, g.merchant, g.date) == (
        "expense", Decimal("12.50"), "MERCADONA 1234 MADRID", date(2026, 11, 5))
    assert g.external_id == "imagin:t1"
    i = bs.normalize(raw("1500", ind="CRDT", name="ACME LTD"), "imagin", "u1")
    assert i.kind == "income" and i.merchant == "ACME LTD"


def test_normalize_drops_pending_and_hash_is_stable():
    assert bs.normalize(raw("5", status="PDNG"), "imagin", "u1") is None
    a = bs.normalize(raw("5", tid=None), "imagin", "u1")
    b = bs.normalize(raw("5", tid=None), "imagin", "u1")
    assert a.external_id == b.external_id and ":h:" in a.external_id


def test_normalize_uses_description_without_a_name():
    m = bs.normalize(raw("9.99", name=None, info=["NETFLIX.COM"]), "Revolut", "u")
    assert m.merchant == "NETFLIX.COM"


def test_categorize_prefers_user_rules_and_longest_pattern():
    rules = {"mercadona": "Food", "amazon": "Shopping", "amazon prime": "Subscriptions"}
    assert bs.categorize("MERCADONA 1234", rules) == "Food"
    assert bs.categorize("AMAZON PRIME ES", rules) == "Subscriptions"
    assert bs.categorize("Savings plan execution", {}) == "Investment"
    assert bs.categorize("XYZ", rules) is None


def test_pattern_of():
    assert bs.pattern_of("MERCADONA 1234 MADRID CENTRO") == "mercadona"
    assert bs.pattern_of("Café Nº5 SL") == "cafe"
    assert bs.pattern_of("Spotify P48FDBA77") == "spotify"
    assert bs.pattern_of("Mercadona Main Street") == "mercadona"
    assert bs.pattern_of("E S Alcampo Norte") == "alcampo"
    assert bs.pattern_of("Google*google*one 01m") == "google"
    assert bs.pattern_of("12345") == ""


def test_bizum_gets_no_rule_and_no_auto_classification():
    """You categorise Bizum yourself: no rule is learned, nothing is auto-classified."""
    assert bs.pattern_of("ALEX SMITH JONES",
                         "Dinner Bizum payment to: Alex S.J.") == ""
    assert bs.categorize("Dinner Bizum payment to: Corner Bar", {"dinner": "Eating out"}) is None
    assert bs.pattern_of("Ale-hop 98 Madrid") == "ale-hop"


def test_base_categories_real_cases():
    cases = {
        "Mercadona Main Street": "Groceries", "Primaprix Norte Ii": "Groceries",
        "Lidl": "Groceries", "Aldi Madrid": "Groceries", "DIA 1234": "Groceries",
        "E S Alcampo Norte": "Fuel",                 # service station, not the supermarket
        "Basic-Fit Campus": "Gym",
        "Spotify P48FDBA77": "Subscriptions", "Google*google*one 01m": "Subscriptions",
        "Vodafone Espana": "Phone & internet", "Digimobil": "Phone & internet",
        "Comedor Universitario": "Eating out", "Cafeteria Plaza": "Eating out",
        "Farmacia Central": "Health", "Pull & Bear": "Clothes",
        "Decathlon Connect Nevada": "Clothes",
        "Ale-hop 98 Madrid": "Shopping", "Fotocopias Express": "Studies",
        "Uber Eats": "Eating out", "Uber BV": "Transport",
    }
    for merchant, cat in cases.items():
        assert bs.categorize(merchant, {}) == cat, merchant
    assert bs.categorize("Corner Store", {}) is None  # unknown: it asks you


def test_base_category_uses_your_name_if_it_exists():
    assert bs.categorize("Mercadona", {}, ["Food", "Leisure"]) == "Food"
    assert bs.categorize("Bar Pepe", {}, ["Restaurants"]) == "Restaurants"
    assert bs.categorize("Mercadona", {"mercadona": "Meals"}, ["Food"]) == "Meals"


def test_matches_whole_words():
    assert bs.matches("dinner", "dinner jane")
    assert not bs.matches("dinner", "dinnerware shop")
    assert bs.matches("amazon prime", "payment amazon prime es")


def test_revolut_internal_transfers():
    for text in ("From Cuenta Remunerada", "To Cuenta Remunerada", "Exchanged to EUR",
                 "Top-Up by *1234", "To pocket EUR Trip"):
        assert bs.text_is_transfer(text, "Revolut"), text
    assert not bs.text_is_transfer("Mercadona Main Street", "Revolut")


def test_sync_inserts_deduplicates_and_updates_balance(db, link):
    db.add(CategoryRule(pattern="mercadona", category="Food"))
    db.commit()
    eb = FakeEB([raw("12.50", tid="a"), raw("3", name="NEWSSTAND PEPE", tid="b")])
    res = bs.sync_all(db, today=date(2026, 11, 10), client=eb)
    assert res.accounts == 1 and len(res.new) == 2
    txs = {t.merchant: t for t in db.scalars(select(Transaction)).all()}
    assert txs["MERCADONA 1234 MADRID"].category == "Food"
    assert txs["MERCADONA 1234 MADRID"].review is False
    assert txs["NEWSSTAND PEPE"].category == bs.UNCATEGORIZED and txs["NEWSSTAND PEPE"].review
    acc = db.scalar(select(BankAccount))
    assert acc.name == "imagin" and acc.balance == Decimal("1234.56")
    assert eb.since == date(2026, 11, 1)

    # Second pass: nothing new, and the window starts 5 days before the last sync.
    res2 = bs.sync_all(db, today=date(2026, 11, 12), client=eb)
    assert res2.new == [] and len(db.scalars(select(Transaction)).all()) == 2
    assert eb.since == date(2026, 11, 5)


def test_sync_reuses_an_existing_manual_account(db, link):
    db.add(BankAccount(name="Imagin", balance=Decimal("10")))
    db.commit()
    bs.sync_all(db, today=date(2026, 11, 10), client=FakeEB([]))
    accounts = db.scalars(select(BankAccount)).all()
    assert len(accounts) == 1 and accounts[0].balance == Decimal("1234.56")


def test_transfers_do_not_count(db, link):
    db.add(BankLink(bank="Revolut", session_id="s2", uid="u2", identification_hash="h2",
                    iban="LT000000000000000002",
                    valid_until=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=100)))
    db.commit()
    eb = FakeEB({"u1": [
        raw("100", name="Someone", iban="LT000000000000000002", tid="x1"),  # own IBAN
        raw("50", name="JANE DOE", tid="x2"),                               # my own name
        raw("20", name="REVOLUT*TOPUP", tid="x3"),                          # top-up
        raw("7", name="CORNER BAR", tid="x4"),
    ], "u2": [
        raw("5", name="Revolut Ltd fee", tid="y1"),  # the bank itself: NOT a transfer
    ]})
    res = bs.sync_all(db, today=date(2026, 11, 10), client=eb)
    assert res.transfers == 3 and len(res.new) == 2
    kinds = {t.merchant: t.kind for t in db.scalars(select(Transaction)).all()}
    assert kinds["CORNER BAR"] == "expense"
    assert kinds["Someone"] == kinds["JANE DOE"] == kinds["REVOLUT*TOPUP"] == "transfer"


def test_expired_consent_is_skipped(db, link):
    link.valid_until = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    db.commit()
    res = bs.sync_all(db, today=date(2026, 11, 10), client=FakeEB([raw("1")]))
    assert res.accounts == 0 and "expired" in res.errors[0]


def test_learn_rule_applies_to_pending(db, link):
    bs.sync_all(db, today=date(2026, 11, 10), client=FakeEB([
        raw("3", name="NEWSSTAND PEPE 01", tid="a"), raw("4", name="NEWSSTAND PEPE 02", tid="b"),
        raw("5", name="OTHER", tid="c")]))
    tx = db.scalar(select(Transaction).where(Transaction.external_id == "imagin:a"))
    others = bs.learn_rule(db, tx, "Treats")
    assert others == 1
    cats = {t.external_id: (t.category, t.review) for t in db.scalars(select(Transaction))}
    assert cats["imagin:b"] == ("Treats", False)
    assert cats["imagin:c"] == (bs.UNCATEGORIZED, True)
    assert db.scalar(select(CategoryRule)).pattern == "newsstand"


def test_save_session_remaps_by_hash(db, link):
    ses = {"session_id": "new", "access": {"valid_until": "2027-05-01T00:00:00Z"},
           "accounts": [{"uid": "u-new", "identification_hash": "h1",
                         "account_id": {"iban": "ES1100000000000000000001"}}]}
    links = bs.save_session(db, ses, "imagin")
    assert len(db.scalars(select(BankLink)).all()) == 1
    assert links[0].uid == "u-new" and links[0].valid_until.year == 2027


def test_long_ids_fit_in_the_columns(db, link):
    tid = "T" * 300
    m = bs.normalize(raw("1", tid=tid), "imagin", "u1")
    assert len(m.external_id) <= 128
    assert m.external_id == bs.normalize(raw("1", tid=tid), "imagin", "u1").external_id
    ses = {"session_id": "s", "access": {"valid_until": "2027-05-01T00:00:00Z"},
           "accounts": [{"uid": "x", "identification_hash": "Ww" * 300}]}
    assert len(bs.save_session(db, ses, "imagin")[0].identification_hash) == 64


def test_save_session_ignores_non_eur_accounts(db):
    ses = {"session_id": "s", "access": {"valid_until": "2027-05-01T00:00:00Z"},
           "accounts": [{"uid": "a", "identification_hash": "eur", "currency": "EUR"},
                        {"uid": "b", "identification_hash": "gbp", "currency": "GBP"}]}
    links = bs.save_session(db, ses, "Revolut")
    assert [link.identification_hash for link in links] == [bs._sha("eur")]


def test_two_accounts_without_iban_do_not_share_a_balance(db):
    until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=100)
    for h in ("hash-aaaa", "hash-bbbb"):
        db.add(BankLink(bank="Revolut", session_id="s", uid=h, identification_hash=h,
                        valid_until=until))
    db.commit()
    bs.sync_all(db, today=date(2026, 11, 10), client=FakeEB([]))
    names = sorted(a.name for a in db.scalars(select(BankAccount)))
    assert names == ["Revolut …aaaa", "Revolut …bbbb"]


def test_sync_reclassifies_old_pending(db, link):
    """Pending entries from before the rules improved get fixed on the next sync."""
    for i, (merchant, kind) in enumerate([("From Cuenta Remunerada", "income"),
                                          ("Mercadona Central", "expense")]):
        db.add(Transaction(date=date(2026, 11, 2), year=2026, month=11, kind=kind,
                           category=bs.UNCATEGORIZED, amount=Decimal("20"), merchant=merchant,
                           note=merchant, origin="bank", account="Revolut", review=True,
                           external_id=f"old{i}"))
    db.add(CategoryRule(pattern="mercadona", category="Food"))
    db.commit()
    res = bs.sync_all(db, today=date(2026, 11, 10), client=FakeEB([]))
    assert res.reclassified == 2
    txs = {t.merchant: t for t in db.scalars(select(Transaction))}
    assert txs["From Cuenta Remunerada"].kind == "transfer"
    assert txs["Mercadona Central"].category == "Food" and not txs["Mercadona Central"].review


def test_suggestion_uses_base_or_your_name():
    assert bs.suggestion("Primaprix Norte Ii", "", ["Groceries", "Leisure"]) == "Groceries"
    assert bs.suggestion("Farmacia Central", "", ["Groceries"]) == "Health"
    assert bs.suggestion("Sam T", "Flat rent Bizum", ["Rent"]) is None


def test_psd2_query_limit(db, link):
    eb = FakeEB([raw("1", tid="z")])
    calls = []
    orig = eb.transactions
    eb.transactions = lambda uid, d: calls.append(uid) or orig(uid, d)
    bs.sync_all(db, today=date(2026, 11, 10), client=eb)                  # daily job
    bs.sync_all(db, today=date(2026, 11, 10), client=eb)                  # same day: no
    r = bs.sync_all(db, today=date(2026, 11, 10), client=eb, force=True)  # manual, < 4 h: no
    assert len(calls) == 1 and r.skipped == ["imagin"]
    bs._LAST_QUERY.clear()                                                # hours pass
    bs.sync_all(db, today=date(2026, 11, 10), client=eb, force=True)
    assert len(calls) == 2


def test_429_gives_a_clear_message(db, link):
    class EB429(FakeEB):
        def transactions(self, uid, date_from):
            raise RuntimeError('GET /accounts/x/transactions -> 429: {"code":429}')
    r = bs.sync_all(db, today=date(2026, 11, 10), client=EB429([]))
    assert "query limit" in r.errors[0]


def test_investment_platforms(db, link):
    tr = BankLink(bank="Trade Republic", session_id="s", uid="tr", identification_hash="htr",
                  role="investment",
                  valid_until=datetime(2030, 1, 1))
    db.add(tr)
    db.add(Transaction(date=date(2026, 11, 1), year=2026, month=11, kind="expense",
                       category="Investment", amount=Decimal("50"), origin="manual"))
    db.add(Transaction(date=date(2026, 11, 2), year=2026, month=11, kind="expense",
                       category=bs.UNCATEGORIZED, amount=Decimal("100"), merchant="Unknown",
                       origin="bank", account="Trade Republic", review=True,
                       external_id="old-tr"))
    db.commit()
    eb = FakeEB({"u1": [raw("200", name="Trade Republic Bank GmbH", tid="inv1")],
                 "tr": [raw("200", name=None, tid="etf1")]})
    bs.sync_all(db, today=date(2026, 11, 10), client=eb)
    txs = {t.external_id: t for t in db.scalars(select(Transaction))}
    assert (txs["imagin:inv1"].kind, txs["imagin:inv1"].category) == ("expense", "Investment")
    assert "Trade Republic:etf1" not in txs                 # internal buys: ignored
    assert txs["old-tr"].kind == "transfer" and not txs["old-tr"].review
    tr_acc = db.scalar(select(BankAccount).where(BankAccount.name == "Trade Republic"))
    assert tr_acc.balance == Decimal("1234.56")             # its balance does count


def test_investment_banks_are_configurable(monkeypatch):
    monkeypatch.setattr(settings, "investment_banks", "Acme Broker", raising=False)
    assert bs.is_platform("ACME Broker Ltd") and not bs.is_platform("Trade Republic")
    assert bs.goes_to_investment("Transfer to Acme Broker", "imagin")
