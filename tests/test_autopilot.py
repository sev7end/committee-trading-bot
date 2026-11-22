import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from autopilot import pair_trades, challenger_wins, kill_switch  # noqa: E402


def test_pair_trades_fifo():
    fills = pd.DataFrame([
        {"filled_at": "2026-09-15T13:31", "symbol": "AAPL", "side": "buy", "qty": 10, "avg_price": 100.0},
        {"filled_at": "2026-09-16T13:31", "symbol": "AAPL", "side": "buy", "qty": 5, "avg_price": 110.0},
        {"filled_at": "2026-09-20T15:00", "symbol": "AAPL", "side": "sell", "qty": 12, "avg_price": 120.0},
        {"filled_at": "2026-09-15T13:31", "symbol": "MSFT", "side": "buy", "qty": 3, "avg_price": 50.0},
        {"filled_at": "2026-09-17T13:31", "symbol": "MSFT", "side": "sell", "qty": 3, "avg_price": 45.0},
    ])
    t = pair_trades(fills)
    assert len(t) == 3
    aapl = t[t.symbol == "AAPL"].sort_values("entry_time")
    assert list(aapl.qty) == [10, 2]
    assert aapl.pnl.sum() == (120 - 100) * 10 + (120 - 110) * 2
    assert t[t.symbol == "MSFT"].pnl.iloc[0] == -15


def _h(pf_s, ratio_s, pf_e, ratio_e):
    return {"stocks": {"profit_factor": pf_s, "recovery_ratio": ratio_s}, "etf": {"profit_factor": pf_e, "recovery_ratio": ratio_e},
            "score": (max(ratio_s, 0) * max(ratio_e, 0)) ** 0.5}


def test_challenger_needs_margin_and_profitability():
    champ = _h(1.2, 1.0, 1.3, 1.5)
    assert not challenger_wins(champ, _h(1.25, 1.1, 1.3, 1.6), 1.15)[0]      # too small an edge
    assert challenger_wins(champ, _h(1.3, 1.5, 1.4, 2.0), 1.15)[0]           # clear win
    assert not challenger_wins(champ, _h(0.9, 3.0, 1.4, 3.0), 1.15)[0]       # loses money on stocks
    assert not challenger_wins(champ, _h(1.1, 2.0, 1.4, 2.0), 1.15)[0]       # PF below champion


def test_kill_switch():
    cfg = {"kill_switch": {"max_drawdown_pct": 10, "min_trades": 30, "min_profit_factor": 0.8}}
    assert not kill_switch(None, cfg)[0]
    assert kill_switch({"max_drawdown_pct_of_cap": 12, "trades": 5, "profit_factor": 2}, cfg)[0]
    assert kill_switch({"max_drawdown_pct_of_cap": 2, "trades": 40, "profit_factor": 0.7}, cfg)[0]
    assert not kill_switch({"max_drawdown_pct_of_cap": 2, "trades": 10, "profit_factor": 0.5}, cfg)[0]  # too few trades to judge
