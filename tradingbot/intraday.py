"""Intraday Opening Range Breakout (ORB) backtester on 5-minute bars.

Rules (all configurable):
  - opening range = high/low of the first `range_bars` bars of the session (3 bars = 15 minutes)
  - long entry when a later bar trades above the range high (+ buffer); short entry below the range low
    (if direction == "both"). One trade per symbol per day, first breakout wins.
  - stop: "range" (opposite side of the range) or ("atr", f) = f x the 14-day ATR from the entry
  - optional target at `target_r` x the initial risk; otherwise ride to the close
  - every position is flat by the last bar of the day (exit at that bar's close). Nothing held overnight.
  - fills pay the same cost model as the daily books (half spread + slippage per side)
  - sizing: risk `risk_pct` of the symbol's capital slice per trade, notional capped at `max_position_pct`

Bars are strictly processed in time order; the entry fills at max(range_high, bar_open) for longs,
so a gap through the level fills at the open of that bar, never at a price that did not trade.
"""
from __future__ import annotations
import datetime as dt
import math

import numpy as np
import pandas as pd

from .costs import CostModel
from .metrics import compute_metrics

DEFAULT = {"range_bars": 3, "direction": "long", "stop": "range", "stop_atr_frac": 0.1, "target_r": None,
           "entry_buffer_bps": 0.0, "risk_pct": 1.0, "max_position_pct": 100.0, "min_range_pct": 0.0, "last_entry_bar": 60}


