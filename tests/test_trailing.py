"""Trailing-stop exits: the stop ratchets up with the highest high, never down, and 'no target' never takes profit."""
import numpy as np
import pandas as pd

from tradingbot.backtest import run_backtest
from tradingbot.config import load_settings
from tradingbot.execution import levels_from_fill


def _trend_bars(n=200, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = 100 + np.cumsum(rng.normal(0.4, 1.0, n))          # a steady uptrend with noise
    close[150:] = close[149] - np.cumsum(np.full(n - 150, 1.5))  # then a slide
    df = pd.DataFrame({"close": close}, index=idx)
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = np.maximum(df["open"], df["close"]) + 0.5
    df["low"] = np.minimum(df["open"], df["close"]) - 0.5
    df["volume"] = 1e6
    return df


def test_trailing_stop_ratchets_and_exits_on_the_slide(benchmark):
    settings = load_settings()
    settings["costs"] = {"spread_bps": 0, "slippage_bps": 0}
    settings["risk_limits"] = {}
    bars = _trend_bars()
    params = {"version": 0,
              "voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5}},
              "entry": {"min_score": 1.0, "min_voters_agree": 1},
              "exit": {"stop_atr": 3.0, "target_atr": 0, "trail_atr": 3.0, "max_hold_bars": 500, "atr_period": 14},
              "risk": {"risk_per_trade_pct": 1.0, "max_position_pct": 50}}
    res = run_backtest({"X": bars}, benchmark, params, settings, str(bars.index[70].date()), str(bars.index[-1].date()))
    assert res.trades, "no trades"
    t = res.trades[0]
    assert t["target"] is None                                   # no profit target
    assert t["exit_reason"] in ("stop", "end_of_data")
    # the recorded (final) stop must be far above the initial one: entry - 3 ATR
    assert t["stop"] > t["entry_price"] - 3.0 * 3.0
    if t["exit_reason"] == "stop":
        assert t["pnl"] > 0                                      # the trend paid before the slide took the stop out


def test_fixed_stop_never_moves(benchmark):
    settings = load_settings()
    settings["costs"] = {"spread_bps": 0, "slippage_bps": 0}
    settings["risk_limits"] = {}
    bars = _trend_bars()
    params = {"version": 0,
              "voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5}},
              "entry": {"min_score": 1.0, "min_voters_agree": 1},
              "exit": {"stop_atr": 3.0, "target_atr": 0, "trail_atr": 0, "max_hold_bars": 500, "atr_period": 14},
              "risk": {"risk_per_trade_pct": 1.0, "max_position_pct": 50}}
    res = run_backtest({"X": bars}, benchmark, params, settings, str(bars.index[70].date()), str(bars.index[-1].date()))
    t = res.trades[0]
    assert t["stop"] < t["entry_price"]                          # untouched initial stop


def test_levels_without_target():
    assert levels_from_fill(100.0, {"stop": 0, "target": None, "atr": 2.0, "stop_atr": 3.0, "target_atr": 0}) == (94.0, None)
    assert levels_from_fill(100.0, {"stop": 0, "target": 0, "atr": 2.0, "stop_atr": 3.0, "target_atr": 2.0}) == (94.0, 104.0)
