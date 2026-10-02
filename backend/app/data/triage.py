"""Reconciliation triage for a coverage-grid cell (SPEC §3.6 step 4). Pure functions.

A cell flagged "values to review" holds annual-report PDF values waiting in the review queue.
For each one, the value already stored for the same item and period from another source is
shown next to it, with the difference, and a default choice:

- default precedence ``triage.precedence`` (exchange XBRL > Indian API > annual-report PDF):
  the stored value from the best-ranked source wins;
- unless the PDF value's confidence is at least ``triage.pdf_confidence_min``: then the PDF;
- a PDF value with nothing else stored for the item is the only source: the PDF.

"Use the PDF" accepts the candidate; "use the other source" rejects it (the stored value
stays). Either way the decision is saved on the candidate (the review queue's status).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

CR = 1e7

Choice = Literal["pdf", "other"]


@dataclass(frozen=True)
class PdfValue:
    candidate_id: int
    item_code: str
    period_end: date
    value_inr: float | None
    confidence: float
    raw_label: str


@dataclass(frozen=True)
class TriageRow:
    candidate_id: int
    item_code: str
    period_end: date
    raw_label: str
    pdf_value_cr: float | None
    pdf_confidence: float
    other_source: str | None
    other_value_cr: float | None
    difference_cr: float | None  # PDF - other
    difference_pct: float | None  # of the other value
    default: Choice
    reason: str


def triage(
    pdf: Sequence[PdfValue],
    stored: Mapping[tuple[str, date], Mapping[str, float]],
    *,
    precedence: Sequence[str],
    pdf_confidence_min: float,
) -> list[TriageRow]:
    """``stored``: (item_code, period_end) → {source: latest value in ₹} for the non-PDF
    sources. Sources not in ``precedence`` rank after those that are."""
    rank = {s: i for i, s in enumerate(precedence)}
    rows = []
    for c in pdf:
        others = stored.get((c.item_code, c.period_end), {})
        best = min(others, key=lambda s: rank.get(s, len(rank)), default=None)
        other = others[best] if best is not None else None
        pdf_cr = c.value_inr / CR if c.value_inr is not None else None
        other_cr = other / CR if other is not None else None
        diff = pdf_cr - other_cr if pdf_cr is not None and other_cr is not None else None
        diff_pct = diff / abs(other_cr) if diff is not None and other_cr else None
        default: Choice
        if best is None:
            default, why = "pdf", "only source for this item"
        elif c.value_inr is None:
            default, why = "other", "the PDF value has no unit"
        elif c.confidence >= pdf_confidence_min:
            default = "pdf"
            why = f"PDF confidence {c.confidence:.0%} >= {pdf_confidence_min:.0%}"
        else:
            default = "other"
            why = f"precedence {' > '.join(precedence)}: {best} (PDF confidence {c.confidence:.0%})"
        rows.append(
            TriageRow(c.candidate_id, c.item_code, c.period_end, c.raw_label, pdf_cr,
                      c.confidence, best, other_cr, diff, diff_pct, default, why)
        )  # fmt: skip
    return rows
