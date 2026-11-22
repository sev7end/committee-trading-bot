import numpy as np
import pandas as pd

from tradingbot.costs import CostModel
from tradingbot.intraday import orb_backtest, daily_atr_from_bars


def _session(day: str, path: list[float], lo: float = 99.5, hi: float = 100.5, wick: float = 0.01) -> pd.DataFrame:
    """78 five-minute bars. The first 3 bars form an explicit opening range [lo, hi]; the remaining
    75 bars follow `path` (linearly interpolated closes) with small wicks."""
    ts = pd.date_range(f"{day} 09:30", periods=78, freq="5min")
    rng = pd.DataFrame({"open": [100.0, 100.3, 99.7], "high": [hi, hi - 0.1, 100.0], "low": [99.8, lo + 0.1, lo],
                        "close": [100.3, 99.7, 100.0], "volume": 1000})
    c = np.interp(np.linspace(0, 1, 75), np.linspace(0, 1, len(path)), path)
    o = np.concatenate([[100.0], c[:-1]])
    rest = pd.DataFrame({"open": o, "high": np.maximum(o, c) + wick, "low": np.minimum(o, c) - wick, "close": c, "volume": 1000})
    df = pd.concat([rng, rest], ignore_index=True)
    df.index = ts
    return df


def test_long_breakout_up_day_wins_and_exits_at_close():
    days = [_session(f"2025-01-{d:02d}", [100, 100.2, 100.8, 101.5, 102.5, 103.5, 104.5, 105]) for d in range(2, 22)]
    tr, eq = orb_backtest(pd.concat(days), {"range_bars": 3, "direction": "long", "stop": "range"}, CostModel.zero(), 10_000)
    assert len(tr) == 20 and all(t["side"] == "long" for t in tr)
    assert all(t["exit_reason"] == "close" for t in tr)
    assert all(t["pnl"] > 0 for t in tr)
    assert eq.iloc[-1] > 10_000


def test_no_trade_when_range_never_breaks():
    days = [_session(f"2025-02-{d:02d}", [100, 100.2, 99.8, 100.1, 99.9, 100]) for d in range(3, 8)]
    tr, eq = orb_backtest(pd.concat(days), {"range_bars": 3, "direction": "both", "stop": "range"}, CostModel.zero(), 10_000)
    assert tr == [] and (eq == 10_000).all()


def test_stop_hit_limits_loss_to_about_risk_pct():
    days = [_session(f"2025-03-{d:02d}", [100, 100.4, 100.8, 100.4, 99.9, 99.3, 98.5, 98.0, 98.0]) for d in range(3, 8)]
    tr, eq = orb_backtest(pd.concat(days), {"range_bars": 3, "direction": "long", "stop": "range", "risk_pct": 1.0}, CostModel.zero(), 10_000)
    assert len(tr) == 5 and all(t["exit_reason"] == "stop" for t in tr)
    assert all(-130 < t["pnl"] < 0 for t in tr)  # ~1% of 10k planned risk, plus a little gap through the stop


def test_short_side_only_when_enabled():
    down = [100, 99.8, 99.2, 98.5, 97.5, 96.5, 96]
    days = [_session(f"2025-05-{d:02d}", down) for d in range(5, 10)]
    long_only, _ = orb_backtest(pd.concat(days), {"range_bars": 3, "direction": "long", "stop": "range"}, CostModel.zero(), 10_000)
    both, _ = orb_backtest(pd.concat(days), {"range_bars": 3, "direction": "both", "stop": "range"}, CostModel.zero(), 10_000)
    assert long_only == []
    assert len(both) == 5 and all(t["side"] == "short" and t["pnl"] > 0 for t in both)


def test_daily_atr_is_known_at_day_start_only():
    days = [_session(f"2025-04-{d:02d}", [100 + d, 101 + d, 100 + d]) for d in range(1, 30) if pd.Timestamp(f"2025-04-{d:02d}").weekday() < 5]
    atr = daily_atr_from_bars(pd.concat(days), 5)
    assert atr.iloc[:5].isna().all()
    assert atr.iloc[6] > 0
