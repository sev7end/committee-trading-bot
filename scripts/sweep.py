"""Compute-only parameter search. Zero Claude tokens.

    python scripts/sweep.py --samples 1500          # random search, then coordinate refinement
    python scripts/sweep.py --samples 0 --refine     # refine only, starting from config/params.json

Every candidate is evaluated on TWO universes over the TRAIN window:
  - the 30 stocks we trade (config/universe.json)
  - a bias-free ETF set (settings.validation_universe)
Hard constraints (all must hold or the candidate is discarded):
  - stocks: trades >= min_trades, profit factor > 1, positive P&L in >= min_positive_years_pct of years,
            recovery ratio (P&L / max DD) above the dumb baseline's
  - ETFs:   profit factor > 1 and positive P&L (the rules must not lose on the bias-free set)
Score to maximise: sqrt(stocks_ratio * etf_ratio), so both universes matter and zero on either kills it.

Outputs: results/sweep/log.csv (every evaluation), results/sweep/best_params.json, and on success
config/params.json is updated (version bumped) with a CHANGELOG entry. The holdout is never touched.
"""
from __future__ import annotations
import argparse
import copy
import csv
import datetime as dt
import json
import math
import os
import random
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import _common  # noqa: F401

from tradingbot.backtest import run_backtest
from tradingbot.config import CONFIG_DIR, RESULTS_DIR, ROOT, load_json, load_settings, load_universe, save_json
from tradingbot.data import load_bars
from tradingbot.metrics import clean_for_json
from tradingbot.tradelog import load_result

SWEEP_DIR = RESULTS_DIR / "sweep"

# knob path -> candidate values
SPACE = {
    "entry.min_score": [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0],
    "entry.min_voters_agree": [2, 3, 4],
    "exit.stop_atr": [1.0, 1.5, 2.0, 2.5, 3.0],
    "exit.target_atr": [0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0],        # 0 = no profit target
    "exit.trail_atr": [0, 2.0, 3.0, 4.0, 6.0],                   # 0 = fixed stop; else the stop trails the highest high
    "exit.max_hold_bars": [5, 10, 15, 20, 30, 60, 120],
    "voters.trend.fast": [10, 20, 50],
    "voters.trend.slow": [50, 100, 200],
    "voters.breakout.lookback": [20, 55, 100],
    "voters.volume.min_ratio": [1.2, 1.5, 2.0],
    "voters.volatility.atr_pct_max": [0.04, 0.06, 0.10],
    "voters.market_regime.sma": [15, 20, 50, 100, 200],
    "voters.momentum.lookback": [20, 60, 120, 250],
    "voters.momentum.full_scale": [0.2, 0.3, 0.5],
    "voters.trend.weight": [0, 0.5, 1.0, 1.5, 2.0],
    "voters.breakout.weight": [0, 0.5, 1.0, 1.5, 2.0],
    "voters.volume.weight": [0, 0.5, 1.0, 1.5],
    "voters.volatility.weight": [0, 0.5, 1.0, 1.5],
    "voters.market_regime.weight": [0, 0.5, 1.0, 1.5, 2.0, 3.0],
    "voters.momentum.weight": [0, 0.5, 1.0, 1.5, 2.0],
    "voters.chop.lookback": [10, 20, 40],
    "voters.chop.min_er": [0.2, 0.3, 0.4],
    "voters.chop.weight": [0, 0.5, 1.0, 1.5],
    "voters.breakout.confirm_bars": [1, 2, 3],
}


SPACE_MR = {
    "entry.min_score": [2.0, 2.5, 3.0, 3.5, 4.0],
    "entry.min_voters_agree": [2, 3, 4, 5],
    "exit.stop_atr": [1.5, 2.0, 2.5, 3.0, 4.0],
    "exit.target_atr": [0.5, 0.75, 1.0, 1.5, 2.0],
    "exit.max_hold_bars": [3, 5, 7, 10],
    "voters.trend.fast": [20, 50],
    "voters.trend.slow": [100, 200],
    "voters.pullback.sma": [10, 20, 50],
    "voters.pullback.full_atr": [1.0, 1.5, 2.0, 3.0],
    "voters.rsi.period": [2, 3, 5],
    "voters.rsi.low": [5, 10, 15, 25],
    "voters.market_regime.sma": [50, 100, 200],
    "voters.volatility.atr_pct_max": [0.04, 0.06, 0.10],
    "voters.trend.weight": [0.5, 1.0, 1.5, 2.0],
    "voters.pullback.weight": [1.0, 1.5, 2.0, 3.0],
    "voters.rsi.weight": [0.5, 1.0, 1.5, 2.0],
    "voters.market_regime.weight": [0, 0.5, 1.0, 2.0],
    "voters.volatility.weight": [0, 0.5, 1.0],
}


