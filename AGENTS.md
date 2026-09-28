# AGENTS.md — Stock Grader (Indian Equities)

Instructions for Codex / any coding agent working in this repo. Read this file and `docs/SPEC.md` before every task.

## What this app does
A web application that, for any NSE-listed stock, computes:
- **Baseline price** (floor), **Fair value** (intrinsic), **Top band** (premium ceiling)
- **Zone**: Deep Discount / Discount / Fair / Premium / Extreme Premium
- **Buy zone** price range + invalidation level (from technicals)
- **Grade** (A+/A/B/C/D) from a 6-pillar score + knock-out filters
- **Action**: Strong Buy / Buy / Accumulate / Buy on Pullback / Momentum Entry / Wait / Hold / Book Profits / Avoid

All formulas, thresholds and weights live in `config/*.yaml`. **Never hard-code a threshold in Python.**

## Stack (do not change without asking)
- Backend: Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, pandas, numpy, scipy
- DB: PostgreSQL 16 (DuckDB allowed only in notebooks/backtests)
- Jobs: APScheduler inside a separate `worker` service (Celery+Redis only if APScheduler proves insufficient)
- Cache / rate limiting: Redis
- Frontend: Next.js 15 (App Router, TypeScript), Tailwind, shadcn/ui, TradingView `lightweight-charts`, Recharts for scorecards
- Broker SDKs: `fyers-apiv3`, `kiteconnect`
- Other data: `yfinance`, NSE public archives (bhavcopy, index constituents, ASM/GSM lists), Screener.in Excel export (manual upload)
- Packaging: Docker Compose (`api`, `worker`, `web`, `db`, `redis`)
- Tests: pytest (+ hypothesis where useful), Playwright for 3–4 critical UI flows

## Repo layout
```
backend/
  app/
    api/            # FastAPI routers only — no business logic
    core/           # settings, logging, security (token encryption), rate limiter
    db/             # SQLAlchemy models, session, alembic
    data/
      providers/    # base.py (Protocol), fyers.py, kite.py, yf.py, nse.py, screener_import.py
      router.py     # picks provider per dataset using config/providers.yaml priority + fallback
      adjust.py     # split/bonus adjustment
    fundamentals/   # metrics.py, forensic.py, banking.py
    valuation/      # dcf.py, reverse_dcf.py, bands.py, relative.py, epv.py, sector_models.py, blend.py
    technical/      # stage.py, rs.py, structure.py, zones.py, avwap.py, volume_profile.py, risk.py
    scoring/        # knockouts.py, pillars.py, grade.py, earned_premium.py, decision.py
    reports/        # assembles StockReport DTO; optional LLM thesis (numbers-in, text-out)
    jobs/           # scheduled pipelines
  tests/
    fixtures/golden/   # hand-verified numbers for 10 golden stocks
config/
  scoring.yaml  sectors.yaml  providers.yaml  valuation.yaml
frontend/
docs/
  SPEC.md  CODEX_PROMPTS.md
```

## Non-negotiable rules
1. **No silent defaults for missing financial data.** Missing → `None` + a `DataGap` record surfaced in the UI. Never substitute 0 or an average.
2. **Pure functions for all maths.** Everything in `fundamentals/`, `valuation/`, `technical/`, `scoring/` takes DataFrames/dataclasses and returns results. No DB or network calls there.
3. **Every formula has a unit test** against a hand-computed fixture. Tolerance: 0.5% for ratios, 2% for valuations.
4. **Point-in-time correctness.** Fundamentals are keyed by *announcement date* (not period end) for backtests. Shareholding by filing date.
5. **Consolidated first.** Use consolidated statements; fall back to standalone only if consolidated is absent, and flag it.
6. **Prices are split/bonus adjusted** before any technical calculation.
7. **Broker safety:** MVP is **read-only**. No order placement code. Alerts/GTT (Phase 8) require an explicit user confirmation step in the UI and are never triggered by a job.
8. **Secrets:** broker API keys and access tokens are stored encrypted (Fernet key from env). Never log tokens. Never commit `.env`.
9. **Rate limits:** all provider calls go through `core/rate_limiter.py` with per-provider limits from `providers.yaml`, plus retries with exponential backoff.
10. **Sector switching:** banks/NBFCs/insurance never use FCFF DCF. See `config/sectors.yaml`.
11. **Explainability:** every score, zone and action carries a `reasons: list[str]` so the UI can show *why*.
12. Type hints everywhere; `ruff` + `mypy --strict` on `backend/app` must pass.

## Definition of done for any task
- Tests added and passing (`make test`)
- Lint/type checks passing (`make check`)
- No new hard-coded thresholds
- If a formula changed, `docs/SPEC.md` updated in the same PR
