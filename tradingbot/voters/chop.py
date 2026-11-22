"""Chop voter: is the market actually trending, or just wobbling in a range?

Kaufman's efficiency ratio over N bars: |net move| / sum of |daily moves|. 1.0 = a straight line, ~0.1 = noise.
Breakouts in a choppy tape get stopped out (eight of sixteen live trend trades in Sep-Oct 2026, all in weeks when
the index went nowhere). This is a FILTER voter: +1 when the ratio is at or above `min_er`, -1 otherwise.
`source` = "benchmark" (the index, default) or "self" (the symbol's own bars).
"""
from __future__ import annotations
import pandas as pd

from .base import Voter, Context


def efficiency_ratio(close: pd.Series, n: int) -> pd.Series:
    net = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n, min_periods=n).sum()
    return net / path.replace(0, float("nan"))


class ChopVoter(Voter):
    name = "chop"
    description = "Efficiency ratio of the index (or the symbol) over N bars: +1 when trending (>= min_er), -1 when choppy."
    directional = False

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        n = int(self.cfg.get("lookback", 20))
        min_er = float(self.cfg.get("min_er", 0.3))
        src = str(self.cfg.get("source", "benchmark"))
        if src == "benchmark" and ctx.benchmark is not None and len(ctx.benchmark):
            er = efficiency_ratio(ctx.benchmark["close"], n).reindex(bars.index, method="ffill")
        else:
            er = efficiency_ratio(bars["close"], n)
        s = pd.Series(-1.0, index=bars.index)
        s[er >= min_er] = 1.0
        s[er.isna()] = float("nan")
        return s


VOTER = ChopVoter
