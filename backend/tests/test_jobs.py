"""Job runner and pipelines against real Postgres + Redis (fake providers behind the router)."""

from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import JobName
from app.db.enums import JobStatus
from app.db.models import (
    CorporateAction,
    DataGap,
    DeliveryDaily,
    FinQuarterly,
    IndexMembership,
    Instrument,
    JobRun,
    PriceDaily,
    Shareholding,
    SurveillanceFlag,
    WatchlistItem,
)
from app.db.upsert import upsert
from app.jobs.common import universe
from app.jobs.registry import REGISTRY
from app.jobs.runner import (
    JobNotImplementedError,
    JobOptions,
    JobOutcome,
    JobSpec,
    run_job,
)
from tests.jobs_support import TODAY, Env, action, ohlcv

# Bonus 1:1 with ex-date 5 Jun: raw 1000 → 500.
BONUS_BARS = ohlcv(
    {"2024-06-03": 1000, "2024-06-04": 1000, "2024-06-05": 500, "2024-06-13": 505,
     "2024-06-14": 510}
)  # fmt: skip


def run(env: Env, name: JobName, **opts: object):  # type: ignore[no-untyped-def]
    return run_job(REGISTRY[name], env.ctx, JobOptions(**opts))  # type: ignore[arg-type]


def prices(env: Env, symbol: str) -> pd.DataFrame:
    with env.session() as s:
        rows = s.execute(
            select(
                PriceDaily.date,
                PriceDaily.close,
                PriceDaily.adj_close,
                PriceDaily.adj_volume,
                PriceDaily.source,
            )
            .join(Instrument, Instrument.id == PriceDaily.instrument_id)
            .where(Instrument.symbol == symbol)
            .order_by(PriceDaily.date)
        ).all()
    return pd.DataFrame(rows, columns=["date", "close", "adj_close", "adj_volume", "source"])


def job_runs(env: Env) -> list[JobRun]:
    with env.session() as s:
        return list(s.scalars(select(JobRun).order_by(JobRun.id)))


# ───────────────────────── runner ─────────────────────────


def test_success_is_logged(env: Env) -> None:
    spec = JobSpec(JobName.EOD_PRICES, "t", lambda c, o: JobOutcome(7, {"k": "v"}))
    record = run_job(spec, env.ctx, JobOptions(symbols=("TCS",)))
    [row] = job_runs(env)
    assert record.status is JobStatus.SUCCESS and row.id == record.run_id
    assert (row.job_name, row.status, row.rows_written) == ("eod_prices", "success", 7)
    assert row.details == {"k": "v"} and row.params["symbols"] == ["TCS"]
    assert row.finished_at is not None


def test_failure_is_logged_not_raised(env: Env) -> None:
    def boom(ctx: object, opts: object) -> JobOutcome:
        raise ValueError("provider exploded")

    record = run_job(JobSpec(JobName.EOD_PRICES, "t", boom), env.ctx)
    [row] = job_runs(env)
    assert record.status is JobStatus.FAILED and row.status == "failed"
    assert row.error == "ValueError: provider exploded"
    assert not env.ctx.redis.exists("job-lock:eod_prices")  # lock released


def test_concurrent_run_is_skipped(env: Env) -> None:
    lock = env.ctx.redis.lock("job-lock:eod_prices", timeout=60)
    assert lock.acquire(blocking=False)
    try:
        ran: list[int] = []
        record = run_job(
            JobSpec(JobName.EOD_PRICES, "t", lambda c, o: ran.append(1) or JobOutcome()), env.ctx
        )
    finally:
        lock.release()
    assert record.status is JobStatus.SKIPPED and ran == []
    [row] = job_runs(env)
    assert row.status == "skipped" and "lock" in row.details["reason"]


def test_lock_has_ttl_while_running(env: Env) -> None:
    seen: list[int] = []

    def peek(ctx, opts):  # type: ignore[no-untyped-def]
        seen.append(ctx.redis.ttl("job-lock:eod_prices"))
        return JobOutcome()

    run_job(JobSpec(JobName.EOD_PRICES, "t", peek), env.ctx)
    assert 0 < seen[0] <= env.ctx.config.jobs.lock_ttl_s


def test_pending_jobs_refuse_to_run(env: Env) -> None:
    # Every registered job is implemented now; the guard still protects future ones.
    pending = JobSpec(JobName.ALERTS_INTRADAY, "later", pending_phase="P99")
    with pytest.raises(JobNotImplementedError, match="P99"):
        run_job(pending, env.ctx)
    assert job_runs(env) == []
    assert all(spec.fn is not None for spec in REGISTRY.values())


def test_registry_covers_every_scheduled_job() -> None:
    assert set(REGISTRY) == set(JobName)


# ───────────────────────── universe ─────────────────────────


