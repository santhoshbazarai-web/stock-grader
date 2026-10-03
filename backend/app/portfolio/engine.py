"""Manual portfolio maths (pure; no DB or network). Nothing here reads broker positions.

Method: average cost. A buy adds ``price * qty + fees`` to the cost; a sell realises
``price * qty - fees - avg_cost * qty`` and removes that share of the cost; a dividend is
income of ``qty * dps`` (qty defaults to the shares held that day). Bonus and splits come from
the stored corporate actions (``ratio_new`` shares for every ``ratio_old`` held): a split
multiplies the quantity and divides the average cost; a bonus adds shares at zero cost. A
manual bonus / split transaction is used only when no stored action of that type lies within
``MANUAL_MATCH_DAYS`` of its date (so the same event is never applied twice); it encodes the
ratio as quantity = ratio_new, price = ratio_old. Same-day order: corporate actions, then
buys, bonus / split, dividends, sells. A sell beyond the shares held is reported in
``warnings`` and capped. A holding without a price has no market value: never 0 (rule 1).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from scipy.optimize import brentq

MANUAL_MATCH_DAYS = 7  # a manual bonus / split this close to a stored one is its duplicate


@dataclass(frozen=True)
class Txn:
    instrument_id: int
    kind: str  # buy | sell | dividend | bonus | split
    on: date
    quantity: float | None
    price: float | None
    fees: float = 0.0


@dataclass(frozen=True)
class Action:
    instrument_id: int
    kind: str  # bonus | split
    ex_date: date
    ratio_old: float
    ratio_new: float


@dataclass
class Position:
    instrument_id: int
    qty: float = 0.0
    cost: float = 0.0
    realised: float = 0.0
    dividends: float = 0.0
    first_buy: date | None = None

    @property
    def avg_cost(self) -> float | None:
        return self.cost / self.qty if self.qty > 1e-9 else None


@dataclass
class Result:
    positions: dict[int, Position]
    cash_flows: list[tuple[date, float]] = field(default_factory=list)  # investor view
    cash_delta: float = 0.0  # sells + dividends - buys - fees
    warnings: list[str] = field(default_factory=list)


_ORDER = {"action": 0, "buy": 1, "bonus": 2, "split": 2, "dividend": 3, "sell": 4}


def _split_or_bonus(p: Position, kind: str, old: float, new: float) -> None:
    if p.qty <= 1e-9 or old <= 0 or new <= 0:
        return
    p.qty = p.qty * new / old if kind == "split" else p.qty + p.qty * new / old


def run(txns: list[Txn], actions: list[Action]) -> Result:
    """Replay transactions and stored corporate actions in date order."""
    stored: dict[tuple[int, str], list[date]] = defaultdict(list)
    for a in actions:
        stored[(a.instrument_id, a.kind)].append(a.ex_date)
    events: list[tuple[date, int, int, object]] = []
    res = Result(positions={})
    for i, t in enumerate(txns):
        if t.kind in ("bonus", "split"):
            near = stored.get((t.instrument_id, t.kind), [])
            if any(abs((t.on - d).days) <= MANUAL_MATCH_DAYS for d in near):
                res.warnings.append(
                    f"{t.kind} on {t.on} ignored: the stored corporate action already covers it"
                )
                continue
        events.append((t.on, _ORDER[t.kind], i, t))
    for i, a in enumerate(actions):
        events.append((a.ex_date, _ORDER["action"], len(txns) + i, a))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    for on, _, _, ev in events:
        if isinstance(ev, Action):
            p = res.positions.setdefault(ev.instrument_id, Position(ev.instrument_id))
            _split_or_bonus(p, ev.kind, ev.ratio_old, ev.ratio_new)
            continue
        assert isinstance(ev, Txn)
        t = ev
        p = res.positions.setdefault(t.instrument_id, Position(t.instrument_id))
        if t.kind == "buy":
            if not t.quantity or t.price is None:
                res.warnings.append(f"buy on {on} skipped: quantity and price are required")
                continue
            amount = t.quantity * t.price + t.fees
            p.qty += t.quantity
            p.cost += amount
            p.first_buy = p.first_buy or on
            res.cash_flows.append((on, -amount))
            res.cash_delta -= amount
        elif t.kind == "sell":
            if not t.quantity or t.price is None:
                res.warnings.append(f"sell on {on} skipped: quantity and price are required")
                continue
            qty = min(t.quantity, p.qty)
            if qty < t.quantity - 1e-9:
                res.warnings.append(
                    f"sell of {t.quantity:g} on {on} exceeds the {p.qty:g} held: capped"
                )
            if qty <= 1e-9:
                continue
            avg = p.cost / p.qty
            proceeds = qty * t.price - t.fees
            p.realised += proceeds - avg * qty
            p.cost -= avg * qty
            p.qty -= qty
            if p.qty < 1e-9:
                p.qty, p.cost = 0.0, 0.0
            res.cash_flows.append((on, proceeds))
            res.cash_delta += proceeds
        elif t.kind == "dividend":
            if t.price is None:
                res.warnings.append(f"dividend on {on} skipped: dividend per share is required")
                continue
            qty = t.quantity if t.quantity else p.qty
            if qty <= 1e-9:
                res.warnings.append(f"dividend on {on} skipped: no shares held")
                continue
            amount = qty * t.price - t.fees
            p.dividends += amount
            res.cash_flows.append((on, amount))
            res.cash_delta += amount
        else:  # manual bonus / split: quantity = ratio_new, price = ratio_old
            if not t.quantity or not t.price:
                res.warnings.append(f"{t.kind} on {on} skipped: ratio needs both numbers")
                continue
            _split_or_bonus(p, t.kind, t.price, t.quantity)
    return res


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annualised internal rate of return of dated cash flows (365-day year); ``None`` when it
    is undefined (no sign change, a single date, or no solution)."""
    flows = [(d, a) for d, a in flows if a]
    if len(flows) < 2 or not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        return None
    d0 = min(d for d, _ in flows)

    def f(r: float) -> float:
        return float(sum(a / (1 + r) ** ((d - d0).days / 365.0) for d, a in flows))

    try:
        return float(brentq(f, -0.9999, 100.0, maxiter=200))
    except ValueError:
        return None
