# Architecture

How the app is put together and why. Written for someone who just cloned the
repository and wants to change something without breaking the rest.

---

## The shape of it

```
browser / Telegram
        │
        ▼
   FastAPI app ──────────────► services ──────────► SQLAlchemy ──► Postgres
   routers/                    parsers, pnl,                       (or SQLite)
   templates/                  aggregation, prices,
                               reports, bot, backup
```

There is no API layer between the pages and the database. Routers read the
request, call a service, and hand a dict to a Jinja template. Business rules
live in `app/services/`, never in a route handler, because the Telegram bot and
the PDF reports call the same functions the web pages do.

---

## Layers

**`app/routers/`** — one module per section. They validate the form, check the
CSRF token, call a service and redirect. They contain no arithmetic.

**`app/services/`** — everything that decides a number:

| Module | Responsibility |
|---|---|
| `parsers/` | Turn a broker file into a list of `Position`. One module per broker plus `dispatch.py`. |
| `pnl.py` | Profit per broker. Statements mean different things, so the formula differs. |
| `aggregation.py` | Every total the app shows: KPIs, chart series, net worth snapshots. |
| `prices.py` | Market prices through yfinance, and the crypto symbol map. |
| `symbol_lookup.py` | ISIN to Yahoo symbol, cached in process. |
| `contributions.py` | One-off and monthly contributions (DCA). |
| `expenses_parse.py` | The paste parser and its safe arithmetic. |
| `reports/` | Excel, PDF and the matplotlib charts embedded in them. |
| `bot.py` | Telegram: dispatch, replies, keyboards, confirmations. |
| `backup.py` | Database dump and delivery. |

**`app/models.py`** — seven tables. Money columns are `NUMERIC(14,2)` and
quantities `NUMERIC(20,8)`. `float` never touches an amount.

---

## Decisions worth knowing

**Money is `Decimal`, end to end.** Parsers return `Decimal`, the ORM stores
`NUMERIC`, the backup serialises decimals as strings. Floats only appear in
chart payloads, where a rounding error is invisible.

**Profit depends on the broker.** `pnl.total_profit()` branches on the broker
because the statements do not describe the same thing: MyInvestor reports
redemptions (so a sold fund must not be counted twice), Kraken nets buys against
sells, and Trade Republic already realises P&L on partial sells. One formula for
all three would silently be wrong for two of them.

**Valuation is manual first.** Automatic pricing is opt-in. It is reliable for
ETFs and crypto and unreliable for funds, so the user decides per position. A
position with no symbol keeps the value that was typed, and only the profit is
recomputed.

**`year` and `month` are stored next to the date.** Every view groups by month,
and an indexed integer comparison behaves the same on Postgres and SQLite,
unlike date part extraction.

**Month ranges use an ordinal.** `year * 12 + month` turns "January 2025 to
March 2026" into a plain integer range, which removes all year-boundary
special cases from the queries.

**Net worth is snapshotted, not recomputed.** Every change to an investment or
an account upserts today's row in `net_worth_snapshots`. The evolution chart
reads that series; recomputing history from current values would redraw the past
every time a price moved.

**Heavy imports live inside functions.** pandas, matplotlib, reportlab, openpyxl
and pdfplumber are imported where they are used, not at module level. On a host
that sleeps, boot time decides whether the Telegram webhook is answered before
Telegram gives up. `tests/test_boot_imports.py` enforces it.

**The bot confirms before writing.** Free text goes through the same parser as
the web import and comes back as a preview with Confirm/Cancel. Pending previews
are held in a capped `OrderedDict`, so a restart loses them and nothing is
written by accident.

---

## Request flows

**Uploading a statement**

```
POST /investments/upload
  -> dispatch.parse(broker, bytes)      # imports the parser lazily
  -> ParseResult(positions, warnings)
  -> investments_preview.html           # user checks values
POST /investments/save
  -> pnl.total_profit(...) per row
  -> BrokerImport + Investment rows
```

An unreadable file never raises: parsers return the error inside `warnings`, so
the page can show it.

**A scheduled job**

```
cron -> POST /tasks/prices?token=...
     -> prices.refresh_all(db)
     -> for each position: fetch price, recompute value and profit
```

The same functions run from `app/scheduler.py` when `USE_SCHEDULER=true`. There
are two entry points because free hosts sleep and cannot be trusted with an
in-process scheduler.

**A Telegram update**

```
POST /tg/webhook  (secret header checked)
  -> bot.handle_update(update)
  -> command or callback -> aggregation / reports
  -> sendMessage or sendDocument
```

The webhook always answers `200`, even when handling failed, because a 5xx makes
Telegram retry the same update forever.

---

## Database

| Table | Holds |
|---|---|
| `users` | The single account. |
| `categories` | Optional catalogue; transactions store the name directly. |
| `transactions` | Expenses and income. |
| `broker_imports` | One row per uploaded statement. |
| `investments` | Valued positions, linked to their import. |
| `bank_accounts` | Accounts, savings and cash. |
| `net_worth_snapshots` | One row per day: investments, banks, total. |

Schema changes go through Alembic:

```bash
alembic revision --autogenerate -m "what changed"
alembic upgrade head
```

`env.py` reads the URL and the metadata from the app, and turns on batch mode on
SQLite, where most `ALTER TABLE` forms do not exist.

---

## Adding a broker

1. Write `app/services/parsers/<broker>.py` with a `parse(content: bytes) ->
   ParseResult`.
2. Register it in `PARSER_MODULES` in `dispatch.py`.
3. If its statement needs different profit maths, add a branch in
   `pnl.total_profit()`.
4. Add a test with a small synthetic file, as in `tests/test_parsers.py`.

Nothing else needs to change: the upload page builds its form from `BROKERS`.