def test_universe_is_open_members_plus_watchlist(env: Env) -> None:
    with env.session() as s:
        upsert(s, Instrument, [{"symbol": x, "source": "nse"} for x in ("A", "B", "C", "D")])
        ids = dict(s.execute(select(Instrument.symbol, Instrument.id)).all())
        start = date(2020, 1, 1)
        upsert(
            s,
            IndexMembership,
            [
                {
                    "index_name": "NIFTY500",
                    "instrument_id": ids["A"],
                    "effective_from": start,
                    "effective_to": None,
                    "source": "nse",
                },
                {
                    "index_name": "NIFTY500",
                    "instrument_id": ids["B"],
                    "effective_from": start,
                    "effective_to": date(2023, 1, 1),
                    "source": "nse",
                },  # left the index
            ],
        )
        s.add(WatchlistItem(instrument_id=ids["D"]))
        s.commit()
    assert universe(env.ctx, JobOptions()) == ["A", "D"]
    assert universe(env.ctx, JobOptions(symbols=("x, y", "Y"))) == ["X", "Y"]  # created


# ───────────────────────── eod_prices + adjustment ─────────────────────────


def test_eod_prices_backfills_new_symbol_and_adjusts_for_bonus(env: Env) -> None:
    env.prices.bars["SAMPLE"] = BONUS_BARS
    env.nse.actions["SAMPLE"] = pd.DataFrame([action("2024-06-05", "bonus", 1, 2)])

    record = run(env, JobName.EOD_PRICES, symbols=("SAMPLE",))

    assert record.status is JobStatus.SUCCESS and record.outcome.rows_written == 5
    [(_, start, end)] = env.prices.requests
    assert end == TODAY and start == TODAY.replace(year=TODAY.year - 10)  # history_years
    assert env.nse.action_requests[0][1] == TODAY.replace(year=TODAY.year - 12)  # full CA history
    df = prices(env, "SAMPLE")
    assert list(df["close"]) == [1000, 1000, 500, 505, 510]  # raw preserved
    assert list(df["adj_close"]) == [500, 500, 500, 505, 510]
    assert list(df["adj_volume"]) == [2000, 2000, 1000, 1000, 1000]
    assert set(df["source"]) == {"fyers"}
    assert record.outcome.details["sources"] == {"fyers": 1}


def test_eod_prices_incremental_and_idempotent(env: Env) -> None:
    env.prices.bars["SAMPLE"] = BONUS_BARS
    run(env, JobName.EOD_PRICES, symbols=("SAMPLE",))
    run(env, JobName.EOD_PRICES, symbols=("SAMPLE",))
    _, start, _ = env.prices.requests[1]
    overlap = env.ctx.config.jobs.eod_prices.overlap_days
    assert start == date(2024, 6, 14) - timedelta(days=overlap)
    assert len(prices(env, "SAMPLE")) == 5


def test_eod_prices_includes_benchmark_indices_for_full_universe(env: Env) -> None:
    env.prices.bars["NIFTY500"] = ohlcv({"2024-06-13": 22000, "2024-06-14": 22100})
    run(env, JobName.EOD_PRICES)
    df = prices(env, "NIFTY500")
    assert list(df["adj_close"]) == [22000, 22100]
    with env.session() as s:
        assert s.scalar(select(Instrument.is_index).where(Instrument.symbol == "NIFTY500"))


def test_eod_prices_records_failures_and_gaps(env: Env) -> None:
    record = run(env, JobName.EOD_PRICES, symbols=("NODATA",))
    assert record.status is JobStatus.SUCCESS
    assert record.outcome.details["failed"] == ["NODATA"]
    with env.session() as s:
        gap = s.scalars(select(DataGap).where(DataGap.dataset == "daily_ohlcv")).one()
    assert "empty" in gap.reason


def test_new_bonus_readjusts_stored_history(env: Env) -> None:
    env.prices.bars["SAMPLE"] = BONUS_BARS
    run(env, JobName.EOD_PRICES, symbols=("SAMPLE",))  # no actions known yet
    assert list(prices(env, "SAMPLE")["adj_close"])[:2] == [1000, 1000]

    env.nse.actions["SAMPLE"] = pd.DataFrame([action("2024-06-05", "bonus", 1, 2)])
    record = run(env, JobName.CORPORATE_ACTIONS, symbols=("SAMPLE",))

    assert record.outcome.details["readjusted"] == ["SAMPLE"]
    assert list(prices(env, "SAMPLE")["adj_close"]) == [500, 500, 500, 505, 510]
    lookback = env.ctx.config.jobs.corporate_actions.lookback_days
    assert env.nse.action_requests[-1][1] == TODAY - timedelta(days=lookback)


