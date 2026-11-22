"""Momentum rotation engine: accounting round-trips, ranks are backward-looking, abs filter keeps cash in a decline."""
import numpy as np
import pandas as pd

from tradingbot.rotation import rotation_backtest, momentum_scores


def _bars(drift, n=600, seed=0, start="2022-01-03"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    df = pd.DataFrame({"close": close}, index=idx)
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = np.maximum(df["open"], df["close"]) * 1.003
    df["low"] = np.minimum(df["open"], df["close"]) * 0.997
    df["volume"] = 1e6
    return df


SETTINGS = {"costs": {"spread_bps": 0, "slippage_bps": 0}, "account": {"starting_cash": 25000}}


def test_equity_equals_cash_plus_pnl_and_holds_the_winner():
    uni = {"UP": _bars(0.0015, seed=1), "FLAT": _bars(0.0, seed=2), "DOWN": _bars(-0.0015, seed=3)}
    r = rotation_backtest(uni, {"lookbacks": [126], "skip": 0, "top_n": 1, "abs_filter": True, "min_history": 130}, SETTINGS, "2023-01-01", "2024-04-30")
    assert r.trades
    assert abs(r.equity.iloc[-1] - (25000 + sum(t["pnl"] for t in r.trades))) < 1.0
    held = pd.Series([t["symbol"] for t in r.trades]).value_counts()
    assert held.index[0] == "UP"


def test_abs_filter_stays_in_cash_when_everything_falls():
    uni = {"A": _bars(-0.002, seed=4), "B": _bars(-0.002, seed=5)}
    r = rotation_backtest(uni, {"lookbacks": [126], "skip": 0, "top_n": 2, "abs_filter": True, "min_history": 130}, SETTINGS, "2023-01-01", "2024-04-30")
    assert r.trades == [] and abs(r.equity.iloc[-1] - 25000) < 1e-6


def test_scores_only_use_the_past():
    df = _bars(0.001, seed=6)
    closes = pd.DataFrame({"X": df["close"]})
    full = momentum_scores(closes, [126], 21)
    cut = momentum_scores(closes.iloc[:400], [126], 21)
    pd.testing.assert_series_equal(full["X"].iloc[:400], cut["X"])
