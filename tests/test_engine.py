import pandas as pd
import pytest

from tradingbot.backtest import run_backtest
from tradingbot.costs import CostModel
from tradingbot.metrics import compute_metrics
from tradingbot.config import load_settings


def test_costs_direction():
    c = CostModel({"spread_bps": 10, "slippage_bps": 10, "sell_fee_bps": 1})
    assert c.buy_price(100) > 100
    assert c.sell_price(100) < 100
    assert c.fees(10, 1000, "sell") > c.fees(10, 1000, "buy")


def test_metrics_basic():
    trades = [{"symbol": "A", "pnl": 200, "exit_reason": "target"}, {"symbol": "B", "pnl": -100, "exit_reason": "stop"}]
    eq = pd.Series([1000, 1200, 1100], index=pd.bdate_range("2025-01-01", periods=3), dtype=float)
    m = compute_metrics(trades, eq, 1000)
    assert m["trades"] == 2
    assert m["win_rate"] == 50
    assert m["profit_factor"] == 2
    assert m["max_drawdown"] == 100
    assert m["total_pnl"] == 100


def test_entry_fills_at_next_open_with_costs(bars, benchmark):
    """Force a signal and check the fill happens at the NEXT bar's open, plus the buy penalty."""
    settings = load_settings()
    settings["costs"] = {"spread_bps": 10, "slippage_bps": 0}
    params = {
        "version": 0,
        "voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5}},
        "entry": {"min_score": 1.0, "min_voters_agree": 1},
        "exit": {"stop_atr": 1.0, "target_atr": 2.0, "max_hold_bars": 3, "atr_period": 14},
        "risk": {"risk_per_trade_pct": 1.0, "max_position_pct": 50},
    }
    res = run_backtest({"X": bars}, benchmark, params, settings, str(bars.index[70].date()), str(bars.index[290].date()))
    assert len(res.trades) > 0
    t = res.trades[0]
    sig = pd.Timestamp(t["signal_date"])
    ent = pd.Timestamp(t["entry_date"])
    assert bars.index.get_loc(ent) == bars.index.get_loc(sig) + 1
    expected = bars.at[ent, "open"] * (1 + 5 / 10_000)
    assert t["entry_price"] == pytest.approx(expected, rel=1e-6)
    assert all(tr["bars_held"] <= 3 for tr in res.trades)


def test_no_costs_beats_costs(bars, benchmark):
    settings = load_settings()
    params = {
        "version": 0,
        "voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5}},
        "entry": {"min_score": 1.0, "min_voters_agree": 1},
        "exit": {"stop_atr": 1.0, "target_atr": 2.0, "max_hold_bars": 5, "atr_period": 14},
        "risk": {"risk_per_trade_pct": 1.0, "max_position_pct": 50},
    }
    s, e = str(bars.index[70].date()), str(bars.index[290].date())
    with_costs = run_backtest({"X": bars}, benchmark, params, settings, s, e)
    settings["costs"] = {}
    without = run_backtest({"X": bars}, benchmark, params, settings, s, e)
    assert len(with_costs.trades) == len(without.trades)
    assert without.metrics["total_pnl"] > with_costs.metrics["total_pnl"]
