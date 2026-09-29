from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.enums import StatementType
from app.db.models import (
    DataGap,
    FinAnnual,
    Instrument,
    PriceDaily,
    UserOverride,
)
from app.db.upsert import upsert

LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


def _instrument(db: Session, symbol: str = "TCS") -> int:
    upsert(db, Instrument, [{"symbol": symbol, "name": f"{symbol} Ltd", "source": "nse"}])
    return db.scalars(select(Instrument.id).where(Instrument.symbol == symbol)).one()


def _prices(instrument_id: int, n: int, close: float = 100.0) -> list[dict[str, Any]]:
    start = date(2024, 1, 1)
    return [
        {
            "instrument_id": instrument_id,
            "date": start + timedelta(days=i),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000 + i,
            "adj_close": None,
            "source": "fyers",
        }
        for i in range(n)
    ]


def _count(db: Session, model: type[Base]) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _snapshot(db: Session, model: type[Base], exclude: set[str]) -> list[dict[str, Any]]:
    cols = [c for c in model.__table__.c if c.name not in exclude]
    rows = db.execute(select(*cols).order_by(*cols[:2])).mappings().all()
    return [dict(r) for r in rows]


# ───────────── idempotency ─────────────


def test_upsert_twice_is_idempotent(db: Session) -> None:
    iid = _instrument(db)
    rows = _prices(iid, 30)

    assert upsert(db, PriceDaily, rows) == 30
    first = _snapshot(db, PriceDaily, exclude={"fetched_at"})
    assert upsert(db, PriceDaily, rows) == 30
    second = _snapshot(db, PriceDaily, exclude={"fetched_at"})

    assert _count(db, PriceDaily) == 30
    assert first == second


def test_upsert_updates_changed_values(db: Session) -> None:
    iid = _instrument(db)
    upsert(db, PriceDaily, _prices(iid, 5, close=100.0))
    upsert(db, PriceDaily, _prices(iid, 5, close=105.0) + _prices(iid, 7, close=105.0)[5:])

    assert _count(db, PriceDaily) == 7
    closes = db.scalars(select(PriceDaily.close)).all()
    assert set(closes) == {105.0}


def test_upsert_refreshes_fetched_at(db: Session) -> None:
    iid = _instrument(db)
    rows = _prices(iid, 1)
    upsert(db, PriceDaily, [{**rows[0], "fetched_at": LONG_AGO}])
    assert db.scalar(select(PriceDaily.fetched_at)) == LONG_AGO

    upsert(db, PriceDaily, rows)  # no fetched_at supplied → now()
    db.expire_all()
    fetched = db.scalar(select(PriceDaily.fetched_at))
    assert fetched is not None and fetched > LONG_AGO


def test_upsert_preserves_created_at(db: Session) -> None:
    iid = _instrument(db)
    upsert(
        db,
        UserOverride,
        [{"instrument_id": iid, "key": "g1", "value": {"value": 0.12}, "created_at": LONG_AGO}],
    )
    upsert(
        db,
        UserOverride,
        [{"instrument_id": iid, "key": "g1", "value": {"value": 0.15}}],
    )
    row = db.execute(select(UserOverride.created_at, UserOverride.value)).one()
    assert row.created_at == LONG_AGO
    assert row.value == {"value": 0.15}
    assert _count(db, UserOverride) == 1


def test_upsert_null_key_columns_are_idempotent(db: Session) -> None:
    # data_gaps key has nullable columns; NULLS NOT DISTINCT keeps re-runs from duplicating.
    iid = _instrument(db)
    gap = {
        "instrument_id": iid,
        "dataset": "shareholding",
        "field": None,
        "period": None,
        "reason": "all providers failed",
        "providers_tried": ["nse", "screener"],
    }
    upsert(db, DataGap, [gap])
    upsert(db, DataGap, [{**gap, "reason": "still failing"}])
    assert _count(db, DataGap) == 1
    assert db.scalar(select(DataGap.reason)) == "still failing"


def test_upsert_composite_key_with_enum(db: Session) -> None:
    iid = _instrument(db)
    base = {
        "instrument_id": iid,
        "period_end": date(2025, 3, 31),
        "fiscal_year": 2025,
        "announcement_date": date(2025, 4, 20),
        "pat": 100.0,
        "source": "screener",
    }
    rows = [
        {**base, "statement_type": StatementType.CONSOLIDATED},
        {**base, "statement_type": StatementType.STANDALONE, "pat": 80.0},
    ]
    upsert(db, FinAnnual, rows)
    upsert(db, FinAnnual, rows)
    assert _count(db, FinAnnual) == 2
    consolidated = db.scalars(
        select(FinAnnual).where(FinAnnual.statement_type == StatementType.CONSOLIDATED)
    ).one()
    assert consolidated.pat == 100.0
    assert consolidated.revenue is None  # missing stays NULL, never 0


def test_upsert_batches_beyond_bind_param_limit(db: Session) -> None:
    iid = _instrument(db)
    rows = _prices(iid, 9_000)  # 9 columns → > 65,535 bind params → multiple statements
    assert upsert(db, PriceDaily, rows) == 9_000
    assert upsert(db, PriceDaily, rows) == 9_000
    assert _count(db, PriceDaily) == 9_000


def test_upsert_empty_is_noop(db: Session) -> None:
    assert upsert(db, PriceDaily, []) == 0


# ───────────── input validation (no silent defaults) ─────────────


def test_rows_must_share_columns(db: Session) -> None:
    rows = _prices(1, 2)
    del rows[1]["adj_close"]
    with pytest.raises(ValueError, match="row 1 has columns"):
        upsert(db, PriceDaily, rows)


def test_unknown_column_rejected(db: Session) -> None:
    rows = [{**_prices(1, 1)[0], "vwap": 1.0}]
    with pytest.raises(ValueError, match="unknown columns"):
        upsert(db, PriceDaily, rows)


def test_key_columns_required(db: Session) -> None:
    row = _prices(1, 1)[0]
    del row["date"]
    with pytest.raises(ValueError, match="key columns"):
        upsert(db, PriceDaily, [row])
