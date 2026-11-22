"""Cross-sectional momentum rotation: hold the handful of assets that have risen the most, re-ranked on a schedule.

A different mechanism from the signal-per-symbol committee. Each rebalance day (first bar of each month by default)
every symbol gets a momentum score, the average of its returns over `lookbacks` bars, each measured up to `skip` bars
ago (the classic 12-1 momentum skips the most recent month, which tends to mean-revert). The `top_n` highest scores
are held in equal weight until the next rebalance; with `abs_filter` on, a symbol is only held if its score is
positive, and the unused share stays in cash (or `cash_symbol`, e.g. a bond ETF). Trades fill at the NEXT bar's open
plus costs, like the committee engine. Rebalances only trade the difference (a symbol kept is not sold and rebought).

Why it is worth a look: decades of published evidence, a handful of trades a month so costs hardly matter, and it
is uncorrelated with the breakout book by construction (monthly ranks vs daily breakouts).
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import CostModel
from .metrics import compute_metrics

DEFAULT = {"lookbacks": [126, 252], "skip": 21, "top_n": 3, "rebalance": "M", "abs_filter": True, "cash_symbol": None,
           "min_history": 260, "vol_weight": False, "max_weight": 0.5,
           "regime_sma": 0,          # > 0: hold nothing (or cash_symbol) while the benchmark closes below its N-day average
           "vol_lookback": 63}       # vol_weight: size each pick by 1 / its realised volatility (risk parity among the picks)


@dataclass
class RotationResult:
    trades: list
    equity: pd.Series
    metrics: dict
    params: dict
    window: dict
    holdings: pd.DataFrame


def momentum_scores(closes: pd.DataFrame, lookbacks: list[int], skip: int) -> pd.DataFrame:
    base = closes.shift(skip)
    parts = [base / closes.shift(skip + int(n)) - 1.0 for n in lookbacks]
    return sum(parts) / len(parts)


def rebalance_dates(index: pd.DatetimeIndex, freq: str) -> list[pd.Timestamp]:
    """First trading day of each period (M = month, W = week, Q = quarter) inside the index."""
    if freq == "W":
        key = index.to_period("W")
    elif freq == "Q":
        key = index.to_period("Q")
    else:
        key = index.to_period("M")
    s = pd.Series(index, index=index)
    return list(s.groupby(key).first())


def rotation_backtest(bars: dict[str, pd.DataFrame], params: dict, settings: dict, start: str, end: str,
                      benchmark: pd.DataFrame | None = None) -> RotationResult:
    p = {**DEFAULT, **params}
    costs = CostModel(settings.get("costs", {}))
    cash0 = float(settings.get("account", {}).get("starting_cash", 25_000))
    closes = pd.DataFrame({s: df["close"] for s, df in bars.items()}).sort_index()
    opens = pd.DataFrame({s: df["open"] for s, df in bars.items()}).reindex(closes.index)
    scores = momentum_scores(closes, list(p["lookbacks"]), int(p["skip"]))
    enough = closes.notna().rolling(int(p["min_history"]), min_periods=1).count() >= int(p["min_history"])
    scores = scores.where(enough)
    vols = closes.pct_change().rolling(int(p["vol_lookback"]), min_periods=20).std()
    risk_on = pd.Series(True, index=closes.index)
    if int(p.get("regime_sma", 0)) > 0 and benchmark is not None and len(benchmark):
        b = benchmark["close"]
        risk_on = (b > b.rolling(int(p["regime_sma"]), min_periods=int(p["regime_sma"])).mean()).reindex(closes.index, method="ffill").fillna(False)

    cal = closes.loc[start:end].index
    if len(cal) < 5:
        return RotationResult([], pd.Series(dtype=float), compute_metrics([], pd.Series(dtype=float), cash0), p, {"start": start, "end": end}, pd.DataFrame())
    rb = set(rebalance_dates(cal, str(p["rebalance"])))
    cash = cash0
    pos: dict[str, dict] = {}       # symbol -> {shares, entry_price, entry_date, fees}
    trades, equity, rows = [], {}, []
    pending_target: dict[str, float] | None = None   # weights decided at a rebalance close, executed next open

    def value_at(date, use_open=False):
        v = cash
        for s, h in pos.items():
            px = opens.at[date, s] if use_open else closes.at[date, s]
            if pd.isna(px):
                px = closes[s].asof(date)
            v += h["shares"] * float(px)
        return v

    for i, date in enumerate(cal):
        # 1. execute yesterday's rebalance decision at today's open
        if pending_target is not None:
            total = value_at(date, use_open=True)
            for s in list(pos):
                if s not in pending_target:
                    h = pos.pop(s)
                    px = costs.sell_price(float(opens.at[date, s]))
                    fee = costs.fees(h["shares"], px * h["shares"], "sell")
                    cash += px * h["shares"] - fee
                    trades.append({"symbol": s, "entry_date": h["entry_date"].date().isoformat(), "exit_date": date.date().isoformat(),
                                   "entry_price": round(h["entry_price"], 4), "exit_price": round(px, 4), "shares": h["shares"],
                                   "pnl": round((px - h["entry_price"]) * h["shares"] - fee - h["fees"], 2),
                                   "pnl_pct": round((px / h["entry_price"] - 1) * 100, 3), "bars_held": int(h["bars"]), "exit_reason": "rotation",
                                   "fees": round(fee + h["fees"], 2)})
            for s, w in pending_target.items():
                raw = opens.at[date, s]
                if pd.isna(raw) or raw <= 0:
                    continue
                target_val = total * w
                have = pos[s]["shares"] * float(raw) if s in pos else 0.0
                diff = target_val - have
                if s in pos and abs(diff) < 0.02 * total:
                    continue   # close enough: no churn
                if diff > 0:
                    px = costs.buy_price(float(raw))
                    sh = int(min(diff, cash) / px)
                    if sh <= 0:
                        continue
                    fee = costs.fees(sh, px * sh, "buy")
                    cash -= px * sh + fee
                    if s in pos:
                        h = pos[s]
                        tot = h["shares"] + sh
                        h["entry_price"] = (h["entry_price"] * h["shares"] + px * sh) / tot
                        h["shares"], h["fees"] = tot, h["fees"] + fee
                    else:
                        pos[s] = {"shares": sh, "entry_price": px, "entry_date": date, "fees": fee, "bars": 0}
                else:
                    h = pos[s]
                    sh = min(h["shares"], int(-diff / float(raw)))
                    if sh <= 0:
                        continue
                    px = costs.sell_price(float(raw))
                    fee = costs.fees(sh, px * sh, "sell")
                    cash += px * sh - fee
                    trades.append({"symbol": s, "entry_date": h["entry_date"].date().isoformat(), "exit_date": date.date().isoformat(),
                                   "entry_price": round(h["entry_price"], 4), "exit_price": round(px, 4), "shares": sh,
                                   "pnl": round((px - h["entry_price"]) * sh - fee, 2), "pnl_pct": round((px / h["entry_price"] - 1) * 100, 3),
                                   "bars_held": int(h["bars"]), "exit_reason": "trim", "fees": round(fee, 2)})
                    h["shares"] -= sh
                    if h["shares"] <= 0:
                        pos.pop(s)
            pending_target = None

        for h in pos.values():
            h["bars"] += 1
        equity[date] = value_at(date)
        rows.append({"date": date, **{s: h["shares"] for s, h in pos.items()}})

        # 2. rank at the close of a rebalance day -> target weights for tomorrow's open
        if date in rb and i < len(cal) - 1:
            sc = scores.loc[date].dropna().sort_values(ascending=False)
            if p["abs_filter"]:
                sc = sc[sc > 0]
            chosen = list(sc.index[: int(p["top_n"])]) if bool(risk_on.get(date, True)) else []
            n = int(p["top_n"])
            if p.get("vol_weight") and chosen:
                inv = pd.Series({s: 1.0 / max(float(vols.at[date, s]), 1e-6) for s in chosen if not pd.isna(vols.at[date, s])})
                weights = (inv / inv.sum() * (len(chosen) / n)).clip(upper=float(p["max_weight"])) if len(inv) else pd.Series(dtype=float)
                target = {s: float(w) for s, w in weights.items()}
            else:
                w = min(1.0 / n, float(p["max_weight"]))
                target = {s: w for s in chosen}
            used = sum(target.values())
            if p.get("cash_symbol") and p["cash_symbol"] in closes.columns and used < 0.999:
                target[p["cash_symbol"]] = target.get(p["cash_symbol"], 0.0) + (1.0 - used)
            pending_target = target

    # close everything at the end so the trade list is complete
    last = cal[-1]
    for s, h in list(pos.items()):
        px = costs.sell_price(float(closes.at[last, s]))
        fee = costs.fees(h["shares"], px * h["shares"], "sell")
        trades.append({"symbol": s, "entry_date": h["entry_date"].date().isoformat(), "exit_date": last.date().isoformat(),
                       "entry_price": round(h["entry_price"], 4), "exit_price": round(px, 4), "shares": h["shares"],
                       "pnl": round((px - h["entry_price"]) * h["shares"] - fee - h["fees"], 2),
                       "pnl_pct": round((px / h["entry_price"] - 1) * 100, 3), "bars_held": int(h["bars"]), "exit_reason": "end_of_data", "fees": round(fee + h["fees"], 2)})
    eq = pd.Series(equity, dtype=float).sort_index()
    return RotationResult(trades, eq, compute_metrics(trades, eq, cash0), p, {"start": start, "end": end}, pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame())
