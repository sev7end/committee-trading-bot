"""Run one backtest and save it under results/<name>/.

    python scripts/run_backtest.py                              # current params, train window -> results/current
    python scripts/run_backtest.py --params config/baseline_params.json --out baseline
    python scripts/run_backtest.py --window holdout --out holdout    # ONLY when you are done tuning
    python scripts/run_backtest.py --no-costs                   # see how much costs matter
"""
import argparse
import time
import _common  # noqa: F401

from tradingbot.backtest import run_backtest
from tradingbot.config import RESULTS_DIR, book_settings, find_book, load_params, load_settings, load_universe
from tradingbot.data import load_bars
from tradingbot.tradelog import format_metrics_table, save_result


def ROOT_PARAMS(book):
    from tradingbot.config import ROOT
    return ROOT / book["params"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default=None, help="params file (default config/params.json)")
    ap.add_argument("--window", default="train", choices=["train", "holdout", "all"])
    ap.add_argument("--out", default=None, help="results sub-folder name (default = current, or holdout)")
    ap.add_argument("--label", default="")
    ap.add_argument("--no-costs", action="store_true", help="idealised fills, for comparison only")
    ap.add_argument("--universe", default=None, help="stocks (config/universe.json), etf (settings.validation_universe) or any <name> with settings.<name>_universe")
    ap.add_argument("--book", default=None, help="use this book's params, universe, benchmark and costs (settings.books)")
    args = ap.parse_args()

    settings = load_settings()
    if args.book:
        book = find_book(settings, args.book)
        settings = book_settings(settings, book)
        args.universe = args.universe or book.get("universe", "stocks")
        args.params = args.params or str(ROOT_PARAMS(book))
    params = load_params(args.params)
    if args.no_costs:
        settings["costs"] = {}
    if args.window == "all":
        start, end = settings["windows"]["train"]["start"], settings["windows"]["holdout"]["end"]
    else:
        start, end = settings["windows"][args.window]["start"], settings["windows"][args.window]["end"]
    out_name = args.out or ("current" if args.window == "train" else args.window)

    from tradingbot.config import resolve_universe
    symbols = resolve_universe(args.universe, settings)
    bench_sym = settings["benchmark"]
    t0 = time.time()
    bars = load_bars(list(dict.fromkeys(symbols + [bench_sym])), settings["data_start"], end, settings)
    bench = bars.get(bench_sym)
    bars = {k: v for k, v in bars.items() if k in symbols}   # the benchmark stays tradable when the universe lists it (SPY in the ETF set)
    res = run_backtest(bars, bench, params, settings, start, end)
    out_dir = RESULTS_DIR / out_name
    save_result(res, out_dir, label=args.label or f"{args.window} window")

    print(f"\n== {out_name}  ({start} -> {end}, params v{params.get('version')}, {len(bars)} symbols, {time.time()-t0:.1f}s)")
    print(format_metrics_table(res.metrics))
    print(f"skipped entries: {res.skipped}")
    print(f"saved -> {out_dir}")


if __name__ == "__main__":
    main()
