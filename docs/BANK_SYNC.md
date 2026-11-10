# Bank sync (optional)

The app can run in two modes. Pick the one you prefer, or switch later: no data
is lost either way.

| Mode | When | How entries get in |
|---|---|---|
| **manual** | `EB_APP_ID` / `EB_PRIVATE_KEY` empty (default) | You keep notes and paste them to the Telegram bot, or to the web import, at the end of the month. |
| **bank** | both `EB_*` set | A daily read-only PSD2 sync pulls movements and balances. Cash and anything else can still be typed by hand. |

`ENTRY_MODE` (`auto` by default) lets you force it: `manual` turns the bank
features off even if the credentials are there, `auto` and `bank` switch them
on when `EB_*` are set. While it is off, `/connect`, `/sync`, `/connections`
and `/disconnect` just answer that bank sync is off, and the `banksync` job
answers 500 so a cron does not look green by mistake.

## Privacy

- It uses the **Account Information** (AIS) service only: accounts, balances and
  movements. There is no payment initiation, nothing can move money.
- Access is granted by you at your own bank and lasts at most 90 to 180 days
  (the bank decides). You can revoke it with `/disconnect` or at the bank.
- [Enable Banking](https://enablebanking.com) is the aggregator between the app
  and the bank. In restricted mode it stores no data about you beyond the
  session; movements are downloaded to **your** database and nowhere else.
- The private key of your application never leaves your server: it is read from
  `EB_PRIVATE_KEY` and only used to sign short-lived JWTs.

## 1. Enable Banking account

1. Create a free account at <https://enablebanking.com> and open the control
   panel.
2. Register an application in the **Production** environment, service
   *Account Information* only. Use the **restricted mode ("link your own
   accounts")**: it is free and lets you connect only accounts you own.
3. Download the application's private key (`<APP_ID>.pem`). Keep it out of git.
4. Add a redirect URL: `<PUBLIC_BASE_URL>/bank-sync/callback`
   (for example `https://your-app.example.com/bank-sync/callback`). It must be
   exactly the URL the app is served from.
5. In the panel, link your own accounts for each bank ("Activate by linking
   accounts"). In restricted mode this step is required before authorising.

Check that your banks are available in your country and note the maximum consent
validity. The country is set by `COUNTRY` in `app/services/enablebanking.py` and
the bank names accepted by `/connect` by `BANKS` in `app/services/bot_banks.py`.

## 2. Configure the app

```env
ENTRY_MODE=auto
EB_APP_ID=<application id>
EB_PRIVATE_KEY=<content of the .pem, multi-line or with \n escapes>
PUBLIC_BASE_URL=https://your-app.example.com
BANK_SYNC_FROM=2026-01-01     # optional: nothing before this date is imported
```

`BANK_SYNC_FROM` avoids duplicating what you already typed by hand. Then apply
the migration: `alembic upgrade head`.

## 3. Connect your banks

In the bot send `/connect` and pick a bank (or `/connect revolut`). Open the
link, press "Continue authentication" and approve at your bank. The callback
saves the session, imports the movements and the bot confirms. `/connections`
shows the days left for each consent.

Your bank is not in the buttons? Press "➕ Other bank", pick the country (or type
`/connect country XX`), then tap the bank in the paginated list or type part of
its name. Link it first in the Enable Banking control panel ("Activate by linking
accounts"); the bot reminds you with a link. Any other command cancels the search.

The first imports leave some entries as "Uncategorized". Use `/pending`: answer
with a button and the app stores a rule for that merchant brand, so the next
charge is classified on its own. Bizum is never classified automatically.

## 4. Schedule the sync

Call the job once a day (08:00 is fine):

```
POST /tasks/banksync?token=<TASKS_TOKEN>
```

On cron-job.org use the `?token=` form. If you run on an always-on host with
`USE_SCHEDULER=true`, the in-process scheduler does it for you. The job syncs
every account, sends only the alerts that matter (unusual charges, new or
pricier subscriptions, budgets at 80 % and 100 %, consents about to expire) and
answers 500 when an account fails.

Also schedule `POST /tasks/reminder` on days 28 to 31: it only sends on the last
day of the month (the month-end close with a chart in bank mode, the paste
reminder in manual mode). `?force=1` sends it anyway.

### Query limits (PSD2)

Without you present, banks may allow only about 4 queries a day per account (a
429 error). Each sync spends 2, so the daily job queries each bank once a day,
and a manual `/sync` or the month-end close only queries a bank again if 4 hours
have passed since its last query. Connecting a bank syncs only that bank. A 429
is reported in plain words and retried the next day.

### Investment platforms

`INVESTMENT_BANKS` (comma list, default `trade republic,myinvestor`) names the
platforms you only use to invest. Money you send to them from a spending account
is stored as an "Investment" expense (your existing name for it if you have one),
not as a transfer. Their own movements (ETF buys, savings plans) are ignored: only
their cash balance is read. Pending rows from those accounts are retired. Set it
empty if you do not want this.

Investment platforms are tracked as invested capital, not valued positions: each
one has an account of kind "investment". Money you send to a platform from a
spending account adds to it, money coming back subtracts (never below 0), and
`/invested [platform amount]` sets it by hand ("1.500,50" and "1500.50" both
work). When any exist, net worth uses them instead of valued positions (no P&L)
and the bank totals and `/banks` leave them out. For platforms that cannot be read
(not connected, or the connector is still beta at Enable Banking) the bot asks
for the amount if not confirmed in 30 days, in `/networth`, `/banks`, the month
close and the day-1 sync, and tells you when the connector stops being beta
(`BANK_COUNTRY`, default ES, is the country it looks them up in).

### Bank roles

Each connected bank has a role, shown in `/connections` (tap "⚙ bank" to change
it): main (only one), secondary, investment or savings. A new bank starts as
investment if its name matches `INVESTMENT_BANKS`, otherwise as main if you have
none yet, otherwise as secondary. The investment role drives the platform logic
above (`INVESTMENT_BANKS` stays as the default for names not connected yet).
Savings: what you move there is a transfer.

### Mirror pairs

If the same amount leaves one account and enters another within 3 days and no
rule caught it (typically your salary moved to another bank), `/pending` first
asks "Is this the same money moving between your accounts?". Yes turns both
into transfers; No marks them reviewed and it is never asked again. The daily
sync warns you when there is any pair waiting. The account owner (`REPORT_OWNER`)
is recognised with any two words of the name.

## 5. Renewing the consent

A consent lasts 90 to 180 days depending on the bank. Ten days before it ends
the bot warns you. Renew with `/connect <bank>`: the same accounts are re-mapped
and nothing is duplicated. After it expires, the sync reports "consent expired"
for that bank until you renew.

## How movements are treated

- Pending movements are ignored; booked ones are deduplicated by the bank's id.
- Transfers between your own accounts (known IBANs, `REPORT_OWNER` as the
  counterparty, top-ups, savings pots, currency exchange) become
  `kind="transfer"` and are excluded from every sum.
- Money that comes in under an expense category (a shop refund, a Bizum for a
  shared bill) is stored as a negative expense, so it subtracts from the
  category.
- Only EUR accounts are imported, because everything is added up in one currency.
- Imports in "replace" mode of the web import only delete entries typed by
  hand, never the ones that came from the bank.
