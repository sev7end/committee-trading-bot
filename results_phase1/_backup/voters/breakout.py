"""Breakout voter: did today's close push above the highest high of the previous N bars?"""
from __future__ import annotations
import pandas as pd

from ..indicators import prior_rolling_max, prior_rolling_min
from .base import Voter, Context


class BreakoutVoter(Voter):
    name = "breakout"
    description = "Close above the prior N-bar high = +1, below the prior N-bar low = -1."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        n = int(self.cfg.get("lookback", 20))
        hi = prior_rolling_max(bars["high"], n)
        lo = prior_rolling_min(bars["low"], n)
        s = pd.Series(0.0, index=bars.index)
        s[bars["close"] > hi] = 1.0
        s[bars["close"] < lo] = -1.0
        s[hi.isna()] = float("nan")
        return s


VOTER = BreakoutVoter
