# Personal Finance Manager

Self-hosted web app to track expenses, income, investments and net worth, with a
Telegram bot to log entries and pull reports from your phone.

![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.137-009688)
![Tests](https://github.com/Ismael-Sallami/personal-finance-manager/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Project status

This app is used privately for real personal finances. This repository is the
public version of it, adapted so anyone can run their own instance: no personal
data, no hardcoded paths, everything driven by environment variables. The code
is the same; only the data is yours.

It is built for **one user**. There is a single login, and every page assumes
the data belongs to that person. That is a deliberate simplification, not a
missing feature.

---

## Features

**Expenses and income**
- Add entries by hand, or paste the notes you already keep on your phone. Each
  line can carry arithmetic (`Groceries: 15-1+3` → 17.00) and it is evaluated
  safely, without `eval`.
- Import preview: fix categories and amounts before saving, then append to the
  month or replace it.
- Monthly and yearly views with category and cashflow charts.

**Investments**
- Import statements from **MyInvestor** (CSV), **Trade Republic** (PDF) and
  **Kraken** (CSV). Each parser returns the same shape, so the rest of the app
  does not care where the data came from.
- Add positions by hand for anything not covered by a parser.
- Automatic revaluation for listed products: give a position a Yahoo symbol, or
  let it resolve from the ISIN, and prices refresh on a schedule. Funds stay
  manual on purpose.
- Contributions (DCA): one-off or monthly, tracked as accumulated cost, so
  profit is always value minus what you actually put in. Money is `Decimal`
  everywhere, never `float`.

**Net worth**
- Bank accounts, savings and cash, added as you like.
- Net worth = investments + accounts, with a snapshot per day feeding the
  evolution chart.

**Reports**
- Excel: monthly and yearly.
- PDF with embedded charts: monthly, yearly, any range of months, and a net
  worth report.

**Telegram bot**
- Log entries from a chat, with a Confirm/Cancel preview before anything is
  saved.
- Summaries, net worth, balances, categories, cashflow and PDF reports on
  demand.
- Weekly database backup delivered to your own chat.

---

## Stack

| Layer | Choice |
|---|---|
| Web | FastAPI, Jinja2, HTMX |
| UI | Tailwind (compiled), Chart.js |
| Data | SQLAlchemy 2, Alembic, Postgres or SQLite |
| Auth | bcrypt, signed session cookie, CSRF tokens |
| Reports | openpyxl, reportlab, matplotlib |
| Market data | yfinance, OpenFIGI |
| Jobs | APScheduler, or any external cron |

---

## Quick start

```bash
git clone https://github.com/Ismael-Sallami/personal-finance-manager.git
cd personal-finance-manager

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit it
python -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY
```

Create the schema and the user:

```bash
alembic upgrade head          # creates the tables
python -m scripts.seed        # creates the user from .env
```

Run it:

```bash
uvicorn app.main:app --reload
# http://localhost:8000  -> sign in with APP_USER_EMAIL / APP_USER_PASSWORD
```

With `DATABASE_URL` empty the app uses a local SQLite file (`dev.db`), which is
enough to try everything. Set it to a Postgres URL for real use.

---

## Configuration

Everything lives in `.env`. Nothing here is baked into the code.

| Variable | What it does | Default |
|---|---|---|
| `DATABASE_URL` | Postgres URL. Empty means local SQLite. | empty |
| `SECRET_KEY` | Signs the session cookie. **Required in production.** | insecure dev value |
| `COOKIE_SECURE` | `true` sends the cookie over HTTPS only. | `false` |
| `IS_PROD` | `true` hides `/docs` and enables HSTS. | `false` |
| `APP_USER_EMAIL` | Login email, created by `scripts/seed.py`. | `admin@local` |
| `APP_USER_PASSWORD` | Login password for the seed script. | `admin` |
| `REPORT_OWNER` | Name printed on generated reports. | `Account owner` |
| `CURRENCY_SYMBOL` | Symbol shown next to every amount. | `€` |
| `TIMEZONE` | IANA timezone the scheduled jobs run in. | `UTC` |
| `OPENFIGI_API_KEY` | Optional key for ISIN lookups (works without one). | empty |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather. Empty disables the bot. | empty |
| `TELEGRAM_CHAT_ID` | Allowlist of chat ids, comma separated. | empty |
| `TELEGRAM_WEBHOOK_SECRET` | Secret Telegram echoes back on every update. | empty |
| `PUBLIC_BASE_URL` | Public URL used to register the webhook. | empty |
| `USE_SCHEDULER` | `true` only on always-on hosts. | `false` |
| `TASKS_TOKEN` | Authorises `/tasks/*`. Empty disables those endpoints. | empty |

---

## Deployment

Two supported paths, both covered in detail in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

**Self-hosted with Docker Compose** — the app plus its own Postgres:

```bash
cp .env.example .env
docker compose up -d
docker compose exec app alembic upgrade head
docker compose exec app python -m scripts.seed
```

**Managed host plus managed Postgres** — `render.yaml` and `fly.toml` are
included as blueprints. Set the secrets in the provider dashboard, then run
`alembic upgrade head` once from the shell.

In production set `COOKIE_SECURE=true` and `IS_PROD=true`, and never keep the
default `SECRET_KEY`: the app refuses to boot with it.

---

## Keeping it alive on a free tier

Free hosting sleeps the app after ~15 minutes without traffic, and free managed
databases pause after several days without queries. If the app is asleep, the
Telegram webhook arrives while the process is still booting and the message is
dropped, so the bot looks broken.

Three pieces prevent it:

| Piece | What it does | Where it runs |
|---|---|---|
| Uptime monitor | `GET /health/db` every 5 minutes | UptimeRobot or similar |
| Task cron | `POST /tasks/*` on a schedule | cron-job.org, GitHub Actions |
| Backup | `POST /tasks/backup` weekly | same cron |

**The two health endpoints are not interchangeable:**

- `/health` always answers and never touches the database. This is the host
  healthcheck. If it failed during a short database outage, the host would tear
  down the deployment.
- `/health/db` runs `SELECT 1` and answers **503** when the database is silent.
  This is the one the monitor should ping: the same request keeps the app awake
  **and** the database alive, and a paused database raises an alert instead of
  going unnoticed.

Recommended cron jobs:

| Job | When |
|---|---|
| `POST /tasks/prices` | daily, 03:00 |
| `POST /tasks/snapshot` | daily, 23:00 |
| `POST /tasks/backup` | weekly |
| `POST /tasks/contributions` | 1st of the month, 06:00 |
| `POST /tasks/reminder` | last day of the month, 20:00 |

The token goes in the `X-Tasks-Token` header or as `?token=`. **On cron-job.org
use `?token=`**: custom headers are not forwarded and the endpoint answers 403
with no further hint. With curl or GitHub Actions the header works and is
preferable, since the token never ends up in a URL. Always use `https://`, or
the redirect will drop the POST body.

### Backups

`POST /tasks/backup` dumps the database to gzipped JSON and sends it to your
Telegram chat. That is the point: the copy lives outside the database provider,
so a paused or deleted project does not take your history with it.

By hand:

```bash
python -m scripts.backup dump                    # finance_backup_<date>.json.gz
python -m scripts.backup restore copy.json.gz    # dry run, writes nothing
python -m scripts.backup restore copy.json.gz --write
```

Restoring **deletes** the tables present in the file before inserting. The
backup never contains `users.password_hash`, so after restoring into an empty
database recreate the user with `python -m scripts.seed`.

---

## Telegram bot

Create a bot with [@BotFather](https://t.me/botfather), put the token in
`TELEGRAM_BOT_TOKEN`, your chat id in `TELEGRAM_CHAT_ID`, and set
`PUBLIC_BASE_URL`. The webhook registers itself on every boot.

| Command | What it returns |
|---|---|
| `/menu` | Button menu |
| `/summary [mm/yyyy]` | Income, expenses, savings, net worth |
| `/net [period]` | Income, expenses and net, any range |
| `/networth` | Investments and accounts |
| `/banks`, `/bank <name> <balance>` | Read or update balances |
| `/expenses [mm/yyyy]` | Expenses by category |
| `/cashflow [yyyy]` | Income vs expenses per month |
| `/savings [mm/yyyy]` | Savings of the period |
| `/report [period]` | PDF with charts |
| `/detailed [period]` | Full PDF: net worth, categories, cashflow |

Any other text is read as expense lines and previewed before saving.

---

## Security

- Password hashed with **bcrypt**; session in a signed `httponly` cookie with
  `samesite=lax` (plus `secure` in production).
- **CSRF token** on every POST form.
- Per-IP sliding window on the login form, so brute force stops after a few
  attempts.
- Uploads limited to 8 MB and filtered by extension.
- Pasted arithmetic is parsed with `ast`, never executed.
- Security headers on every response: CSP, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy`, and HSTS in production.
- API docs hidden when `IS_PROD=true`.
- No personal data in the code: it all comes from `.env`.

---

## Tests

```bash
python -m pytest -q
```

They cover the broker parsers, the P&L maths in `Decimal`, the paste parser,
aggregation, the bot, the task endpoints, backups and the health contracts.

`tests/test_boot_imports.py` deserves a note: it asserts that importing
`app.main` does **not** pull in pandas, matplotlib, reportlab, openpyxl or
pdfplumber. Those add seconds to startup and are only needed when a report is
generated or a statement is uploaded. If that test fails, a heavy import has
crept to the top of a module: move it inside the function that uses it.

---

## Project structure

```
app/
  main.py       config.py  db.py  models.py  auth.py  security.py  templating.py
  format.py     scheduler.py
  routers/      dashboard, expenses, investments, banks, reports, tasks,
                telegram, auth_routes
  services/
    parsers/    myinvestor, traderepublic, kraken, dispatch
    reports/    excel.py  pdf.py  charts.py  generate.py
    aggregation.py  pnl.py  prices.py  symbol_lookup.py  contributions.py
    backup.py   bot.py     expenses_parse.py
  templates/    static/
migrations/     Alembic
scripts/        seed.py  backup.py
tests/
docs/           ARCHITECTURE.md  DEPLOYMENT.md
```

---

## License

MIT. See [LICENSE](LICENSE).
