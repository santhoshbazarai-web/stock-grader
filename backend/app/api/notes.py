"""Research notes (markdown, many per stock) and the glossary (``config/glossary.yaml``)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ConfigDep, SessionDep
from app.api.schemas import SymbolField
from app.core.config import GlossaryEntry
from app.db.models import Instrument, StockNote
from app.jobs.common import ensure_instruments

router = APIRouter(tags=["notes & glossary"])


class NoteIn(BaseModel):
    symbol: SymbolField
    body: str = Field(min_length=1, max_length=20_000, description="Markdown")


class NoteUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=20_000, description="Markdown")


class NoteOut(BaseModel):
    id: int
    symbol: str
    name: str | None
    body: str
    created_at: datetime
    updated_at: datetime


def _out(n: StockNote, inst: Instrument) -> NoteOut:
    return NoteOut(
        id=n.id,
        symbol=inst.symbol,
        name=inst.name,
        body=n.body,
        created_at=n.created_at,
        updated_at=n.updated_at,
    )


@router.get("/notes")
def list_notes(
    session: SessionDep,
    symbol: str | None = Query(None, description="Only this stock"),
    q: str | None = Query(None, description="Text search in the note body"),
) -> list[NoteOut]:
    stmt = (
        select(StockNote, Instrument)
        .join(Instrument, Instrument.id == StockNote.instrument_id)
        .order_by(StockNote.created_at.desc(), StockNote.id.desc())
    )
    if symbol:
        stmt = stmt.where(Instrument.symbol == symbol.upper())
    if q:
        stmt = stmt.where(StockNote.body.ilike(f"%{q}%"))
    return [_out(n, i) for n, i in session.execute(stmt).all()]


@router.post("/notes", status_code=status.HTTP_201_CREATED)
def create_note(body: NoteIn, session: SessionDep) -> NoteOut:
    symbol = body.symbol.upper()
    iid = ensure_instruments(session, [symbol])[symbol]
    note = StockNote(instrument_id=iid, body=body.body.strip())
    session.add(note)
    session.commit()
    session.refresh(note)
    inst = session.get(Instrument, iid)
    assert inst is not None
    return _out(note, inst)


@router.put("/notes/{note_id}", responses={404: {"description": "No such note"}})
def update_note(note_id: int, body: NoteUpdate, session: SessionDep) -> NoteOut:
    note = session.get(StockNote, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no note {note_id}")
    note.body = body.body.strip()
    session.commit()
    session.refresh(note)
    inst = session.get(Instrument, note.instrument_id)
    assert inst is not None
    return _out(note, inst)


@router.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "No such note"}},
)
def delete_note(note_id: int, session: SessionDep) -> None:
    note = session.get(StockNote, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no note {note_id}")
    session.delete(note)
    session.commit()


@router.get("/glossary")
def glossary(config: ConfigDep) -> list[GlossaryEntry]:
    """Every entry, A-Z by term."""
    return sorted(config.glossary.entries, key=lambda e: e.term.lower())
