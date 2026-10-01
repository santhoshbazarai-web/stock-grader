"""yfinance provider on saved sample files (tests/fixtures/yfinance); a fake Ticker replays
them, so yfinance never touches the network."""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.core.config import Provider, load_config
from app.data.canonical import fields_for
from app.data.providers.base import (
    CorporateActionsProvider,
    FundamentalsProvider,
    IndexPriceProvider,
    PriceProvider,
    ProviderError,
    ProviderUnavailable,
)
from app.data.providers.yf import (
    LIMITED_ANNUAL,
    YFinanceProvider,
    build_yfinance_provider,
    to_yf_symbol,
    unadjust_splits,
)
from tests.conftest import REPO_CONFIG_DIR

FIX = Path(__file__).parent / "fixtures" / "yfinance"
INDEX_TICKERS = {"NIFTY500": "^CRSLDX"}


def _series(name: str) -> pd.Series:
    s = pd.read_csv(FIX / name, index_col=0).iloc[:, 0]
    s.index = pd.to_datetime(s.index, utc=True).tz_convert("Asia/Kolkata")
    return s


def _statement(name: str) -> pd.DataFrame:
    df = pd.read_csv(FIX / name, index_col=0)
    df.columns = pd.to_datetime(df.columns)
    return df


@dataclass
class FakeTicker:
    ticker: str
    calls: list[str] = field(default_factory=list)
    fail: Exception | None = None
    price: float | None = 1450.0

    def _hit(self, what: str) -> None:
        self.calls.append(what)
        if self.fail:
            raise self.fail

    def history(self, **kwargs: Any) -> pd.DataFrame:
        self._hit(f"history {kwargs}")
        df = pd.read_csv(FIX / "history_sample.csv", index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Kolkata")
        start = pd.Timestamp(kwargs["start"], tz="Asia/Kolkata")
        end = pd.Timestamp(kwargs["end"], tz="Asia/Kolkata")  # exclusive, like yfinance
        return df[(df.index >= start) & (df.index < end)]

    @property
    def splits(self) -> pd.Series:
        self._hit("splits")
        return _series("splits_sample.csv")

    @property
    def dividends(self) -> pd.Series:
        self._hit("dividends")
        return _series("dividends_sample.csv")

    @property
    def fast_info(self) -> dict[str, Any]:
        self._hit("fast_info")
        return {"last_price": self.price}

    def get_income_stmt(self, *, pretty: bool = False, freq: str = "yearly") -> pd.DataFrame:
        assert pretty is False
        self._hit(f"income {freq}")
        return _statement("income_yearly.csv" if freq == "yearly" else "income_quarterly.csv")

    def get_balance_sheet(self, *, pretty: bool = False, freq: str = "yearly") -> pd.DataFrame:
        self._hit(f"balance {freq}")
        return _statement("balance_yearly.csv")

    def get_cashflow(self, *, pretty: bool = False, freq: str = "yearly") -> pd.DataFrame:
        self._hit(f"cashflow {freq}")
        return _statement("cashflow_yearly.csv")


@dataclass
class Factory:
    tickers: dict[str, FakeTicker] = field(default_factory=dict)
    fail: Exception | None = None

    def __call__(self, ticker: str) -> FakeTicker:
        t = self.tickers.setdefault(ticker, FakeTicker(ticker, fail=self.fail))
        return t


@dataclass
class CountingLimiter:
    calls: list[Provider] = field(default_factory=list)

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        self.calls.append(provider)


def provider(
    factory: Factory | None = None, limiter: CountingLimiter | None = None
) -> YFinanceProvider:
    return YFinanceProvider(INDEX_TICKERS, limiter=limiter, ticker_factory=factory or Factory())


def test_protocols() -> None:
    p = provider()
    for proto in (
        PriceProvider,
        IndexPriceProvider,
        FundamentalsProvider,
        CorporateActionsProvider,
    ):
        assert isinstance(p, proto)
    assert p.name is Provider.YFINANCE


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [("TCS", "TCS.NS"), ("m&m", "M&M.NS"), ("BAJAJ-AUTO", "BAJAJ-AUTO.NS"), ("^NSEI", "^NSEI")],
)
def test_ticker_mapping(symbol: str, ticker: str) -> None:
    assert to_yf_symbol(symbol) == ticker


# ───────────── prices ─────────────


def test_daily_ohlcv_reverses_yahoo_split_adjustment() -> None:
    f = Factory()
    df = provider(f).daily_ohlcv("SAMPLE", date(2024, 6, 3), date(2024, 6, 14))

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is None and df.index[0] == pd.Timestamp("2024-06-03")
    # raw close 1000 before the 1:1 bonus on 5 Jun, 500 after (Yahoo shows 100 throughout)
    assert df.loc["2024-06-04", "close"] == pytest.approx(1000.0)
    assert df.loc["2024-06-05", "close"] == pytest.approx(500.0)
    assert df.loc["2024-06-04", "volume"] == 1000 and df.loc["2024-06-14", "volume"] == 2000
    assert df["volume"].dtype == "int64"
    [history_call] = [c for c in f.tickers["SAMPLE.NS"].calls if c.startswith("history")]
    assert "'auto_adjust': False" in history_call and "'end': '2024-06-15'" in history_call


def test_unadjust_with_no_splits_is_identity() -> None:
    df = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [10]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-01")]),
    )
    pd.testing.assert_frame_equal(unadjust_splits(df, pd.Series(dtype=float)), df)


