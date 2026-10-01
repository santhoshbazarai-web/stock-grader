# Deploying Stock Grader

> For a home PC (Docker Desktop on Windows/WSL2, phone access over Tailscale, nothing public),
> use the home stack instead: README → "Home deployment" (`docker-compose.home.yml`,
> `make home-up`, `make doctor`).

This guide runs the production stack (`docker-compose.prod.yml`) on one Linux server.
Caddy terminates HTTPS with an automatic Let's Encrypt certificate. Postgres is backed up
every night, and all settings come from `.env.production`.

```
Internet ──443/80──▶ caddy ──/api/*──▶ api (FastAPI) ──┐
                       └───── else ──▶ web (Next.js)    ├─▶ db (Postgres 16) ◀── backup (nightly pg_dump)
                                          worker (jobs) ─┴─▶ redis
```

Caddy is the only service that publishes ports. Postgres and Redis sit on an internal Docker
network with no published ports. `migrate` is a one-shot service that applies Alembic
migrations. `api`, `worker` and `backup` start only after it succeeds.

## 1. Server and DNS

- **Server:** a Linux VM with 2 vCPU, 4 GB RAM and 20 GB disk is plenty for one user. Install
  Docker Engine with the Compose plugin (`docker compose version` should print v2 or later).
- **DNS:** create an `A` record, and an `AAAA` record if you have IPv6, pointing your domain
  (e.g. `grader.example.com`) at the server.
- **Firewall:** allow inbound 22 (SSH), 80 and 443 (TCP) and 443/UDP (HTTP/3).
  - Port 80 must stay open: Let's Encrypt validates on it, and Caddy redirects it to HTTPS.
  - Nothing else needs to be reachable.

## 2. Configure

```bash
git clone <your repo> stock-grader && cd stock-grader
cp .env.production.example .env.production
chmod 600 .env.production
```

Fill in `.env.production`:

| Variable | What to put |
|---|---|
| `DOMAIN` | The hostname from step 1, without `https://` |
| `ACME_EMAIL` | Your e-mail, for certificate expiry notices |
| `APP_PASSWORD` | The login password, at least 12 characters |
| `FERNET_KEY` | Generate one with the command in the file. It encrypts broker tokens at rest |
| `POSTGRES_PASSWORD` | Generate with `openssl rand -hex 24`. Use URL-safe characters only, because it goes into `DATABASE_URL` |
| `FYERS_*`, `KITE_*` | Your broker app credentials (step 4). Leave a broker empty to disable it |
| `TELEGRAM_*` | Optional: alert delivery to Telegram |

You don't set these yourself. The compose file derives them from `DOMAIN` and `POSTGRES_*`,
and they override anything in the env file:

