"""Trend voter: is price above its fast average, and is the fast average above the slow one?"""
from __future__ import annotations
import pandas as pd

from ..indicators import sma
from .base import Voter, Context


class TrendVoter(Voter):
    name = "trend"
    description = "Price above fast moving average and fast above slow = uptrend."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        fast = sma(bars["close"], int(self.cfg.get("fast", 20)))
        slow = sma(bars["close"], int(self.cfg.get("slow", 50)))
        s = pd.Series(0.0, index=bars.index)
        s += (bars["close"] > fast).astype(float) - (bars["close"] < fast).astype(float)
        s += (fast > slow).astype(float) - (fast < slow).astype(float)
        s = s / 2.0
        s[slow.isna()] = float("nan")
        return self.clip(s)


VOTER = TrendVoter
