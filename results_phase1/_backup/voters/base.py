"""Base class for committee voters.

A voter looks at one symbol's price history and returns a score per bar:
  +1  = strongly bullish (vote to buy)
   0  = no opinion
  -1  = strongly bearish (vote against buying)

RULES FOR WRITING A VOTER
1. compute() must be vectorised: take the whole bars DataFrame, return a Series
   aligned to bars.index with values in [-1, 1] (NaN allowed during warm-up).
2. Only use the current bar and earlier bars. Never shift(-1), never use
   .iloc[i+1], never peek at the future in any way. tests/test_no_lookahead.py
   will fail if you do.
3. Read every tunable number from self.cfg so the iteration loop can tune it
   from config/params.json.
4. Set `name` to the key used in params.json and give a one-line `description`
   in plain English.
"""
from __future__ import annotations
from dataclasses import dataclass

import pandas as pd


@dataclass
class Context:
    """Extra data a voter may use besides its own symbol's bars."""
    benchmark: pd.DataFrame | None = None  # e.g. SPY bars, same column layout


class Voter:
    name: str = "base"
    description: str = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def compute(self, bars: pd.DataFrame, ctx: Context) -> pd.Series:
        raise NotImplementedError

    @staticmethod
    def clip(s: pd.Series) -> pd.Series:
        return s.clip(-1.0, 1.0)
