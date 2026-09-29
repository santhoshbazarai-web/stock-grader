"""Recording of missing data (AGENTS.md rule 1): gaps are stored, never papered over."""

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import Dataset
from app.db.models import DataGap, Instrument
from app.db.upsert import upsert


@dataclass(frozen=True)
class GapRecord:
    dataset: Dataset
    symbol: str | None
    reason: str
    providers_tried: list[str] = dc_field(default_factory=list)
    field: str | None = None
    period: date | None = None


class GapRecorder(Protocol):
    def record(self, gap: GapRecord) -> None:
        """Store (or refresh) an open gap for this dataset/symbol."""

    def resolve(self, dataset: Dataset, symbol: str | None) -> None:
        """Mark an open dataset-level gap as resolved after a successful fetch."""


class InMemoryGapRecorder:
    """For tests and dry runs."""

    def __init__(self) -> None:
        self.open: dict[tuple[Dataset, str | None], GapRecord] = {}
        self.resolved: list[tuple[Dataset, str | None]] = []

    def record(self, gap: GapRecord) -> None:
        self.open[(gap.dataset, gap.symbol)] = gap

    def resolve(self, dataset: Dataset, symbol: str | None) -> None:
        if self.open.pop((dataset, symbol), None) is not None:
            self.resolved.append((dataset, symbol))


class DbGapRecorder:
    """Writes ``data_gaps`` rows in its own committed transaction, so a gap survives even when
    the caller's work is rolled back."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _instrument_id(session: Session, symbol: str | None) -> int | None:
        if symbol is None:
            return None
        return session.scalar(select(Instrument.id).where(Instrument.symbol == symbol))

    def record(self, gap: GapRecord) -> None:
        session = self._session_factory()
        try:
            instrument_id = self._instrument_id(session, gap.symbol)
            reason = gap.reason
            if gap.symbol is not None and instrument_id is None:
                reason = f"{gap.symbol} (not in instruments): {reason}"
            upsert(
                session,
                DataGap,
                [
                    {
                        "instrument_id": instrument_id,
                        "dataset": str(gap.dataset),
                        "field": gap.field,
                        "period": gap.period,
                        "reason": reason,
                        "providers_tried": gap.providers_tried,
                        "resolved_at": None,  # re-opens a previously resolved gap
                    }
                ],
            )
            session.commit()
        finally:
            session.close()

    def resolve(self, dataset: Dataset, symbol: str | None) -> None:
        session = self._session_factory()
        try:
            instrument_id = self._instrument_id(session, symbol)
            if symbol is not None and instrument_id is None:
                return
            instrument_match = (
                DataGap.instrument_id.is_(None)
                if instrument_id is None
                else DataGap.instrument_id == instrument_id
            )
            session.execute(
                update(DataGap)
                .where(
                    instrument_match,
                    DataGap.dataset == str(dataset),
                    DataGap.field.is_(None),
                    DataGap.period.is_(None),
                    DataGap.resolved_at.is_(None),
                )
                .values(resolved_at=func.now())
            )
            session.commit()
        finally:
            session.close()


class SessionGapRecorder:
    """Writes gaps into the caller's session, so they commit (or roll back) with the caller's
    data — used where the data and its gaps belong together, e.g. a Screener upload."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, gap: GapRecord) -> None:
        instrument_id = DbGapRecorder._instrument_id(self._session, gap.symbol)
        upsert(
            self._session,
            DataGap,
            [
                {
                    "instrument_id": instrument_id,
                    "dataset": str(gap.dataset),
                    "field": gap.field,
                    "period": gap.period,
                    "reason": gap.reason,
                    "providers_tried": gap.providers_tried,
                    "resolved_at": None,
                }
            ],
        )

    def resolve(self, dataset: Dataset, symbol: str | None) -> None:
        instrument_id = DbGapRecorder._instrument_id(self._session, symbol)
        if symbol is not None and instrument_id is None:
            return
        self._session.execute(
            update(DataGap)
            .where(
                DataGap.instrument_id == instrument_id,
                DataGap.dataset == str(dataset),
                DataGap.field.is_(None),
                DataGap.period.is_(None),
                DataGap.resolved_at.is_(None),
            )
            .values(resolved_at=func.now())
        )