SPACE_XA = {
    "entry.min_score": [2.0, 2.5, 3.0, 3.5],
    "entry.min_voters_agree": [2, 3, 4],
    "exit.stop_atr": [2.0, 3.0, 4.0, 5.0],
    "exit.target_atr": [3.0, 4.0, 6.0, 8.0, 12.0],
    "exit.max_hold_bars": [20, 40, 60, 100],
    "voters.trend.fast": [20, 50, 100],
    "voters.trend.slow": [150, 200, 250],
    "voters.breakout.lookback": [20, 55, 100, 150],
    "voters.momentum.lookback": [60, 120, 180, 250],
    "voters.momentum.full_scale": [0.05, 0.10, 0.15, 0.25],
    "voters.volatility.atr_pct_max": [0.03, 0.05, 0.10],
    "voters.trend.weight": [0.5, 1.0, 1.5, 2.0],
    "voters.breakout.weight": [0.5, 1.0, 1.5, 2.0],
    "voters.momentum.weight": [0.5, 1.0, 1.5, 2.0],
    "voters.volatility.weight": [0, 0.5, 1.0],
}


# Two rules only: what to buy (breakout) and when to be allowed to buy (market filter). Six knobs, no weights.
SPACE_SIMPLE = {
    "voters.breakout.lookback": [20, 55, 100, 150],
    "voters.market_regime.sma": [50, 100, 200],
    "exit.stop_atr": [1.5, 2.0, 2.5, 3.0, 4.0],
    "exit.target_atr": [0, 2.0, 3.0, 4.0],
    "exit.trail_atr": [0, 3.0, 4.0, 6.0],
    "exit.max_hold_bars": [10, 20, 40, 60, 120],
}


