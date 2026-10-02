"""Knock-out filters (SPEC §7.1). Pure functions.

Any triggered knock-out caps the grade at ``knockouts.cap_grade`` (C):

    pledge          promoter pledge % > max_pledge_pct
    negative_cfo    CFO < 0 in >= negative_cfo_years_in_5 of the last negative_cfo_window_years
    auditor         an auditor resignation within auditor_resignation_years of ``as_of``
    asm_gsm         on NSE's ASM / GSM surveillance list (when ``on_asm_gsm`` is enabled)
    mcap            market cap < min_mcap_cr (₹ Cr)
    liquidity       20-day average traded value < min_avg_traded_value_cr_20d (₹ Cr)
    beneish         Beneish M-score > beneish_m_max

Missing inputs are never assumed to pass or fail: the check is reported as *unknown*
(``KnockoutResult.unknown``, for the caller to record as a data gap). The CFO check is decided
from partial data when the known years already settle it either way.
"""

from dataclasses import dataclass, field
from datetime import date

from app.core.config import KnockoutsConfig
from app.scoring.common import Grade


@dataclass(frozen=True)
class KnockoutInputs:
    as_of: date
    pledge_pct: float | None = None  # % of promoter holding pledged
    cfo_history: list[float | None] | None = None  # CFO per fiscal year, oldest → newest
    auditor_resignations: list[date] | None = None  # None = unknown; [] = none on record
    on_asm_gsm: bool | None = None
    mcap_cr: float | None = None
    avg_traded_value_cr_20d: float | None = None
    beneish_m: float | None = None
    # False for banks / NBFCs / insurers: their operating cash flow moves with deposits and
    # loans, so the negative-CFO check does not apply (and needs no cash-flow data)
    cfo_applies: bool = True
    # False for lenders / insurers: the Beneish M-score is built for industrial companies
    beneish_applies: bool = True


@dataclass(frozen=True)
class KnockoutResult:
    cap: Grade | None  # grade ceiling if any knock-out triggered
    triggered: list[str] = field(default_factory=list)  # check codes
    unknown: list[str] = field(default_factory=list)  # checks that lacked data
    reasons: list[str] = field(default_factory=list)


def _negative_cfo(
    history: list[float | None] | None, cfg: KnockoutsConfig
) -> tuple[bool | None, str]:
    n, need = cfg.negative_cfo_window_years, cfg.negative_cfo_years_in_5
    if not history:
        return None, "CFO history unavailable"
    window = history[-n:]
    unknown = n - len(window) + sum(v is None for v in window)
    negative = sum(v is not None and v < 0 for v in window)
    if negative >= need:
        return True, f"CFO negative in {negative} of the last {n} years (knock-out at {need})"
    if negative + unknown < need:
        return False, f"CFO negative in {negative} of the last {n} years"
    return None, f"CFO negative in {negative} known years, {unknown} of the last {n} missing"


def knockouts(inputs: KnockoutInputs, cfg: KnockoutsConfig) -> KnockoutResult:
    triggered: list[str] = []
    unknown: list[str] = []
    reasons: list[str] = []

    def check(code: str, hit: bool | None, hit_reason: str, missing_reason: str) -> None:
        if hit is None:
            unknown.append(code)
            reasons.append(f"knock-out check '{code}' not evaluated: {missing_reason}")
        elif hit:
            triggered.append(code)
            reasons.append(f"knock-out: {hit_reason}")

    p = inputs.pledge_pct
    check(
        "pledge",
        None if p is None else p > cfg.max_pledge_pct,
        f"promoter pledge {p}% > {cfg.max_pledge_pct:g}%",
        "pledge data unavailable",
    )

    if inputs.cfo_applies:
        hit, why = _negative_cfo(inputs.cfo_history, cfg)
        check("negative_cfo", hit, why, why)
    else:
        reasons.append("knock-out check 'negative_cfo' not applicable to a lender / insurer")

    if inputs.auditor_resignations is None:
        check("auditor", None, "", "auditor-resignation record unavailable")
    else:
        years = cfg.auditor_resignation_years
        d0 = inputs.as_of
        cutoff = d0.replace(
            year=d0.year - years, day=28 if (d0.month, d0.day) == (2, 29) else d0.day
        )
        recent = [d for d in inputs.auditor_resignations if cutoff <= d <= inputs.as_of]
        check(
            "auditor",
            bool(recent),
            f"auditor resigned on {max(recent) if recent else ''} (within {years} years)",
            "",
        )

    if cfg.on_asm_gsm:
        check(
            "asm_gsm",
            inputs.on_asm_gsm,
            "on the ASM/GSM surveillance list",
            "ASM/GSM list unavailable",
        )

    m = inputs.mcap_cr
    check(
        "mcap",
        None if m is None else m < cfg.min_mcap_cr,
        f"market cap ₹{m:,.0f} Cr < ₹{cfg.min_mcap_cr:,.0f} Cr" if m is not None else "",
        "market cap unavailable",
    )

    tv = inputs.avg_traded_value_cr_20d
    check(
        "liquidity",
        None if tv is None else tv < cfg.min_avg_traded_value_cr_20d,
        f"20-day average traded value ₹{tv:,.2f} Cr < ₹{cfg.min_avg_traded_value_cr_20d:g} Cr"
        if tv is not None
        else "",
        "traded value unavailable",
    )

    b = inputs.beneish_m
    if inputs.beneish_applies:
        check(
            "beneish",
            None if b is None else b > cfg.beneish_m_max,
            f"Beneish M-score {b:.2f} > {cfg.beneish_m_max:g}" if b is not None else "",
            "Beneish M-score unavailable",
        )
    else:
        reasons.append("knock-out check 'beneish' not applicable to a lender / insurer")

    cap = Grade(cfg.cap_grade) if triggered else None
    if cap is not None:
        reasons.append(f"grade capped at {cap.label} by {len(triggered)} knock-out(s)")
    else:
        reasons.append(
            "no knock-outs triggered" + (f" ({len(unknown)} unknown)" if unknown else "")
        )
    return KnockoutResult(cap, triggered, unknown, reasons)
