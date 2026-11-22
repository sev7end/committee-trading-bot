"""RSI voter: short-term Relative Strength Index. Very low = oversold = vote to buy.

RSI runs 0..100. Score is +1 when RSI <= low, -1 when RSI >= high, linear in between,
so a 2-3 day RSI under 10 says "washed out", over 90 says "stretched".
"""
from __future__ import annotations
import pandas as pd

from ..indicators import rsi
from .base import Voter, Context


class RsiVoter(Voter):
    name = "rsi"
    description = "Short RSI(period): +1 at or below `low`, -1 at or above `high`, linear between."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        n = int(self.cfg.get("period", 3))
        lo = float(self.cfg.get("low", 15))
        hi = float(self.cfg.get("high", 85))
        r = rsi(bars["close"], n)
        mid = (lo + hi) / 2
        s = self.clip((mid - r) / max(mid - lo, 1e-6))
        s[r.isna()] = float("nan")
        return s


VOTER = RsiVoter
