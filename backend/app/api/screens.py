"""Screener v2 (SPEC §9): field catalogue, filtered / sorted / paged scans over the stored
latest reports, the Screening Ideas presets, and named saved screens."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ConfigDep, SessionDep
from app.core.config import ScreenerField, ScreenFilter
from app.db.models import SavedScreen
from app.db.upsert import upsert
from app.reports.service import latest_payloads
from app.screener.engine import apply_filters, extract, paginate, sort_rows

router = APIRouter(tags=["screener"])

PAGE_SIZES = (10, 20, 40, 100)
ScreenName = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[\w .()\-+&%]+$")]


class FieldOut(BaseModel):
    key: str
    label: str
    group: str
    type: str
    unit: str | None
    options: list[str] | None = Field(None, description="Values seen in the stored reports (enums)")


class FieldsOut(BaseModel):
    groups: list[str]
    fields: list[FieldOut]


class ScanOut(BaseModel):
    rows: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    universe: int = Field(description="Stored reports scanned")
    scanned_at: datetime


class ScreenFilterIn(BaseModel):
    key: str
    min: float | None = None
    max: float | None = None
    in_: list[str] | None = Field(None, alias="in")


class ScreenDefinition(BaseModel):
    filters: list[ScreenFilterIn] = Field(default_factory=list)
    sort: str = "total_score"
    order: Literal["asc", "desc"] = "desc"


class ScreenIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[\w .()\-+&%]+$")
    definition: ScreenDefinition


class ScreenOut(BaseModel):
    name: str
    definition: ScreenDefinition
    updated_at: datetime


class IdeaOut(BaseModel):
    id: str
    name: str
    icon: str
    description: str
    sort: str
    order: str
    filters: list[ScreenFilterIn]


def _fields(config: ConfigDep) -> dict[str, ScreenerField]:
    return config.screener_fields.by_key()


def parse_filters(params: Any, fields: dict[str, ScreenerField]) -> list[ScreenFilter]:
    """``min.<key>=5``, ``max.<key>=20``, ``in.<key>=a,b`` query parameters → filters."""
    merged: dict[str, dict[str, Any]] = {}
    for name, raw in params.multi_items():
        op, _, key = name.partition(".")
        if op not in ("min", "max", "in") or not key:
            continue
        spec = fields.get(key)
        if spec is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown field {key!r}")
        numeric = spec.type in ("number", "percent")
        if (op == "in") == numeric:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{key!r} is {spec.type}: use {'min / max' if numeric else 'in'}",
            )
        try:
            merged.setdefault(key, {})["in" if op == "in" else op] = (
                [v for v in raw.split(",") if v] if op == "in" else float(raw)
            )
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: not a number"
            ) from exc
    return [ScreenFilter.model_validate({"key": k, **v}) for k, v in merged.items()]


@router.get("/screener/fields")
def screener_fields(config: ConfigDep, session: SessionDep) -> FieldsOut:
    """The filterable fields by group; enum fields list the values present in stored reports."""
    cfg = config.screener_fields
    rows = [extract(p, cfg.fields) for p in latest_payloads(session).values()]
    out = []
    for f in cfg.fields:
        options = (
            sorted({str(r[f.key]) for r in rows if r[f.key] is not None})
            if f.type == "enum"
            else None
        )
        out.append(FieldOut(key=f.key, label=f.label, group=f.group, type=f.type, unit=f.unit,
                            options=options))  # fmt: skip
    return FieldsOut(groups=cfg.groups, fields=out)


@router.get("/screener/scan")
def scan(
    request: Request,
    config: ConfigDep,
    session: SessionDep,
    sort: str = "total_score",
    order: Literal["asc", "desc"] = "desc",
    page: int = 1,
    page_size: int = 20,
) -> ScanOut:
    """Scan the stored latest reports. Filters are query parameters ``min.<field>``,
    ``max.<field>`` (numbers, inclusive) and ``in.<field>=a,b`` (enums). A stock with no value
    for a filtered field is left out; one with none for the sort field sorts last."""
    fields = _fields(config)
    if sort not in fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown sort field {sort!r}")
    if page_size not in PAGE_SIZES or page < 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"page_size is one of {PAGE_SIZES}"
        )
    filters = parse_filters(request.query_params, fields)
    rows = [extract(p, config.screener_fields.fields) for p in latest_payloads(session).values()]
    kept = sort_rows(apply_filters(rows, filters), sort, order)
    return ScanOut(
        rows=paginate(kept, page, page_size),
        total=len(kept),
        page=page,
        page_size=page_size,
        universe=len(rows),
        scanned_at=datetime.now(UTC),
    )


@router.get("/screener/ideas")
def ideas(config: ConfigDep) -> list[IdeaOut]:
    """The Screening Ideas presets (``config/screen_presets.yaml``)."""

    def flt(f: ScreenFilter) -> ScreenFilterIn:
        return ScreenFilterIn.model_validate(f.model_dump(by_alias=True))

    return [
        IdeaOut(id=p.id, name=p.name, icon=p.icon, description=p.description, sort=p.sort,
                order=p.order, filters=[flt(f) for f in p.filters])
        for p in config.screen_presets.presets
    ]  # fmt: skip


@router.get("/screens")
def list_screens(session: SessionDep) -> list[ScreenOut]:
    return [
        ScreenOut(name=s.name, definition=ScreenDefinition.model_validate(s.definition),
                  updated_at=s.updated_at)
        for s in session.scalars(select(SavedScreen).order_by(SavedScreen.name))
    ]  # fmt: skip


@router.post("/screens", status_code=status.HTTP_201_CREATED)
def save_screen(body: ScreenIn, config: ConfigDep, session: SessionDep) -> ScreenOut:
    """Create or replace a named screen. Fields and sort must exist in the catalogue."""
    fields = _fields(config)
    if body.definition.sort not in fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown sort field")
    for f in body.definition.filters:
        spec = fields.get(f.key)
        if spec is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown field {f.key!r}")
        ScreenFilter.model_validate(f.model_dump(by_alias=True, exclude_none=True))
    upsert(session, SavedScreen, [{
        "name": body.name, "definition": body.definition.model_dump(by_alias=True, mode="json"),
    }])  # fmt: skip
    session.commit()
    row = session.scalars(select(SavedScreen).where(SavedScreen.name == body.name)).one()
    session.refresh(row)
    return ScreenOut(name=row.name, definition=body.definition, updated_at=row.updated_at)


@router.delete("/screens/{name}", status_code=status.HTTP_204_NO_CONTENT,
               responses={404: {"description": "No such screen"}})  # fmt: skip
def delete_screen(name: ScreenName, session: SessionDep) -> None:
    row = session.scalar(select(SavedScreen).where(SavedScreen.name == name))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no screen {name!r}")
    session.delete(row)
    session.commit()
