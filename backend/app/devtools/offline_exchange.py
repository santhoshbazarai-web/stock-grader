"""Offline exchange: a deterministic stand-in for NSE and the price broker, for development and
the v1 acceptance test (P25) where nseindia.com and the brokers cannot be reached.

    python -m app.devtools.offline_exchange seed     # symbol master + index for the 5 companies
    OFFLINE_EXCHANGE=1 python -m app.jobs pipeline-worker   # the worker then uses this exchange

Five **synthetic** companies (names end in "(synthetic)", symbols start with ``OFF``, ISINs
``INE9OFF…``) cover the main valuation models: a bank, IT services, auto, FMCG and cement.
For each the exchange serves:

- **results filings**: a listing and one XBRL document per quarter from FY2014 to the last
  quarter whose results would be out by now, in the SEBI Ind AS / banking results layout the
  real parser reads (Q4 filings also carry the fiscal year's P&L, cash flow and balance
  sheet). Figures grow at the company's rate with a small deterministic wobble, so every
  identity inside a filing holds (all lines of a period are scaled together);
- **daily prices**: a deterministic random walk around P/E x EPS, and a NIFTY500 index;
- **shareholding** for the last eight quarters; no corporate actions, annual reports or
  events (the pipeline reports those as empty / unavailable, as it would for a quiet stock).

Nothing here is real market data. It is refused unless ``APP_ENV=development``.
"""

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.core.config import Provider, ProvidersConfig
from app.data.providers.base import ProviderUnavailable
from app.data.providers.nse import CA_COLUMNS, RESULTS_COLUMNS

IST = ZoneInfo("Asia/Kolkata")
HOST = "https://offline-exchange.invalid"  # reserved TLD: never a real host (RFC 2606)
FIRST_QUARTER_END = date(2013, 6, 30)  # Q1 FY2014
BASE = date(2024, 3, 31)  # the templates' period: figures are scaled from here
LAG_DAYS = 40  # results are out 40 days after the quarter (60 for Q4)
LAG_Q4_DAYS = 55
INDEX = "NIFTY500"

# Template figures (rupees) for one quarter (Q), one fiscal year (Y) and the year-end balance
# sheet (I), consistent within each period. From the synthetic filings in tests/fixtures/xbrl.
INDUSTRIAL: dict[str, dict[str, float]] = {
    "Q": {"RevenueFromOperations": 12.5e9, "OtherIncome": 0.3e9, "Income": 12.8e9,
          "CostOfMaterialsConsumed": 5.0e9, "PurchasesOfStockInTrade": 1.0e9,
          "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": -0.2e9,
          "EmployeeBenefitExpense": 2.0e9, "FinanceCosts": 0.15e9,
          "DepreciationDepletionAndAmortisationExpense": 0.4e9, "OtherExpenses": 1.5e9,
          "Expenses": 9.85e9, "ProfitBeforeTax": 2.95e9, "CurrentTax": 0.7e9,
          "DeferredTax": 0.05e9, "TaxExpense": 0.75e9, "ProfitLossForPeriod": 2.2e9,
          "ProfitOrLossAttributableToOwnersOfParent": 2.1e9,
          "ProfitOrLossAttributableToNonControllingInterests": 0.1e9,
          "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": 10.52,
          "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": 10.50},
    "Y": {"RevenueFromOperations": 48e9, "OtherIncome": 1e9, "Income": 49e9,
          "CostOfMaterialsConsumed": 20e9, "PurchasesOfStockInTrade": 4e9,
          "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": -0.5e9,
          "EmployeeBenefitExpense": 8e9, "FinanceCosts": 0.6e9,
          "DepreciationDepletionAndAmortisationExpense": 1.5e9, "OtherExpenses": 4.4e9,
          "Expenses": 38e9, "ProfitBeforeTax": 11e9, "CurrentTax": 2.6e9,
          "DeferredTax": 0.15e9, "TaxExpense": 2.75e9, "ProfitLossForPeriod": 8.25e9,
          "ProfitOrLossAttributableToOwnersOfParent": 8e9,
          "ProfitOrLossAttributableToNonControllingInterests": 0.25e9,
          "CashFlowsFromUsedInOperatingActivities": 9.5e9,
          "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": -3e9,
          "ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": 0.1e9,
          "DividendsPaidClassifiedAsFinancingActivities": 2e9,
          "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": 40.0},
    "I": {"PropertyPlantAndEquipment": 20e9, "CapitalWorkInProgress": 1.5e9,
          "OtherIntangibleAssets": 1e9, "NoncurrentInvestments": 3e9, "CurrentInvestments": 2e9,
          "Inventories": 6e9, "TradeReceivablesCurrent": 7e9, "CashAndCashEquivalents": 1.5e9,
          "BankBalanceOtherThanCashAndCashEquivalents": 0.5e9, "CurrentAssets": 16e9,
          "Assets": 45e9, "EquityShareCapital": 0.2e9, "OtherEquity": 29.8e9,
          "EquityAttributableToOwnersOfParent": 30e9, "NonControllingInterest": 1e9,
          "Equity": 31e9, "BorrowingsNoncurrent": 4e9, "BorrowingsCurrent": 1e9,
          "TradePayablesCurrent": 5e9, "CurrentLiabilities": 9e9, "Liabilities": 14e9,
          "EquityAndLiabilities": 45e9},
}  # fmt: skip
BANK: dict[str, dict[str, float]] = {
    "Q": {"InterestEarned": 100e9, "OtherIncome": 20e9, "InterestExpended": 55e9,
          "OperatingExpenses": 25e9, "ProvisionsOtherThanTaxAndContingencies": 8e9,
          "ProfitLossFromOrdinaryActivitiesBeforeTax": 32e9, "TaxExpense": 8e9,
          "NetProfitLossForThePeriod": 24e9, "GrossNonPerformingAssets": 60e9,
          "NetNonPerformingAssets": 15e9,
          "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": 24.0},
    "Y": {"InterestEarned": 380e9, "OtherIncome": 70e9, "InterestExpended": 210e9,
          "OperatingExpenses": 95e9, "ProvisionsOtherThanTaxAndContingencies": 25e9,
          "ProfitLossFromOrdinaryActivitiesBeforeTax": 120e9, "TaxExpense": 30e9,
          "NetProfitLossForThePeriod": 90e9,
          "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": 90.0},
    "I": {"Advances": 3000e9, "Deposits": 3500e9, "Investments": 1000e9, "Assets": 5000e9,
          "Capital": 10e9, "ReservesAndSurplus": 590e9, "Borrowings": 400e9},
}  # fmt: skip
CAR = 0.1685  # bank capital adequacy (a ratio: not scaled)
PER_SHARE = {"BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
             "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"}  # fmt: skip


