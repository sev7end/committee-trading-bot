"""Live planning for a momentum-rotation book: what to sell and buy at the next open.

Mirrors tradingbot/rotation.py exactly: ranks are computed on completed daily bars at the close before a rebalance
day (the first session of the month), and the orders go out for that session's open. On any other day the book
does nothing except let its protective stops sit. Positions carry a wide "disaster" stop (default 20% below the
fill) that the backtest does not model; it exists so the reconciliation safety net has a sell order to find and
so a broken name cannot sink the month.
"""
from __future__ import annotations
import datetime as dt

import pandas as pd

from .rotation import DEFAULT, momentum_scores


def is_rebalance_day(today: dt.date, sessions: pd.DatetimeIndex, freq: str = "M") -> bool:
    """True when `today` is the first session of a new period: the last completed session is in a different period."""
    past = sessions[sessions < pd.Timestamp(today)]
    if len(past) == 0:
        return True
    last = past[-1]
    if freq == "W":
        return pd.Timestamp(today).to_period("W") != last.to_period("W")
    if freq == "Q":
        return pd.Timestamp(today).to_period("Q") != last.to_period("Q")
    return (today.year, today.month) != (last.year, last.month)


def target_weights(bars: dict[str, pd.DataFrame], params: dict, benchmark: pd.DataFrame | None, exclude: set | None = None) -> dict[str, float]:
    """{symbol: weight} the book wants to hold from the next open, from completed bars only."""
    p = {**DEFAULT, **params}
    closes = pd.DataFrame({s: df["close"] for s, df in bars.items()}).sort_index()
    sc = momentum_scores(closes, list(p["lookbacks"]), int(p["skip"])).iloc[-1].dropna()
    enough = closes.notna().sum() >= int(p["min_history"])
    sc = sc[[s for s in sc.index if enough.get(s, False) and s not in (exclude or set())]]
    if int(p.get("regime_sma", 0)) > 0 and benchmark is not None and len(benchmark):
        b = benchmark["close"]
        if float(b.iloc[-1]) <= float(b.rolling(int(p["regime_sma"])).mean().iloc[-1]):
            return {}
    if p["abs_filter"]:
        sc = sc[sc > 0]
    chosen = list(sc.sort_values(ascending=False).index[: int(p["top_n"])])
    n = int(p["top_n"])
    if not chosen:
        return {}
    if p.get("vol_weight"):
        vols = closes[chosen].pct_change().rolling(int(p["vol_lookback"]), min_periods=20).std().iloc[-1]
        inv = (1.0 / vols.clip(lower=1e-6)).dropna()
        w = (inv / inv.sum() * (len(chosen) / n)).clip(upper=float(p["max_weight"]))
        return {s: float(x) for s, x in w.items()}
    w = min(1.0 / n, float(p["max_weight"]))
    return {s: w for s in chosen}


def plan_orders(targets: dict[str, float], holdings: dict[str, float], prices: dict[str, float], cap: float,
                min_notional: float = 0.0, churn_pct: float = 2.0) -> tuple[list[dict], list[dict]]:
    """Sells (symbols leaving, or trims) and buys (symbols entering, or top-ups) to move `holdings` to `targets`.
    A position within `churn_pct` of the book's capital of its target is left alone."""
    sells, buys = [], []
    for sym, qty in holdings.items():
        if qty <= 0:
            continue
        px = float(prices.get(sym, 0) or 0)
        want = targets.get(sym, 0.0) * cap
        have = qty * px
        if sym not in targets:
            sells.append({"symbol": sym, "qty": qty, "reason": "left the top ranks"})
        elif px > 0 and have - want > churn_pct / 100 * cap:
            trim = int((have - want) / px)
            if trim > 0:
                sells.append({"symbol": sym, "qty": trim, "reason": "trim to target weight"})
    for sym, w in targets.items():
        px = float(prices.get(sym, 0) or 0)
        if px <= 0:
            continue
        want = w * cap
        have = float(holdings.get(sym, 0)) * px
        if want - have > churn_pct / 100 * cap:
            qty = int((want - have) / px)
            if qty > 0 and qty * px >= min_notional:
                buys.append({"symbol": sym, "qty": qty, "ref_price": round(px, 2), "weight": round(w, 4)})
    return sells, buys
