"""Live rotation planning: rebalance-day detection, targets from completed bars, and sell/buy orders."""
import datetime as dt

import numpy as np
import pandas as pd

from tradingbot.rotation_live import is_rebalance_day, target_weights, plan_orders


def _bars(drift, n=400, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-02", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    df = pd.DataFrame({"close": close}, index=idx)
    df["open"] = df["close"].shift(1).fillna(df["close"]); df["high"] = df["close"] * 1.003; df["low"] = df["close"] * 0.997; df["volume"] = 1e6
    return df


def test_rebalance_day_is_first_session_of_month():
    sessions = pd.bdate_range("2026-09-01", "2026-10-30")
    assert is_rebalance_day(dt.date(2026, 10, 1), sessions)          # Sep 30 was the last session
    assert not is_rebalance_day(dt.date(2026, 10, 2), sessions)
    assert is_rebalance_day(dt.date(2026, 11, 2), sessions)          # Oct 30 (Fri) was the last session; Nov 2 is a Monday


def test_targets_pick_the_strongest_and_skip_claimed():
    bars = {"UP": _bars(0.002, seed=1), "MID": _bars(0.001, seed=2), "DOWN": _bars(-0.002, seed=3)}
    p = {"lookbacks": [126], "skip": 0, "top_n": 2, "abs_filter": True, "min_history": 200}
    t = target_weights(bars, p, None)
    assert set(t) == {"UP", "MID"} and abs(sum(t.values()) - 1.0) < 1e-9
    t2 = target_weights(bars, p, None, exclude={"UP"})
    assert set(t2) == {"MID"}                                      # DOWN has negative momentum: cash instead


def test_plan_orders_sells_leavers_and_buys_entrants():
    targets = {"A": 0.5, "B": 0.5}
    holdings = {"A": 10, "C": 7}
    prices = {"A": 100.0, "B": 50.0, "C": 20.0}
    sells, buys = plan_orders(targets, holdings, prices, cap=2000.0, min_notional=100)
    assert [s["symbol"] for s in sells] == ["C"]
    assert [b["symbol"] for b in buys] == ["B"] and buys[0]["qty"] == 20   # A is already at its $1000 target
