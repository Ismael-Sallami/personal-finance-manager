"""Telegram bot tests (no network: send_* mocked, in-memory DB)."""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import BankAccount, Transaction
from app.services import bot


@pytest.fixture
def captured(monkeypatch):
    """Capture outgoing messages and plug an in-memory DB into the bot."""
    msgs: list[tuple] = []
    edits: list[tuple] = []
    docs: list[tuple] = []

    async def fake_send(chat_id, text, reply_markup=None):
        msgs.append((chat_id, text, reply_markup))

    async def fake_edit(chat_id, message_id, text, reply_markup=None):
        edits.append((chat_id, message_id, text))

    async def fake_answer(callback_id, text=""):
        pass

    async def fake_doc(chat_id, data, filename, caption=""):
        docs.append((chat_id, filename, len(data)))

    monkeypatch.setattr(bot, "send_message", fake_send)
    monkeypatch.setattr(bot, "edit_message_text", fake_edit)
    monkeypatch.setattr(bot, "answer_callback_query", fake_answer)
    monkeypatch.setattr(bot, "send_document", fake_doc)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(bot, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(settings, "telegram_chat_id", "999", raising=False)
    bot._PENDING.clear()
    return {"msgs": msgs, "edits": edits, "docs": docs, "engine": engine}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_is_allowed(monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", "999", raising=False)
    assert bot.is_allowed(999) is True
    assert bot.is_allowed(111) is False


def test_parse_period():
    assert bot._parse_period("06/2026") == (2026, 6)
    assert bot._parse_period("2026") == (2026, None)


def test_unauthorised_chat_is_ignored(captured):
    _run(bot.handle_update({"message": {"chat": {"id": 111}, "text": "/help"}}))
    # Not on the allowlist and the allowlist is not empty -> stay silent.
    assert captured["msgs"] == []


def test_menu_has_buttons(captured):
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/start"}}))
    chat, text, kb = captured["msgs"][0]
    assert "Finance Manager" in text
    assert kb is not None and "inline_keyboard" in kb


def test_entries_need_confirmation(captured):
    # 1) Paste the lines -> preview with buttons, nothing saved yet.
    _run(bot.handle_update({"message": {"chat": {"id": 999},
                                        "text": "- Groceries: 15-1+3\n- Income: Salary: 1200"}}))
    Session = sessionmaker(bind=captured["engine"])
    assert Session().query(Transaction).count() == 0
    chat, text, kb = captured["msgs"][-1]
    assert "Confirm" in text and kb is not None
    # pull the token out of callback_data "c:ok:<token>"
    token = kb["inline_keyboard"][0][0]["callback_data"].split(":")[2]

    # 2) Press Confirm -> rows are inserted.
    _run(bot.handle_update({"callback_query": {
        "id": "cb1", "data": f"c:ok:{token}",
        "message": {"message_id": 5, "chat": {"id": 999}},
    }}))
    txs = Session().query(Transaction).all()
    assert len(txs) == 2
    groceries = next(t for t in txs if t.category == "Groceries")
    assert float(groceries.amount) == 17.0
    assert any("Added 2" in e[2] for e in captured["edits"])


def test_cancelling_discards_the_entries(captured):
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "- Groceries: 10"}}))
    kb = captured["msgs"][-1][2]
    token = kb["inline_keyboard"][0][0]["callback_data"].split(":")[2]
    _run(bot.handle_update({"callback_query": {
        "id": "cb1", "data": f"c:no:{token}",
        "message": {"message_id": 5, "chat": {"id": 999}},
    }}))
    Session = sessionmaker(bind=captured["engine"])
    assert Session().query(Transaction).count() == 0
    assert any("Cancelled" in e[2] for e in captured["edits"])


def test_bank_command_creates_the_account(captured):
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/bank Savings 1500"}}))
    Session = sessionmaker(bind=captured["engine"])
    accounts = Session().query(BankAccount).all()
    assert len(accounts) == 1 and accounts[0].balance == 1500
    assert "Total in banks" in captured["msgs"][-1][1]


