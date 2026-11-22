"""Short positions: a falling market pays, a rising one hits the (upper) stop, and the accounting round-trips."""
import numpy as np
import pandas as pd

from tradingbot.backtest import run_backtest
from tradingbot.committee import Committee
from tradingbot.config import load_settings
from tradingbot.voters import Context


def _bars(drift, n=220, seed=2):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = 100 + np.cumsum(rng.normal(drift, 0.8, n))
    df = pd.DataFrame({"close": close}, index=idx)
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = np.maximum(df["open"], df["close"]) + 0.4
    df["low"] = np.minimum(df["open"], df["close"]) - 0.4
    df["volume"] = 1e6
    return df


def _params(side):
    return {"version": 0,
            "voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5}},
            "entry": {"min_score": 1.0, "min_voters_agree": 1, "side": side},
            "exit": {"stop_atr": 3.0, "target_atr": 2.0, "max_hold_bars": 15, "atr_period": 14},
            "risk": {"risk_per_trade_pct": 1.0, "max_position_pct": 50}}


def test_short_side_signals_mirror_long(benchmark):
    bars = _bars(-0.5)
    out = Committee(_params("short")).score(bars, Context(benchmark=None))
    assert (out["total_short"] == -out["total"]).all()          # breakout is directional: flipped
    assert out["enter_short"].sum() > out["enter"].sum()         # a falling series gives short entries


def test_short_book_profits_in_a_decline_and_accounting_round_trips(benchmark):
    settings = load_settings()
    settings["costs"] = {"spread_bps": 0, "slippage_bps": 0}
    settings["risk_limits"] = {}
    bars = _bars(-0.5)
    res = run_backtest({"X": bars}, None, _params("short"), settings, str(bars.index[60].date()), str(bars.index[-1].date()))
    assert res.trades and all(t["side"] == "short" for t in res.trades)
    assert res.metrics["total_pnl"] > 0
    # every trade's pnl equals (entry - exit) * shares (no costs), and equity ends at cash + sum(pnl)
    for t in res.trades:
        assert abs(t["pnl"] - (t["entry_price"] - t["exit_price"]) * t["shares"]) < 0.01
    assert abs(res.equity.iloc[-1] - (settings["account"]["starting_cash"] + sum(t["pnl"] for t in res.trades))) < 0.01
    assert all(t["stop"] > t["entry_price"] for t in res.trades)  # stops sit above a short entry


def test_short_stop_is_hit_when_price_rises(benchmark):
    settings = load_settings()
    settings["costs"] = {"spread_bps": 0, "slippage_bps": 0}
    settings["risk_limits"] = {}
    bars = _bars(-0.5, n=120)
    up = _bars(+1.2, n=120, seed=5)
    up.index = pd.bdate_range(bars.index[-1] + pd.Timedelta(days=1), periods=120)
    up[["open", "high", "low", "close"]] += bars["close"].iloc[-1] - up["close"].iloc[0]
    both = pd.concat([bars, up])
    res = run_backtest({"X": both}, None, _params("short"), settings, str(both.index[60].date()), str(both.index[-1].date()))
    reasons = {t["exit_reason"] for t in res.trades}
    assert "stop" in reasons
    stopped = [t for t in res.trades if t["exit_reason"] == "stop"]
    assert all(t["pnl"] < 0 for t in stopped)
