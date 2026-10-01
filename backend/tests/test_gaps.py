from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Dataset
from app.data.gaps import DbGapRecorder, GapRecord
from app.db.models import DataGap, Instrument
from app.db.upsert import upsert


def _recorder(db: Session) -> DbGapRecorder:
    return DbGapRecorder(lambda: db)


def _gaps(db: Session) -> list[DataGap]:
    db.expire_all()
    return list(db.scalars(select(DataGap)))


def test_record_links_instrument_and_is_idempotent(db: Session) -> None:
    upsert(db, Instrument, [{"symbol": "TCS", "source": "nse"}])
    iid = db.scalars(select(Instrument.id)).one()
    rec = _recorder(db)
    gap = GapRecord(Dataset.DAILY_OHLCV, "TCS", "fyers: error", ["fyers", "kite"])

    rec.record(gap)
    rec.record(gap)

    [row] = _gaps(db)
    assert row.instrument_id == iid
    assert row.dataset == "daily_ohlcv"
    assert row.providers_tried == ["fyers", "kite"]
    assert row.resolved_at is None


def test_resolve_then_reopen(db: Session) -> None:
    upsert(db, Instrument, [{"symbol": "TCS", "source": "nse"}])
    rec = _recorder(db)
    rec.record(GapRecord(Dataset.FIN_ANNUAL, "TCS", "empty"))

    rec.resolve(Dataset.FIN_ANNUAL, "TCS")
    [row] = _gaps(db)
    assert row.resolved_at is not None

    rec.record(GapRecord(Dataset.FIN_ANNUAL, "TCS", "empty again"))
    [row] = _gaps(db)
    assert row.resolved_at is None and row.reason == "empty again"


def test_unknown_symbol_recorded_without_instrument(db: Session) -> None:
    _recorder(db).record(GapRecord(Dataset.DAILY_OHLCV, "NOPE", "all failed"))
    [row] = _gaps(db)
    assert row.instrument_id is None
    assert row.reason.startswith("NOPE (not in instruments)")


def test_resolve_other_dataset_untouched(db: Session) -> None:
    upsert(db, Instrument, [{"symbol": "TCS", "source": "nse"}])
    rec = _recorder(db)
    rec.record(GapRecord(Dataset.FIN_ANNUAL, "TCS", "empty"))
    rec.resolve(Dataset.DAILY_OHLCV, "TCS")
    [row] = _gaps(db)
    assert row.resolved_at is None
