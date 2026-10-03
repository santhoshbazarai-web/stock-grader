"""Data for the stock page's Financials, Key Metrics and Dividends tabs (SPEC §9).

Statements: up to 12 annual (or quarterly) columns, oldest first, grouped into income
statement, balance sheet and cash flow (banks: their own lines). Key metrics: the latest value
of each metric with its 5-year median and where it sits in its own history, computed from the
stored annual statements. Dividends: DPS by fiscal year from the stored dividend actions.

A value that is not stored stays ``None`` (never 0) and its row carries the reason.
"""

# ruff: noqa: E501
import math
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AppConfig, SectorModel
from app.db.enums import CorporateActionType, EventKind
from app.db.models import CorporateAction, Event, FinAnnual, FinQuarterly, Instrument
from app.fundamentals.banking import bank_frame, bank_metrics
from app.fundamentals.metrics import annual_metrics, by_year, cagr, capex, column, ratio
from app.reports.data import load_financials, load_overrides, load_shares_year_end, vendor_rows
from app.reports.history import fundamentals_history

WINDOW = 12
Unit = Literal["cr", "inr", "pct", "x", "days"]


def _num(v: object) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


# ───────────────────────── statements ─────────────────────────


class StatementColumn(BaseModel):
    period_end: date
    label: str = Field(description="FY2025 or Mar-25")
    fiscal_year: int | None
    source: str | None
    derived: bool = Field(description="Summed from quarters (annual P&L only)")
    vendor_reclassified: bool


class StatementRow(BaseModel):
    key: str
    label: str
    unit: Unit
    values: list[float | None]
    yoy: bool = Field(description="Show a YoY % row under this one")
    reason: str | None = Field(description="Why the empty cells are empty")


class StatementSection(BaseModel):
    id: Literal["income", "balance", "cashflow"]
    title: str
    rows: list[StatementRow]


class Statements(BaseModel):
    symbol: str
    period: Literal["annual", "quarterly"]
    model: Literal["general", "bank"]
    basis: str | None = Field(description="consolidated | standalone")
    columns: list[StatementColumn]
    sections: list[StatementSection]


_GENERAL_INCOME = [
    ("revenue", "Revenue", "cr", True), ("cogs", "Cost of goods sold", "cr", False),
    ("ebitda", "EBITDA", "cr", True), ("other_income", "Other income", "cr", False),
    ("depreciation", "Depreciation", "cr", False), ("ebit", "EBIT", "cr", True),
    ("interest", "Interest", "cr", False), ("pbt", "Profit before tax", "cr", True),
    ("tax", "Tax", "cr", False), ("pat", "Profit after tax", "cr", True),
    ("eps_diluted", "EPS (diluted)", "inr", True),
]  # fmt: skip
_GENERAL_BALANCE = [
    ("total_assets", "Total assets", "cr", True), ("current_assets", "Current assets", "cr", False),
    ("current_liabilities", "Current liabilities", "cr", False),
    ("total_equity", "Shareholders' equity", "cr", True), ("total_debt", "Total debt", "cr", False),
    ("cash_and_equivalents", "Cash and equivalents", "cr", False),
    ("receivables", "Receivables", "cr", False), ("inventory", "Inventory", "cr", False),
    ("payables", "Payables", "cr", False), ("net_block", "Net block", "cr", False),
    ("book_value_per_share", "Book value per share", "inr", False),
]  # fmt: skip
_GENERAL_CASH = [
    ("cfo", "Cash from operations", "cr", True),
    ("purchase_of_fixed_assets", "Purchase of fixed assets", "cr", False),
    ("sale_of_fixed_assets", "Sale of fixed assets", "cr", False),
    ("fcf", "Free cash flow", "cr", True), ("dividends_paid", "Dividends paid", "cr", False),
]  # fmt: skip


def _label(period_end: date, fy: int | None, annual: bool) -> str:
    return f"FY{fy}" if annual and fy else pd.Timestamp(period_end).strftime("%b-%y")


