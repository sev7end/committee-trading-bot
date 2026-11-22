import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_bars(n: int = 300, seed: int = 0, start_price: float = 100.0) -> pd.DataFrame:
    """Random-walk daily bars with realistic open/high/low/close/volume."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.02, n)
    close = start_price * np.cumprod(1 + rets)
    open_ = np.concatenate([[start_price], close[:-1]]) * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


@pytest.fixture
def bars():
    return make_bars()


@pytest.fixture
def benchmark():
    return make_bars(seed=42, start_price=500.0)
