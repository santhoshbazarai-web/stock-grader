"""Cross-source reconciliation (SPEC v0.2 §3.9). Pure functions.

For every (period, item) that at least two sources report, the first source in
``reconciliation.sources`` with a value is the reference; any other source that differs by
more than ``tolerance_rel`` of the reference **and** by more than ``min_diff_inr`` rupees is a
finding. Each finding gets the likeliest cause, checked in this order:

- **units**: the ratio of the two figures is within the tolerance of one of
  ``unit_factors`` (100, 1,000, 1 lakh, 1 crore) or its inverse: amounts keyed in the wrong unit;
- **basis**: the differing figure matches the reference's *other* basis (standalone for a
  consolidated reference): a consolidated / standalone mix-up;
- **restatement**: it matches an earlier version of the reference (the figure first filed,
  since restated): the source did not pick up the restatement;
- otherwise no cause is named ("unexplained").
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

CRORE = 1e7

Key = tuple[date, str, str]  # (period_end, period_type, item_code)

SOURCE_LABELS = {
    "nse_xbrl": "NSE XBRL",
    "bse_xbrl": "BSE XBRL",
    "annual_report_pdf": "annual report (PDF)",
    "indianapi": "Indian API",
    "market_lens": "Market Lens",
    "yfinance": "yfinance",
}
ITEM_LABELS = {
    "revenue": "Sales",
    "ebitda": "EBITDA",
    "pat": "PAT",
    "cfo": "CFO",
    "total_assets": "Total assets",
    "total_equity": "Equity",
    "deposits": "Deposits",
    "advances": "Advances (loans)",
    "eps_diluted": "Diluted EPS",
}


@dataclass(frozen=True)
class Finding:
    period_end: date
    period_type: str  # year | quarter
    item_code: str
    source: str  # the source that differs
    reference_source: str
    reference_value: float  # ₹
    value: float  # ₹
    diff_rel: float  # |value - reference| / |reference|
    cause: str | None  # units | basis | restatement
    values: dict[str, float]  # every source's figure for this key (₹)
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Reconciliation:
    findings: list[Finding]
    compared: list[tuple[Key, str]]  # (key, source) pairs checked against a reference
    reasons: list[str]


def _near(a: float, b: float, tol: float) -> bool:
    return b != 0 and abs(a / b - 1) <= tol


def _cr(v: float, per_share: bool = False) -> str:
    return f"₹{v:,.2f}" if per_share else f"₹{v / CRORE:,.2f} cr"


def _period(end: date, ptype: str) -> str:
    return f"{'quarter' if ptype == 'quarter' else 'year'} to {end:%d %b %Y}"


def label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def reconcile(
    values: Mapping[Key, Mapping[str, float]],
    *,
    sources: Sequence[str],
    tolerance_rel: float,
    min_diff_inr: float,
    unit_factors: Sequence[float],
    other_basis: Mapping[Key, float] | None = None,
    earlier_versions: Mapping[Key, Sequence[float]] | None = None,
    basis: str = "consolidated",
    per_share: Collection[str] = (),
) -> Reconciliation:
    """``values[key][source]`` in rupees. ``other_basis[key]``: the reference source's figure
    for the other basis; ``earlier_versions[key]``: its superseded (restated) figures.
    ``per_share`` items (EPS) are in ₹ per share: ``min_diff_inr`` does not apply to them."""
    other = "standalone" if basis == "consolidated" else "consolidated"
    order = {s: i for i, s in enumerate(sources)}
    findings: list[Finding] = []
    compared: list[tuple[Key, str]] = []
    for key in sorted(values):
        by_source = {s: v for s, v in values[key].items() if s in order and v is not None}
        if len(by_source) < 2:
            continue
        ref_src = min(by_source, key=order.__getitem__)
        ref = by_source[ref_src]
        end, ptype, item = key
        for src, v in sorted(by_source.items(), key=lambda kv: order[kv[0]]):
            if src == ref_src:
                continue
            compared.append((key, src))
            diff = abs(v - ref)
            rel = diff / abs(ref) if ref else float("inf")
            ps = item in per_share
            if rel <= tolerance_rel or (not ps and diff <= min_diff_inr):
                continue
            what = (f"{ITEM_LABELS.get(item, item)} ({_period(end, ptype)}, {basis}): "
                    f"{label(src)} {_cr(v, ps)} vs {label(ref_src)} {_cr(ref, ps)}, "
                    + (f"{rel:.1%} apart" if ref else "reference is zero"))  # fmt: skip
            cause, why = None, "no cause found (unexplained difference)"
            ratio = v / ref if ref else None
            factor = next((f for f in unit_factors if ratio is not None
                           and (_near(ratio, f, tolerance_rel) or _near(ratio, 1 / f,
                                                                        tolerance_rel))),
                          None)  # fmt: skip
            ob = (other_basis or {}).get(key)
            old = [o for o in (earlier_versions or {}).get(key, []) if _near(v, o, tolerance_rel)]
            if factor is not None:
                cause = "units"
                why = (f"the figures are {factor:,.0f}x apart: amounts keyed in the wrong unit "
                       "(e.g. lakh vs crore)")  # fmt: skip
            elif ob is not None and _near(v, ob, tolerance_rel):
                cause = "basis"
                why = (f"{label(src)} matches the {other} figure ({_cr(ob, ps)}): consolidated / "
                       "standalone mix-up")  # fmt: skip
            elif old:
                cause = "restatement"
                why = (f"{label(src)} matches the figure first filed ({_cr(old[0], ps)}), since "
                       f"restated by {label(ref_src)}")  # fmt: skip
            findings.append(Finding(end, ptype, item, src, ref_src, ref, v, rel, cause,
                                    dict(by_source), [what, why]))  # fmt: skip
    reasons = [f"{len(compared)} figure(s) compared across sources, {len(findings)} differ by "
               f"more than {tolerance_rel:.0%}"]  # fmt: skip
    return Reconciliation(findings, compared, reasons)
