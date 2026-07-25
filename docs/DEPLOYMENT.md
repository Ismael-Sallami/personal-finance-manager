# Deployment

Two ways to run this in production. Pick one; they do not mix.

- **Self-hosted**: Docker Compose on your own machine or VPS. The app and
  Postgres live side by side. Nothing depends on a provider.
- **Managed**: a hosting service for the app plus a managed Postgres. Free tiers
  work, with the caveats in the last section.

Everything below assumes the app answers over **HTTPS**. On plain HTTP the
session cookie is dropped when `COOKIE_SECURE=true`, and Telegram refuses to
register a webhook at all.

---

## 1. Self-hosted with Docker Compose

```bash
git clone https://github.com/Ismael-Sallami/personal-finance-manager.git
cd personal-finance-manager
cp .env.example .env
```

Edit `.env`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY
```

Set at least `SECRET_KEY`, `APP_USER_EMAIL`, `APP_USER_PASSWORD` and
`IS_PROD=true`. Leave `DATABASE_URL` alone: compose overrides it to point at the
database container.

```bash
docker compose up -d
docker compose exec app alembic upgrade head
docker compose exec app python -m scripts.seed
```

The app listens on `:8080`. Put a reverse proxy with TLS in front of it (Caddy,
nginx, Traefik) and set `COOKIE_SECURE=true` once HTTPS is live.

The compose stack sets `USE_SCHEDULER=true`, because the container stays up. No
external cron is needed.

Postgres data lives in the `pgdata` volume. That volume is your database: back it
up, or rely on the Telegram backup described below.

---

## 2. Managed host plus managed Postgres

### 2.1 The database

Create a Postgres instance anywhere (Supabase, Neon, Railway, a managed RDS,
your own). Take the connection URI and adapt it:

```
DATABASE_URL=postgresql+psycopg2://user:PASSWORD@host:5432/dbname?sslmode=require
```

Two details matter:

- the `+psycopg2` driver prefix, which SQLAlchemy needs;
- `sslmode=require`, so the traffic is encrypted.

If the provider offers a pooled and a direct endpoint, use the pooled one. The
engine already sets `pool_pre_ping` and `pool_recycle`, which is what keeps a
pooler from handing back a dead socket.

### 2.2 The app

`render.yaml` is a ready blueprint: **New > Blueprint**, connect the repository,
and fill in the values marked `sync: false` in the dashboard. `fly.toml` covers
Fly.io, where the machine stays on and runs the migration as a release command.

Minimum environment for production:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the URI above |
| `SECRET_KEY` | `openssl rand -hex 32` |
| `IS_PROD` | `true` |
| `COOKIE_SECURE` | `true` |
| `USE_SCHEDULER` | `false` on hosts that sleep, `true` on always-on machines |
| `TASKS_TOKEN` | `openssl rand -hex 16` |
| `PUBLIC_BASE_URL` | the public https URL of the service |
| `APP_USER_EMAIL`, `APP_USER_PASSWORD` | your login |

The app refuses to boot in production with the default `SECRET_KEY`. That is
deliberate: a known key means anyone can forge a session cookie.

### 2.3 First deploy

From the service shell, once:

```bash
alembic upgrade head
python -m scripts.seed
```

Migrations do **not** run automatically on every deploy (except on Fly, where
`release_command` does it). When a release carries a migration, run it by hand.

---

## 3. Telegram bot

1. Create the bot with [@BotFather](https://t.me/botfather) and copy the token.
2. Get your chat id: message the bot with the app running and no allowlist set,
   and it replies with the id. Put it in `TELEGRAM_CHAT_ID`.
3. Generate a webhook secret:
   `python -c "import secrets; print(secrets.token_hex(16))"`.
4. Set `PUBLIC_BASE_URL` and redeploy. The webhook registers itself at boot.

Check it:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

`last_error_message` tells you what Telegram saw. An empty `url` means
`PUBLIC_BASE_URL` was not set when the app booted.

---

## 4. Free tiers: keeping the thing alive

Free hosting sleeps the app after ~15 minutes without traffic. Free databases
pause after several days without queries. Together they produce the same
symptom: the bot stops answering, because the webhook arrives while the process
is booting and Telegram drops the message.

### 4.1 Uptime monitor

Point a monitor (UptimeRobot's free plan needs no card) at:

```
GET https://<your-app>/health/db     every 5 minutes
```

Use `/health/db`, not `/health`. It runs a real `SELECT 1` and answers 503 when
the database is silent, so the same ping keeps the app awake, keeps the database
from pausing, and alerts you when either is gone.

`/health` is the host healthcheck. It never touches the database on purpose: if
it failed during a brief outage, the host would tear down the deployment.

### 4.2 Task cron

Free plans cannot run an in-process scheduler reliably, so keep
`USE_SCHEDULER=false` and drive the jobs from outside:

| Job | Schedule |
|---|---|
| `POST /tasks/prices` | daily 03:00 |
| `POST /tasks/snapshot` | daily 23:00 |
| `POST /tasks/backup` | weekly |
| `POST /tasks/contributions` | day 1, 06:00 |
| `POST /tasks/reminder` | day 28-31, 20:00 |

Authenticate with the header:

```bash
curl -X POST -H "X-Tasks-Token: $TASKS_TOKEN" https://<your-app>/tasks/prices
```

**On cron-job.org use the query string instead**: custom headers are not
forwarded there, and the endpoint answers 403 with nothing else to go on.

```
https://<your-app>/tasks/prices?token=<TASKS_TOKEN>
```

Always `https://`. With `http://` the host issues a redirect and the POST body
is lost, so the job silently does nothing.

---

## 5. Backups

`POST /tasks/backup` dumps every table to gzipped JSON and sends it to your
Telegram chat, which puts the copy outside the database provider. Schedule it
weekly and the failure mode of a free tier stops being interesting.

By hand, from anywhere with `DATABASE_URL` set:

```bash
python -m scripts.backup dump                    # finance_backup_<date>.json.gz
python -m scripts.backup restore copy.json.gz    # dry run
python -m scripts.backup restore copy.json.gz --write
```

Restoring deletes the contents of every table present in the file before
inserting. `--write` is required precisely because of that. The login user is
not in the backup, so after restoring into an empty database run
`python -m scripts.seed`.

---

## 6. Troubleshooting

**The bot went quiet.**

1. `curl https://<your-app>/health/db` — if it takes a minute, the app was
   asleep: check the monitor.
2. `curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"` — read
   `last_error_message`.
3. The webhook re-registers on every boot, so a redeploy usually fixes it. If
   not, register it manually with `setWebhook`.

**A `/tasks/*` call returns 403.** Either `TASKS_TOKEN` is empty (which disables
the endpoints on purpose) or the token is not arriving. Try `?token=` before
assuming the value is wrong.

**Login works locally but not in production.** `COOKIE_SECURE=true` over plain
HTTP drops the cookie. Either serve HTTPS or set it to `false`.

**The app will not start: "SECRET_KEY is not configured in production".**
Exactly what it says. Generate one and set it.

**Prices never update.** Positions only revalue with a Yahoo symbol, a quantity
and `auto_value` on. Funds are manual by design; check the position in the
Investments page.
