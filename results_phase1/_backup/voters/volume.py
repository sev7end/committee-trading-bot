"""Volume voter: is today's trading volume unusually high compared with recent days?"""
from __future__ import annotations
import pandas as pd

from ..indicators import volume_ratio
from .base import Voter, Context


class VolumeVoter(Voter):
    name = "volume"
    description = "Volume vs its N-bar average; reaches +1 at min_ratio, -1 when volume is half of normal."

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        n = int(self.cfg.get("lookback", 20))
        min_ratio = float(self.cfg.get("min_ratio", 1.3))
        ratio = volume_ratio(bars["volume"], n)
        scale = max(min_ratio - 1.0, 1e-6)
        return self.clip((ratio - 1.0) / scale)


VOTER = VolumeVoter