def test_unparseable_bonus_leaves_history_unadjusted_and_flags_gap(env: Env) -> None:
    env.prices.bars["SAMPLE"] = BONUS_BARS
    env.nse.actions["SAMPLE"] = pd.DataFrame([action("2024-06-05", "bonus")])  # no ratio
    run(env, JobName.EOD_PRICES, symbols=("SAMPLE",))
    df = prices(env, "SAMPLE")
    assert df["adj_close"].isna().tolist() == [True, True, False, False, False]
    with env.session() as s:
        gap = s.scalars(select(DataGap).where(DataGap.field == "adj_close")).one()
    assert "no usable ratio" in gap.reason


def test_corporate_actions_stored_with_source(env: Env) -> None:
    env.nse.actions["SAMPLE"] = pd.DataFrame(
        [action("2024-06-05", "bonus", 1, 2), action("2024-06-10", "dividend", dps=12.0)]
    )
    run(env, JobName.CORPORATE_ACTIONS, symbols=("SAMPLE",), full=True)
    with env.session() as s:
        rows = s.scalars(select(CorporateAction).order_by(CorporateAction.ex_date)).all()
    assert [(r.action_type, r.source) for r in rows] == [("bonus", "nse"), ("dividend", "nse")]
    assert rows[1].dividend_per_share == 12.0


# ───────────────────────── nse_bhavcopy ─────────────────────────


def _surveillance(*rows: tuple[str, str, str | None]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "list_name", "stage"])


def test_bhavcopy_delivery_and_surveillance_point_in_time(env: Env) -> None:
    with env.session() as s:
        upsert(s, Instrument, [{"symbol": x, "source": "nse"} for x in ("AAA", "BBB", "CCC")])
        s.commit()
    env.nse.delivery_frame = pd.DataFrame(
        [
            {"symbol": "AAA", "series": "EQ", "date": TODAY, "traded_qty": 100,
             "deliverable_qty": 55, "delivery_pct": 55.0, "traded_value_cr": 1.2},
            {"symbol": "UNLISTED", "series": "EQ", "date": TODAY, "traded_qty": 1,
             "deliverable_qty": 1, "delivery_pct": 100.0, "traded_value_cr": 0.1},
        ]
    )  # fmt: skip
    env.nse.surveillance_frame = _surveillance(("AAA", "asm_lt", "Stage I"), ("BBB", "gsm", None))

    first = run(env, JobName.NSE_BHAVCOPY)

    assert first.status is JobStatus.SUCCESS
    assert first.outcome.details["delivery_rows"] == 1
    assert first.outcome.details["unknown_symbols"] == 1
    with env.session() as s:
        d = s.scalars(select(DeliveryDaily)).one()
        assert (d.delivery_pct, d.deliverable_qty, d.source) == (55.0, 55, "nse")

    # Next day: AAA moves to Stage II, BBB leaves GSM, CCC enters the F&O ban list.
    env.ctx.clock = lambda: pd.Timestamp("2024-06-17 13:00", tz="UTC").to_pydatetime()
    env.nse.surveillance_frame = _surveillance(
        ("AAA", "asm_lt", "Stage II"), ("CCC", "fno_ban", None)
    )
    run(env, JobName.NSE_BHAVCOPY)

    with env.session() as s:
        flags = {(f.instrument_id, f.list_name): f for f in s.scalars(select(SurveillanceFlag))}
        ids = dict(s.execute(select(Instrument.symbol, Instrument.id)).all())
    aaa = flags[(ids["AAA"], "asm_lt")]
    bbb = flags[(ids["BBB"], "gsm")]
    ccc = flags[(ids["CCC"], "fno_ban")]
    assert (aaa.stage, aaa.effective_from, aaa.effective_to) == ("Stage II", TODAY, None)
    assert bbb.effective_to == date(2024, 6, 17)
    assert (ccc.effective_from, ccc.effective_to) == (date(2024, 6, 17), None)


def test_bhavcopy_fails_when_no_delivery_data(env: Env) -> None:
    record = run(env, JobName.NSE_BHAVCOPY)
    assert record.status is JobStatus.FAILED and "no delivery data" in (record.error or "")


# ───────────────────────── index_constituents ─────────────────────────


def _members(*symbols: str) -> pd.DataFrame:
    return pd.DataFrame(
        [{"symbol": s, "name": f"{s} Ltd", "industry": "IT", "series": "EQ", "isin": f"INE{s}01"}
         for s in symbols]
    )  # fmt: skip


