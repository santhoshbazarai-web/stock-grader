# Stock Grader (Indian Equities)

Personal research tool that grades NSE stocks — Baseline / Fair value / Top band, zone, buy zone,
grade and action. See [`AGENTS.md`](AGENTS.md) and [`docs/SPEC.md`](docs/SPEC.md).

## Layout

| Path | What |
| --- | --- |
| `backend/` | FastAPI api + APScheduler worker (Python 3.12, uv) |
| `frontend/` | Next.js 15 + Tailwind + shadcn/ui |
| `config/` | `providers.yaml`, `valuation.yaml`, `sectors.yaml`, `scoring.yaml`, `technical.yaml` — every threshold and weight, validated at startup |
| `docs/` | Spec and build prompts |

## Quick start

```bash
cp .env.example .env
# set FERNET_KEY and APP_PASSWORD (required — the api and worker refuse to start without them)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

make up        # db, redis, api (:8000), worker, web (:3000)
make migrate   # alembic upgrade head
```

Health check: `curl localhost:8000/api/health`.

## Development

```bash
make install   # uv sync (backend) + npm ci (frontend)
make test      # pytest; DB tests use TEST_DATABASE_URL
               # (default: stockgrader_test on localhost:5432, created by `make up`)
               # and are skipped if Postgres is unreachable; rate-limiter tests
               # likewise use TEST_REDIS_URL (default redis://localhost:6379/15)
make check     # ruff + mypy --strict + eslint + tsc
```

## Config

`app.core.config` loads every `config/*.yaml` into Pydantic models on api/worker startup and
refuses to start on any problem: missing file or key, unknown key, out-of-range value,
weights that do not sum to 1 (sectors) or 100 (pillars), non-monotonic score maps, a bank or
insurance sector that uses FCFF DCF, etc. The config directory is `CONFIG_DIR` (defaults to
`./config`; mounted read-only at `/config` in Docker).

## Database

Models live in `backend/app/db/models.py` (all SPEC §3.4 tables). Ingested data tables carry
`source` + `fetched_at`; derived snapshots carry `computed_at`. Write with
`app.db.upsert.upsert(session, Model, rows)`, which does `INSERT … ON CONFLICT DO UPDATE` on
each model's `__upsert_key__`, so re-running a job is idempotent.

After changing models: `make revision m="describe change"`, review the generated file, then
`make migrate`.

## Data routing

`app.data.router.DataRouter` serves each dataset from the providers listed in
`providers.yaml` `priority`, in order. Per provider it takes a Redis token-bucket token
(`app.core.rate_limiter`, limits from `rate_limits`) before every try and retries transient
`ProviderError`s with exponential backoff (`retry`). It falls through on
`ProviderUnavailable`, exhausted retries, empty data, stale data (`staleness_hours`) or a
rate-limit timeout. If nothing usable comes back it returns `data=None` and records a
`data_gaps` row; if only stale data came back it returns the freshest copy flagged `stale`.
Every result carries `source`, `fetched_at` and `reasons`.
