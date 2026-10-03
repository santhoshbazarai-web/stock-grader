"""Manual portfolios (SPEC §9). Holdings come only from the transactions entered or imported
here: nothing is ever fetched from Fyers or Zerodha (brokers stay read-only for prices).
Maths is in ``app.portfolio.engine``; prices are the latest stored ``prices_daily`` close."""

# ruff: noqa: E501
import csv
import io
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep, SessionDep
from app.api.schemas import SymbolField
from app.api.watchlist import _previous_payloads
from app.core.config import AppConfig
from app.db.enums import CorporateActionType, TxnType
from app.db.models import Alert, CorporateAction, Instrument, Portfolio, PortfolioTxn, PriceDaily
from app.jobs.common import ensure_instruments
from app.portfolio.engine import Action, Txn, xirr
from app.portfolio.engine import run as replay
from app.reports.service import latest_payloads

router = APIRouter(tags=["portfolio"])
GRADE_ORDER = ["A_plus", "A", "B", "C", "D"]  # best first (the keys of scoring.grade_cutoffs)
PREMIUM = {"premium", "extreme_premium"}
CSV_COLUMNS = ["date", "symbol", "type", "quantity", "price", "fees", "notes"]


class PortfolioIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    opening_cash: float = Field(0.0, ge=0, description="Cash at the start, in ₹ (entered by hand)")


class PortfolioOut(BaseModel):
    id: int
    name: str
    opening_cash: float
    transactions: int


class TxnIn(BaseModel):
    symbol: SymbolField
    txn_type: TxnType
    txn_date: date
    quantity: float | None = Field(None, gt=0)
    price: float | None = Field(None, ge=0)
    fees: float = Field(0.0, ge=0)
    notes: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _check(self) -> "TxnIn":
        t = self.txn_type
        if self.txn_date > date.today():
            raise ValueError("the date is in the future")
        if t in (TxnType.BUY, TxnType.SELL) and (not self.quantity or self.price is None):
            raise ValueError(f"a {t.value} needs quantity and price")
        if t == TxnType.DIVIDEND and not self.price:
            raise ValueError("a dividend needs the dividend per share in price")
        if t in (TxnType.BONUS, TxnType.SPLIT) and (not self.quantity or not self.price):
            raise ValueError(
                f"a {t.value} needs both numbers: quantity = new shares, price = per old shares"
            )
        return self


class TxnOut(BaseModel):
    id: int
    symbol: str
    name: str | None
    txn_type: TxnType
    txn_date: date
    quantity: float | None
    price: float | None
    fees: float
    notes: str | None


class PositionOut(BaseModel):
    symbol: str
    name: str | None
    sector: str | None
    quantity: float
    avg_cost: float | None
    cost: float
    price: float | None
    price_date: date | None
    value: float | None
    pnl: float | None
    pnl_pct: float | None
    weight_pct: float | None
    realised_pnl: float
    dividends: float
    grade: str | None
    zone: str | None
    fair_value: float | None
    fv_gap_pct: float | None = Field(
        description="(price / fair value - 1) * 100; negative = discount"
    )
    flags: list[str]
    missing: str | None = Field(description="Why value / P&L are empty")


class Summary(BaseModel):
    market_value: float | None
    total_cost: float
    unrealised_pnl: float | None
    unrealised_pct: float | None
    realised_pnl: float
    dividends: float
    cash: float
    xirr: float | None
    xirr_reason: str | None
    holdings: int
    unpriced: list[str]


class AllocationRow(BaseModel):
    sector: str
    value: float
    weight_pct: float


class PortfolioView(BaseModel):
    portfolio: PortfolioOut
    as_of: date
    summary: Summary
    positions: list[PositionOut]
    allocation: list[AllocationRow]
    warnings: list[str]


class ImportIn(BaseModel):
    csv: str = Field(max_length=500_000)


class ImportOut(BaseModel):
    added: int
    errors: list[str]


# ───────────────────────── portfolios ─────────────────────────


def _portfolio(session: Session, pid: int) -> Portfolio:
    p = session.get(Portfolio, pid)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no portfolio {pid}")
    return p


def _out(session: Session, p: Portfolio) -> PortfolioOut:
    n = session.scalar(select(func.count()).where(PortfolioTxn.portfolio_id == p.id)) or 0
    return PortfolioOut(id=p.id, name=p.name, opening_cash=p.opening_cash, transactions=n)


@router.get("/portfolios")
def list_portfolios(session: SessionDep) -> list[PortfolioOut]:
    return [_out(session, p) for p in session.scalars(select(Portfolio).order_by(Portfolio.id))]


