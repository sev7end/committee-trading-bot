"""Walk-forward validation of the whole search PROCESS, not just one parameter set.

    python scripts/walk_forward.py                       # trend book: test years 2018..2025, 5-year train each
    python scripts/walk_forward.py --book xa --samples 150

For each test year Y: run the compute sweep on [Y-train_years, Y-1] exactly as the autopilot would
(same space, same constraints, same plateau re-ranking), take its winner, and trade it through year Y
which the sweep never saw. Also score the dumb baseline and the CURRENT live params on year Y for context.

Eight fresh out-of-sample years say far more than one holdout window. If the sweep's winners are
profitable in most test years, the process finds edge; if not, it fits the past.

Writes results/walk_forward/<book>.json and prints a table. Never touches config/ or results/best.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys

import _common  # noqa: F401

from tradingbot.backtest import run_backtest
from tradingbot.config import RESULTS_DIR, ROOT, book_settings, load_json, load_settings, resolve_universe
from tradingbot.data import load_bars
from tradingbot.metrics import clean_for_json

OUT = RESULTS_DIR / "walk_forward"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="trend")
    ap.add_argument("--years", default="2018-2025", help="test years, e.g. 2018-2025")
    ap.add_argument("--train-years", type=int, default=5)
    ap.add_argument("--samples", type=int, default=150)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--seed-params", default=None, help="params file the sweep starts from (default: the book's live params; use a neutral file to avoid contamination)")
    ap.add_argument("--out-name", default=None, help="results file name (default <book>.json)")
    ap.add_argument("--sweep-args", default="", help="extra arguments passed to every fold's sweep, e.g. \"--fix exit.trail_atr=0\"")
    ap.add_argument("--space", default=None, help="override the book's knob grid (trend, mr, xa, simple)")
    args = ap.parse_args()

    settings = load_settings()
    book = next(b for b in settings["books"] if b["name"] == args.book)
    settings = book_settings(settings, book)   # the book's benchmark, costs and sizing
    primary = book.get("universe", "stocks")
    secondary = book.get("secondary", "stocks" if primary == "etf" else "etf")
    y0, y1 = (int(x) for x in args.years.split("-"))
    OUT.mkdir(parents=True, exist_ok=True)

    syms = list(dict.fromkeys(resolve_universe(primary, settings) + resolve_universe(secondary, settings) + [settings["benchmark"]]))
    bars = load_bars(syms, settings["data_start"], f"{y1}-12-31", settings)
    bench = bars[settings["benchmark"]]
    uni = {k: bars[k] for k in resolve_universe(primary, settings) if k in bars}   # benchmark stays tradable if the universe lists it
    live_params = load_json(ROOT / book["params"])
    baseline_params = load_json(ROOT / "config" / "baseline_params.json")

    rows = []
    for y in range(y0, y1 + 1):
        tr_start, tr_end = f"{y - args.train_years}-01-01", f"{y - 1}-12-31"
        te_start, te_end = f"{y}-01-01", f"{y}-12-31"
        out_dir = f"walk_forward/sweep_{args.book}_{y}"
        if (RESULTS_DIR / out_dir).exists():
            shutil.rmtree(RESULTS_DIR / out_dir)
        cmd = [sys.executable, "-u", "scripts/sweep.py", "--book", args.book, "--params", args.seed_params or book["params"], "--out", out_dir, "--space", args.space or book.get("space", "trend"),
               "--primary", primary, "--secondary", secondary, "--samples", str(args.samples), "--workers", str(args.workers),
               "--train-start", tr_start, "--train-end", tr_end, "--seed", str(100 + y)]
        if args.book == "trend":
            cmd += ["--baseline-params", "config/baseline_params.json"]
        else:
            cmd += ["--no-baseline-check"]
        if args.sweep_args.strip():
            cmd += args.sweep_args.split()
        t0 = dt.datetime.now()
        print(f"\n=== fold {y}: sweep on {tr_start}..{tr_end} ({args.samples} samples) ...", flush=True)
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3 * 3600)
        (OUT / f"sweep_{args.book}_{y}.log").write_text(r.stdout + r.stderr, encoding="utf-8")
        best_p = RESULTS_DIR / out_dir / "best_params.json"
        row = {"year": y, "train": [tr_start, tr_end], "sweep_seconds": (dt.datetime.now() - t0).seconds}
        if not best_p.exists():
            row["note"] = "no candidate passed the constraints"
            print(f"  fold {y}: no candidate passed")
        else:
            winner = load_json(best_p)["params"]
            row["winner_params"] = winner
            row["winner_train_pf"] = load_json(best_p)["stocks"]["profit_factor"]
            for label, params in (("winner", winner), ("live", live_params), ("baseline", baseline_params)):
                m = clean_for_json(run_backtest(uni, bench, params, settings, te_start, te_end).metrics)
                row[label] = {k: m[k] for k in ("trades", "profit_factor", "total_pnl", "max_drawdown", "recovery_ratio", "win_rate")}
            w, lv, b = row["winner"], row["live"], row["baseline"]
            print(f"  fold {y} TEST: winner PF {w['profit_factor']:.2f} ${w['total_pnl']:>7,.0f} DD ${w['max_drawdown']:>6,.0f} ({w['trades']}t) | "
                  f"live params PF {lv['profit_factor']:.2f} ${lv['total_pnl']:>7,.0f} | baseline PF {b['profit_factor']:.2f} ${b['total_pnl']:>7,.0f}", flush=True)
        rows.append(row)
        (OUT / f"{args.out_name or args.book}.json").write_text(json.dumps({"book": args.book, "primary": primary, "train_years": args.train_years,
                                                            "samples": args.samples, "folds": rows}, indent=2, default=str), encoding="utf-8")

    done = [r for r in rows if "winner" in r]
    if done:
        pos = sum(1 for r in done if r["winner"]["total_pnl"] > 0)
        pnl = sum(r["winner"]["total_pnl"] for r in done)
        beat = sum(1 for r in done if r["winner"]["recovery_ratio"] > r["baseline"]["recovery_ratio"])
        print(f"\nWALK-FORWARD {args.book}: {len(done)} folds, winner profitable in {pos}/{len(done)} test years, "
              f"total OOS P&L ${pnl:,.0f}, beat baseline (P&L/DD) in {beat}/{len(done)} years")
        print("year  winnerPF  livePF  basePF  | winner P&L")
        for r in done:
            print(f"{r['year']}  {r['winner']['profit_factor']:7.2f}  {r['live']['profit_factor']:6.2f}  {r['baseline']['profit_factor']:6.2f}  | ${r['winner']['total_pnl']:>8,.0f}")


if __name__ == "__main__":
    main()