def test_index_ohlcv_uses_configured_ticker_and_no_unadjust() -> None:
    f = Factory()
    df = provider(f).index_ohlcv("NIFTY 500", date(2024, 6, 3), date(2024, 6, 14))
    assert "^CRSLDX" in f.tickers
    assert "splits" not in f.tickers["^CRSLDX"].calls
    assert df.loc["2024-06-04", "close"] == pytest.approx(100.0)


def test_unknown_index_unavailable() -> None:
    with pytest.raises(ProviderUnavailable, match="NIFTYSMALLCAP"):
        provider().index_ohlcv("NIFTYSMALLCAP", date(2024, 1, 1), date(2024, 1, 5))


def test_empty_history_gives_empty_frame() -> None:
    df = provider().daily_ohlcv("SAMPLE", date(2020, 1, 1), date(2020, 1, 31))
    assert df.empty and list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_library_errors_become_transient_provider_errors() -> None:
    with pytest.raises(ProviderError, match="RuntimeError: Too Many Requests") as info:
        provider(Factory(fail=RuntimeError("Too Many Requests"))).daily_ohlcv(
            "SAMPLE", date(2024, 6, 3), date(2024, 6, 14)
        )
    assert not isinstance(info.value, ProviderUnavailable)


def test_extra_requests_are_rate_limited() -> None:
    limiter = CountingLimiter()
    provider(limiter=limiter).daily_ohlcv("SAMPLE", date(2024, 6, 3), date(2024, 6, 14))
    assert limiter.calls == [Provider.YFINANCE]  # history (router-paid) + splits


def test_ltp() -> None:
    f = Factory()
    f.tickers["NOPRICE.NS"] = FakeTicker("NOPRICE.NS", price=None)
    assert provider(f).ltp(["SAMPLE", "NOPRICE"]) == {"SAMPLE": 1450.0}


# ───────────── corporate actions ─────────────


def test_corporate_actions() -> None:
    df = provider().corporate_actions("SAMPLE", date(2023, 1, 1), date(2024, 12, 31))
    rows = df.to_dict("records")
    assert [(r["ex_date"], r["action_type"]) for r in rows] == [
        (date(2023, 5, 31), "dividend"),
        (date(2023, 10, 19), "dividend"),
        (date(2024, 6, 5), "split"),
        (date(2024, 9, 2), "split"),
    ]
    assert rows[1]["dividend_per_share"] == 9.0  # two payouts on one ex-date summed
    assert (rows[2]["ratio_old"], rows[2]["ratio_new"]) == (1.0, 2.0)
    assert "bonus" in df.attrs["warnings"][0].lower()


def test_corporate_actions_range_filter() -> None:
    df = provider().corporate_actions("SAMPLE", date(2024, 1, 1), date(2024, 6, 30))
    assert list(df["ex_date"]) == [date(2024, 6, 5)]


# ───────────── fundamentals ─────────────


def test_annual_mapped_to_canonical_crore() -> None:
    df = provider().annual("SAMPLE")

    assert list(df.index) == [pd.Timestamp(f"{y}-03-31") for y in (2021, 2022, 2023, 2024)]
    fy24 = df.loc["2024-03-31"]
    assert fy24["revenue"] == pytest.approx(1000.0)
    assert fy24["pat"] == pytest.approx(232.0)  # NetIncomeCommonStockholders wins over NetIncome
    assert fy24["minority_interest_pl"] == pytest.approx(1.0)  # sign flipped to positive
    assert fy24["eps_diluted"] == pytest.approx(11.6)  # per-share: not scaled
    assert fy24["shares_diluted_cr"] == pytest.approx(20.0)
    assert fy24["purchase_of_fixed_assets"] == pytest.approx(120.0)  # outflow → positive
    assert fy24["dividends_paid"] == pytest.approx(50.0)
    assert fy24["non_operating_investments"] == pytest.approx(200.0)  # first label wins
    assert fy24["fiscal_year"] == 2024
    fy23 = df.loc["2023-03-31"]
    assert fy23["non_operating_investments"] == pytest.approx(190.0)  # falls back to 2nd label
    assert pd.isna(fy23["sale_of_fixed_assets"])  # missing stays missing, never 0
    assert set(fields_for("fin_annual")) - {"book_value_per_share"} <= set(df.columns)


def test_annual_flags_limited_history() -> None:
    df = provider().annual("SAMPLE")
    assert df.attrs["limited_history"] is True
    assert LIMITED_ANNUAL in df.attrs["warnings"]
    assert df.attrs["statement_type"] == "consolidated"


def test_quarterly_uses_income_statement_only() -> None:
    f = Factory()
    df = provider(f).quarterly("SAMPLE")
    assert len(df) == 4 and df.loc["2024-03-31", "revenue"] == pytest.approx(270.0)
    assert df.loc["2024-03-31", "eps_diluted"] == pytest.approx(3.2)
    assert not any(c.startswith(("balance", "cashflow")) for c in f.tickers["SAMPLE.NS"].calls)
    assert "fiscal_year" not in df.columns


def test_factory_from_config() -> None:
    cfg = load_config(REPO_CONFIG_DIR).providers
    p = build_yfinance_provider(cfg, None)
    assert isinstance(p, YFinanceProvider)
    assert cfg.yfinance_index_tickers["NIFTY500"] == "^CRSLDX"
