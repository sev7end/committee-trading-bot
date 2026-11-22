"""Market regime voter: is the overall market (SPY) in an uptrend? Most momentum longs fail when it is not."""
from __future__ import annotations
import pandas as pd

from ..indicators import sma
from .base import Voter, Context


class MarketRegimeVoter(Voter):
    name = "market_regime"
    description = "+1 when the benchmark (SPY) closes above its N-day average, -1 otherwise."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        if ctx.benchmark is None or len(ctx.benchmark) == 0:
            return pd.Series(0.0, index=bars.index)
        n = int(self.cfg.get("sma", 50))
        bench = ctx.benchmark["close"]
        above = (bench > sma(bench, n)).astype(float) * 2 - 1
        above[sma(bench, n).isna()] = float("nan")
        # Align the benchmark's dates to this symbol's dates. ffill only carries
        # PAST values forward, so no future information leaks in.
        return above.reindex(bars.index, method="ffill")


VOTER = MarketRegimeVoter
