"""Pullback voter: how far has price dropped BELOW its short moving average, measured in ATRs?

Mean-reversion logic: a stock in a long-term uptrend that has just fallen sharply below its
20-day average tends to snap back. +1 at a deep dip (>= full_atr ATRs below the average),
0 at or above the average. It never votes negative; the committee's other voters decide
whether the stock deserves to be bought at all.
"""
from __future__ import annotations
import pandas as pd

from ..indicators import atr, sma
from .base import Voter, Context


class PullbackVoter(Voter):
    name = "pullback"
    description = "Depth of the dip below the N-day average in ATRs: 0 at the average, +1 at full_atr ATRs below."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        n = int(self.cfg.get("sma", 20))
        full = max(float(self.cfg.get("full_atr", 2.0)), 1e-6)
        a = atr(bars, int(self.cfg.get("atr_period", 14)))
        depth = (sma(bars["close"], n) - bars["close"]) / a
        s = (depth / full).clip(lower=0.0, upper=1.0)
        s[depth.isna()] = float("nan")
        return s


VOTER = PullbackVoter
