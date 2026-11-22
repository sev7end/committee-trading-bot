"""Download and cache price data for the whole universe plus the benchmark.

    python scripts/fetch_data.py            # fetch what is missing
    python scripts/fetch_data.py --refresh  # re-download everything
"""
import argparse
import _common  # noqa: F401

from tradingbot.config import load_settings, load_universe
from tradingbot.data import load_bars, provider_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    settings = load_settings()
    symbols = load_universe() + [settings["benchmark"]]
    end = settings["windows"]["holdout"]["end"]
    print(f"provider: {provider_name(settings)}")
    bars = load_bars(symbols, settings["data_start"], end, settings, refresh=args.refresh)
    for sym, df in sorted(bars.items()):
        print(f"{sym:6s} {len(df):4d} bars  {df.index.min().date()} -> {df.index.max().date()}")
    print(f"\n{len(bars)}/{len(symbols)} symbols cached in data/")


if __name__ == "__main__":
    main()
