"""Benchmark one sweep worker: how long does one candidate evaluation take, with and without the memo?

    python scripts/bench_sweep.py --candidates 12 --universe stocks --years 5

Evaluates the SAME random candidates twice in one process (memo off, then memo on) and checks that every
metric is identical, so the speed-up is a pure implementation gain. Timings are wall-clock on this machine
and will be noisy if a sweep is running at the same time; the ratio is still meaningful.
"""
from __future__ import annotations
import argparse
import random
import time

import _common  # noqa: F401

from tradingbot import memo
from tradingbot.backtest import run_backtest
from tradingbot.config import load_settings, resolve_universe
from tradingbot.data import load_bars
from tradingbot.metrics import clean_for_json
import sweep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", type=int, default=12)
    ap.add_argument("--universe", default="stocks")
    ap.add_argument("--space", default="trend", choices=["trend", "mr", "xa"])
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args()

    settings = load_settings()
    end = settings["windows"]["train"]["end"]
    start = f"{int(end[:4]) - args.years}{end[4:]}"
    sweep.SPACE = {"trend": sweep.SPACE, "mr": sweep.SPACE_MR, "xa": sweep.SPACE_XA}[args.space]
    base = {"trend": "config/params.json", "mr": "config/params_mr.json", "xa": "config/params_xa.json"}[args.space]
    from tradingbot.config import load_json, ROOT
    base_params = load_json(ROOT / base)
    syms = resolve_universe(args.universe, settings)
    bars = load_bars(syms + [settings["benchmark"]], settings["data_start"], end, settings)
    bench = bars.pop(settings["benchmark"])
    uni = {k: bars[k] for k in syms if k in bars}
    rng = random.Random(args.seed)
    cands = [sweep.sample(base_params, rng) for _ in range(args.candidates)]
    print(f"{len(uni)} symbols, {start} -> {end}, {len(cands)} candidates")

    def run(label):
        t0 = time.perf_counter()
        out = []
        for p in cands:
            out.append(clean_for_json(run_backtest(uni, bench, p, settings, start, end).metrics))
        dt = time.perf_counter() - t0
        print(f"{label:18s} {dt:7.1f}s total  {dt / len(cands):6.2f}s per evaluation")
        return out, dt

    memo.disable()
    a, ta = run("memo OFF")
    memo.enable(5000)
    b, tb = run("memo ON (cold)")
    c, tc = run("memo ON (warm)")
    same = all(x["trades"] == y["trades"] and abs(x["total_pnl"] - y["total_pnl"]) < 1e-6 for x, y in zip(a, c))
    print(f"identical results: {same} | memo hits {memo.stats['hits']} misses {memo.stats['misses']}")
    print(f"speed-up warm vs off: {ta / tc:.2f}x   (cold {ta / tb:.2f}x)")


if __name__ == "__main__":
    main()