@dataclass(frozen=True)
class Company:
    symbol: str
    name: str
    sector: str  # config/sectors.yaml key
    layout: str  # industrial | bank
    isin: str
    bse_code: str
    growth: float  # yearly
    scale: float  # x the template figures at FY2024
    pe: float  # prices hover around this P/E
    seed: int

    @property
    def is_bank(self) -> bool:
        return self.layout == "bank"


COMPANIES = (
    Company("OFFBANK", "Offline Housing Bank Ltd (synthetic)", "banks", "bank", "INE9OFF00011",
            "980001", 0.16, 0.5, 18.0, 101),
    Company("OFFIT", "Offline Infotech Ltd (synthetic)", "it_services", "industrial",
            "INE9OFF00029", "980002", 0.12, 2.0, 26.0, 102),
    Company("OFFAUTO", "Offline Motors Ltd (synthetic)", "auto", "industrial", "INE9OFF00037",
            "980003", 0.10, 3.0, 20.0, 103),
    Company("OFFFMCG", "Offline Consumer Products Ltd (synthetic)", "fmcg", "industrial",
            "INE9OFF00045", "980004", 0.09, 1.5, 40.0, 104),
    Company("OFFCEM", "Offline Cement Ltd (synthetic)", "cement", "industrial", "INE9OFF00052",
            "980005", 0.08, 1.2, 22.0, 105),
)  # fmt: skip
BY_SYMBOL = {c.symbol: c for c in COMPANIES}


# ───────────────────────── filings ─────────────────────────


def quarter_ends(until: date) -> list[date]:
    out, d = [], FIRST_QUARTER_END
    while d <= until:
        out.append(d)
        d = (pd.Timestamp(d) + pd.offsets.QuarterEnd(1)).date()
    return out


def quarter_start(end: date) -> date:
    return date(end.year, end.month - 2, 1)  # quarter ends: Mar, Jun, Sep, Dec


def _is_q4(end: date) -> bool:
    return end.month == 3


def disseminated(end: date) -> datetime:
    lag = LAG_Q4_DAYS if _is_q4(end) else LAG_DAYS
    return datetime.combine(end + timedelta(days=lag), time(17, 0), tzinfo=IST)


def factor(c: Company, end: date) -> float:
    """Size of the period ending ``end`` relative to the template: growth plus a small
    deterministic wobble (the same for every line of the period)."""
    years = (end - BASE).days / 365.25
    wobble = 1 + 0.03 * math.sin(end.toordinal() / 97.0 + c.seed)
    return c.scale * float((1 + c.growth) ** years) * wobble