def _sector_model(session: Session, config: AppConfig, iid: int) -> SectorModel:
    sector = load_overrides(session, iid).sector or session.scalar(
        select(Instrument.sector).where(Instrument.id == iid)
    )
    return config.sectors.for_sector(sector).model


def statements(
    session: Session, symbol: str, config: AppConfig, period: Literal["annual", "quarterly"]
) -> Statements | None:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    if iid is None:
        return None
    annual_p = period == "annual"
    model_cls: type[FinAnnual] | type[FinQuarterly] = FinAnnual if annual_p else FinQuarterly
    df, basis, _ = load_financials(
        session, model_cls, iid, "fin_annual" if annual_p else "fin_quarterly"
    )
    bank = _sector_model(session, config, iid) in (SectorModel.BANK, SectorModel.INSURANCE)
    df = df.iloc[-WINDOW:]
    vendor = vendor_rows(session, model_cls, iid)
    src: dict[date, str | None] = {}
    if basis:
        rows = session.execute(
            select(model_cls.period_end, model_cls.source).where(
                model_cls.instrument_id == iid, model_cls.statement_type == basis
            )
        ).all()
        src = {pe: so for pe, so in rows}
    cols = [
        StatementColumn(
            period_end=idx.date(),
            label=_label(idx.date(), int(r["fiscal_year"]) if annual_p and pd.notna(r.get("fiscal_year")) else None, annual_p),
            fiscal_year=int(r["fiscal_year"]) if annual_p and pd.notna(r.get("fiscal_year")) else None,
            source=src.get(idx.date()),
            derived=bool(r.get("is_derived")) if annual_p else False,
            vendor_reclassified=idx.date() in vendor,
        )
        for idx, (_, r) in zip(pd.DatetimeIndex(df.index), df.iterrows(), strict=True)
    ]  # fmt: skip
    empty = "not reported in the stored statements"

    def row(key: str, label: str, unit: Unit, series: pd.Series | None, yoy: bool,
            reason: str | None = None) -> StatementRow:  # fmt: skip
        vals: list[float | None] = (
            [None] * len(df) if series is None else [_num(v) for v in series.reindex(df.index)]
        )
        return StatementRow(key=key, label=label, unit=unit, values=vals, yoy=yoy,
                            reason=reason or empty)  # fmt: skip

    secs: list[StatementSection] = []
    if bank:
        bf = bank_frame(df) if not df.empty else df
        bm = None
        if not df.empty:
            try:
                bm = bank_metrics(df)
            except (KeyError, ValueError, TypeError):  # sparse quarterly frames
                bm = None
        if annual_p and bm is not None and not df.empty:
            nii = pd.Series(bm["nii"].values, index=df.index) if len(bm) == len(df) else None
        else:
            nii = None
        if nii is None and not df.empty:
            nii = column(bf, "net_interest_income").fillna(
                column(bf, "interest_earned") - column(bf, "interest_expended")
            )
        bv = None
        if annual_p and not df.empty:
            h = fundamentals_history(
                symbol.upper(), df, pd.DataFrame(), config, bank=True,
                shares_year_end=load_shares_year_end(session, iid, df),
            )  # fmt: skip
            by_fy = {p.fiscal_year: p.bvps for p in h.years}
            bv = pd.Series([by_fy.get(int(fy)) for fy in df["fiscal_year"]], index=df.index)
        secs.append(StatementSection(id="income", title="Income statement", rows=[
            row("nii", "Net interest income", "cr", nii, True),
            row("other_income", "Non-interest income", "cr", column(bf, "other_income"), True),
            row("operating_expenses", "Non-interest expense", "cr", column(bf, "operating_expenses"), False),
            row("loan_loss_provisions", "Provisions", "cr", column(bf, "loan_loss_provisions"), False),
            row("pat", "Profit after tax", "cr", column(bf, "pat"), True),
            row("eps_diluted", "EPS (diluted)", "inr", column(bf, "eps_diluted"), True),
        ]))  # fmt: skip
        if annual_p:
            secs.append(StatementSection(id="balance", title="Balance sheet", rows=[
                row("advances", "Loans (advances)", "cr", column(bf, "advances"), True),
                row("deposits", "Deposits", "cr", column(bf, "deposits"), True),
                row("total_equity", "Equity", "cr", column(bf, "total_equity"), True),
                row("total_assets", "Total assets", "cr", column(bf, "total_assets"), True),
                row("bvps", "Book value per share", "inr", bv, True,
                    "needs year-end shares outstanding"),
            ]))  # fmt: skip
            secs.append(StatementSection(id="cashflow", title="Cash flow", rows=[
                row("cfo", "Cash from operations", "cr", column(bf, "cfo"), False,
                    "bank cash-flow statements are not in the exchange XBRL"),
                row("dividends_paid", "Dividends paid", "cr", column(bf, "dividends_paid"), False),
            ]))  # fmt: skip
    else:

        def block(
            spec: list[tuple[str, str, str, bool]], fcf: pd.Series | None = None
        ) -> list[StatementRow]:
            out = []
            for key, label, unit, yoy in spec:
                s = fcf if key == "fcf" else (df[key] if key in df.columns else None)
                out.append(row(key, label, unit, s, yoy))  # type: ignore[arg-type]
            return out

        secs.append(
            StatementSection(id="income", title="Income statement", rows=block(_GENERAL_INCOME))
        )
        if annual_p:
            fcf = (column(df, "cfo") - capex(df)) if not df.empty else None
            secs.append(
                StatementSection(id="balance", title="Balance sheet", rows=block(_GENERAL_BALANCE))
            )
            secs.append(
                StatementSection(id="cashflow", title="Cash flow", rows=block(_GENERAL_CASH, fcf))
            )
    return Statements(symbol=symbol.upper(), period=period, model="bank" if bank else "general",
                      basis=basis, columns=cols, sections=secs)  # fmt: skip


