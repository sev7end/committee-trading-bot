"""Save and load backtest results. One folder per run:
  metrics.json  - the dashboard numbers
  trades.csv    - every trade with every voter's score at signal time
  equity.csv    - account value per day
  params.json   - the exact params that produced it
"""
from __future__ import annotations
from pathlib import Path

import pandas as pd

from .backtest import BacktestResult
from .config import save_json, load_json
from .metrics import clean_for_json


def save_result(res: BacktestResult, out_dir: Path, label: str = "") -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_json(
        {
            "label": label,
            "window": res.window,
            "metrics": clean_for_json(res.metrics),
            "skipped": res.skipped,
            "params_version": res.params.get("version"),
        },
        out_dir / "metrics.json",
    )
    save_json(res.params, out_dir / "params.json")
    pd.DataFrame(res.trades).to_csv(out_dir / "trades.csv", index=False)
    res.equity.rename("equity").to_csv(out_dir / "equity.csv", index_label="date")


def load_result(out_dir: Path) -> dict | None:
    out_dir = Path(out_dir)
    if not (out_dir / "metrics.json").exists():
        return None
    d = load_json(out_dir / "metrics.json")
    d["params"] = load_json(out_dir / "params.json") if (out_dir / "params.json").exists() else {}
    d["trades"] = pd.read_csv(out_dir / "trades.csv") if (out_dir / "trades.csv").exists() else pd.DataFrame()
    eq_path = out_dir / "equity.csv"
    d["equity"] = pd.read_csv(eq_path, index_col="date", parse_dates=True)["equity"] if eq_path.exists() else pd.Series(dtype=float)
    return d


def worst_trades(trades: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    if trades is None or len(trades) == 0:
        return pd.DataFrame()
    return trades.nsmallest(n, "pnl")


def format_metrics_table(m: dict) -> str:
    """Compact text table used in console output and in the prompt to Claude."""
    rows = [
        ("Trades", f"{m['trades']}"),
        ("Win rate", f"{m['win_rate']:.1f}%"),
        ("Total P&L", f"${m['total_pnl']:,.0f}"),
        ("Avg P&L / trade", f"${m['avg_pnl']:,.0f}"),
        ("Profit factor", f"{m['profit_factor']:.2f}"),
        ("Win/loss ratio", f"{m['win_loss_ratio']:.2f}"),
        ("Max drawdown", f"${m['max_drawdown']:,.0f} ({m['max_drawdown_pct']:.1f}%)"),
        ("Recovery ratio (P&L / max DD)", f"{m['recovery_ratio']:.2f}"),
        ("Sharpe", f"{m['sharpe']:.2f}"),
        ("Return", f"{m['return_pct']:.1f}%"),
        ("Exit reasons", ", ".join(f"{k}={v}" for k, v in m.get("exit_reasons", {}).items())),
    ]
    w = max(len(r[0]) for r in rows)
    return "\n".join(f"{k:<{w}}  {v}" for k, v in rows)