def filings(c: Company, now: datetime) -> list[dict[str, Any]]:
    """The listing NSE's results API would give for ``c`` (RESULTS_COLUMNS)."""
    rows = []
    for end in quarter_ends(now.date()):
        when = disseminated(end)
        if when > now:
            continue
        rows.append({"url": f"{HOST}/{c.symbol}/{c.symbol}_{end:%Y%m%d}_consolidated.xml",
                     "period_start": quarter_start(end), "period_end": end,
                     "statement_type": "consolidated", "audited": _is_q4(end),
                     "is_bank": c.is_bank, "disseminated_at": when})  # fmt: skip
    return rows


def _ctx(cid: str, c: Company, *, start: date | None = None, end: date | None = None,
         instant: date | None = None) -> str:  # fmt: skip
    period = (
        f"<xbrli:instant>{instant}</xbrli:instant>"
        if instant
        else f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
    )
    return (f'  <xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme='
            f'"http://www.bseindia.com">{c.bse_code}</xbrli:identifier></xbrli:entity>'
            f"<xbrli:period>{period}</xbrli:period></xbrli:context>\n")  # fmt: skip


def _fact(name: str, ctx: str, value: float) -> str:
    if name in PER_SHARE:
        return (
            f'  <in-bse-fin:{name} contextRef="{ctx}" unitRef="INRPerShare" decimals="2">'
            f"{value:.2f}</in-bse-fin:{name}>\n"
        )
    lakhs = round(value / 1e5) * 100000
    return (
        f'  <in-bse-fin:{name} contextRef="{ctx}" unitRef="INR" decimals="-5">'
        f"{lakhs:.0f}</in-bse-fin:{name}>\n"
    )


def _text(name: str, value: str) -> str:
    return f'  <in-bse-fin:{name} contextRef="OneD">{value}</in-bse-fin:{name}>\n'


def xbrl_document(c: Company, end: date) -> bytes:
    """The filing for the quarter ending ``end`` (Q4: with the year's figures)."""
    tpl = BANK if c.is_bank else INDUSTRIAL
    q_start = quarter_start(end)
    fy_end = date(end.year + (1 if end.month > 3 else 0), 3, 31)
    fy_start = date(fy_end.year - 1, 4, 1)
    q4 = _is_q4(end)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        f"<!-- Offline exchange (synthetic, not a real company): {c.name}, quarter to {end} -->\n",
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">\n',
        _ctx("OneD", c, start=q_start, end=end),
    ]
    if q4:
        parts += [_ctx("FourD", c, start=fy_start, end=fy_end), _ctx("OneI", c, instant=end)]
    parts.append(
        '  <xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>\n'
        '  <xbrli:unit id="INRPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>'
        "iso4217:INR</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>"
        "xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>\n"
        '  <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>\n'
    )
    parts += [
        _text("NameOfTheCompany", c.name), _text("ScripCode", c.bse_code),
        _text("Symbol", c.symbol), _text("ISIN", c.isin),
        _text("DateOfStartOfFinancialYear", str(fy_start)),
        _text("DateOfEndOfFinancialYear", str(fy_end)),
        _text("DateOfBoardMeetingWhenFinancialResultsWereApproved",
              str(disseminated(end).date())),
        _text("LevelOfRoundingUsedInFinancialStatements", "Lakhs"),
        _text("NatureOfReportStandaloneConsolidated", "Consolidated"),
        _text("DateOfStartOfReportingPeriod", str(q_start)),
        _text("DateOfEndOfReportingPeriod", str(end)),
        _text("WhetherResultsAreAuditedOrUnaudited", "Audited" if q4 else "Unaudited"),
    ]  # fmt: skip
    f = factor(c, end)
    parts += [_fact(k, "OneD", v * f) for k, v in tpl["Q"].items()]
    if c.is_bank:
        car = "CapitalAdequacyRatioBaselIii"
        parts.append(
            f'  <in-bse-fin:{car} contextRef="OneD" unitRef="pure" decimals="4">'
            f"{CAR:.4f}</in-bse-fin:{car}>\n"
        )
    if q4:
        parts += [_fact(k, "FourD", v * f) for k, v in tpl["Y"].items()]
        parts += [_fact(k, "OneI", v * f) for k, v in tpl["I"].items()]
    parts.append("</xbrli:xbrl>\n")
    return "".join(parts).encode()