# ───────────────────────── key metrics ─────────────────────────


class KeyMetric(BaseModel):
    key: str
    glossary_key: str
    label: str
    unit: Unit
    value: float | None
    fiscal_year: int | None = Field(description="The year the value is for")
    median_5y: float | None
    percentile: float | None = Field(description="0-100: share of its own history at or below")
    years: int = Field(description="Years of history behind the median / percentile")
    reason: str | None = Field(description="Why a value is missing")


class KeyMetricGroup(BaseModel):
    title: str
    metrics: list[KeyMetric]


class KeyMetricsOut(BaseModel):
    symbol: str
    model: Literal["general", "bank"]
    groups: list[KeyMetricGroup]


def _stats(s: pd.Series) -> tuple[float | None, int | None, float | None, float | None, int]:
    """latest value, its fiscal year, 5-year median, percentile in history, history length."""
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    if s.empty:
        return None, None, None, None, 0
    last, fy = float(s.iloc[-1]), int(s.index[-1])
    hist = s.iloc[-10:]
    pctl = float((hist <= last).mean() * 100) if len(hist) >= 3 else None
    med = float(s.iloc[-5:].median()) if len(s) >= 3 else None
    return last, fy, med, pctl, len(hist)


def _growth(s: pd.Series) -> pd.Series:
    prev = s.shift(1)
    return ((s / prev) - 1).where(prev > 0) * 100


