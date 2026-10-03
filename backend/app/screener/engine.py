"""Screener engine (SPEC §9). Pure functions over STORED report payloads: no database, no
recomputation. Fields come from ``config/screener_fields.yaml``.

A stock with no value for a field never passes a filter on it (nothing is assumed), and sorts
last whichever the direction."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.core.config import ScreenerField, ScreenFilter


def _dig(payload: Mapping[str, Any], path: str) -> Any:
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _value(payload: Mapping[str, Any], f: ScreenerField) -> Any:
    if f.path == "computed:discount_pct":
        cmp, fv = payload.get("cmp"), (payload.get("levels") or {}).get("fair_value")
        return (cmp / fv - 1) * 100 if cmp and fv else None
    if f.path.startswith("bank:"):
        name = f.path.split(":", 1)[1]
        for m in payload.get("bank_metrics") or []:
            if m.get("name") == name:
                return m.get("value")
        return None
    return _dig(payload, f.path)


def extract(payload: Mapping[str, Any], fields: Sequence[ScreenerField]) -> dict[str, Any]:
    """One screener row: every field's value (percent fields in percent points), plus the six
    pillar scores for the cards view."""
    row: dict[str, Any] = {}
    for f in fields:
        v = _value(payload, f)
        if v is None:
            row[f.key] = None
        elif f.type in ("number", "percent"):
            row[f.key] = float(v) * f.scale if isinstance(v, int | float) else None
        else:
            row[f.key] = str(v)
    row["pillars"] = [
        {"pillar": p.get("pillar"), "score": p.get("score")} for p in payload.get("pillars") or []
    ]
    return row


def passes(row: Mapping[str, Any], flt: ScreenFilter) -> bool:
    v = row.get(flt.key)
    if v is None:
        return False
    if flt.in_:
        return str(v) in flt.in_
    if not isinstance(v, int | float):
        return False
    return (flt.min is None or v >= flt.min) and (flt.max is None or v <= flt.max)


def apply_filters(
    rows: Iterable[Mapping[str, Any]], filters: Sequence[ScreenFilter]
) -> list[Mapping[str, Any]]:
    return [r for r in rows if all(passes(r, f) for f in filters)]


def sort_rows(rows: Sequence[Mapping[str, Any]], key: str, order: str) -> list[Mapping[str, Any]]:
    present = [r for r in rows if r.get(key) is not None]
    missing = sorted((r for r in rows if r.get(key) is None), key=lambda r: str(r["symbol"]))
    present.sort(key=lambda r: (str(r[key]).lower() if isinstance(r[key], str) else r[key]),
                 reverse=order == "desc")  # fmt: skip
    return [*present, *missing]


def paginate(rows: Sequence[Any], page: int, page_size: int) -> list[Any]:
    start = (page - 1) * page_size
    return list(rows[start : start + page_size])