def _parse_url(url: str) -> tuple[Company, date]:
    if not url.startswith(HOST + "/"):
        raise ProviderUnavailable(f"offline exchange: not one of its documents: {url}")
    sym, name = url[len(HOST) + 1 :].split("/", 1)
    if sym not in BY_SYMBOL:
        raise ProviderUnavailable(f"offline exchange: unknown company {sym}")
    return BY_SYMBOL[sym], datetime.strptime(name.split("_")[1], "%Y%m%d").date()


# ───────────────────────── prices ─────────────────────────


def annual_eps(c: Company, d: date) -> float:
    tpl = BANK if c.is_bank else INDUSTRIAL
    eps = tpl["Y"]["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"]
    return eps * c.scale * float((1 + c.growth) ** ((d - BASE).days / 365.25))


@cache
def _bars(key: str, seed: int, start: date, level0: float, drift: float) -> pd.DataFrame:
    """Deterministic daily bars from ``start`` to 2032 (sliced by callers)."""
    days = pd.bdate_range(start, date(2032, 12, 31))
    rng = np.random.default_rng(seed)
    n = len(days)
    # mean-reverting log deviation around a smooth trend: realistic swings, no blow-ups
    dev = np.zeros(n)
    shocks = rng.normal(0, 0.014, n)
    for i in range(1, n):
        dev[i] = 0.995 * dev[i - 1] + shocks[i]
    years = np.arange(n) / 252
    close = level0 * np.exp(drift * years + dev)
    spread = close * rng.uniform(0.004, 0.02, n)
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.004, n)), "high": close + spread,
        "low": close - spread, "close": close,
        "volume": rng.integers(200_000, 2_000_000, n).astype(float),
    }, index=pd.DatetimeIndex(days, name="date"))  # fmt: skip


def company_bars(c: Company) -> pd.DataFrame:
    start = date(2014, 1, 1)
    level0 = c.pe * annual_eps(c, start)
    return _bars(c.symbol, c.seed, start, level0, math.log(1 + c.growth))


def index_bars() -> pd.DataFrame:
    return _bars(INDEX, 7, date(2014, 1, 1), 6000.0, math.log(1.10))


def _slice(df: pd.DataFrame, start: date, end: date, today: date) -> pd.DataFrame:
    return df.loc[pd.Timestamp(start) : pd.Timestamp(min(end, today))].copy()


# ───────────────────────── provider ─────────────────────────


