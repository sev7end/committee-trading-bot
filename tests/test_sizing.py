import numpy as np
import pandas as pd

from tradingbot.sizing import vol_scale_series, vol_scale_now


def test_disabled_is_all_ones(bars, benchmark):
    s = vol_scale_series(bars, benchmark, None)
    assert (s == 1.0).all()
    assert vol_scale_now(bars, benchmark, {"enabled": False}) == 1.0


def test_benchmark_mode_bounds_and_direction(bars, benchmark):
    cfg = {"enabled": True, "mode": "benchmark", "lookback": 20, "target_ann_vol": 0.15, "min_scale": 0.5, "max_scale": 1.5}
    s = vol_scale_series(bars, benchmark, cfg)
    assert s.between(0.5, 1.5).all()
    # a much calmer benchmark must give a larger (or equal) scale than a wilder one
    calm = benchmark.copy(); calm["close"] = 100 + np.arange(len(calm)) * 0.01
    wild = benchmark.copy(); wild["close"] = 100 * np.cumprod(1 + np.random.default_rng(1).normal(0, 0.05, len(wild)))
    assert vol_scale_now(bars, calm, cfg) >= vol_scale_now(bars, wild, cfg)


def test_self_mode_is_backward_looking(bars, benchmark):
    cfg = {"enabled": True, "mode": "self", "lookback": 20, "min_scale": 0.5, "max_scale": 1.5}
    full = vol_scale_series(bars, benchmark, cfg)
    for i in (150, 220, 299):
        part = vol_scale_series(bars.iloc[: i + 1], benchmark, cfg)
        assert full.iloc[i] == part.iloc[-1]
    assert full.between(0.5, 1.5).all()
