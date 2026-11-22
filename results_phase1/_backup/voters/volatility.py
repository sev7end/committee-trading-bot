"""Volatility voter: is the stock moving enough to be worth trading, but not so wildly the stop gets hit by noise?"""
from __future__ import annotations
import pandas as pd

from ..indicators import atr
from .base import Voter, Context


class VolatilityVoter(Voter):
    name = "volatility"
    description = "ATR as a percent of price must sit inside [atr_pct_min, atr_pct_max]; +1 inside, -1 outside."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        n = int(self.cfg.get("atr_period", 14))
        lo = float(self.cfg.get("atr_pct_min", 0.01))
        hi = float(self.cfg.get("atr_pct_max", 0.06))
        atr_pct = atr(bars, n) / bars["close"]
        s = pd.Series(-1.0, index=bars.index)
        s[(atr_pct >= lo) & (atr_pct <= hi)] = 1.0
        s[atr_pct.isna()] = float("nan")
        return s


VOTER = VolatilityVoter
