"""Account-level risk limits: loss cooling-off, open-risk budget, theme cap (engine and shared helpers)."""
import pandas as pd

from tradingbot import risklimits
from tradingbot.backtest import run_backtest
from tradingbot.config import load_settings


def test_loss_block_daily_and_weekly():
    cfg = {"daily_loss_pct": 2.0, "weekly_loss_pct": 4.0}
    assert risklimits.loss_block([25000, 24400], 25000, cfg)                       # -2.4% yesterday
    assert risklimits.loss_block([25000, 24600], 25000, cfg) is None                # -1.6%: fine
    assert risklimits.loss_block([25000, 24800, 24700, 24600, 24500, 23900], 25000, cfg)  # -4.4% over 5 sessions
    assert risklimits.loss_block([25000, 24800, 24700, 24600, 24500, 24200], 25000, cfg) is None


def test_budget_theme_and_fit():
    settings = {"risk_limits": {"max_open_risk_pct": 4.0, "max_per_theme": 2}}
    pos = {"AAPL": {"qty": 10, "stop": 90.0}, "MSFT": {"qty": 5, "stop": 380.0}}
    st = risklimits.from_positions(settings, 25000.0, pos, {"AAPL": 100.0, "MSFT": 400.0})
    st.themes = {"AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "XOM": "Energy"}
    st.theme_counts = {}
    for s, i in pos.items():
        st.theme_counts[st.themes[s]] = st.theme_counts.get(st.themes[s], 0) + 1
    assert st.open_risk == 10 * 10 + 5 * 20                     # $200 at risk
    assert st.budget == 0.04 * 25000 - 200                      # $800 left
    assert st.theme_full("NVDA") and not st.theme_full("XOM")
    assert st.fit_qty(100, 5.0) == 100                          # $500 fits
    assert st.fit_qty(100, 10.0) == 80                          # $1000 does not: cut to $800
    st.add("XOM", 80, 10.0)
    assert st.fit_qty(10, 1.0) == 0                             # budget exhausted


def test_engine_open_risk_cap_limits_shares(bars, benchmark):
    settings = load_settings()
    settings["costs"] = {"spread_bps": 0, "slippage_bps": 0}
    params = {"version": 0,
              "voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5}},
              "entry": {"min_score": 1.0, "min_voters_agree": 1},
              "exit": {"stop_atr": 2.0, "target_atr": 50.0, "max_hold_bars": 400, "atr_period": 14},
              "risk": {"risk_per_trade_pct": 3.0, "max_position_pct": 50}}
    start, end = str(bars.index[70].date()), str(bars.index[290].date())
    b = bars.copy()
    b["close"] = b["close"] * 1.02
    uni = {"A": bars, "B": b, "C": bars.copy() * 1.05}
    settings["risk_limits"] = {"max_open_risk_pct": 0.0}
    free = run_backtest(uni, benchmark, params, settings, start, end)
    settings["risk_limits"] = {"max_open_risk_pct": 4.0}   # 3% per trade would allow 6%+ of open risk with two positions
    capped = run_backtest(uni, benchmark, params, settings, start, end)
    assert capped.skipped.get("risk_cap", 0) > 0 or sum(t["shares"] for t in capped.trades) < sum(t["shares"] for t in free.trades)
