"""Momentum voter: how much has this stock gained over the last N bars?"""
from __future__ import annotations
import pandas as pd

from .base import Voter, Context


class MomentumVoter(Voter):
    name = "momentum"
    description = "Return over the last N bars, scaled: +1 at a full_scale gain, -1 at a full_scale loss."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        n = int(self.cfg.get("lookback", 60))
        full_scale = max(float(self.cfg.get("full_scale", 0.30)), 1e-6)
        past = bars["close"].shift(n)
        ret = bars["close"] / past - 1.0
        s = self.clip(ret / full_scale)
        s[past.isna()] = float("nan")
        return s


VOTER = MomentumVoter