def key_metrics(
    session: Session, symbol: str, config: AppConfig, peer_stats: dict[str, Any] | None
) -> KeyMetricsOut | None:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    if iid is None:
        return None
    annual, _, _ = load_financials(session, FinAnnual, iid, "fin_annual")
    bank = _sector_model(session, config, iid) in (SectorModel.BANK, SectorModel.INSURANCE)
    no_data = "no annual statements stored"
    groups: list[KeyMetricGroup] = []

    def metric(key: str, label: str, unit: Unit, s: pd.Series, gk: str | None = None,
               reason: str | None = None) -> KeyMetric:  # fmt: skip
        value, fy, med, pctl, n = _stats(s)
        why = None
        if value is None:
            why = (
                reason or no_data
                if annual.empty
                else (reason or "not computable from the stored statements")
            )
        elif med is None:
            why = "needs at least 3 years of history for the median and percentile"
        return KeyMetric(key=key, glossary_key=gk or key, label=label, unit=unit, value=value,
                         fiscal_year=fy, median_5y=med, percentile=pctl, years=n, reason=why)  # fmt: skip

    def add(title: str, metrics: list[KeyMetric]) -> None:
        groups.append(KeyMetricGroup(title=title, metrics=metrics))

    ps = peer_stats or {}
    none = pd.Series(dtype=float)

    def current(key: str, label: str, unit: Unit, gk: str) -> KeyMetric:
        v = _num(ps.get(key))
        return KeyMetric(key=key, glossary_key=gk, label=label, unit=unit, value=v, fiscal_year=None,
                         median_5y=None, percentile=None, years=0,
                         reason=None if v is not None else "needs the latest price and statements")  # fmt: skip

    hist_note = "year-end prices adjusted for splits are not stored per year, so no history"

    def valuation(keys: list[tuple[str, str, str]]) -> None:
        ms = [current(k, label, "x", gk) for k, label, gk in keys]
        for mt in ms:
            mt.reason = mt.reason or hist_note
        add("Valuation ratios", ms)

    if bank:
        bm = bank_metrics(annual) if not annual.empty else pd.DataFrame()
        bf = by_year(bank_frame(annual)) if not annual.empty else pd.DataFrame()

        def b(col: str) -> pd.Series:
            return bm[col] if col in bm.columns else none

        def bcol(name: str) -> pd.Series:
            return column(bf, name) if not bf.empty else none

        why = "not in the stored statements; add the bank's disclosure via an annual-report upload"
        add("Profitability", [
            metric("roe_pct", "ROE", "pct", b("roe_pct"), "roe"),
            metric("roa_pct", "ROA", "pct", b("roa_pct")),
            metric("nim_pct", "Net interest margin", "pct", b("nim_pct"),
                   reason="needs interest earned / expended and investments"),
        ])  # fmt: skip
        add("Asset quality", [
            metric("gnpa_pct", "Gross NPA", "pct", b("gnpa_pct"), reason=why),
            metric("nnpa_pct", "Net NPA", "pct", b("nnpa_pct"), reason=why),
            metric("credit_cost_pct", "Credit cost", "pct", b("credit_cost_pct"),
                   reason="needs loan-loss provisions"),
        ])  # fmt: skip
        add("Capital and funding", [
            metric("car_pct", "Capital adequacy (CAR)", "pct", b("car_pct"), reason=why),
            metric("casa_pct", "CASA ratio", "pct", b("casa_pct"), reason=why),
            metric("cd_ratio_pct", "Credit-deposit ratio", "pct", b("cd_ratio_pct")),
            metric("equity_to_assets_pct", "Equity / assets", "pct", b("equity_to_assets_pct")),
        ])  # fmt: skip
        add("Efficiency", [
            metric("cost_to_income_pct", "Cost-to-income", "pct", b("cost_to_income_pct"),
                   reason="needs operating expenses"),
        ])  # fmt: skip
        add("Growth", [
            metric("loan_growth_pct", "Loan growth", "pct", b("loan_growth_pct")),
            metric("deposit_growth_pct", "Deposit growth", "pct", b("deposit_growth_pct")),
            metric("pat_growth", "PAT growth", "pct", _growth(bcol("pat"))),
        ])  # fmt: skip
        h = None
        if not annual.empty:
            h = fundamentals_history(
                symbol.upper(), annual, pd.DataFrame(), config, bank=True,
                shares_year_end=load_shares_year_end(session, iid, annual),
            )  # fmt: skip
        bv = pd.Series({p.fiscal_year: p.bvps for p in h.years}, dtype=float) if h else none
        add("Per-share data", [
            metric("eps", "EPS (diluted)", "inr", bcol("eps_diluted")),
            metric("bvps", "Book value per share", "inr", bv, reason="needs year-end shares outstanding"),
        ])  # fmt: skip
        valuation([("pb", "P/B", "pb"), ("pe", "P/E", "pe")])
    else:
        if annual.empty:
            m = pd.DataFrame()
            df = pd.DataFrame()
        else:
            m = annual_metrics(annual, tax_rate_fallback=config.valuation.tax_rate_default,
                               days=config.scoring.fundamentals.days_in_year)  # fmt: skip
            df = by_year(annual)

        def g(col: str, scale: float = 1.0) -> pd.Series:
            return m[col] * scale if col in m.columns else none

        def c(name: str) -> pd.Series:
            return column(df, name) if not df.empty else none

        rev, pat, ta = c("revenue"), c("pat"), c("total_assets")
        add("Profitability", [
            metric("roe", "ROE", "pct", g("roe", 100)),
            metric("roce", "ROCE", "pct", g("roce", 100)),
            metric("roic", "ROIC", "pct", g("roic", 100)),
            metric("opm", "Operating margin", "pct", g("opm", 100), "opm_ttm"),
            metric("net_margin", "Net margin", "pct", ratio(pat, rev) * 100),
        ])  # fmt: skip
        add("Growth", [
            metric("sales_growth", "Sales growth", "pct", _growth(rev)),
            metric("ebitda_growth", "EBITDA growth", "pct", _growth(c("ebitda"))),
            metric("pat_growth", "PAT growth", "pct", _growth(pat)),
            metric("eps_growth", "EPS growth", "pct", _growth(c("eps_diluted"))),
        ])  # fmt: skip
        add("Financial strength", [
            metric("debt_to_equity", "Debt / equity", "x", g("debt_to_equity")),
            metric("net_debt_to_ebitda", "Net debt / EBITDA", "x", g("net_debt_to_ebitda")),
            metric("interest_coverage", "Interest coverage", "x", g("interest_coverage"),
                   reason="debt-free: no interest cost, so coverage is not defined"),
            metric("current_ratio", "Current ratio", "x", ratio(c("current_assets"), c("current_liabilities"))),
        ])  # fmt: skip
        add("Efficiency", [
            metric("asset_turnover", "Asset turnover", "x", ratio(rev, ta.rolling(2, min_periods=2).mean())),
            metric("debtor_days", "Debtor days", "days", g("debtor_days")),
            metric("inventory_days", "Inventory days", "days", g("inventory_days")),
            metric("payable_days", "Payable days", "days", g("payable_days")),
            metric("ccc_days", "Cash conversion cycle", "days", g("ccc_days")),
        ])  # fmt: skip
        valuation(
            [("pe", "P/E", "pe"), ("pb", "P/B", "pb"), ("ev_ebitda", "EV / EBITDA", "ev_ebitda")]
        )
        add("Per-share data", [
            metric("eps", "EPS (diluted)", "inr", c("eps_diluted")),
            metric("bvps", "Book value per share", "inr", c("book_value_per_share")),
            metric("sales_per_share", "Sales per share", "inr", ratio(rev, c("shares_diluted_cr")),
                   reason="needs diluted shares"),
        ])  # fmt: skip
    return KeyMetricsOut(symbol=symbol.upper(), model="bank" if bank else "general", groups=groups)


