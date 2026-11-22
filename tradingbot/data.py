"""Fetch and cache daily bars.

Provider "auto": uses Alpaca if ALPACA_API_KEY is set in .env, otherwise
Yahoo Finance (yfinance), which needs no account. Both return the same shape:
DataFrame indexed by date with columns open, high, low, close, volume.
Cached as CSV in data/ so repeated backtests do not hit the network.
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from .config import DATA_DIR, ROOT

load_dotenv(ROOT / ".env")

COLUMNS = ["open", "high", "low", "close", "volume"]


def alpaca_keys() -> tuple[str, str] | None:
    k, s = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
    return (k, s) if k and s else None


def provider_name(settings: dict) -> str:
    p = settings.get("data_provider", "auto")
    if p == "auto":
        return "alpaca" if alpaca_keys() else "yfinance"
    return p


def _fetch_yfinance(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    raw = yf.download(symbols, start=start, end=end, auto_adjust=True, group_by="ticker", progress=False, threads=True)
    out = {}
    for sym in symbols:
        try:
            df = raw[sym] if len(symbols) > 1 else raw
            if isinstance(df.columns, pd.MultiIndex):   # a single-symbol download still comes back grouped
                df = df.droplevel(0, axis=1) if df.columns.nlevels > 1 else df
            df = df.rename(columns=str.lower)[COLUMNS].dropna()
        except (KeyError, TypeError, ValueError):       # unknown/delisted symbol: skip it, keep the rest
            continue
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        df.index.name = "date"
        # Yahoo returns today's bar while the session is still running. A partial bar is
        # not a finished day, so drop it; tomorrow's refresh will fetch the complete one.
        df = df[df.index < pd.Timestamp(datetime.now().date())]
        if len(df):
            out[sym] = df
    return out


def _fetch_alpaca(symbols: list[str], start: str, end: str, timeframe: str) -> dict[str, pd.DataFrame]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment

    key, secret = alpaca_keys()
    client = StockHistoricalDataClient(key, secret)
    tf = {"1Day": TimeFrame.Day, "1Hour": TimeFrame.Hour, "1Min": TimeFrame.Minute}[timeframe]
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=tf,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        feed=DataFeed.IEX,
        adjustment=Adjustment.ALL,
    )
    df = client.get_stock_bars(req).df
    out = {}
    if df is None or len(df) == 0:
        return out
    for sym, g in df.groupby(level=0):
        g = g.droplevel(0)[COLUMNS].copy()
        g.index = pd.to_datetime(g.index).tz_convert("America/New_York").tz_localize(None).normalize()
        g.index.name = "date"
        out[sym] = g
    return out


def _cache_path(sym: str, timeframe: str, cache_dir: Path | None = None) -> Path:
    return (cache_dir or DATA_DIR) / f"{sym}_{timeframe}.csv"


def load_bars(symbols: list[str], start: str, end: str, settings: dict, refresh: bool = False,
              cache_dir: Path | None = None) -> dict[str, pd.DataFrame]:
    """Return {symbol: bars} covering [start, end]. Uses cache unless stale or refresh=True.

    cache_dir: where CSVs live. The backtests use data/ (frozen research data). The paper
    runner passes data/live so its daily refresh never rewrites the research cache.
    """
    cache_dir = Path(cache_dir) if cache_dir else DATA_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    timeframe = settings.get("timeframe", "1Day")
    end_dt = pd.Timestamp(end)
    start_dt = pd.Timestamp(start)

    result, missing = {}, []
    for sym in symbols:
        p = _cache_path(sym, timeframe, cache_dir)
        if p.exists() and not refresh:
            df = pd.read_csv(p, index_col="date", parse_dates=True)
            # Only staleness at the END triggers a refetch. A symbol that listed after
            # `start` (e.g. an IPO) would otherwise be refetched on every run. To extend
            # history backwards, run scripts/fetch_data.py --refresh.
            covers_end = df.index.max() >= end_dt - timedelta(days=4)
            if covers_end:
                result[sym] = df.loc[start_dt:end_dt]
                continue
        missing.append(sym)

    if missing:
        fetch_end = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        # never shrink the cache: if a symbol's file already starts earlier than `start`, refetch from there
        earliest = start_dt
        for sym in missing:
            p = _cache_path(sym, timeframe, cache_dir)
            if p.exists():
                try:
                    first = pd.read_csv(p, nrows=1, index_col="date", parse_dates=True).index[0]
                    earliest = min(earliest, pd.Timestamp(first))
                except Exception:  # noqa: BLE001
                    pass
        fetch_start = earliest.strftime("%Y-%m-%d")
        prov = provider_name(settings)
        print(f"[data] fetching {len(missing)} symbols from {prov} ({fetch_start} -> {end})")
        if prov == "alpaca":
            fetched = _fetch_alpaca(missing, fetch_start, fetch_end, timeframe)
        else:
            fetched = _fetch_yfinance(missing, fetch_start, fetch_end)
        for sym, df in fetched.items():
            df.to_csv(_cache_path(sym, timeframe, cache_dir))
            result[sym] = df.loc[start_dt:end_dt]
        for sym in missing:
            if sym not in fetched:
                p = _cache_path(sym, timeframe, cache_dir)
                if p.exists():   # Yahoo hiccup on a refresh: keep the cached history (the runner's stale check judges its age)
                    result[sym] = pd.read_csv(p, index_col="date", parse_dates=True).loc[start_dt:end_dt]
                    print(f"[data] WARNING refresh failed for {sym}; using the cached bars (last {result[sym].index.max().date()})")
                else:
                    print(f"[data] WARNING no data for {sym}")
    return result