class OfflineExchange:
    """Registered as both the price broker (prices, index) and NSE (filings, shareholding).
    Implements PriceProvider, IndexPriceProvider, CorporateActionsProvider,
    ShareholdingProvider, ResultsFilingsProvider, AnnualReportsProvider and DeliveryProvider."""

    def __init__(self, name: Provider, *, clock: Any = lambda: datetime.now(UTC)) -> None:
        self.name = name
        self._clock = clock

    def _now(self) -> datetime:
        now: datetime = self._clock()
        return now.astimezone(IST)

    def _company(self, symbol: str) -> Company:
        c = BY_SYMBOL.get(symbol.strip().upper())
        if c is None:
            raise ProviderUnavailable(f"offline exchange: {symbol} is not one of its companies")
        return c

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return _slice(company_bars(self._company(symbol)), start, end, self._now().date())

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame:
        if index.replace(" ", "").upper() != INDEX:
            raise ProviderUnavailable(f"offline exchange: no {index} index")
        return _slice(index_bars(), start, end, self._now().date())

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: float(company_bars(c)["close"].iloc[-1]) for s in symbols
                if (c := BY_SYMBOL.get(s)) is not None}  # fmt: skip

    def corporate_actions(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self._company(symbol)
        return pd.DataFrame(columns=CA_COLUMNS)

    def shareholding(self, symbol: str) -> pd.DataFrame:
        self._company(symbol)
        ends = [d for d in quarter_ends(self._now().date() - timedelta(days=21))][-8:]
        rows = {pd.Timestamp(d): {"promoter_pct": 52.0 - 0.1 * i, "promoter_pledge_pct": 0.0,
                                  "fii_pct": 18.0 + 0.2 * i, "dii_pct": 14.0 + 0.1 * i,
                                  "mf_pct": 8.0, "public_pct": 16.0 - 0.2 * i,
                                  "filing_date": d + timedelta(days=21)}
                for i, d in enumerate(ends)}  # fmt: skip
        df = pd.DataFrame.from_dict(rows, orient="index")
        df.index = pd.DatetimeIndex(df.index, name="period_end")
        return df

    def results_filings(self, symbol: str) -> pd.DataFrame:
        rows = filings(self._company(symbol), self._now())
        return pd.DataFrame(rows, columns=RESULTS_COLUMNS).astype(object)

    def results_document(self, url: str) -> bytes:
        c, end = _parse_url(url)
        if disseminated(end) > self._now():
            raise ProviderUnavailable(f"offline exchange: {url} not filed yet")
        return xbrl_document(c, end)

    def annual_reports(self, symbol: str) -> pd.DataFrame:
        raise ProviderUnavailable("offline exchange: no annual-report PDFs (XBRL covers BS/CF)")

    def annual_report_document(self, url: str) -> bytes:
        raise ProviderUnavailable("offline exchange: no annual-report PDFs")

    def delivery(self, day: date) -> pd.DataFrame:
        return pd.DataFrame()


def offline_providers(clock: Any = lambda: datetime.now(UTC)) -> dict[Provider, object]:
    """The providers a context uses with OFFLINE_EXCHANGE=1: the offline exchange alone (no
    network). Data it serves is stored with source "offline", never as a real provider's."""
    return {Provider.OFFLINE: OfflineExchange(Provider.OFFLINE, clock=clock)}


def offline_router_config(pc: ProvidersConfig) -> ProvidersConfig:
    """``providers.yaml`` with every dataset routed to the offline exchange only."""
    return pc.model_copy(update={"priority": {d: [Provider.OFFLINE] for d in pc.priority}})


class NoLimiter:
    """The offline exchange is in-process: nothing to rate-limit."""

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        return None


# ───────────────────────── seed ─────────────────────────


def seed(session: Any, universe_index: str) -> list[str]:
    """Instruments and symbol master rows (searchable by name, symbol, ISIN, BSE code) for the
    five companies, and synthetic NIFTY500 bars (the benchmark) only when the index has no
    prices at all, so real data is never overwritten. The companies are not made members of
    the universe index: synthetic stocks never join real universe lists or outrank real ones
    in search. Idempotent. Financials and prices are *not* written: the pipeline fetches
    them."""
    from sqlalchemy import select

    from app.db.enums import SymbolStatus
    from app.db.models import Instrument, PriceDaily, Symbol
    from app.db.upsert import upsert
    from app.devtools.synthetic import store_prices

    now = datetime.now(UTC)
    upsert(session, Instrument, [{
        "symbol": c.symbol, "isin": c.isin, "name": c.name, "series": "EQ", "sector": c.sector,
        "industry": c.sector.replace("_", " ").title(), "is_index": False, "is_active": True,
        "source": "offline", "fetched_at": now} for c in COMPANIES],
        update=["isin", "name", "series", "sector", "industry", "is_active", "source"])  # fmt: skip
    upsert(session, Instrument, [{"symbol": universe_index, "isin": None, "name": "Nifty 500",
                                  "series": None, "sector": None, "industry": None,
                                  "is_index": True, "is_active": True, "source": "offline",
                                  "fetched_at": now}], update=[])  # fmt: skip
    ids = dict(session.execute(select(Instrument.symbol, Instrument.id).where(
        Instrument.symbol.in_([*BY_SYMBOL, universe_index]))).all())  # fmt: skip
    upsert(session, Symbol, [{
        "isin": c.isin, "name": c.name, "nse_symbol": c.symbol, "nse_series": "EQ",
        "bse_code": c.bse_code, "instrument_id": ids[c.symbol], "status": SymbolStatus.ACTIVE,
        "sources": ["offline"], "last_seen": now.date()} for c in COMPANIES])  # fmt: skip
    has_index = session.scalar(select(PriceDaily.instrument_id).where(
        PriceDaily.instrument_id == ids[universe_index]).limit(1))  # fmt: skip
    if has_index is None:
        bars = _slice(index_bars(), date(2014, 1, 1), now.date(), now.astimezone(IST).date())
        store_prices(session, ids[universe_index], bars, source="offline")
    session.commit()
    return [c.symbol for c in COMPANIES]


def main(argv: list[str] | None = None) -> int:
    from app.core.config import get_config
    from app.core.settings import get_settings
    from app.db.session import get_session_factory

    parser = argparse.ArgumentParser(description="Offline exchange for development / P25")
    parser.add_argument("command", choices=["seed", "list"])
    args = parser.parse_args(argv)
    if get_settings().app_env != "development":
        print("the offline exchange is for APP_ENV=development only", file=sys.stderr)
        return 2
    if args.command == "list":
        for c in COMPANIES:
            print(f"{c.symbol:<8} {c.bse_code}  {c.isin}  {c.sector:<12} {c.name}")
        return 0
    with get_session_factory()() as session:
        symbols = seed(session, get_config().jobs.universe_index)
    print(f"seeded offline exchange companies: {', '.join(symbols)} (synthetic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
