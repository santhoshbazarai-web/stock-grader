"""Technical debug endpoint and the ``technicals`` job (Postgres)."""

import json
from collections.abc import Iterator

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import JobName
from app.db.models import Instrument, PriceDaily, TechnicalSnapshot
from app.db.session import get_session
from app.db.upsert import upsert
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from app.main import create_app
from tests.jobs_support import Env
from tests.test_technical import synthetic_daily


def store_prices(
    db: Session, symbol: str, daily: pd.DataFrame, *, adjusted: bool = True, is_index: bool = False
) -> None:
    upsert(db, Instrument, [{"symbol": symbol, "source": "nse", "is_index": is_index}])
    iid = db.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
    rows = [
        {
            "instrument_id": iid,
            "date": d.date(),
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": int(r.volume),
            "adj_factor": 1.0 if adjusted else None,
            "adj_open": r.open if adjusted else None,
            "adj_high": r.high if adjusted else None,
            "adj_low": r.low if adjusted else None,
            "adj_close": r.close if adjusted else None,
            "adj_volume": int(r.volume) if adjusted else None,
            "source": "fyers",
        }
        for d, r in daily.iterrows()
    ]
    upsert(db, PriceDaily, rows)


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()

    def session_override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as c:
        yield c


def test_debug_endpoint_returns_overlays(db: Session, client: TestClient) -> None:
    store_prices(db, "SYNTH", synthetic_daily())
    store_prices(db, "NIFTY500", synthetic_daily(seed=5), is_index=True)
    res = client.get("/api/stocks/synth/technical/debug")
    assert res.status_code == 200
    body = res.json()
    assert body["symbol"] == "SYNTH" and body["timeframe"] == "weekly"
    for key in (
        "bars",
        "sma_30w",
        "swings",
        "structure_events",
        "zones",
        "fvgs",
        "avwaps",
        "volume_profile",
        "dealing_range",
        "stage",
        "rs",
        "supports",
    ):
        assert key in body, key
    assert body["rs"]["mansfield_benchmark"] is not None  # NIFTY500 stored → RS computed
    assert body["buy_zone"] is None and body["valuation_levels"] is None


def test_debug_endpoint_with_valuation_levels(db: Session, client: TestClient) -> None:
    store_prices(db, "SYNTH", synthetic_daily())
    cmp = float(synthetic_daily()["close"].iloc[-1])
    res = client.get(
        "/api/stocks/SYNTH/technical/debug",
        params={"fair_value": cmp * 1.5, "baseline": cmp * 0.7, "grade": "B"},
    )
    body = res.json()
    assert body["valuation_levels"]["mos"] == 0.275  # grade B default from config
    assert body["buy_zone"]["status"] in ("zone", "none", "suppressed")
    assert body["buy_zone"]["valuation_range"] == pytest.approx(
        [cmp * 0.7, cmp * 1.5 * 0.725], rel=1e-3
    )


def test_debug_endpoint_errors(db: Session, client: TestClient) -> None:
    assert client.get("/api/stocks/NOPE/technical/debug").status_code == 404
    store_prices(db, "RAW", synthetic_daily(n_days=300), adjusted=False)
    res = client.get("/api/stocks/RAW/technical/debug")
    assert res.status_code == 409 and "adjusted" in res.json()["detail"]
    assert (
        client.get(
            "/api/stocks/RAW/technical/debug", params={"fair_value": 100, "grade": "Z"}
        ).status_code
        == 422
    )


def test_technicals_job_writes_snapshots_with_rs_percentiles(env: Env) -> None:
    env.prices.bars["AAA"] = synthetic_daily(seed=1)
    env.prices.bars["BBB"] = synthetic_daily(seed=2)
    env.prices.bars["NIFTY500"] = synthetic_daily(seed=5)
    run_job(REGISTRY[JobName.EOD_PRICES], env.ctx, JobOptions(symbols=("AAA", "BBB")))
    run_job(REGISTRY[JobName.EOD_PRICES], env.ctx)  # benchmark indices

    record = run_job(REGISTRY[JobName.TECHNICALS], env.ctx, JobOptions(symbols=("AAA", "BBB")))

    assert record.outcome is not None and record.outcome.details["analysed"] == 2
    with env.session() as s:
        snaps = {
            sym: snap
            for sym, snap in s.execute(
                select(Instrument.symbol, TechnicalSnapshot).join(
                    Instrument, Instrument.id == TechnicalSnapshot.instrument_id
                )
            ).all()
        }
    assert set(snaps) == {"AAA", "BBB"}
    assert sorted(x.rs_percentile for x in snaps.values()) == [25.0, 75.0]  # two-stock universe
    a = snaps["AAA"]
    assert a.timeframe == "weekly" and a.stage in (1, 2, 3, 4) and a.atr and a.atr > 0
    assert a.buy_zone_low is None  # written with valuation levels by valuation_scores
    assert "zones" in a.detail and "bars" not in a.detail
    json.dumps(a.detail)


def test_technicals_job_reports_missing_prices(env: Env) -> None:
    record = run_job(REGISTRY[JobName.TECHNICALS], env.ctx, JobOptions(symbols=("NOBARS",)))
    assert record.outcome is not None
    assert "NOBARS" in record.outcome.details["failed"]
