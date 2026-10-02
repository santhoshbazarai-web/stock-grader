"""UI preferences (key → JSON), e.g. the "My metrics" picked on the stock page."""

import re
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import SessionDep
from app.db.models import UiPreference
from app.db.upsert import upsert

router = APIRouter(prefix="/preferences", tags=["preferences"])
Key = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]


class PreferenceIn(BaseModel):
    value: Any = Field(description="Any JSON value (max about 8 KB)")


class PreferenceOut(BaseModel):
    key: str
    value: Any | None


@router.get("/{key}")
def get_preference(key: Key, session: SessionDep) -> PreferenceOut:
    row = session.scalar(select(UiPreference).where(UiPreference.key == key))
    return PreferenceOut(key=key, value=row.value.get("value") if row else None)


@router.put("/{key}")
def put_preference(key: Key, body: PreferenceIn, session: SessionDep) -> PreferenceOut:
    if len(re.sub(r"\s", "", str(body.value))) > 8192:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "value too large")
    upsert(session, UiPreference, [{"key": key, "value": {"value": body.value}}])
    session.commit()
    return PreferenceOut(key=key, value=body.value)
