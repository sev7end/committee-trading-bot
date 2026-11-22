"""Volatility-based position scaling.

Each trade already risks a fixed % of equity via ATR-sized stops. This module adds a second,
portfolio-level dial: when the market (or the instrument) is far more volatile than normal,
trade smaller; when it is calmer, trade a little larger. Configured per book in params:

  "risk": {
    "risk_per_trade_pct": 1.0,
    "vol_target": {"enabled": true, "mode": "benchmark", "lookback": 20,
                   "target_ann_vol": 0.15, "min_scale": 0.5, "max_scale": 1.5}
  }

mode "benchmark": scale = target_ann_vol / realised annualised vol of the benchmark (SPY) over `lookback` days.
mode "self":      scale = (1-year median of the instrument's own 20-day vol) / its current 20-day vol.
Both are clipped to [min_scale, max_scale] and are strictly backward-looking.
"""
from __future__ import annotations
import math

import numpy as np
import pandas as pd

DEFAULT = {"enabled": False, "mode": "benchmark", "lookback": 20, "target_ann_vol": 0.15, "min_scale": 0.5, "max_scale": 1.5, "median_window": 252}


def _ann_vol(close: pd.Series, lookback: int) -> pd.Series:
    return close.pct_change().rolling(lookback, min_periods=lookback).std() * math.sqrt(252)


def vol_scale_series(bars: pd.DataFrame, bench: pd.DataFrame | None, cfg: dict | None) -> pd.Series:
    """Multiplier for risk_per_trade, indexed like `bars`. All 1.0 when disabled."""
    c = {**DEFAULT, **(cfg or {})}
    ones = pd.Series(1.0, index=bars.index)
    if not c.get("enabled"):
        return ones
    lo, hi = float(c["min_scale"]), float(c["max_scale"])
    if c["mode"] == "self":
        v = _ann_vol(bars["close"], int(c["lookback"]))
        ref = v.rolling(int(c["median_window"]), min_periods=int(c["lookback"]) * 3).median()
        scale = (ref / v)
    else:
        if bench is None or len(bench) == 0:
            return ones
        v = _ann_vol(bench["close"], int(c["lookback"]))
        scale = (float(c["target_ann_vol"]) / v).reindex(bars.index, method="ffill")
    scale = scale.replace([np.inf, -np.inf], np.nan).clip(lower=lo, upper=hi)
    return scale.fillna(1.0)


def vol_scale_now(bars: pd.DataFrame, bench: pd.DataFrame | None, cfg: dict | None) -> float:
    """Scale for the most recent bar (used by the live runner)."""
    s = vol_scale_series(bars, bench, cfg)
    return float(s.iloc[-1]) if len(s) else 1.0
