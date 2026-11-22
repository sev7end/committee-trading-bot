"""Regression tests for the defects found in the 2026-09-22 code review."""
import numpy as np
import pandas as pd
import pytest

from tradingbot.committee import Committee
from tradingbot.indicators import rsi
from tradingbot.backtest import run_backtest
from tradingbot.execution import levels_from_fill
from tradingbot.config import load_settings


def test_rsi_all_gains_is_100_not_nan():
    s = pd.Series([100, 101, 102, 103, 104, 105], dtype=float)
    r = rsi(s, 3)
    assert r.iloc[-1] == 100.0
    down = rsi(pd.Series([105, 104, 103, 102, 101, 100], dtype=float), 3)
    assert down.iloc[-1] == 0.0
    flat = rsi(pd.Series([100.0] * 6), 3)
    assert flat.iloc[-1] == 50.0


def test_zero_weight_voter_does_not_count_toward_agree(bars, benchmark):
    from tradingbot.voters import Context
    params = {"voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5},
                         "volume": {"enabled": True, "weight": 0.0, "lookback": 5, "min_ratio": 1.0}},
              "entry": {"min_score": 0.5, "min_voters_agree": 2}}
    c = Committee(params)
    assert c.voter_names == ["breakout"]          # the weight-0 voter is off
    out = c.score(bars, Context(benchmark=benchmark))
    assert out["agree"].max() <= 1                # so it can never satisfy a quorum of 2
    assert not out["enter"].any()


def test_sizing_does_not_use_fill_day_close(bars, benchmark):
    """Shares for a fill at day t's open must not change when day t's CLOSE of another open position changes."""
    settings = load_settings()
    settings["costs"] = {"spread_bps": 0, "slippage_bps": 0}
    params = {"version": 0,
              "voters": {"breakout": {"enabled": True, "weight": 1.0, "lookback": 5}},
              "entry": {"min_score": 1.0, "min_voters_agree": 1},
              "exit": {"stop_atr": 1.0, "target_atr": 50.0, "max_hold_bars": 400, "atr_period": 14},
              "risk": {"risk_per_trade_pct": 1.0, "max_position_pct": 50}}
    a = bars.copy()
    b = bars.copy()
    b["close"] = b["close"] * 1.02          # a different symbol with the same shape but a different level
    start, end = str(bars.index[70].date()), str(bars.index[290].date())
    base = run_backtest({"A": a, "B": b}, benchmark, params, settings, start, end)
    if len(base.trades) < 2:
        pytest.skip("synthetic data produced too few trades")
    # inflate A's close on the day B fills: sizing of B must be unchanged
    tb = next(t for t in base.trades if t["symbol"] == "B")
    fill_day = pd.Timestamp(tb["entry_date"])
    a2 = a.copy()
    a2.loc[fill_day, "close"] *= 3.0
    a2.loc[fill_day, "high"] = max(a2.loc[fill_day, "high"], a2.loc[fill_day, "close"])
    again = run_backtest({"A": a2, "B": b}, benchmark, params, settings, start, end)
    tb2 = next(t for t in again.trades if t["symbol"] == "B" and t["entry_date"] == tb["entry_date"])
    assert tb2["shares"] == tb["shares"]


def test_levels_rebuilt_from_fill():
    e = {"stop": 90.0, "target": 120.0, "atr": 2.0, "stop_atr": 3.0, "target_atr": 2.0}
    assert levels_from_fill(101.0, e) == (95.0, 105.0)
    assert levels_from_fill(101.0, {"stop": 90.0, "target": 120.0}) == (90.0, 120.0)


def test_yearly_pnl_starts_from_cash_and_drops_stubs():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    from sweep import yearly_pnl
    idx = pd.bdate_range("2020-11-01", "2022-12-31")
    eq = pd.Series(np.linspace(25_000, 26_000, len(idx)), index=idx)
    yp = yearly_pnl(eq, 25_000.0)
    assert len(yp) == 2                         # the 2020 stub (2 months) is not a year
    assert yp[0] == pytest.approx(float(eq["2021"].iloc[-1] - eq["2020"].iloc[-1]))   # 2021 measured from where 2020 ended
    full = yearly_pnl(pd.Series(np.linspace(25_000, 26_000, 504), index=pd.bdate_range("2021-01-01", periods=504)), 25_000.0)
    assert sum(full) == pytest.approx(1000.0, abs=1.0)                                  # first full year starts from cash, nothing lost


def test_challenger_needs_absolute_floor_when_champion_lost():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    from autopilot import challenger_wins
    def m(pf, ratio):
        return {"profit_factor": pf, "recovery_ratio": ratio}
    champ = {"starts": [{"stocks": m(0.8, -1), "etf": m(0.9, -1), "score": 0.0}] * 3, "score": 0.0}
    weak = {"starts": [{"stocks": m(1.05, 0.2), "etf": m(1.05, 0.2), "score": 0.2}] * 3, "score": 0.2}
    ok, why = challenger_wins(champ, weak, 1.15)
    assert not ok and "did not beat" in why
    strong = {"starts": [{"stocks": m(1.3, 1.0), "etf": m(1.2, 1.0), "score": 1.0}] * 3, "score": 1.0}
    assert challenger_wins(champ, strong, 1.15)[0]


def test_record_entries_tracks_fill_without_bracket(tmp_path, monkeypatch):
    import sys, pathlib, types
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    import run_paper
    monkeypatch.setattr(run_paper, "PAPER_DIR", tmp_path)
    monkeypatch.setattr(run_paper, "ORDERS_FILE", tmp_path / "orders.csv")
    args = types.SimpleNamespace(dry_run=False)
    summary = {"book": "trend", "tracked": {}, "placed": [], "planned": [
        {"symbol": "AAA", "qty": 10, "stop": 95.0, "target": 105.0, "ref_price": 100.0, "status": "filled_no_bracket", "fill_price": 100.5},
        {"symbol": "BBB", "qty": 5, "stop": 9.0, "target": 11.0, "ref_price": 10.0, "status": "unfilled"},
    ]}
    run_paper.record_entries(summary, args, pd.Timestamp("2026-09-22"))
    assert "AAA" in summary["tracked"] and summary["unprotected"] == ["AAA"]
    assert "BBB" not in summary["tracked"] and summary["failed"] == ["BBB (unfilled)"]