def test_report_sends_a_pdf(captured):
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/report 06/2026"}}))
    assert captured["docs"] and captured["docs"][0][1].endswith(".pdf")


def test_report_over_a_range(captured):
    _seed_tx(captured["engine"])
    _run(bot.handle_update({"message": {"chat": {"id": 999},
                                        "text": "/report 01/2026 03/2026"}}))
    assert captured["docs"]
    name = captured["docs"][-1][1]
    assert name.endswith(".pdf") and "012026-032026" in name


def test_detailed_report_sends_a_pdf(captured):
    _seed_tx(captured["engine"])
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/detailed 2026"}}))
    assert captured["docs"]
    assert "Detailed" in captured["docs"][-1][1]


def test_detailed_report_from_the_button(captured):
    _seed_tx(captured["engine"])
    _run(bot.handle_update({"callback_query": {
        "id": "cb1", "data": "m:detailed",
        "message": {"message_id": 1, "chat": {"id": 999}},
    }}))
    assert captured["docs"] and captured["docs"][-1][1].endswith(".pdf")


def test_donut_drops_negative_slices():
    """A matplotlib pie raises on slices <= 0; donut has to filter them out."""
    from app.services.reports import charts
    png = charts.donut(["A", "B"], [100.0, -50.0])
    assert png[:4] == b"\x89PNG"
    png2 = charts.donut(["A"], [-5.0])               # all negative -> "No data"
    assert png2[:4] == b"\x89PNG"


def test_detailed_report_with_a_negative_category(captured):
    """Regression: a category with a negative net must not break /detailed."""
    from datetime import date
    from decimal import Decimal
    s = sessionmaker(bind=captured["engine"])()
    s.add(Transaction(date=date(2025, 10, 1), year=2025, month=10,
                      category="Groceries", kind="expense", amount=Decimal(100)))
    s.add(Transaction(date=date(2025, 10, 2), year=2025, month=10,
                      category="Refund", kind="expense", amount=Decimal(-300)))
    s.commit()
    s.close()
    _run(bot.handle_update({"message": {"chat": {"id": 999},
                                        "text": "/detailed 10/2025 06/2026"}}))
    assert captured["docs"] and captured["docs"][-1][1].endswith(".pdf")


def test_invested_in_period_and_range(captured):
    from datetime import date
    from decimal import Decimal
    from app.services.aggregation import invested_in_period, invested_in_range
    s = sessionmaker(bind=captured["engine"])()
    # a normal expense plus investment-tagged ones, in different months
    s.add(Transaction(date=date(2025, 10, 1), year=2025, month=10,
                      category="Groceries", kind="expense", amount=Decimal(100)))
    s.add(Transaction(date=date(2025, 10, 2), year=2025, month=10,
                      category="Investment", kind="expense", amount=Decimal(300)))
    s.add(Transaction(date=date(2026, 1, 1), year=2026, month=1,
                      category="investing", kind="expense", amount=Decimal(50)))
    s.commit()
    try:
        assert invested_in_period(s, 2025, 10) == 300.0
        assert invested_in_period(s, 2025, None) == 300.0
        assert invested_in_period(s, 2026, 3) == 0.0          # nothing that month
        assert invested_in_range(s, 2025, 10, 2026, 6) == 350.0
    finally:
        s.close()


def test_savings_message_mentions_investing(captured):
    from datetime import date
    from decimal import Decimal
    s = sessionmaker(bind=captured["engine"])()
    s.add(Transaction(date=date(2026, 6, 1), year=2026, month=6,
                      category="Investment", kind="expense", amount=Decimal(200)))
    s.add(Transaction(date=date(2026, 6, 2), year=2026, month=6,
                      category="Groceries", kind="expense", amount=Decimal(80)))
    s.commit()
    s.close()
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/savings 06/2026"}}))
    assert "was invested" in captured["msgs"][-1][1]
    # a month with no investing -> no note
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/savings 05/2026"}}))
    assert "was invested" not in captured["msgs"][-1][1]


