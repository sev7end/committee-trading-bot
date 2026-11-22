"""Step 1 of the day-trading question: does an Opening Range Breakout make money AFTER costs on the
free-feed history (July 2020 -> now)? Grid over the classic variants, several cost levels, three universes.

    python scripts/orb_research.py

Writes results/intraday/orb_grid.csv and prints the best few by profit factor with their yearly P&L.
Pure research: nothing here touches the live books.
"""
from __future__ import annotations
import itertools
import sys

import pandas as pd

import _common  # noqa: F401

from tradingbot.config import RESULTS_DIR, load_settings
from tradingbot.costs import CostModel
from tradingbot.intraday import portfolio
from tradingbot.intraday_data import load_intraday

ETFS = ["SPY", "QQQ"]
STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "AMD", "GOOGL", "JPM", "XOM"]
START, END = "2020-07-27", "2026-09-16"


def main():
    s = load_settings()
    out_dir = RESULTS_DIR / "intraday"
    out_dir.mkdir(parents=True, exist_ok=True)
    bars = {sym: load_intraday(sym, "2020-07-01", "2026-09-17", 5) for sym in ETFS + STOCKS}
    bars = {k: v for k, v in bars.items() if len(v) > 10_000}
    universes = {"etfs": [k for k in ETFS if k in bars], "stocks": [k for k in STOCKS if k in bars], "all": list(bars)}
    cost_levels = {"base(3+5bps)": s["costs"], "double(6+10bps)": {**s["costs"], "spread_bps": 6, "slippage_bps": 10},
                   "zero": {}}
    grid = list(itertools.product([1, 3, 6], ["long", "both"], [("range", None), ("atr", 0.1), ("atr", 0.25)], [None, 2.0]))
    rows = []
    total = len(grid) * len(universes)
    k = 0
    for range_bars, direction, (stop, frac), target_r in grid:
        cfg = {"range_bars": range_bars, "direction": direction, "stop": stop, "stop_atr_frac": frac or 0.1, "target_r": target_r}
        for uname, syms in universes.items():
            k += 1
            sub = {x: bars[x] for x in syms}
            for cname, cc in cost_levels.items():
                r = portfolio(sub, cfg, CostModel(cc), 25_000, START, END)
                m = r["metrics"]
                yp = r["yearly"]
                rows.append({"range_min": range_bars * 5, "direction": direction, "stop": f"{stop}{'' if stop == 'range' else f' {frac}'}",
                             "target_r": target_r or 0, "universe": uname, "costs": cname, "trades": m["trades"], "pf": round(m["profit_factor"], 3),
                             "win_rate": round(m["win_rate"], 1), "pnl": round(m["total_pnl"]), "max_dd": round(m["max_drawdown"]),
                             "ratio": round(m["recovery_ratio"], 2), "positive_years": sum(1 for v in yp.values() if v > 0), "years": len(yp),
                             "yearly": " ".join(f"{y}:{int(v):+d}" for y, v in yp.items())})
            if k % 6 == 0:
                print(f"  {k}/{total} configs x universes done", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "orb_grid.csv", index=False)
    base = df[df["costs"] == "base(3+5bps)"].sort_values("pf", ascending=False)
    print("\nTOP 10 at base costs (spread 3 bps + slippage 5 bps per side):")
    print(base.head(10)[["range_min", "direction", "stop", "target_r", "universe", "trades", "pf", "win_rate", "pnl", "max_dd", "ratio", "positive_years", "years"]].to_string(index=False))
    print("\nSame configs at ZERO costs vs DOUBLE costs (how much the edge depends on execution):")
    top = base.head(5)[["range_min", "direction", "stop", "target_r", "universe"]]
    for _, t in top.iterrows():
        sel = df[(df["range_min"] == t["range_min"]) & (df["direction"] == t["direction"]) & (df["stop"] == t["stop"]) & (df["target_r"] == t["target_r"]) & (df["universe"] == t["universe"])]
        print("  " + " | ".join(f"{r['costs']}: PF {r['pf']:.2f} ${r['pnl']:,}" for _, r in sel.iterrows()) + f"   <- {t['range_min']}min {t['direction']} {t['stop']} tgt {t['target_r']} {t['universe']}")
    print(f"\nfull grid -> {out_dir / 'orb_grid.csv'}  ({len(df)} rows)")


if __name__ == "__main__":
    main()
