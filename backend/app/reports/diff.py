"""Before/after comparison of a stock's report for results notifications (SPEC v0.2 §3.8).
Pure functions.

A change is notified when the grade, zone or action changed, or the fair value moved by more
than ``results_watch.notify.fv_change_rel`` (relative). The message reads like
"XYZ Q2 FY25 results: Grade B→A, FV ₹1,240→₹1,390, zone Fair→Discount".
"""

from dataclasses import dataclass
from datetime import date
from typing import Any


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The fields a change notification compares, from a stored report payload."""
    levels = payload.get("levels") or {}
    return {
        "as_of": payload.get("as_of"),
        "grade": payload.get("grade_label") or payload.get("grade"),
        "zone": payload.get("zone"),
        "action": payload.get("action"),
        "fair_value": levels.get("fair_value"),
        "cmp": payload.get("cmp"),
    }


def _words(value: str | None) -> str:
    if not value:
        return "n/a"
    return " ".join(w if w in ("on", "of") else w.capitalize() for w in value.split("_"))


def _money(v: float) -> str:
    return f"₹{v:,.0f}"


@dataclass(frozen=True)
class ReportChange:
    field: str  # grade | zone | action | fair_value
    before: Any
    after: Any
    text: str  # "Grade B→A"


def report_changes(
    before: dict[str, Any], after: dict[str, Any], *, fv_change_rel: float
) -> list[ReportChange]:
    """What changed between two :func:`summary` dicts that is worth a notification."""
    out: list[ReportChange] = []
    if before.get("grade") != after.get("grade"):
        out.append(ReportChange("grade", before.get("grade"), after.get("grade"),
                                f"Grade {before.get('grade') or 'n/a'}→"
                                f"{after.get('grade') or 'n/a'}"))  # fmt: skip
    fv0, fv1 = before.get("fair_value"), after.get("fair_value")
    if (fv0 is None) != (fv1 is None) or (
        fv0 is not None and fv1 is not None
        and (fv0 <= 0 or abs(fv1 / fv0 - 1) > fv_change_rel)
    ):  # fmt: skip
        out.append(ReportChange("fair_value", fv0, fv1,
                                f"FV {_money(fv0) if fv0 is not None else 'n/a'}→"
                                f"{_money(fv1) if fv1 is not None else 'n/a'}"))  # fmt: skip
    if before.get("zone") != after.get("zone"):
        out.append(
            ReportChange(
                "zone",
                before.get("zone"),
                after.get("zone"),
                f"zone {_words(before.get('zone'))}→{_words(after.get('zone'))}",
            )
        )
    if before.get("action") != after.get("action"):
        out.append(ReportChange("action", before.get("action"), after.get("action"),
                                f"action {_words(before.get('action'))}→"
                                f"{_words(after.get('action'))}"))  # fmt: skip
    return out


def quarter_label(period_end: date | None, fy_end_month: int) -> str | None:
    """ "Q2 FY25" for a quarter ending Sep 2024 when the fiscal year ends in March."""
    if period_end is None:
        return None
    months_into_fy = (period_end.month - fy_end_month - 1) % 12 + 1  # 1..12
    fy = period_end.year + (1 if period_end.month > fy_end_month else 0)
    return f"Q{(months_into_fy - 1) // 3 + 1} FY{fy % 100:02d}"


def change_message(
    symbol: str, label: str | None, changes: list[ReportChange], after: dict[str, Any]
) -> tuple[str, str]:
    """(title, body) of the notification."""
    parts = [c.text for c in changes]
    if not any(c.field == "fair_value" for c in changes) and after.get("fair_value") is not None:
        parts.insert(1 if changes and changes[0].field == "grade" else 0,
                     f"FV {_money(after['fair_value'])}")  # fmt: skip
    title = f"{symbol} {label or 'results'}: {', '.join(parts)}"
    body = (
        f"New report as of {after.get('as_of')}: grade {after.get('grade') or 'n/a'}, "
        f"zone {_words(after.get('zone'))}, action {_words(after.get('action'))}"
        + (f", CMP {_money(after['cmp'])}" if after.get("cmp") is not None else "")
        + "."
    )
    return title, body