def daily_atr_from_bars(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    d = bars.groupby(bars.index.date).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    d.index = pd.to_datetime(d.index)
    prev = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean().shift(1)  # ATR known at the START of the day (yesterday's value)


def orb_backtest(bars: pd.DataFrame, cfg: dict | None, costs: CostModel, capital: float, symbol: str = "") -> tuple[list[dict], pd.Series]:
    c = {**DEFAULT, **(cfg or {})}
    atr = daily_atr_from_bars(bars)
    trades: list[dict] = []
    equity = capital
    eq_points = {}
    rb, buf = int(c["range_bars"]), float(c["entry_buffer_bps"]) / 10_000
    for day, g in bars.groupby(bars.index.date):
        day_ts = pd.Timestamp(day)
        eq_points[day_ts] = equity
        if len(g) < rb + 2:
            continue
        a = atr.get(day_ts, np.nan)
        if c["stop"] == "atr" and (a is None or np.isnan(a)):
            continue
        rng = g.iloc[:rb]
        hi, lo = float(rng["high"].max()), float(rng["low"].min())
        if hi <= lo or (hi - lo) / lo * 100 < float(c["min_range_pct"]):
            continue
        o, h, l, cl = g["open"].values, g["high"].values, g["low"].values, g["close"].values
        n = len(g)
        last_entry = min(n - 2, rb + int(c["last_entry_bar"]))
        pos = None
        for i in range(rb, n):
            if pos is None:
                if i > last_entry:
                    break
                if h[i] > hi * (1 + buf):
                    raw = max(hi * (1 + buf), o[i])
                    entry = costs.buy_price(raw)
                    stop = lo if c["stop"] == "range" else entry - float(c["stop_atr_frac"]) * a
                    risk = entry - stop
                    if risk <= 0:
                        break
                    shares = int(min(equity * float(c["risk_pct"]) / 100 / risk, equity * float(c["max_position_pct"]) / 100 / entry))
                    if shares <= 0:
                        break
                    target = entry + float(c["target_r"]) * risk if c.get("target_r") else None
                    pos = {"side": "long", "entry": entry, "stop": stop, "target": target, "shares": shares, "i": i}
                    fees_in = costs.fees(shares, shares * entry, "buy")
                    # stop hit in the entry bar itself
                    if l[i] <= stop:
                        px = costs.sell_price(stop)
                        _close(trades, pos, px, g.index[i], "stop_same_bar", fees_in, costs, symbol, day_ts)
                        equity += trades[-1]["pnl"]
                        pos = None
                        break
                    continue
                if c["direction"] == "both" and l[i] < lo * (1 - buf):
                    raw = min(lo * (1 - buf), o[i])
                    entry = costs.sell_price(raw)
                    stop = hi if c["stop"] == "range" else entry + float(c["stop_atr_frac"]) * a
                    risk = stop - entry
                    if risk <= 0:
                        break
                    shares = int(min(equity * float(c["risk_pct"]) / 100 / risk, equity * float(c["max_position_pct"]) / 100 / entry))
                    if shares <= 0:
                        break
                    target = entry - float(c["target_r"]) * risk if c.get("target_r") else None
                    pos = {"side": "short", "entry": entry, "stop": stop, "target": target, "shares": shares, "i": i}
                    fees_in = costs.fees(shares, shares * entry, "sell")
                    if h[i] >= stop:
                        px = costs.buy_price(stop)
                        _close(trades, pos, px, g.index[i], "stop_same_bar", fees_in, costs, symbol, day_ts)
                        equity += trades[-1]["pnl"]
                        pos = None
                        break
                    continue
            else:
                if pos["side"] == "long":
                    if l[i] <= pos["stop"]:
                        px = costs.sell_price(min(o[i], pos["stop"])); reason = "stop"
                    elif pos["target"] is not None and h[i] >= pos["target"]:
                        px = costs.sell_price(max(o[i], pos["target"])); reason = "target"
                    elif i == n - 1:
                        px = costs.sell_price(cl[i]); reason = "close"
                    else:
                        continue
                else:
                    if h[i] >= pos["stop"]:
                        px = costs.buy_price(max(o[i], pos["stop"])); reason = "stop"
                    elif pos["target"] is not None and l[i] <= pos["target"]:
                        px = costs.buy_price(min(o[i], pos["target"])); reason = "target"
                    elif i == n - 1:
                        px = costs.buy_price(cl[i]); reason = "close"
                    else:
                        continue
                _close(trades, pos, px, g.index[i], reason, fees_in, costs, symbol, day_ts)
                equity += trades[-1]["pnl"]
                pos = None
                break
        eq_points[day_ts] = equity
    return trades, pd.Series(eq_points, dtype=float).sort_index()


def _close(trades, pos, px, ts, reason, fees_in, costs, symbol, day_ts):
    sh = pos["shares"]
    fees_out = costs.fees(sh, sh * px, "sell" if pos["side"] == "long" else "buy")
    pnl = (px - pos["entry"]) * sh if pos["side"] == "long" else (pos["entry"] - px) * sh
    pnl -= fees_in + fees_out
    trades.append({"symbol": symbol, "entry_date": day_ts.date().isoformat(), "exit_date": day_ts.date().isoformat(), "side": pos["side"],
                   "entry_price": round(pos["entry"], 4), "exit_price": round(px, 4), "shares": sh, "stop": round(pos["stop"], 4),
                   "pnl": round(pnl, 2), "pnl_pct": round((px / pos["entry"] - 1) * 100 * (1 if pos["side"] == "long" else -1), 3),
                   "exit_reason": reason, "exit_time": str(ts)[11:16], "bars_held": 0})


def portfolio(bars_by_symbol: dict[str, pd.DataFrame], cfg: dict | None, costs: CostModel, starting_cash: float, start: str, end: str) -> dict:
    """Run ORB per symbol on an equal capital slice and combine. Returns metrics, yearly P&L, per-symbol PF."""
    n = len(bars_by_symbol)
    slice_cap = starting_cash / n
    all_trades, curves, per_sym = [], [], {}
    for sym, df in bars_by_symbol.items():
        d = df.loc[start:end]
        tr, eq = orb_backtest(d, cfg, costs, slice_cap, sym)
        all_trades += tr
        curves.append(eq)
        wins = sum(t["pnl"] for t in tr if t["pnl"] > 0); losses = -sum(t["pnl"] for t in tr if t["pnl"] <= 0)
        per_sym[sym] = {"trades": len(tr), "pf": (wins / losses) if losses > 0 else float("inf"), "pnl": sum(t["pnl"] for t in tr)}
    idx = curves[0].index
    for c in curves[1:]:
        idx = idx.union(c.index)
    total = sum(c.reindex(idx).ffill().fillna(slice_cap) for c in curves)
    m = compute_metrics(all_trades, total, starting_cash)
    yearly = {int(y): round(float(v.iloc[-1] - v.iloc[0]), 0) for y, v in total.groupby(total.index.year)}
    return {"metrics": m, "yearly": yearly, "per_symbol": per_sym, "equity": total, "trades": all_trades}
