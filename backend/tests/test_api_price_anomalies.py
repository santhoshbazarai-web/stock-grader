"""Price anomalies API (SPEC §3.2 "price adjustment"): a raw price series whose 1:1 bonus is
missing from corporate actions is detected on re-adjustment, listed, and fixed with one click
(the bonus is added and the prices re-adjusted); dismissing keeps an anomaly closed."""

from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.core.config import get_config
from app.db.enums import CorporateActionType
from app.db.models import CorporateAction, PriceAnomaly, PriceDaily
from app.jobs.common import ensure_instruments, readjust
from tests.api_support import app_client
from tests.test_adjust_sources import BONUS, SPLIT, as_traded, true_series

ADJ = get_config().providers.adjustment


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db)


def seed(db: Session) -> int:
    """ACME: raw (as traded) bars from NSE; only the split is on record, the bonus is not."""
    iid = ensure_instruments(db, ["ACME"])["ACME"]
    raw = as_traded(true_series())
    db.execute(insert(PriceDaily), [
        {"instrument_id": iid, "date": d.date(), "open": r.open, "high": r.high, "low": r.low,
         "close": r.close, "volume": int(r.volume), "source": "nse"}
        for d, r in raw.iterrows()
    ])  # fmt: skip
    split = CorporateActionType.SPLIT
    db.add(CorporateAction(instrument_id=iid, ex_date=SPLIT.date(), source="nse",
                           action_type=split, ratio_old=1, ratio_new=2))  # fmt: skip
    db.flush()
    readjust(db, iid, ADJ)
    db.flush()
    return iid


def test_missing_bonus_is_listed_and_fixed_in_one_click(client: TestClient, db: Session) -> None:
    iid = seed(db)
    body = client.get("/api/stocks/ACME/price-anomalies").json()
    [a] = body["open"]
    assert a["day"] == str(BONUS.date()) and a["kind"] == "missing_action"
    assert (a["ratio_old"], a["ratio_new"]) == (1, 2) and a["volume_confirmed"]
    assert "no split/bonus on record: suggested 1:2" in a["text"]

    res = client.post(f"/api/stocks/ACME/price-anomalies/{a['id']}/apply")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["done"] == f"added a 1:2 split/bonus ex {BONUS.date()}"
    assert out["anomaly"]["status"] == "applied" and out["remaining"] == []
    assert out["readjusted_bars"] > 1000  # every bar before the bonus halves
    added = db.scalar(select(CorporateAction).where(CorporateAction.source == "owner"))
    assert added is not None and added.ex_date == BONUS.date()
    closes = db.scalars(select(PriceDaily.adj_close).where(PriceDaily.instrument_id == iid)
                        .order_by(PriceDaily.date)).all()  # fmt: skip
    np.testing.assert_allclose(closes, true_series()["close"].to_numpy(), rtol=1e-9)
    # applied once: a second click is refused
    again = client.post(f"/api/stocks/ACME/price-anomalies/{a['id']}/apply")
    assert again.status_code == 409


def test_dismiss_keeps_it_closed(client: TestClient, db: Session) -> None:
    iid = seed(db)
    [a] = client.get("/api/stocks/ACME/price-anomalies").json()["open"]
    res = client.post(f"/api/stocks/ACME/price-anomalies/{a['id']}/dismiss")
    assert res.json()["status"] == "dismissed"
    readjust(db, iid, ADJ)  # detected again on the next re-adjustment: stays dismissed
    db.flush()
    assert client.get("/api/stocks/ACME/price-anomalies").json()["open"] == []
    assert db.scalar(select(PriceAnomaly.status)) == "dismissed"
    assert client.get("/api/stocks/NOPE/price-anomalies").status_code == 404
    assert client.post("/api/stocks/ACME/price-anomalies/999999/apply").status_code == 404
