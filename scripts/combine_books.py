"""Does a candidate book earn its place next to the others? Combine equity curves and look at the account, not the book.

    python scripts/combine_books.py --window train
    python scripts/combine_books.py --window holdout --add short=results/sweep_short/best_params.json:stocks:20

Runs every enabled book (and any --add candidates) on the window, scales each P&L to its capital share of the sizing
cap, and reports per-book stats, the correlation matrix of daily P&L, and the combined account's P&L, max drawdown
and P&L/DD with and without each candidate. A book is worth adding when the COMBINED P&L/DD improves, even if the
book alone looks mediocre; a book that only duplicates the trend book (correlation > 0.6) is not diversification.
"""
from __future__ import annotations
import argparse

import numpy as np
import pandas as pd

import _common  # noqa: F401

from tradingbot.backtest import run_backtest
from tradingbot.config import ROOT, load_json, load_settings, resolve_universe
from tradingbot.data import load_bars
from tradingbot import memo


def book_equity(params, uni, bench, settings, start, end, share_pct):
    res = run_backtest(uni, bench, params, settings, start, end)
    eq = res.equity
    pnl = eq.diff().fillna(eq.iloc[0] - settings["account"]["starting_cash"]) * (share_pct / 100)   # daily P&L scaled to the share
    return pnl, res.metrics


def dd(pnl: pd.Series) -> float:
    cum = pnl.cumsum()
    return float((cum.cummax() - cum).max())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="train", choices=["train", "holdout"])
    ap.add_argument("--add", action="append", default=[], metavar="NAME=PARAMS:UNIVERSE:PCT",
                    help="candidate book to test alongside the live ones (params file, universe name, capital share %)")
    args = ap.parse_args()
    settings = load_settings()
    memo.enable(20000)
    w = settings["windows"][args.window]
    books = [(b["name"], ROOT / b["params"], b.get("universe", "stocks"), float(b.get("capital_pct", 100))) for b in settings["books"] if b.get("enabled", True)]
    cands = []
    for spec in args.add:
        name, rest = spec.split("=", 1)
        pf, uni, pct = rest.split(":")
        cands.append((name, ROOT / pf, uni, float(pct)))
    syms = sorted({s for _, _, u, _ in books + cands for s in resolve_universe(u, settings)} | {settings["benchmark"]})
    bars = load_bars(syms, settings["data_start"], w["end"], settings)
    bench = bars[settings["benchmark"]]

    pnls, stats = {}, {}
    for name, pf, uni, pct in books + cands:
        u = {k: bars[k] for k in resolve_universe(uni, settings) if k in bars}
        pnls[name], m = book_equity(load_json(pf), u, bench, settings, w["start"], w["end"], pct)
        stats[name] = (m, pct)
        print(f"{name:8} share {pct:4.0f}%  trades {m['trades']:5}  PF {m['profit_factor']:.2f}  P&L(scaled) ${pnls[name].sum():9,.0f}  DD(scaled) ${dd(pnls[name]):7,.0f}")
    df = pd.DataFrame(pnls).fillna(0.0)
    print("\ncorrelation of daily P&L:")
    print(df.corr().round(2).to_string())
    live = [b[0] for b in books]
    base = df[live].sum(axis=1)
    print(f"\nlive books combined ({'+'.join(live)}): P&L ${base.sum():,.0f}  DD ${dd(base):,.0f}  P&L/DD {base.sum() / max(dd(base), 1):.2f}")
    for name, _, _, pct in cands:
        tot = base + df[name]
        print(f"  + {name} ({pct:.0f}%): P&L ${tot.sum():,.0f}  DD ${dd(tot):,.0f}  P&L/DD {tot.sum() / max(dd(tot), 1):.2f}"
              f"   | corr with live combo {np.corrcoef(base, df[name])[0, 1]:.2f}")
    if cands:
        # fairer: give the candidate its share by taking it from the primary book (capital is not free)
        prim = live[0]
        for name, _, _, pct in cands:
            scaled = df[prim] * (stats[prim][1] - pct) / stats[prim][1]
            tot = scaled + df[[b for b in live if b != prim]].sum(axis=1) + df[name]
            print(f"  {name} funded by cutting {prim} to {stats[prim][1] - pct:.0f}%: P&L ${tot.sum():,.0f}  DD ${dd(tot):,.0f}  P&L/DD {tot.sum() / max(dd(tot), 1):.2f}")


if __name__ == "__main__":
    main()
