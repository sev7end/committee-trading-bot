"""Turn a list of trades and an equity curve into the numbers on the dashboard."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd


def _streaks(pnls: list[float]) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
        best_w = max(best_w, cur_w)
        best_l = max(best_l, cur_l)
    return best_w, best_l


def compute_metrics(trades: list[dict], equity: pd.Series, starting_cash: float) -> dict:
    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    total_pnl = sum(pnls)

    if len(equity) > 0:
        running_peak = equity.cummax()
        dd = equity - running_peak
        max_dd = float(-dd.min())
        max_dd_pct = float(-(dd / running_peak).min() * 100) if running_peak.max() > 0 else 0.0
        rets = equity.pct_change().dropna()
        sharpe = float(rets.mean() / rets.std() * math.sqrt(252)) if len(rets) > 1 and rets.std() > 0 else 0.0
        ending = float(equity.iloc[-1])
        n_days = len(equity)
    else:
        max_dd = max_dd_pct = sharpe = 0.0
        ending = starting_cash
        n_days = 0

    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    best_w, best_l = _streaks(pnls)

    best = max(trades, key=lambda t: t["pnl"]) if trades else None
    worst = min(trades, key=lambda t: t["pnl"]) if trades else None

    def ratio(a, b):
        if b > 0:
            return a / b
        return float("inf") if a > 0 else 0.0

    return {
        "trades": n,
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": (total_pnl / n) if n else 0.0,
        "avg_win": avg_win,
        "avg_loss": -avg_loss,
        "win_loss_ratio": ratio(avg_win, avg_loss),
        "profit_factor": ratio(gross_win, gross_loss),
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "recovery_ratio": ratio(total_pnl, max_dd),
        "sharpe": sharpe,
        "starting_cash": starting_cash,
        "ending_equity": ending,
        "return_pct": (ending / starting_cash - 1) * 100 if starting_cash else 0.0,
        "trading_days": n_days,
        "trades_per_day": (n / n_days) if n_days else 0.0,
        "longest_win_streak": best_w,
        "longest_loss_streak": best_l,
        "best_trade": {"symbol": best["symbol"], "pnl": best["pnl"]} if best else None,
        "worst_trade": {"symbol": worst["symbol"], "pnl": worst["pnl"]} if worst else None,
        "exit_reasons": pd.Series([t["exit_reason"] for t in trades]).value_counts().to_dict() if trades else {},
    }


def clean_for_json(m: dict) -> dict:
    """inf is not valid JSON; store it as a big sentinel."""
    out = {}
    for k, v in m.items():
        if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
            out[k] = 999.0 if v > 0 else 0.0
        elif isinstance(v, (np.floating, np.integer)):
            out[k] = float(v)
        else:
            out[k] = v
    return out