# Crypto: 24/7 daily bars, much higher volatility (wider ATR filter), no profit target (the live venue has no OCO:
# exits are a trailing stop order plus the time exit), BTC as the regime benchmark.
SPACE_CRYPTO = {
    "entry.min_score": [1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
    "entry.min_voters_agree": [2, 3, 4],
    "exit.stop_atr": [1.5, 2.0, 2.5, 3.0, 4.0],
    "exit.trail_atr": [0, 2.0, 3.0, 4.0, 6.0, 8.0],
    "exit.max_hold_bars": [10, 20, 30, 60, 120],
    "voters.trend.fast": [10, 20, 50],
    "voters.trend.slow": [50, 100, 200],
    "voters.breakout.lookback": [20, 55, 100],
    "voters.volatility.atr_pct_max": [0.06, 0.10, 0.15, 0.25],
    "voters.market_regime.sma": [20, 50, 100, 200],
    "voters.momentum.lookback": [20, 60, 90, 180],
    "voters.momentum.full_scale": [0.3, 0.5, 1.0],
    "voters.trend.weight": [0, 0.5, 1.0, 1.5, 2.0],
    "voters.breakout.weight": [0, 0.5, 1.0, 1.5, 2.0],
    "voters.volatility.weight": [0, 0.5, 1.0, 1.5],
    "voters.market_regime.weight": [0, 0.5, 1.0, 1.5, 2.0],
    "voters.momentum.weight": [0, 0.5, 1.0, 1.5, 2.0],
}


# Single-coin mean reversion: buy a sharp dip in a coin that is in a long-term uptrend, sell the bounce within days.
# Exits keep a target here (the bounce IS the trade) and the runner sells it at the daily run when the target is reached.
SPACE_CRYPTO_MR = {
    "entry.min_score": [2.0, 2.5, 3.0, 3.5, 4.0],
    "entry.min_voters_agree": [2, 3, 4],
    "exit.stop_atr": [1.5, 2.0, 2.5, 3.0, 4.0],
    "exit.target_atr": [0.5, 0.75, 1.0, 1.5, 2.0, 3.0],
    "exit.max_hold_bars": [2, 3, 5, 7, 10],
    "voters.trend.fast": [20, 50],
    "voters.trend.slow": [100, 200],
    "voters.pullback.sma": [10, 20, 50],
    "voters.pullback.full_atr": [1.0, 1.5, 2.0, 3.0],
    "voters.rsi.period": [2, 3, 5],
    "voters.rsi.low": [5, 10, 15, 25],
    "voters.market_regime.sma": [50, 100, 200],
    "voters.volatility.atr_pct_max": [0.06, 0.10, 0.15, 0.25],
    "voters.trend.weight": [0.5, 1.0, 1.5, 2.0],
    "voters.pullback.weight": [1.0, 1.5, 2.0, 3.0],
    "voters.rsi.weight": [0.5, 1.0, 1.5, 2.0],
    "voters.market_regime.weight": [0, 0.5, 1.0, 2.0],
    "voters.volatility.weight": [0, 0.5, 1.0],
}


def get(d, path):
    for k in path.split("."):
        d = d[k]
    return d


def get_or(d, path, default=None):
    try:
        return get(d, path)
    except (KeyError, TypeError):
        return default


def put(d, path, v):
    ks = path.split(".")
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = v


def sample(base: dict, rng: random.Random) -> dict:
    p = copy.deepcopy(base)
    for path, vals in SPACE.items():
        put(p, path, rng.choice(vals))
    if "trend" in p.get("voters", {}) and get(p, "voters.trend.fast") >= get(p, "voters.trend.slow"):
        put(p, "voters.trend.fast", 20)
        put(p, "voters.trend.slow", rng.choice([50, 100, 200]))
    return p


def _fast_slow_ok(p: dict) -> bool:
    v = p.get("voters", {})
    return "trend" not in v or get(p, "voters.trend.fast") < get(p, "voters.trend.slow")


def yearly_pnl(equity, start_value: float | None = None, min_bars: int = 200) -> list[float]:
    """P&L per calendar year. The first year starts from `start_value` (starting cash), not from the end of its
    first day; years with fewer than `min_bars` bars (the stubs at both ends of a window) are not counted."""
    if equity is None or len(equity) < 2:
        return []
    out = []
    prev_end = float(start_value) if start_value is not None else float(equity.iloc[0])
    for _, s in equity.groupby(equity.index.year):
        pnl = float(s.iloc[-1]) - prev_end
        prev_end = float(s.iloc[-1])
        if len(s) >= min_bars:
            out.append(pnl)
    return out


# ---- worker state -----------------------------------------------------------
_W = {}


def lower_priority() -> None:
    """Run below normal priority so the laptop stays responsive while a sweep uses most cores (Windows only)."""
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            os.nice(5)
        except Exception:  # noqa: BLE001
            pass


def _init(settings, stocks, etfs, bench, start, end, names=("stocks", "etf"), cfg=None, baseline_ratio=None):
    from tradingbot import memo
    lower_priority()
    memo.enable(int(settings.get("sweep", {}).get("memo_entries", 5000)))
    _W.update(settings=settings, stocks=stocks, etfs=etfs, bench=bench, start=start, end=end, names=names, cfg=cfg, baseline_ratio=baseline_ratio)


SKIPPED = {"skipped": True, "trades": 0, "profit_factor": 0.0, "total_pnl": 0.0, "max_drawdown": 0.0, "recovery_ratio": 0.0, "win_rate": 0.0}


def primary_fails(st: dict, cfg: dict, baseline_ratio) -> list[str]:
    fails = []
    if st["trades"] < cfg["min_trades"]:
        fails.append("few trades")
    if st["profit_factor"] <= 1.0:
        fails.append("stocks PF<=1")
    if st["positive_years_pct"] < cfg["min_positive_years_pct"]:
        fails.append("losing years")
    if baseline_ratio is not None and st["recovery_ratio"] <= baseline_ratio:
        fails.append("below baseline")
    return fails


def _eval_universe(uni, params, s):
    r = run_backtest(uni, _W["bench"], params, s, _W["start"], _W["end"])
    m = clean_for_json(r.metrics)
    yp = yearly_pnl(r.equity, float(s["account"]["starting_cash"]))
    m["positive_years_pct"] = (sum(1 for v in yp if v > 0) / len(yp) * 100) if yp else 0.0
    return m


def evaluate(params: dict) -> dict:
    """Primary universe first; the secondary is only run when the primary passed its hard constraints
    (a candidate that already failed cannot win, so its ETF leg is wasted work)."""
    s = _W["settings"]
    p_name, s_name = _W["names"]
    out = {"params": params}
    try:
        out[p_name] = _eval_universe(_W["stocks"], params, s)
    except Exception as e:  # a broken combination must not kill the sweep
        out[p_name] = {"error": str(e)}
        out[s_name] = dict(SKIPPED)
        return out
    if _W.get("cfg") is not None and primary_fails(out[p_name], _W["cfg"], _W.get("baseline_ratio")):
        out[s_name] = dict(SKIPPED)
        return out
    try:
        out[s_name] = _eval_universe(_W["etfs"], params, s)
    except Exception as e:  # noqa: BLE001
        out[s_name] = {"error": str(e)}
    return out


def score_and_check(ev: dict, baseline_ratio: float, cfg: dict, primary: str = "stocks") -> tuple[float, list[str]]:
    names = [k for k in ev if k != "params"]
    other = next((k for k in names if k != primary), None)
    st, et = ev.get(primary, {}), (ev.get(other, {}) if other else {})
    if "error" in st or "error" in et or not st or not et:
        return -1.0, ["error"]
    fails = primary_fails(st, cfg, baseline_ratio)
    if et.get("skipped"):
        return 0.0, fails + ["secondary skipped"]
    if et["profit_factor"] <= 1.0 or et["total_pnl"] <= 0:
        fails.append("ETF not profitable")
    if et["trades"] < max(30, cfg["min_trades"] // 5):
        fails.append("few ETF trades")  # a handful of lucky trades with a tiny drawdown would otherwise inflate the score
    sc = math.sqrt(max(st["recovery_ratio"], 0) * max(et["recovery_ratio"], 0))
    return sc, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=1500)
    ap.add_argument("--refine", action="store_true", help="coordinate-descent refinement after (or instead of) random search")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="write the winner to the params file")
    ap.add_argument("--params", default=None, help="params file to start from and (with --apply) write to; default config/params.json")
    ap.add_argument("--out", default=None, help="results sub-folder; default results/sweep")
    ap.add_argument("--space", default="trend", choices=["trend", "mr", "xa", "simple", "short", "crypto", "crypto_mr"], help="which knob grid to search")
    ap.add_argument("--min-trades", type=int, default=None, help="override settings.sweep.min_trades (a two-coin book cannot make 300 trades)")
    ap.add_argument("--book", default=None, help="take space, universes, seed, benchmark and costs from settings.books[<name>]")
    ap.add_argument("--min-positive-years", type=float, default=None, help="override settings.sweep.min_positive_years_pct (a hedge book loses in most bull years)")
    ap.add_argument("--refine-passes", type=int, default=6, help="max coordinate-refinement passes (research runs can use 2-3)")
    ap.add_argument("--plateau-top-k", type=int, default=None, help="candidates re-ranked by neighbourhood (default settings.sweep.plateau_top_k)")
    ap.add_argument("--no-baseline-check", action="store_true", help="do not require beating the dumb baseline (for a secondary book)")
    ap.add_argument("--primary", default="stocks", help="universe the hard constraints apply to (stocks, etf, or <name> with settings.<name>_universe)")
    ap.add_argument("--secondary", default=None, help="second universe that must merely be profitable (default: etf, or stocks when primary is etf)")
    ap.add_argument("--train-start", default=None, help="override settings.windows.train.start (walk-forward folds)")
    ap.add_argument("--train-end", default=None, help="override settings.windows.train.end (walk-forward folds)")
    ap.add_argument("--baseline-params", default=None, help="compute the baseline-to-beat on THIS train window from these params instead of results/baseline")
    ap.add_argument("--fix", action="append", default=[], metavar="KNOB=VALUE",
                    help="pin a knob (removed from the search space, set in every candidate), e.g. --fix exit.trail_atr=0")
    args = ap.parse_args()

    book = None
    if args.book:
        from tradingbot.config import find_book
        _settings0 = load_settings()
        book = find_book(_settings0, args.book)
        args.space = book.get("space", args.space)
        args.primary = book.get("universe", "stocks")
        args.secondary = args.secondary or book.get("secondary")
        if not args.params:
            args.params = book.get("seed", book["params"])
        if args.book != _settings0["books"][0]["name"]:
            args.no_baseline_check = True

    global SPACE, SWEEP_DIR
    if args.space == "mr":
        SPACE = SPACE_MR
    elif args.space == "xa":
        SPACE = SPACE_XA
    elif args.space == "simple":
        SPACE = SPACE_SIMPLE
    elif args.space == "short":
        SPACE = {k: v for k, v in SPACE.items()}   # same knobs as the trend book; params.entry.side = "short" flips the votes
    elif args.space == "crypto":
        SPACE = SPACE_CRYPTO
    elif args.space == "crypto_mr":
        SPACE = SPACE_CRYPTO_MR
    if args.out:
        SWEEP_DIR = RESULTS_DIR / args.out
    params_path = Path(args.params) if args.params else CONFIG_DIR / "params.json"
    fixed = {}
    for kv in args.fix:
        k, v = kv.split("=", 1)
        fixed[k] = json.loads(v)
    if fixed:
        SPACE = {k: v for k, v in SPACE.items() if k not in fixed}
        print(f"pinned knobs: {fixed}")

    from tradingbot.config import book_settings
    settings = book_settings(load_settings(), book)
    cfg = dict(settings.get("sweep", {"min_trades": 300, "min_positive_years_pct": 70}))
    if args.min_positive_years is not None:
        cfg["min_positive_years_pct"] = args.min_positive_years
    if args.min_trades is not None:
        cfg["min_trades"] = args.min_trades
    elif book and book.get("min_trades"):
        cfg["min_trades"] = int(book["min_trades"])
    win = settings["windows"]["train"]
    start, end = args.train_start or win["start"], args.train_end or win["end"]
    base_params = load_json(params_path)
    for k, v in fixed.items():
        put(base_params, k, v)
    for path, vals in SPACE.items():        # a seed written before a knob existed gets that knob's neutral (first) value
        if get_or(base_params, path) is None:
            put(base_params, path, vals[0])
    workers = args.workers or cfg.get("workers") or max(1, (os.cpu_count() or 4) - 6)   # leave the laptop usable

    from tradingbot.config import resolve_universe
    secondary = args.secondary or ("stocks" if args.primary == "etf" else "etf")
    stock_syms, etf_syms, bench_sym = resolve_universe(args.primary, settings), resolve_universe(secondary, settings), settings["benchmark"]
    all_bars = load_bars(list(dict.fromkeys(stock_syms + etf_syms + [bench_sym])), settings["data_start"], end, settings)
    bench = all_bars[bench_sym]
    stocks = {k: all_bars[k] for k in stock_syms if k in all_bars}   # primary universe
    etfs = {k: all_bars[k] for k in etf_syms if k in all_bars}       # secondary universe
    uni_names = (args.primary, secondary)

    if args.no_baseline_check:
        baseline_ratio = None
    elif args.baseline_params:
        bres = run_backtest(stocks, bench, load_json(args.baseline_params), settings, start, end)
        baseline_ratio = clean_for_json(bres.metrics)["recovery_ratio"]
    else:
        base = load_result(RESULTS_DIR / "baseline")
        if base is None:
            sys.exit("run the 10-year baseline first: python scripts/run_backtest.py --params config/baseline_params.json --out baseline")
        baseline_ratio = base["metrics"]["recovery_ratio"]
    print(f"train {start} -> {end} | {len(stocks)} stocks, {len(etfs)} ETFs | baseline ratio to beat: {baseline_ratio} | space {args.space} | workers {workers}")

    SWEEP_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SWEEP_DIR / "log.csv"
    fields = ["i", "score", "fails", "s_trades", "s_pf", "s_pnl", "s_dd", "s_ratio", "s_pos_years", "e_trades", "e_pf", "e_pnl", "e_ratio", "params"]
    new_log = not log_path.exists()
    logf = open(log_path, "a", newline="", encoding="utf-8")
    logw = csv.DictWriter(logf, fieldnames=fields)
    if new_log:
        logw.writeheader()

    best = {"score": -1, "params": None, "ev": None}
    counter = [0]
    records: list[tuple[float, list[str], dict]] = []  # (score, fails, ev) for the plateau pass

    def record(ev):
        sc, fails = score_and_check(ev, baseline_ratio, cfg, args.primary)
        st, et = ev.get(uni_names[0], {}), ev.get(uni_names[1], {})
        counter[0] += 1
        records.append((sc, fails, ev))
        logw.writerow({"i": counter[0], "score": round(sc, 4), "fails": ";".join(fails),
                       "s_trades": st.get("trades"), "s_pf": st.get("profit_factor"), "s_pnl": st.get("total_pnl"), "s_dd": st.get("max_drawdown"),
                       "s_ratio": st.get("recovery_ratio"), "s_pos_years": st.get("positive_years_pct"),
                       "e_trades": et.get("trades"), "e_pf": et.get("profit_factor"), "e_pnl": et.get("total_pnl"), "e_ratio": et.get("recovery_ratio"),
                       "params": json.dumps(ev["params"], separators=(",", ":"))})
        logf.flush()
        if not fails and sc > best["score"]:
            best.update(score=sc, params=ev["params"], ev=ev)
            save_json({"score": sc, "params": ev["params"], "stocks": st, "etf": et, "time": dt.datetime.now().isoformat(timespec="seconds")}, SWEEP_DIR / "best_params.json")
            print(f"  [{counter[0]}] NEW BEST score {sc:.3f} | stocks PF {st['profit_factor']:.2f} ratio {st['recovery_ratio']:.2f} P&L ${st['total_pnl']:,.0f} DD ${st['max_drawdown']:,.0f} years+ {st['positive_years_pct']:.0f}% | etf PF {et['profit_factor']:.2f} ratio {et['recovery_ratio']:.2f}")
        return sc, fails

    t0 = time.time()
    lower_priority()
    with Pool(workers, initializer=_init, initargs=(settings, stocks, etfs, bench, start, end, uni_names, cfg, baseline_ratio)) as pool:
        # 0. the current params, as a reference point
        record(pool.apply(evaluate, (base_params,)))

        # 1. random search
        if args.samples > 0:
            rng = random.Random(args.seed)
            cands = [sample(base_params, rng) for _ in range(args.samples)]
            print(f"random search: {len(cands)} candidates")
            for k, ev in enumerate(pool.imap_unordered(evaluate, cands, chunksize=2), 1):
                record(ev)
                if k % 100 == 0:
                    print(f"  {k}/{len(cands)} done, {time.time()-t0:.0f}s, best score {best['score']:.3f}")

        # 2. coordinate refinement from the best (or from current params if nothing passed)
        if args.refine or args.samples > 0:
            seed_params = best["params"] or base_params
            improved, passes = True, 0
            while improved and passes < args.refine_passes:
                improved, passes = False, passes + 1
                # Every one-knob variation of the seed goes out in ONE parallel batch (~70 candidates), then the
                # seed moves to the best of them. The old knob-by-knob loop sent 2-6 candidates at a time and left
                # most workers idle for the whole refinement phase.
                trial = []
                for path, vals in SPACE.items():
                    cur = get_or(seed_params, path)
                    for v in vals:
                        if v == cur:
                            continue
                        p = copy.deepcopy(seed_params)
                        put(p, path, v)
                        if not _fast_slow_ok(p):
                            continue
                        trial.append(p)
                before = best["score"]
                for ev in pool.imap_unordered(evaluate, trial, chunksize=2):
                    record(ev)
                if best["score"] > before:
                    improved = True
                    seed_params = best["params"]
                print(f"refinement pass {passes} done ({len(trial)} neighbours), best score {best['score']:.3f}, {time.time()-t0:.0f}s")

        # 3. plateau pass: prefer settings whose NEIGHBOURS also work. A sharp peak is luck;
        #    a broad plateau is an edge. Re-rank the top passing candidates by the average
        #    score of themselves plus every one-step neighbour (failing neighbours count 0).
        top_k = int(args.plateau_top_k or cfg.get("plateau_top_k", 8))
        passing = sorted([r for r in records if not r[1]], key=lambda r: -r[0])
        seen, tops = set(), []
        for sc, _, ev in passing:
            key = json.dumps(ev["params"], sort_keys=True)
            if key not in seen:
                seen.add(key)
                tops.append((sc, ev["params"]))
            if len(tops) >= top_k:
                break
        if tops:
            print(f"plateau pass over top {len(tops)} candidates")
            plateau = []
            for sc, prm in tops:
                neigh = []
                for path, vals in SPACE.items():
                    cur = get_or(prm, path)
                    if cur not in vals:
                        continue
                    i = vals.index(cur)
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(vals):
                            p2 = copy.deepcopy(prm)
                            put(p2, path, vals[j])
                            if _fast_slow_ok(p2):
                                neigh.append(p2)
                scores = [sc]
                for ev in pool.imap_unordered(evaluate, neigh):
                    s2, f2 = score_and_check(ev, baseline_ratio, cfg, args.primary)
                    scores.append(s2 if not f2 else 0.0)
                    record(ev)
                avg = sum(scores) / len(scores)
                plateau.append((avg, sc, prm))
                print(f"  candidate score {sc:.3f} -> plateau {avg:.3f} ({len(neigh)} neighbours, {sum(1 for x in scores if x > 0)} pass)")
            plateau.sort(key=lambda x: -x[0])
            avg, sc, prm = plateau[0]
            chosen = next(r for r in records if r[2]["params"] == prm)
            best.update(score=sc, params=prm, ev=chosen[2], plateau=avg)
            st, et = chosen[2][uni_names[0]], chosen[2][uni_names[1]]
            save_json({"score": sc, "plateau_score": avg, "params": prm, "stocks": st, "etf": et, "universes": list(uni_names),
                       "time": dt.datetime.now().isoformat(timespec="seconds")}, SWEEP_DIR / "best_params.json")
            print(f"plateau winner: score {sc:.3f}, plateau {avg:.3f}")

    logf.close()
    print(f"\nsweep finished in {time.time()-t0:.0f}s, {counter[0]} evaluations")
    if best["params"] is None:
        print("No candidate passed every constraint. Nothing applied. See results/sweep/log.csv.")
        return
    st, et = best["ev"][uni_names[0]], best["ev"][uni_names[1]]
    print(f"WINNER score {best['score']:.3f}\n  stocks: trades {st['trades']} PF {st['profit_factor']:.2f} P&L ${st['total_pnl']:,.0f} DD ${st['max_drawdown']:,.0f} ratio {st['recovery_ratio']:.2f} positive years {st['positive_years_pct']:.0f}%\n  etf:    trades {et['trades']} PF {et['profit_factor']:.2f} P&L ${et['total_pnl']:,.0f} DD ${et['max_drawdown']:,.0f} ratio {et['recovery_ratio']:.2f}")
    print(json.dumps(best["params"], indent=2))
    if args.apply:
        new_version = int(base_params.get("version", 1)) + 1
        p = copy.deepcopy(best["params"])
        p["version"] = new_version
        save_json(p, params_path)
        with open(ROOT / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write(f"\n## v{new_version} — parameter sweep ({dt.datetime.now().date()})\n"
                    f"Compute-only search, {counter[0]} evaluations on {start} -> {end}, both universes.\n\n"
                    f"| | stocks | ETFs |\n|---|---|---|\n| trades | {st['trades']} | {et['trades']} |\n| profit factor | {st['profit_factor']:.2f} | {et['profit_factor']:.2f} |\n"
                    f"| total P&L | ${st['total_pnl']:,.0f} | ${et['total_pnl']:,.0f} |\n| max drawdown | ${st['max_drawdown']:,.0f} | ${et['max_drawdown']:,.0f} |\n| P&L / DD | {st['recovery_ratio']:.2f} | {et['recovery_ratio']:.2f} |\n"
                    f"| positive years | {st['positive_years_pct']:.0f}% | |\n")
        print(f"applied -> {params_path} v{new_version}")


if __name__ == "__main__":
    main()
