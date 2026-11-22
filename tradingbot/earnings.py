"""Earnings-date blackout: no NEW entries in a stock around its earnings report.

Why: the bot's stop cannot protect against an overnight earnings gap. Positions already open
are left alone (their bracket orders still work); only new entries are blocked.

Dates come from Yahoo (yfinance get_earnings_dates, up to 100 per symbol = ~25 years) and are
cached in data/earnings/<SYM>.csv. refresh_earnings() re-downloads anything older than `max_age_days`.

Params (per book, under "entry"):
  "earnings_blackout": {"enabled": true, "days_before": 3, "days_after": 1}
A signal on day D is blocked if an earnings date E exists with D in [E - days_before, E + days_after]
(counted in trading days on the symbol's own calendar). ETFs and symbols without data are never blocked.
"""
from __future__ import annotations
import datetime as dt
from pathlib import Path

import pandas as pd

from .config import DATA_DIR

EARN_DIR = DATA_DIR / "earnings"


def _path(sym: str) -> Path:
    return EARN_DIR / f"{sym}.csv"


def load_earnings_dates(sym: str) -> pd.DatetimeIndex:
    p = _path(sym)
    if not p.exists():
        return pd.DatetimeIndex([])
    try:
        s = pd.read_csv(p)["date"]
        return pd.DatetimeIndex(pd.to_datetime(s)).normalize().unique().sort_values()
    except Exception:  # noqa: BLE001
        return pd.DatetimeIndex([])


def refresh_earnings(symbols: list[str], max_age_days: int = 7, verbose: bool = True) -> dict[str, int]:
    """Download/refresh earnings dates for symbols whose cache is missing or older than max_age_days."""
    import yfinance as yf

    EARN_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    now = dt.datetime.now()
    for sym in symbols:
        p = _path(sym)
        if p.exists() and (now - dt.datetime.fromtimestamp(p.stat().st_mtime)).days < max_age_days:
            out[sym] = len(load_earnings_dates(sym))
            continue
        try:
            ed = yf.Ticker(sym).get_earnings_dates(limit=100)
            if ed is None or len(ed) == 0:
                dates = pd.DatetimeIndex([])
            else:
                idx = pd.to_datetime(ed.index)
                idx = idx.tz_localize(None) if idx.tz is not None else idx
                dates = pd.DatetimeIndex(idx).normalize().unique().sort_values()
            pd.DataFrame({"date": [d.date().isoformat() for d in dates]}).to_csv(p, index=False)
            out[sym] = len(dates)
            if verbose:
                print(f"[earnings] {sym}: {len(dates)} dates")
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"[earnings] {sym}: failed ({type(e).__name__}: {str(e)[:80]})")
            out[sym] = len(load_earnings_dates(sym))
    return out


def blackout_mask(index: pd.DatetimeIndex, dates: pd.DatetimeIndex, days_before: int, days_after: int) -> pd.Series:
    """True on bars where a new entry is NOT allowed. Counted in bars of `index` (the symbol's trading calendar)."""
    mask = pd.Series(False, index=index)
    if len(dates) == 0 or len(index) == 0:
        return mask
    pos = index.searchsorted(dates)  # position of the first bar >= earnings date
    n = len(index)
    for p in pos:
        lo, hi = max(0, p - days_before), min(n, p + days_after + 1)
        if lo < n:
            mask.iloc[lo:hi] = True
    return mask


def is_blackout_now(sym: str, index: pd.DatetimeIndex, cfg: dict | None) -> bool:
    """Live check for the runner: is the latest bar inside a blackout window (including the next report)?"""
    if not cfg or not cfg.get("enabled"):
        return False
    dates = load_earnings_dates(sym)
    if len(dates) == 0 or len(index) == 0:
        return False
    last = index[-1]
    before, after = int(cfg.get("days_before", 3)), int(cfg.get("days_after", 1))
    # past/current reports: reuse the bar-based mask
    if bool(blackout_mask(index, dates[dates <= last], before, after).iloc[-1]):
        return True
    # upcoming report within `before` trading days: approximate with business days
    upcoming = dates[dates > last]
    if len(upcoming):
        bdays = len(pd.bdate_range(last, upcoming[0])) - 1
        return bdays <= before
    return False
