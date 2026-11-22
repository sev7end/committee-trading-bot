"""Set stops from the data: how far did price move AGAINST each historical trade before it resolved?

    python scripts/trade_analysis.py                       # trend book, live params, train window
    python scripts/trade_analysis.py --book mr --window holdout

For every trade the backtester produced, this walks the daily bars between entry and exit and measures, in ATR
units at entry:
  MAE  maximum adverse excursion  = how far below the entry the LOW got (the pain before the outcome)
  MFE  maximum favourable excursion = how far above the entry the HIGH got (the gain that was available)
Then it answers the questions a discretionary trader asks of a trade journal:
  - of the trades that ended as winners, how deep did they dip first? If 90% of winners never dipped more than
    1.5 ATR, a 3 ATR stop is paying for insurance that is rarely needed and losing more when it is hit.
  - of the losers, how much profit was on the table before they turned? If most losers were up 1.5 ATR at some
    point, the exit is leaving money behind and a trailing stop or a nearer target would capture it.
  - a "what if" table: for each alternative stop distance, the P&L had every trade been stopped there (approximate:
    a trade whose MAE exceeds the stop is closed at the stop, otherwise it keeps its real result).
Writes results/analysis/<book>_<window>_excursions.csv and prints the summary.
"""
from __future__ import annotations
import argparse

import numpy as np
import pandas as pd

import _common  # noqa: F401

from tradingbot.backtest import run_backtest
from tradingbot.config import RESULTS_DIR, ROOT, load_json, load_settings, resolve_universe
from tradingbot.data import load_bars
from tradingbot.indicators import atr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="trend")
    ap.add_argument("--window", default="train", choices=["train", "holdout"])
    ap.add_argument("--params", default=None)
    args = ap.parse_args()
    settings = load_settings()
    book = next(b for b in settings["books"] if b["name"] == args.book)
    params = load_json(ROOT / (args.params or book["params"]))
    uni_name = book.get("universe", "stocks")
    syms = resolve_universe(uni_name, settings)
    w = settings["windows"][args.window]
    bars = load_bars(list(dict.fromkeys(syms + [settings["benchmark"]])), settings["data_start"], w["end"], settings)
    bench = bars[settings["benchmark"]]
    uni = {k: bars[k] for k in syms if k in bars}
    res = run_backtest(uni, bench, params, settings, w["start"], w["end"])
    trades = pd.DataFrame(res.trades)
    stop_atr = float(params["exit"]["stop_atr"])
    n_atr = int(params["exit"].get("atr_period", 14))
    atrs = {s: atr(df, n_atr) for s, df in uni.items()}

    rows = []
    for t in trades.itertuples():
        df = uni[t.symbol]
        seg = df.loc[t.entry_date:t.exit_date]
        a = float(atrs[t.symbol].asof(pd.Timestamp(t.entry_date)))
        if not (a > 0) or len(seg) == 0:
            continue
        mae = (t.entry_price - seg["low"].min()) / a           # ATRs of pain
        mfe = (seg["high"].max() - t.entry_price) / a          # ATRs of joy that were available
        rows.append({"symbol": t.symbol, "entry_date": t.entry_date, "exit_reason": t.exit_reason, "pnl": t.pnl, "bars_held": t.bars_held,
                     "mae_atr": mae, "mfe_atr": mfe, "result_atr": (t.exit_price - t.entry_price) / a, "shares": t.shares, "atr": a})
    ex = pd.DataFrame(rows)
    out = RESULTS_DIR / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    ex.to_csv(out / f"{args.book}_{args.window}_excursions.csv", index=False)

    win, loss = ex[ex.pnl > 0], ex[ex.pnl <= 0]
    q = [0.5, 0.75, 0.9, 0.95]
    print(f"{args.book} {args.window}: {len(ex)} trades, stop {stop_atr} ATR, target {params['exit'].get('target_atr')} ATR, hold {params['exit'].get('max_hold_bars')} bars")
    print(f"winners {len(win)} ({len(win) / len(ex) * 100:.0f}%), losers {len(loss)}")
    print("\nHow deep did WINNERS dip before winning (MAE, ATR units)?")
    print("  " + "  ".join(f"p{int(p * 100)}={win.mae_atr.quantile(p):.2f}" for p in q))
    for s in (0.5, 1.0, 1.5, 2.0, 2.5):
        print(f"  share of winners that dipped more than {s:.1f} ATR: {(win.mae_atr > s).mean() * 100:5.1f}%")
    print("\nHow much profit did LOSERS have on the table before losing (MFE, ATR units)?")
    print("  " + "  ".join(f"p{int(p * 100)}={loss.mfe_atr.quantile(p):.2f}" for p in q))
    for s in (0.5, 1.0, 1.5, 2.0):
        print(f"  share of losers that were up more than {s:.1f} ATR at some point: {(loss.mfe_atr > s).mean() * 100:5.1f}%")
    print("\nHow far did WINNERS run (MFE) vs what the target took:")
    print("  " + "  ".join(f"p{int(p * 100)}={win.mfe_atr.quantile(p):.2f}" for p in q))

    print("\nWhat-if: every trade stopped at X ATR instead (approximation, same entries; costs ignored on the moved exits)")
    print("  stop ATR   trades stopped   total P&L    vs actual")
    actual = ex.pnl.sum()
    for s in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
        stopped = ex.mae_atr >= s
        pnl = np.where(stopped, -s * ex.atr * ex.shares, ex.pnl).sum()
        print(f"  {s:6.1f}     {int(stopped.sum()):6d}          ${pnl:10,.0f}   {pnl - actual:+10,.0f}")
    print(f"\n(actual P&L ${actual:,.0f}; the real stop is {stop_atr} ATR so its row should be close to actual)")


if __name__ == "__main__":
    main()
