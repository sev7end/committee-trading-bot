"""Intraday (5-minute) bars from Alpaca's free IEX historical feed, cached as CSV.

Regular session only (09:30-16:00 New York). Timestamps are converted to New York time and
kept tz-naive. Note the IEX feed only sees trades on that one exchange (~3% of volume), so
bar volume is small and thin names have gaps; for SPY/QQQ and mega-caps it is usable.
"""
from __future__ import annotations
import datetime as dt
from pathlib import Path

import pandas as pd

from .config import DATA_DIR

INTRA_DIR = DATA_DIR / "intraday"
NY = "America/New_York"


def _path(sym: str, tf: str) -> Path:
    return INTRA_DIR / f"{sym}_{tf}.csv.gz"


def fetch_intraday(sym: str, start: str, end: str, minutes: int = 5) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed, Adjustment
    from .data import alpaca_keys

    client = StockHistoricalDataClient(*alpaca_keys())
    req = StockBarsRequest(symbol_or_symbols=[sym], timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
                           start=dt.datetime.fromisoformat(start), end=dt.datetime.fromisoformat(end),
                           feed=DataFeed.IEX, adjustment=Adjustment.ALL, limit=None)
    df = client.get_stock_bars(req).df
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = df.droplevel(0)[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index).tz_convert(NY).tz_localize(None)
    df.index.name = "ts"
    t = df.index.time
    df = df[(t >= dt.time(9, 30)) & (t < dt.time(16, 0))]
    return df


def load_intraday(sym: str, start: str, end: str, minutes: int = 5, refresh: bool = False) -> pd.DataFrame:
    INTRA_DIR.mkdir(parents=True, exist_ok=True)
    tf = f"{minutes}Min"
    p = _path(sym, tf)
    if p.exists() and not refresh:
        df = pd.read_csv(p, index_col="ts", parse_dates=True)
        if len(df) and df.index.max() >= pd.Timestamp(end) - pd.Timedelta(days=5):
            return df.loc[start:end]
    print(f"[intraday] fetching {sym} {tf} {start} -> {end} ...", flush=True)
    df = fetch_intraday(sym, start, end, minutes)
    df.to_csv(p, compression="gzip")
    return df.loc[start:end]


def quality_report(df: pd.DataFrame) -> dict:
    """Bars per day and gap statistics: a regular 5-minute session has 78 bars."""
    if len(df) == 0:
        return {"days": 0}
    per_day = df.groupby(df.index.date).size()
    return {"days": int(len(per_day)), "first": str(df.index.min().date()), "last": str(df.index.max().date()),
            "median_bars_per_day": float(per_day.median()), "pct_days_full": float((per_day >= 70).mean() * 100),
            "median_bar_volume": float(df["volume"].median())}
