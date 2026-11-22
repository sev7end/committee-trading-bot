import pandas as pd

from tradingbot.earnings import blackout_mask


def test_blackout_window_in_trading_days():
    idx = pd.bdate_range("2026-01-01", periods=30)
    e = pd.DatetimeIndex([idx[10]])
    m = blackout_mask(idx, e, days_before=3, days_after=1)
    assert m.iloc[7:12].all()          # 3 bars before, the day, 1 after
    assert not m.iloc[6] and not m.iloc[12]
    assert m.sum() == 5


def test_blackout_handles_weekend_report_and_edges():
    idx = pd.bdate_range("2026-01-05", periods=10)  # Mon 5 Jan .. Fri 16 Jan
    e = pd.DatetimeIndex([pd.Timestamp("2026-01-10")])  # a Saturday -> maps to Mon 12 Jan
    m = blackout_mask(idx, e, days_before=2, days_after=0)
    assert m.loc["2026-01-08"] and m.loc["2026-01-09"] and m.loc["2026-01-12"]
    assert not m.loc["2026-01-13"]
    m2 = blackout_mask(idx, pd.DatetimeIndex([idx[0]]), 5, 5)  # window clipped at the start
    assert m2.iloc[:6].all() and not m2.iloc[6]
    assert blackout_mask(idx, pd.DatetimeIndex([]), 3, 1).sum() == 0
