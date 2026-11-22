"""The most important test in the project.

For every voter: the score on day i computed from the FULL history must equal the
score on day i computed from history truncated at day i. If they differ, the
voter is peeking at the future and its backtest results are fiction.
"""
import numpy as np
import pandas as pd
import pytest

from tradingbot.config import load_params
from tradingbot.voters import discover, Context
from tradingbot.backtest import run_backtest
from tradingbot.config import load_settings

CHECK_POINTS = [80, 120, 200, 299]


def _cfg_for(name: str) -> dict:
    params = load_params()
    cfg = dict(params["voters"].get(name, {}))
    cfg.pop("enabled", None)
    return cfg


@pytest.mark.parametrize("name", sorted(discover().keys()))
def test_voter_is_backward_looking(name, bars, benchmark):
    cls = discover()[name]
    voter = cls(_cfg_for(name))
    full = voter.compute(bars, Context(benchmark=benchmark))
    assert len(full) == len(bars)
    assert full.dropna().between(-1, 1).all(), f"{name} produced scores outside [-1, 1]"
    for i in CHECK_POINTS:
        truncated_bars = bars.iloc[: i + 1]
        truncated_bench = benchmark.loc[: bars.index[i]]
        part = voter.compute(truncated_bars, Context(benchmark=truncated_bench))
        a, b = full.iloc[i], part.iloc[-1]
        if np.isnan(a) and np.isnan(b):
            continue
        assert a == pytest.approx(b), f"{name} looks ahead: full={a} truncated={b} at bar {i}"


def test_engine_never_uses_future_bars(bars, benchmark):
    """Changing bars AFTER the window end must not change trades inside the window."""
    settings = load_settings()
    settings["warmup_bars"] = 60
    params = load_params()
    start, end = bars.index[100], bars.index[220]
    res_a = run_backtest({"X": bars}, benchmark, params, settings, str(start.date()), str(end.date()))
    mutated = bars.copy()
    mutated.loc[bars.index[221]:, ["open", "high", "low", "close"]] *= 3.0
    res_b = run_backtest({"X": mutated}, benchmark, params, settings, str(start.date()), str(end.date()))
    assert res_a.trades == res_b.trades
    pd.testing.assert_series_equal(res_a.equity, res_b.equity)