@router.post("/portfolios", status_code=status.HTTP_201_CREATED)
def create_portfolio(body: PortfolioIn, session: SessionDep) -> PortfolioOut:
    name = body.name.strip()
    if session.scalar(select(Portfolio.id).where(Portfolio.name == name)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"a portfolio named {name!r} exists")
    p = Portfolio(name=name, opening_cash=body.opening_cash)
    session.add(p)
    session.commit()
    return _out(session, p)


@router.put("/portfolios/{pid}")
def update_portfolio(pid: int, body: PortfolioIn, session: SessionDep) -> PortfolioOut:
    p = _portfolio(session, pid)
    name = body.name.strip()
    clash = session.scalar(select(Portfolio.id).where(Portfolio.name == name, Portfolio.id != pid))
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"a portfolio named {name!r} exists")
    p.name, p.opening_cash = name, body.opening_cash
    session.commit()
    return _out(session, p)


@router.delete("/portfolios/{pid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_portfolio(pid: int, session: SessionDep) -> None:
    session.delete(_portfolio(session, pid))
    session.commit()


# ───────────────────────── transactions ─────────────────────────


def _txn_out(t: PortfolioTxn, inst: Instrument) -> TxnOut:
    return TxnOut(
        id=t.id, symbol=inst.symbol, name=inst.name, txn_type=t.txn_type, txn_date=t.txn_date,
        quantity=t.quantity, price=t.price, fees=t.fees, notes=t.notes,
    )  # fmt: skip


@router.get("/portfolios/{pid}/transactions")
def list_transactions(pid: int, session: SessionDep) -> list[TxnOut]:
    _portfolio(session, pid)
    rows = session.execute(
        select(PortfolioTxn, Instrument)
        .join(Instrument, Instrument.id == PortfolioTxn.instrument_id)
        .where(PortfolioTxn.portfolio_id == pid)
        .order_by(PortfolioTxn.txn_date.desc(), PortfolioTxn.id.desc())
    ).all()
    return [_txn_out(t, i) for t, i in rows]


def _apply(session: Session, t: PortfolioTxn, body: TxnIn) -> None:
    symbol = body.symbol.upper()
    t.instrument_id = ensure_instruments(session, [symbol])[symbol]
    t.txn_type, t.txn_date = body.txn_type, body.txn_date
    t.quantity, t.price, t.fees, t.notes = body.quantity, body.price, body.fees, body.notes


@router.post("/portfolios/{pid}/transactions", status_code=status.HTTP_201_CREATED)
def add_transaction(pid: int, body: TxnIn, session: SessionDep) -> TxnOut:
    _portfolio(session, pid)
    t = PortfolioTxn(portfolio_id=pid)
    _apply(session, t, body)
    session.add(t)
    session.commit()
    inst = session.get(Instrument, t.instrument_id)
    assert inst is not None
    return _txn_out(t, inst)


@router.put("/portfolios/{pid}/transactions/{tid}")
def edit_transaction(pid: int, tid: int, body: TxnIn, session: SessionDep) -> TxnOut:
    t = session.get(PortfolioTxn, tid)
    if t is None or t.portfolio_id != pid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no transaction {tid}")
    _apply(session, t, body)
    session.commit()
    inst = session.get(Instrument, t.instrument_id)
    assert inst is not None
    return _txn_out(t, inst)


@router.delete("/portfolios/{pid}/transactions/{tid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction(pid: int, tid: int, session: SessionDep) -> None:
    t = session.get(PortfolioTxn, tid)
    if t is None or t.portfolio_id != pid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no transaction {tid}")
    session.delete(t)
    session.commit()


def _date(raw: str) -> date:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unreadable date {raw!r} (use YYYY-MM-DD)")


def _getter(row: dict[str | Any, Any], cols: dict[str, str]) -> Callable[[str], str | None]:
    def get(c: str) -> str | None:
        return row.get(cols[c]) if c in cols else None

    return get


def _float(raw: str | None) -> float | None:
    raw = (raw or "").strip().replace(",", "")
    return float(raw) if raw else None


@router.post("/portfolios/{pid}/transactions/import")
def import_transactions(pid: int, body: ImportIn, session: SessionDep) -> ImportOut:
    """CSV with a header row: date, symbol, type, quantity, price, fees, notes (qty accepted).
    Valid rows are added; each bad row is reported with its line number."""
    _portfolio(session, pid)
    reader = csv.DictReader(io.StringIO(body.csv))
    if reader.fieldnames is None:
        return ImportOut(added=0, errors=["empty file"])
    alias = {
        "qty": "quantity",
        "ticker": "symbol",
        "txn_type": "type",
        "kind": "type",
        "note": "notes",
    }
    names = {(f or "").strip().lower(): f for f in reader.fieldnames}
    cols = {alias.get(k, k): v for k, v in names.items()}
    missing = [c for c in ("date", "symbol", "type") if c not in cols]
    if missing:
        return ImportOut(added=0, errors=[f"missing column(s): {', '.join(missing)}"])
    added, errors = 0, []
    for n, row in enumerate(reader, start=2):
        try:
            get = _getter(row, cols)
            sym = (get("symbol") or "").strip().upper().removeprefix("NSE:").removesuffix("-EQ")
            item = TxnIn(
                symbol=sym, txn_type=TxnType((get("type") or "").strip().lower()),
                txn_date=_date(get("date") or ""), quantity=_float(get("quantity")),
                price=_float(get("price")), fees=_float(get("fees")) or 0.0,
                notes=(get("notes") or "").strip() or None,
            )  # fmt: skip
            t = PortfolioTxn(portfolio_id=pid)
            _apply(session, t, item)
            session.add(t)
            added += 1
        except ValueError as e:
            errors.append(f"line {n}: {str(e).splitlines()[0]}")
    session.commit()
    return ImportOut(added=added, errors=errors)


@router.get("/portfolios/{pid}/transactions/export")
def export_transactions(pid: int, session: SessionDep) -> Response:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for t in reversed(list_transactions(pid, session)):
        w.writerow([t.txn_date, t.symbol, t.txn_type.value, t.quantity if t.quantity is not None else "",
                    t.price if t.price is not None else "", t.fees, t.notes or ""])  # fmt: skip
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="portfolio-{pid}.csv"'})  # fmt: skip


# ───────────────────────── the view ─────────────────────────


def _latest_prices(session: Session, ids: list[int]) -> dict[int, tuple[float, date]]:
    if not ids:
        return {}
    rn = (
        func.row_number()
        .over(partition_by=PriceDaily.instrument_id, order_by=PriceDaily.date.desc())
        .label("rn")
    )
    sub = (
        select(PriceDaily.instrument_id, PriceDaily.date, PriceDaily.close, rn)
        .where(PriceDaily.instrument_id.in_(ids))
        .subquery()
    )
    rows = session.execute(
        select(sub.c.instrument_id, sub.c.close, sub.c.date).where(sub.c.rn == 1)
    )
    return {iid: (float(c), d) for iid, c, d in rows}


def _flags(
    now: dict[str, Any], prev: dict[str, Any] | None, alert: tuple[str, datetime] | None
) -> list[str]:
    out = []
    g1, g0 = now.get("grade"), (prev or {}).get("grade")
    if g1 in GRADE_ORDER and g0 in GRADE_ORDER and GRADE_ORDER.index(g1) > GRADE_ORDER.index(g0):
        out.append(
            f"Grade dropped {str(g0).replace('_plus', '+')} to {str(g1).replace('_plus', '+')}"
        )
    if now.get("zone") in PREMIUM and prev and prev.get("zone") not in PREMIUM:
        out.append(f"Zone changed to {str(now['zone']).replace('_', ' ').title()}")
    if alert:
        out.append(f"Alert fired: {alert[0].replace('_', ' ')} on {alert[1]:%d %b}")
    return out


def build_view(session: Session, p: Portfolio, config: AppConfig, today: date) -> PortfolioView:
    rows = session.execute(
        select(PortfolioTxn).where(PortfolioTxn.portfolio_id == p.id).order_by(PortfolioTxn.txn_date, PortfolioTxn.id)
    ).scalars().all()  # fmt: skip
    ids = sorted({t.instrument_id for t in rows})
    actions = [
        Action(a.instrument_id, a.action_type.value, a.ex_date, a.ratio_old, a.ratio_new)
        for a in session.scalars(
            select(CorporateAction).where(
                CorporateAction.instrument_id.in_(ids),
                CorporateAction.action_type.in_(
                    [CorporateActionType.BONUS, CorporateActionType.SPLIT]
                ),
                CorporateAction.ratio_old.is_not(None),
                CorporateAction.ratio_new.is_not(None),
                CorporateAction.ex_date <= today,
            )
        )
        if a.ratio_old and a.ratio_new
    ]
    res = replay(
        [
            Txn(t.instrument_id, t.txn_type.value, t.txn_date, t.quantity, t.price, t.fees)
            for t in rows
        ],
        actions,
    )
    insts = {i.id: i for i in session.scalars(select(Instrument).where(Instrument.id.in_(ids)))}
    prices = _latest_prices(session, ids)
    payloads = latest_payloads(session, ids)
    prevs = _previous_payloads(session, ids)
    since = datetime.now(UTC) - timedelta(days=config.jobs.alerts.portfolio_flag_days)
    fired: dict[int, tuple[str, datetime]] = {}
    for a in session.scalars(select(Alert).where(Alert.instrument_id.in_(ids), Alert.last_triggered_at >= since)):  # fmt: skip
        if a.last_triggered_at and (a.instrument_id not in fired or a.last_triggered_at > fired[a.instrument_id][1]):  # fmt: skip
            fired[a.instrument_id] = (a.alert_type.value, a.last_triggered_at)

    positions: list[PositionOut] = []
    flows = list(res.cash_flows)
    unpriced: list[str] = []
    value_total = cost_priced = 0.0
    for iid, pos in res.positions.items():
        inst = insts[iid]
        if pos.qty <= 1e-9:
            continue
        price_row = prices.get(iid)
        payload = payloads.get(iid) or {}
        price = price_row[0] if price_row else None
        value = price * pos.qty if price is not None else None
        if value is None:
            unpriced.append(inst.symbol)
        else:
            value_total += value
            cost_priced += pos.cost
        fv = (payload.get("levels") or {}).get("fair_value")
        positions.append(PositionOut(
            symbol=inst.symbol, name=inst.name, sector=inst.sector, quantity=pos.qty,
            avg_cost=pos.avg_cost, cost=pos.cost, price=price, price_date=price_row[1] if price_row else None,
            value=value, pnl=value - pos.cost if value is not None else None,
            pnl_pct=(value / pos.cost - 1) * 100 if value is not None and pos.cost > 0 else None,
            weight_pct=None, realised_pnl=pos.realised, dividends=pos.dividends,
            grade=payload.get("grade_label"), zone=payload.get("zone"), fair_value=fv,
            fv_gap_pct=(price / fv - 1) * 100 if price is not None and fv else None,
            flags=_flags(payload, prevs.get(iid), fired.get(iid)),
            missing=None if price is not None else "no stored price for this stock",
        ))  # fmt: skip
    for pos_out in positions:
        if pos_out.value is not None and value_total > 0:
            pos_out.weight_pct = pos_out.value / value_total * 100
    positions.sort(key=lambda x: -(x.value or 0))

    sectors: dict[str, float] = {}
    for x in positions:
        if x.value is not None:
            sectors[x.sector or "Unclassified"] = sectors.get(x.sector or "Unclassified", 0.0) + x.value  # fmt: skip
    alloc = [AllocationRow(sector=s, value=v, weight_pct=v / value_total * 100) for s, v in sorted(sectors.items(), key=lambda kv: -kv[1])]  # fmt: skip

    held = [x for x in positions]
    xirr_val: float | None = None
    xirr_reason: str | None = None
    if not rows:
        xirr_reason = "no transactions yet"
    elif unpriced:
        xirr_reason = f"needs a stored price for {', '.join(unpriced)}"
    else:
        xirr_val = xirr(flows + ([(today, value_total)] if held else []))
        if xirr_val is None:
            xirr_reason = "needs at least one purchase and one later cash flow or holding"
    total_cost = sum(x.cost for x in held)
    summary = Summary(
        market_value=value_total if held and len(unpriced) < len(held) else (0.0 if not held else None),
        total_cost=total_cost,
        unrealised_pnl=value_total - cost_priced if held and len(unpriced) < len(held) else None,
        unrealised_pct=(value_total / cost_priced - 1) * 100 if cost_priced > 0 else None,
        realised_pnl=sum(x.realised for x in res.positions.values()),
        dividends=sum(x.dividends for x in res.positions.values()),
        cash=p.opening_cash + res.cash_delta,
        xirr=xirr_val, xirr_reason=xirr_reason, holdings=len(held), unpriced=unpriced,
    )  # fmt: skip
    return PortfolioView(portfolio=_out(session, p), as_of=today, summary=summary, positions=positions,
                         allocation=alloc, warnings=res.warnings)  # fmt: skip


@router.get("/portfolios/{pid}/view")
def portfolio_view(pid: int, session: SessionDep, config: ConfigDep) -> PortfolioView:
    """Summary cards, positions with our grade / zone / fair-value gap, sector allocation and
    flags (grade dropped, zone turned Premium, alert fired recently)."""
    return build_view(session, _portfolio(session, pid), config, date.today())


Kind = Literal["buy", "sell", "dividend", "bonus", "split"]