def test_index_membership_diff(env: Env) -> None:
    env.nse.constituents["NIFTY500"] = _members("XXX", "YYY")
    run(env, JobName.INDEX_CONSTITUENTS)

    env.ctx.clock = lambda: pd.Timestamp("2024-07-01 02:00", tz="UTC").to_pydatetime()
    env.nse.constituents["NIFTY500"] = _members("XXX", "ZZZ")
    record = run(env, JobName.INDEX_CONSTITUENTS)

    assert record.outcome.details["NIFTY500"]["joined"] == 1
    assert record.outcome.details["NIFTY500"]["left"] == 1
    with env.session() as s:
        rows = s.execute(
            select(Instrument.symbol, IndexMembership.effective_from, IndexMembership.effective_to)
            .join(Instrument, Instrument.id == IndexMembership.instrument_id)
            .order_by(Instrument.symbol)
        ).all()
        name = s.scalar(select(Instrument.name).where(Instrument.symbol == "ZZZ"))
    assert rows == [
        ("XXX", TODAY, None),
        ("YYY", TODAY, date(2024, 7, 1)),
        ("ZZZ", date(2024, 7, 1), None),
    ]
    assert name == "ZZZ Ltd"
    assert "NIFTY50" in record.outcome.details["failed"]  # not scripted → left untouched


def test_failed_index_download_does_not_close_memberships(env: Env) -> None:
    env.nse.constituents["NIFTY500"] = _members("XXX")
    run(env, JobName.INDEX_CONSTITUENTS)
    env.nse.constituents["NIFTY500"] = pd.DataFrame()  # empty download
    env.nse.constituents["NIFTY50"] = _members("XXX")  # keep the job from failing outright
    run(env, JobName.INDEX_CONSTITUENTS)
    with env.session() as s:
        m = s.scalars(select(IndexMembership).where(IndexMembership.index_name == "NIFTY500")).one()
    assert m.effective_to is None


def test_all_index_downloads_failing_fails_the_job(env: Env) -> None:
    assert run(env, JobName.INDEX_CONSTITUENTS).status is JobStatus.FAILED


# ───────────────────────── seasonal jobs ─────────────────────────


def test_shareholding_skips_outside_season(env: Env) -> None:
    record = run(env, JobName.SHAREHOLDING)  # 14 June: not a filing month
    assert record.status is JobStatus.SKIPPED
    assert job_runs(env)[0].details["reason"].startswith("outside season")


def test_shareholding_does_not_blank_other_sources(env: Env) -> None:
    with env.session() as s:
        upsert(s, Instrument, [{"symbol": "AAA", "source": "nse"}])
        iid = s.scalar(select(Instrument.id))
        existing = {
            "instrument_id": iid,
            "period_end": date(2024, 3, 31),
            "promoter_pct": 50.0,
            "fii_pct": 20.0,
            "source": "screener",
        }
        upsert(s, Shareholding, [existing])
        s.commit()
    env.nse.holdings["AAA"] = pd.DataFrame(
        {"promoter_pct": [54.9], "public_pct": [45.1], "fii_pct": [None],
         "filing_date": [date(2024, 4, 19)]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-03-31")]),
    )  # fmt: skip
    record = run(env, JobName.SHAREHOLDING, symbols=("AAA",))
    assert record.status is JobStatus.SUCCESS
    with env.session() as s:
        row = s.scalars(select(Shareholding)).one()
    assert (row.promoter_pct, row.public_pct, row.fii_pct) == (54.9, 45.1, 20.0)
    assert row.filing_date == date(2024, 4, 19)


def test_results_backfill_falls_back_to_yfinance_with_first_seen_date(env: Env) -> None:
    # No NSE results list for AAA (FakeNse raises) → the yfinance fallback (see
    # test_results_backfill.py for the XBRL path)
    env.quarterly.frames["AAA"] = pd.DataFrame(
        {"revenue": [260.0, 270.0], "pat": [60.0, 64.0], "ebitda": [None, None]},
        index=pd.DatetimeIndex([pd.Timestamp("2023-12-31"), pd.Timestamp("2024-03-31")]),
    )
    with env.session() as s:
        upsert(s, Instrument, [{"symbol": "AAA", "source": "nse"}])
        iid = s.scalar(select(Instrument.id))
        upsert(s, FinQuarterly, [{"instrument_id": iid, "statement_type": "consolidated",
                                  "period_end": date(2023, 12, 31), "revenue": 259.0,
                                  "source": "screener"}])  # fmt: skip
        s.commit()

    record = run(env, JobName.RESULTS_BACKFILL, force=True, symbols=("AAA",))

    assert record.outcome.details["fallback_flagged"] == ["AAA"]
    with env.session() as s:
        rows = {r.period_end: r for r in s.scalars(select(FinQuarterly))}
        gap = s.scalars(select(DataGap).where(DataGap.field == "results_filing")).one()
    assert rows[date(2023, 12, 31)].revenue == 259.0  # older quarter untouched
    new = rows[date(2024, 3, 31)]
    assert (new.revenue, new.source, new.announcement_date) == (270.0, "yfinance", TODAY)
    assert "2024-03-31" in gap.reason

    again = run(env, JobName.RESULTS_BACKFILL, force=True, symbols=("AAA",))
    assert again.outcome.details["fallback_flagged"] == []
