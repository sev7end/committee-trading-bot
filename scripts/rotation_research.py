"""Momentum rotation: grid search + honest walk-forward, like walk_forward.py but for tradingbot/rotation.py.

    python scripts/rotation_research.py --universe rotation --years 2018-2025 --train-years 5

For each test year the small grid is searched on the preceding years only (score = P&L/DD, must be profitable and
have >= 60% positive years); the winner then trades the unseen year. Also prints SPY buy-and-hold for the same year.
Writes results/walk_forward/rotation_<universe>.json.
"""
from __future__ import annotations
import argparse
import itertools
import json
import statistics

import pandas as pd

import _common  # noqa: F401

from tradingbot.config import RESULTS_DIR, load_settings, resolve_universe
from tradingbot.data import load_bars
from tradingbot.metrics import clean_for_json
from tradingbot.rotation import rotation_backtest
from tradingbot import memo

GRID = {
    "lookbacks": [[126], [252], [63, 126, 252]],
    "skip": [0, 21],
    "top_n": [5, 8, 10],
    "abs_filter": [True],
    "regime_sma": [0, 200],
    "vol_weight": [False, True],
    "rebalance": ["M"],
}


def yearly(eq: pd.Series, start_value: float) -> list[float]:
    out, prev = [], start_value
    for _, s in eq.groupby(eq.index.year):
        out.append(float(s.iloc[-1]) - prev)
        prev = float(s.iloc[-1])
    return [v for v, (_, s) in zip(out, eq.groupby(eq.index.year)) if len(s) >= 200]


BENCH = {}


def evaluate(bars, params, settings, start, end):
    r = rotation_backtest(bars, params, settings, start, end, benchmark=BENCH.get("df"))
    m = clean_for_json(r.metrics)
    yp = yearly(r.equity, settings["account"]["starting_cash"]) if len(r.equity) else []
    m["positive_years_pct"] = (sum(1 for v in yp if v > 0) / len(yp) * 100) if yp else 0.0
    return m, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="rotation")
    ap.add_argument("--years", default="2018-2025")
    ap.add_argument("--train-years", type=int, default=5)
    ap.add_argument("--cash-symbol", default=None)
    args = ap.parse_args()
    settings = load_settings()
    syms = resolve_universe(args.universe, settings)
    y0, y1 = (int(x) for x in args.years.split("-"))
    bars = load_bars(list(dict.fromkeys(syms + [settings["benchmark"]])), settings["data_start"], f"{y1}-12-31", settings)
    uni = {s: bars[s] for s in syms if s in bars}
    spy = bars[settings["benchmark"]]["close"]
    BENCH["df"] = bars[settings["benchmark"]]
    keys = list(GRID)
    grid = [dict(zip(keys, vals)) for vals in itertools.product(*GRID.values())]
    for g in grid:
        g["cash_symbol"] = args.cash_symbol
    print(f"{len(uni)} symbols, {len(grid)} grid points, test years {y0}-{y1}")
    rows = []
    for y in range(y0, y1 + 1):
        tr_s, tr_e, te_s, te_e = f"{y - args.train_years}-01-01", f"{y - 1}-12-31", f"{y}-01-01", f"{y}-12-31"
        best = None
        for g in grid:
            m, _ = evaluate(uni, g, settings, tr_s, tr_e)
            if m["trades"] < 10 or m["profit_factor"] <= 1.0 or m["positive_years_pct"] < 60:
                continue
            if best is None or m["recovery_ratio"] > best[0]:
                best = (m["recovery_ratio"], g, m)
        row = {"year": y}
        if best is None:
            row["note"] = "no grid point passed"
            print(f"  {y}: no grid point passed")
        else:
            m, r = evaluate(uni, best[1], settings, te_s, te_e)
            spy_ret = float(spy.asof(pd.Timestamp(te_e)) / spy.asof(pd.Timestamp(te_s)) - 1) * 100
            row.update(winner_params=best[1], train=best[2], winner={k: m[k] for k in ("trades", "profit_factor", "total_pnl", "max_drawdown", "recovery_ratio", "return_pct", "sharpe")},
                       spy_return_pct=spy_ret)
            print(f"  {y} TEST: ret {m['return_pct']:6.1f}%  PF {m['profit_factor']:.2f}  DD {m['max_drawdown_pct']:4.1f}%  sharpe {m['sharpe']:.2f} ({m['trades']}t) "
                  f"| SPY {spy_ret:6.1f}% | params {best[1]['lookbacks']} skip {best[1]['skip']} top {best[1]['top_n']} abs {best[1]['abs_filter']}")
        rows.append(row)
    done = [r for r in rows if "winner" in r]
    if done:
        rets = [r["winner"]["return_pct"] for r in done]
        print(f"\nROTATION {args.universe}: {len(done)} folds, profitable {sum(1 for x in rets if x > 0)}/{len(done)}, mean yearly return {statistics.mean(rets):.1f}% "
              f"(SPY {statistics.mean(r['spy_return_pct'] for r in done):.1f}%), median PF {statistics.median(r['winner']['profit_factor'] for r in done):.2f}, "
              f"worst DD {max(r['winner']['max_drawdown'] for r in done):,.0f}")
    (RESULTS_DIR / "walk_forward").mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "walk_forward" / f"rotation_{args.universe}.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