# ───────────────────────── dividends ─────────────────────────


class DpsYear(BaseModel):
    fiscal_year: int
    dps: float
    eps: float | None
    payout_pct: float | None = Field(
        description="DPS / EPS * 100; None when EPS is missing or <= 0"
    )


class ActionRow(BaseModel):
    ex_date: date
    action_type: str
    dividend_per_share: float | None
    ratio_old: float | None
    ratio_new: float | None
    description: str | None


class BuybackRow(BaseModel):
    date: date | None
    title: str


class DividendsOut(BaseModel):
    symbol: str
    history: list[DpsYear]
    ttm_dps: float | None
    yield_pct: float | None
    yield_reason: str | None
    dps_cagr: dict[str, float | None] = Field(description="'3y' | '5y' | '10y' -> fraction")
    cagr_reason: str | None
    corporate_actions: list[ActionRow] = Field(description="Bonus and split history, newest first")
    upcoming: list[ActionRow]
    buybacks: list[BuybackRow]
    buyback_note: str | None
    note: str | None


def _fy(d: date) -> int:
    return d.year + (1 if d.month >= 4 else 0)


def dividends(session: Session, symbol: str, cmp: float | None, today: date) -> DividendsOut | None:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    if iid is None:
        return None
    acts = list(session.scalars(select(CorporateAction).where(CorporateAction.instrument_id == iid).order_by(CorporateAction.ex_date)))  # fmt: skip
    divs = [
        a for a in acts if a.action_type == CorporateActionType.DIVIDEND and a.dividend_per_share
    ]
    eps_by_fy: dict[int, float] = {}
    for r in session.scalars(select(FinAnnual).where(FinAnnual.instrument_id == iid)):
        if r.eps_diluted is not None and r.fiscal_year not in eps_by_fy:
            eps_by_fy[r.fiscal_year] = float(r.eps_diluted)
    paid = [a for a in divs if a.ex_date <= today]
    by_fy: dict[int, float] = {}
    for a in paid:
        by_fy[_fy(a.ex_date)] = by_fy.get(_fy(a.ex_date), 0.0) + float(a.dividend_per_share or 0)
    history = []
    for fy in sorted(by_fy)[-12:]:
        eps = eps_by_fy.get(fy)
        history.append(DpsYear(fiscal_year=fy, dps=round(by_fy[fy], 4), eps=eps,
                               payout_pct=by_fy[fy] / eps * 100 if eps and eps > 0 else None))  # fmt: skip
    ttm = sum(
        float(a.dividend_per_share or 0) for a in paid if a.ex_date > today - timedelta(days=365)
    )
    have = bool(paid)
    ttm_dps = ttm if have else None
    y_reason = None
    if not have:
        y_reason = "no dividend records stored (this does not prove none were paid)"
    elif not cmp:
        y_reason = "needs the latest price"
    current_fy = _fy(today)
    done = {fy: v for fy, v in by_fy.items() if fy < current_fy}  # completed fiscal years only
    last = max(done) if done else None
    cg: dict[str, float | None] = {}
    for n in (3, 5, 10):
        start = done.get(last - n) if last else None
        cg[f"{n}y"] = cagr(done[last], start, n) if last and start else None
    c_reason = (
        None
        if any(v is not None for v in cg.values())
        else ("needs dividend records for completed fiscal years that far back")
    )

    def to_row(a: CorporateAction) -> ActionRow:
        return ActionRow(ex_date=a.ex_date, action_type=str(a.action_type.value),
                         dividend_per_share=a.dividend_per_share, ratio_old=a.ratio_old,
                         ratio_new=a.ratio_new, description=a.description)  # fmt: skip

    ev = session.execute(
        select(Event.event_date, Event.title)
        .where(Event.instrument_id == iid, Event.kind == EventKind.ANNOUNCEMENT,
               Event.title.ilike("%buy%back%"))
        .order_by(Event.event_date.desc()).limit(10)
    ).all()  # fmt: skip
    return DividendsOut(
        symbol=symbol.upper(),
        history=history,
        ttm_dps=ttm_dps,
        yield_pct=ttm / cmp * 100 if have and cmp else None,
        yield_reason=y_reason,
        dps_cagr=cg,
        cagr_reason=c_reason,
        corporate_actions=[
            to_row(a)
            for a in reversed(acts)
            if a.action_type in (CorporateActionType.BONUS, CorporateActionType.SPLIT)
        ],
        upcoming=[to_row(a) for a in acts if a.ex_date >= today],
        buybacks=[BuybackRow(date=d, title=t) for d, t in ev],
        buyback_note=None if ev else "no buyback announcements in the stored events",
        note=None if have else "No dividend records are stored for this stock.",
    )