def test_categories_in_range(captured):
    from app.services.aggregation import (
        expense_categories_in_range,
        income_categories_in_range,
    )
    _seed_tx(captured["engine"])
    s = sessionmaker(bind=captured["engine"])()
    try:
        # _seed_tx uses category "X" for expenses and "Y" for income.
        expenses = expense_categories_in_range(s, 2026, 1, 2026, 3)
        income = income_categories_in_range(s, 2026, 1, 2026, 3)
        assert expenses == [("X", 600.0)]
        assert income == [("Y", 3000.0)]
    finally:
        s.close()


def test_cashflow_in_range(captured):
    from app.services.aggregation import cashflow_in_range
    _seed_tx(captured["engine"])
    s = sessionmaker(bind=captured["engine"])()
    try:
        labels, income, expenses = cashflow_in_range(s, 2026, 1, 2026, 3)
        assert labels == ["Jan 26", "Feb 26", "Mar 26"]
        assert income == [1000.0, 1000.0, 1000.0]
        assert expenses == [100.0, 200.0, 300.0]
    finally:
        s.close()


def test_net_worth_button(captured):
    _run(bot.handle_update({"callback_query": {
        "id": "cb1", "data": "m:networth",
        "message": {"message_id": 1, "chat": {"id": 999}},
    }}))
    assert any("Net worth" in m[1] for m in captured["msgs"])


def _seed_tx(engine):
    from datetime import date
    from decimal import Decimal
    s = sessionmaker(bind=engine)()
    # Jan/Feb/Mar: expenses 100/200/300, income 1000 each; Jun: 50 and 500.
    for m, (e, i) in {1: (100, 1000), 2: (200, 1000), 3: (300, 1000), 6: (50, 500)}.items():
        s.add(Transaction(date=date(2026, m, 1), year=2026, month=m,
                          category="X", kind="expense", amount=Decimal(e)))
        s.add(Transaction(date=date(2026, m, 2), year=2026, month=m,
                          category="Y", kind="income", amount=Decimal(i)))
    s.commit()
    s.close()


def test_range_totals(captured):
    from app.services.aggregation import range_totals
    _seed_tx(captured["engine"])
    s = sessionmaker(bind=captured["engine"])()
    try:
        # Jan-Mar: expenses 600, income 3000
        assert range_totals(s, 2026, 1, 2026, 3) == (600.0, 3000.0)
        # June only
        assert range_totals(s, 2026, 6, 2026, 6) == (50.0, 500.0)
        # whole year
        assert range_totals(s, 2026, 1, 2026, 12) == (650.0, 3500.0)
    finally:
        s.close()


def test_net_over_a_range(captured):
    _seed_tx(captured["engine"])
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/net 01/2026 03/2026"}}))
    txt = captured["msgs"][-1][1]
    assert "Net" in txt
    assert "3,000.00" in txt   # income
    assert "600.00" in txt     # expenses
    assert "2,400.00" in txt   # net = 3000 - 600


def test_net_for_a_month_and_a_year(captured):
    _seed_tx(captured["engine"])
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/net 06/2026"}}))
    assert "450.00" in captured["msgs"][-1][1]     # 500 - 50
    _run(bot.handle_update({"message": {"chat": {"id": 999}, "text": "/net 2026"}}))
    assert "2,850.00" in captured["msgs"][-1][1]   # 3500 - 650


def test_net_button(captured):
    _seed_tx(captured["engine"])
    _run(bot.handle_update({"callback_query": {
        "id": "cb1", "data": "m:net",
        "message": {"message_id": 1, "chat": {"id": 999}},
    }}))
    assert any("Net" in m[1] for m in captured["msgs"])