- `WEB_URL=https://$DOMAIN`
- `FYERS_REDIRECT_URI` and `KITE_REDIRECT_URI` (step 4)
- `DATABASE_URL` and `REDIS_URL`
- `SESSION_COOKIE_SECURE=true`
- `TRUSTED_PROXIES` (Caddy's fixed address)
- `APP_ENV=production`

With `APP_ENV=production`, the API, worker and migrations refuse to start on unsafe settings,
listing every problem at once:

- a non-HTTPS `WEB_URL`
- an insecure cookie
- a short password
- the development database password
- a redirect URI that doesn't match the domain

The error never echoes secret values.

**Config directory.** The YAML config is bind-mounted from `./config`, or from `CONFIG_PATH`
if set. The API container runs as uid 1000 and needs write access for Settings → Config to
save:

```bash
sudo chown -R 1000:1000 config
```

If the directory isn't writable, saving fails with "config directory is not writable by the
API" and nothing changes. The worker reads the config at start-up, so after saving a change
run `docker compose -f docker-compose.prod.yml --env-file .env.production restart worker`.

## 3. Launch

```bash
make prod-config   # validates the compose file and .env.production
make prod-up       # builds images, runs migrations, starts everything, waits for health
make prod-ps       # every service "healthy" (migrate: "exited (0)")
```

Open `https://<DOMAIN>` and sign in with `APP_PASSWORD`. The first certificate takes a few
seconds; `make prod-logs s=caddy` shows its progress.

**Seed the universe.** The worker's jobs run on their schedules (`config/jobs.yaml`, IST).
To fill the database right away, connect a broker first (step 4), then run:

```bash
P="docker compose -f docker-compose.prod.yml --env-file .env.production"
$P exec worker python -m app.jobs run index_constituents
$P exec worker python -m app.jobs run eod_prices --full
$P exec worker python -m app.jobs run nse_bhavcopy
$P exec worker python -m app.jobs run technicals
$P exec worker python -m app.jobs run valuation_scores
```

Never run `make demo` or `python -m app.devtools.demo` in production: it writes synthetic
stocks.

## 4. Register the broker redirect URIs

Broker logins are OAuth. After you approve the login on the broker's site, the broker sends
the browser back to a redirect URI registered on your app. The API exchanges the one-time
code for an access token, stores it encrypted, and returns you to Settings with a banner.

The production redirect URIs are fixed by your domain:

| Broker | Redirect URI to register |
|---|---|
| Fyers | `https://<DOMAIN>/api/brokers/fyers/callback` |
| Zerodha Kite | `https://<DOMAIN>/api/brokers/kite/callback` |

Register them exactly as shown: `https`, no port, no trailing slash, the same host as
`DOMAIN`. The compose file sets `FYERS_REDIRECT_URI` and `KITE_REDIRECT_URI` to these
values, and production start-up rejects any other value.

**Fyers (API v3)**
1. Sign in at <https://myapi.fyers.in/dashboard> and open your app (or create one). The
   app needs data permissions only; the app never places orders.
2. Set the app's **Redirect URL** to `https://<DOMAIN>/api/brokers/fyers/callback` and save.
3. Copy the **App ID** (for example `XXXXXXXXXX-100`) into `FYERS_APP_ID` and the
   **Secret ID** into `FYERS_SECRET`.

Fyers checks that the `redirect_uri` sent at login matches the registered one. A mismatch
fails on the Fyers page, before any callback.

**Zerodha Kite Connect**
1. Sign in at <https://developers.kite.trade/apps> and open your Kite Connect app.
2. Set its **Redirect URL** to `https://<DOMAIN>/api/brokers/kite/callback` and save.
3. Copy the **API key** into `KITE_API_KEY` and the **API secret** into `KITE_API_SECRET`.

Kite always redirects to the one URL registered on the app. The login request doesn't carry
it; the login state travels in Kite's `redirect_params`. So if the app still points at
`http://localhost:8000/...`, a production login lands on localhost and fails.

To keep a local development setup working, use a separate Kite app (and a separate Fyers
app, if you prefer) with the localhost redirect URI from `.env.example`.

**Apply and check:**
1. After editing `.env.production`, run `make prod-up`; it recreates only the changed
   containers.
2. In the app, open **Settings → Brokers**. A broker with its credentials set shows as
   configured.
3. Click **Connect**, approve on the broker's site, and you should return to
   `/settings?broker=…&status=connected`.

The login must finish within `oauth_state_ttl_s` (10 minutes, `config/providers.yaml`). The
callback URL's one-time `auth_code` / `request_token` and `state` are redacted in both Caddy's
and the API's access logs.

**Daily reconnect:** broker access tokens expire every day (Kite at 06:00 IST). Reconnect
each morning from Settings before the market opens; the dashboard shows each broker's
status.

## 5. Backups

The `backup` service runs `pg_dump` (custom format) every day at `BACKUP_AT`, 23:30 IST by
default. That's after the evening jobs.

**Where:** dumps go to `BACKUP_PATH` (default `./backups`):

| Directory | Contents | Kept |
|---|---|---|
| `daily/` | Every dump | `BACKUP_KEEP_DAYS` (14) days |
| `monthly/` | The first dump of each month | `BACKUP_KEEP_MONTHS` (12) months |

**How a backup is written:**
- Each dump is checked with `pg_restore --list` before it gets its final name.
- A partial file never looks like a backup.
- An existing backup is never overwritten.
- Files are `0600`, owned by root.

**Health:**
- A backup also runs at start-up when the last one is more than a day old.
- The service's health check turns unhealthy when the last successful backup is older than
  26 hours; `make prod-ps` shows it.

```bash
make prod-backup                                  # take one now
ls backups/daily
make prod-restore file=backups/daily/stockgrader-2026-09-28T233000.dump
```

`prod-restore` (`deploy/restore.sh`) asks for confirmation, then:
1. Takes a safety backup of the current database.
2. Stops `api`, `worker` and `web`.
3. Restores the dump in one transaction (`pg_restore --clean --single-transaction`), so a
   failed restore leaves the database unchanged.
4. Runs any newer migrations and starts everything again.

**Keep copies off the server.** The dumps sit on the same disk as the database. Sync
`backups/` elsewhere from the host's cron. For example, with restic:

```cron
# 00:15 IST (after the 23:30 dump), host crontab in UTC
45 18 * * * restic -r s3:s3.amazonaws.com/<bucket>/stock-grader backup /srv/stock-grader/backups --quiet
```

Also keep `.env.production` somewhere safe, separately from the dumps. The `FERNET_KEY` in it
decrypts the broker tokens stored in the database (they expire daily, so losing it only
means reconnecting). The same applies to your `config/` if you edit it in the UI.

**Test a restore now and then,** ideally on a spare box with the same `.env.production`.
Redis holds only locks, the refresh queue and login counters, so it isn't backed up.

## 6. Updates and operations

```bash
make prod-backup && git pull && make prod-up   # migrations run automatically before the API starts
make prod-logs s=api                           # or worker, caddy, backup, …
make prod-ps
make prod-down                                 # stops everything; volumes and backups are kept
```

- **Logs** are JSON files rotated by Docker (5 × 10 MB per service). Cookies, `Authorization`
  headers and OAuth codes are never written.
- **API processes:** `WEB_CONCURRENCY` (default 2).
- **Scale:** run exactly one `worker`; jobs are locked in Redis anyway.
- **HTTP headers:** Caddy sends HSTS, `X-Frame-Options: DENY`, `nosniff` and a strict
  referrer policy. It caps request bodies at 2 MB, and at 25 MB for Screener uploads (the API
  enforces `UPLOAD_MAX_BYTES`).
- **Login throttling** uses the real client IP. The API trusts `X-Forwarded-For` only from
  Caddy's fixed address (`CADDY_IP`), and Caddy replaces any client-supplied value.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Caddy logs `challenge failed` / no certificate | Check the DNS record points here and ports 80/443 are open. Let's Encrypt rate-limits repeated failures, so fix the cause before retrying |
| `api`/`migrate` exits with `APP_ENV=production: …` | Fix each listed setting in `.env.production`, then `make prod-up` |
| `required variable DOMAIN is missing` | `.env.production` is missing or incomplete; always use `make prod-*`, which passes `--env-file` |
| Settings → Config: "not writable by the API" | `sudo chown -R 1000:1000 config` (or your `CONFIG_PATH`) |
| Broker login ends on the broker's page or on localhost | The redirect URI registered with the broker isn't `https://<DOMAIN>/api/brokers/<broker>/callback` (step 4) |
| Callback banner "invalid_state" | The login took longer than 10 minutes, or was started from a different deployment. Click Connect again |
| `Pool overlaps with other one on this host` | Set `EDGE_SUBNET` and `CADDY_IP` to a free range in `.env.production` |
| Test server without public DNS | Set `CADDY_GLOBAL_OPTIONS=local_certs` to use Caddy's internal CA; browsers will warn |
