"""Regenerate yfinance sample CSVs: ``uv run python tests/fixtures/yfinance/make_samples.py``.

Shapes match what ``yfinance.Ticker`` returns (1.x): statements via ``get_*(pretty=False)``
have raw keys as rows and period-end columns, amounts in absolute rupees; ``history()`` has a
tz-aware Asia/Kolkata index and **split-adjusted** OHLCV; ``splits``/``dividends`` are Series.

Fictional SAMPLE.NS: raw close 1000 until a 1:1 bonus (Yahoo: split 2.0) on 2024-06-05, then
500; a later 5:1 split on 2024-09-02. Yahoo therefore shows an adjusted close of 100 throughout
June and volume x10 before the bonus / x5 after.
"""

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
TZ = "Asia/Kolkata"

days = pd.bdate_range("2024-06-03", "2024-06-14", tz=TZ)
raw_close = np.where(days < pd.Timestamp("2024-06-05", tz=TZ), 1000.0, 500.0)
raw_vol = np.where(days < pd.Timestamp("2024-06-05", tz=TZ), 1000, 2000)
factor = np.where(days < pd.Timestamp("2024-06-05", tz=TZ), 10.0, 5.0)
hist = pd.DataFrame(
    {
        "Open": raw_close / factor, "High": raw_close * 1.01 / factor,
        "Low": raw_close * 0.99 / factor, "Close": raw_close / factor,
        "Adj Close": raw_close / factor * 0.98, "Volume": raw_vol * factor,
    },
    index=pd.DatetimeIndex(days, name="Date"),
)  # fmt: skip
hist.to_csv(HERE / "history_sample.csv")

pd.Series(
    [2.0, 5.0],
    index=pd.DatetimeIndex(
        pd.to_datetime(["2024-06-05", "2024-09-02"]).tz_localize(TZ), name="Date"
    ),
    name="Stock Splits",
).to_csv(HERE / "splits_sample.csv")
pd.Series(
    [12.0, 6.0, 3.0],
    index=pd.DatetimeIndex(
        pd.to_datetime(["2023-05-31", "2023-10-19", "2023-10-19"]).tz_localize(TZ), name="Date"
    ),
    name="Dividends",
).to_csv(HERE / "dividends_sample.csv")

periods = pd.to_datetime(["2024-03-31", "2023-03-31", "2022-03-31", "2021-03-31", "2020-03-31"])


def statement(rows: dict[str, list[float | None]]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=periods).T


g = [1.0, 1 / 1.1, 1 / 1.21, 1 / 1.331]  # FY24..FY21; FY20 column is all-NaN (dropped)
income = statement({
    "TotalRevenue": [1.0e10 * x for x in g] + [None],
    "CostOfRevenue": [3.9e9 * x for x in g] + [None],
    "EBITDA": [3.3e9 * x for x in g] + [None],
    "EBIT": [3.2e9 * x for x in g] + [None],
    "OtherNonOperatingIncomeExpenses": [2.0e8 * x for x in g] + [None],
    "ReconciledDepreciation": [3.0e8 * x for x in g] + [None],
    "InterestExpense": [1.0e8 * x for x in g] + [None],
    "PretaxIncome": [3.1e9 * x for x in g] + [None],
    "TaxProvision": [7.8e8 * x for x in g] + [None],
    "NetIncomeCommonStockholders": [2.32e9 * x for x in g] + [None],
    "NetIncome": [2.42e9 * x for x in g] + [None],
    "MinorityInterests": [-1.0e7 * x for x in g] + [None],
    "DilutedEPS": [11.6 * x for x in g] + [None],
    "DilutedAverageShares": [2.0e8] * 4 + [None],
})  # fmt: skip
income.to_csv(HERE / "income_yearly.csv")
balance = statement({
    "TotalAssets": [1.5e10, 1.4e10, None, None, None],
    "CurrentLiabilities": [2.5e9, 2.3e9, None, None, None],
    "StockholdersEquity": [1.0e10, 9.0e9, None, None, None],
    "MinorityInterest": [5.0e7, 4.0e7, None, None, None],
    "TotalDebt": [1.5e9, 1.6e9, None, None, None],
    "CashAndCashEquivalents": [9.0e8, 8.0e8, None, None, None],
    "LongTermEquityInvestment": [2.0e9, None, None, None, None],
    "InvestmentsAndAdvances": [2.5e9, 1.9e9, None, None, None],
    "AccountsReceivable": [1.8e9, 1.7e9, None, None, None],
    "Inventory": [1.2e9, 1.1e9, None, None, None],
    "AccountsPayable": [8.0e8, 7.5e8, None, None, None],
    "NetPPE": [6.0e9, 5.5e9, None, None, None],
})  # fmt: skip
balance.to_csv(HERE / "balance_yearly.csv")
cashflow = statement({
    "OperatingCashFlow": [2.8e9, 2.5e9, None, None, None],
    "PurchaseOfPPE": [-1.2e9, -1.0e9, None, None, None],
    "SaleOfPPE": [5.0e7, None, None, None, None],
    "CashDividendsPaid": [-5.0e8, -4.5e8, None, None, None],
})  # fmt: skip
cashflow.to_csv(HERE / "cashflow_yearly.csv")

qperiods = pd.to_datetime(["2024-03-31", "2023-12-31", "2023-09-30", "2023-06-30"])
pd.DataFrame(
    {
        "TotalRevenue": [2.7e9, 2.6e9, 2.5e9, 2.4e9],
        "EBITDA": [9.0e8, 8.6e8, 8.2e8, 7.9e8],
        "PretaxIncome": [8.5e8, 8.1e8, 7.8e8, 7.4e8],
        "NetIncomeCommonStockholders": [6.4e8, 6.1e8, 5.8e8, 5.5e8],
        "DilutedEPS": [3.2, 3.05, 2.9, 2.75],
    },
    index=qperiods,
).T.to_csv(HERE / "income_quarterly.csv")
