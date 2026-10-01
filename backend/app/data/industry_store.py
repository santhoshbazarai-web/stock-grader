"""Read a stock's industry classification and set its sector model (``industries.yaml``)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import IndustriesConfig
from app.data.industry import IndustryInfo, classify
from app.data.router import DataRouter
from app.db.models import Instrument


@dataclass(frozen=True)
class ClassifyOutcome:
    symbol: str
    sector: str | None  # sectors.yaml key now set, None = unmapped / not fetched
    label: str | None
    source: str | None
    message: str
    fetched: bool  # a source answered


def classify_symbol(
    session: Session, router: DataRouter, symbol: str, cfg: IndustriesConfig, *, now: datetime
) -> ClassifyOutcome:
    """Fetch the industry (``priority.industry``), map it, and store label, source and sector
    on the instrument. An unmapped label clears a sector this classification set earlier but
    never one set by hand (user overrides live in user_overrides and always win)."""
    sym = symbol.strip().upper()
    inst = session.scalar(select(Instrument).where(Instrument.symbol == sym))
    if inst is None:
        return ClassifyOutcome(sym, None, None, None, f"{sym} is not an instrument", False)
    res = router.industry(sym)
    info = res.data
    if not isinstance(info, IndustryInfo):
        why = "; ".join(res.reasons)[:300] or "no source answered"
        return ClassifyOutcome(sym, inst.sector, None, None,
                               f"industry unavailable: {why}", False)  # fmt: skip
    result = classify(info, cfg)
    if result.sector is not None:
        inst.sector = result.sector
    elif inst.industry_source is not None:  # our own earlier mapping no longer applies
        inst.sector = None
    inst.basic_industry = info.label
    inst.industry_source = info.source
    inst.classified_at = now
    session.flush()
    return ClassifyOutcome(sym, inst.sector, info.label, info.source, result.reason, True)
