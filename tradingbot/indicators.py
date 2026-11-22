"""Backward-looking indicator functions.

Every function here may only use the current bar and bars BEFORE it. Never
shift(-1) or look at future rows. tests/test_no_lookahead.py checks this by
truncating the data and confirming values do not change.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average: mean of the last n values."""
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average: recent values weigh more."""
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    parts = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    return parts.max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range: how far price typically moves in one bar."""
    return true_range(df).rolling(n, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Relative Strength Index, 0..100. Above 70 = stretched up, below 30 = stretched down."""
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(n, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).rolling(n, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # a window with gains and no losses is RSI 100 by definition (not undefined); no movement at all is neutral 50
    out = out.mask((loss == 0) & (gain > 0), 100.0)
    out = out.mask((loss == 0) & (gain == 0), 50.0)
    return out


def prior_rolling_max(s: pd.Series, n: int) -> pd.Series:
    """Highest value of the PREVIOUS n bars (excludes the current bar)."""
    return s.shift(1).rolling(n, min_periods=n).max()


def prior_rolling_min(s: pd.Series, n: int) -> pd.Series:
    return s.shift(1).rolling(n, min_periods=n).min()


def volume_ratio(v: pd.Series, n: int) -> pd.Series:
    """Today's volume divided by the average of the previous n bars."""
    return v / v.shift(1).rolling(n, min_periods=n).mean()
