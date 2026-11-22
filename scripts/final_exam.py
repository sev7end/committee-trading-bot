"""The final exam: run the best params AND the baseline on the HOLDOUT window.

    python scripts/final_exam.py

Run this only when you are done tuning. If you look at the holdout, change the
strategy, and look again, the holdout is no longer a holdout. If you must keep
tuning afterwards, move the windows forward in config/settings.json so a fresh
untouched period becomes the new holdout.
"""
import subprocess
import sys

import _common  # noqa: F401

from tradingbot.config import CONFIG_DIR, RESULTS_DIR, ROOT, load_settings
from tradingbot.report import build_report
from tradingbot.tradelog import load_result


def run(params, out, label):
    r = subprocess.run(
        [sys.executable, "scripts/run_backtest.py", "--params", str(params), "--window", "holdout", "--out", out, "--label", label],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        sys.exit(r.stdout + r.stderr)
    return load_result(RESULTS_DIR / out)


def main():
    s = load_settings()
    best_params = RESULTS_DIR / "best" / "params.json"
    if not best_params.exists():
        best_params = CONFIG_DIR / "params.json"
    train_best = load_result(RESULTS_DIR / "best")
    train_base = load_result(RESULTS_DIR / "baseline")
    hb = run(best_params, "holdout", "best params on HOLDOUT")
    hbase = run(CONFIG_DIR / "baseline_params.json", "baseline_holdout", "baseline on HOLDOUT")
    build_report()

    w = s["windows"]
    print(f"\nHOLDOUT {w['holdout']['start']} -> {w['holdout']['end']}  (train was {w['train']['start']} -> {w['train']['end']})\n")
    print(f"{'':28s}{'trades':>8s}{'win%':>7s}{'P&L':>10s}{'PF':>7s}{'maxDD':>10s}{'P&L/DD':>8s}")
    for label, r in [("best — TRAIN", train_best), ("best — HOLDOUT", hb), ("baseline — TRAIN", train_base), ("baseline — HOLDOUT", hbase)]:
        if r is None:
            continue
        m = r["metrics"]
        print(f"{label:28s}{m['trades']:>8d}{m['win_rate']:>6.0f}%{m['total_pnl']:>10,.0f}{m['profit_factor']:>7.2f}{m['max_drawdown']:>10,.0f}{m['recovery_ratio']:>8.2f}")

    if train_best and hb:
        tr, ho = train_best["metrics"], hb["metrics"]
        print("\nVERDICT")
        if ho["profit_factor"] < 1.0:
            print("  Lost money on unseen data. The train result was overfit. Simplify the rules.")
        elif ho["profit_factor"] < tr["profit_factor"] * 0.6:
            print("  Much weaker on unseen data than on train. Partly overfit. Be sceptical.")
        elif ho["recovery_ratio"] <= hbase["metrics"]["recovery_ratio"] and ho["profit_factor"] <= hbase["metrics"]["profit_factor"]:
            print("  Profitable, but no better than the dumb baseline on unseen data (risk-adjusted). The committee adds nothing yet.")
        else:
            print("  Held up on unseen data and beat the baseline on profit per unit of risk. Worth paper trading. Still not proof.")


if __name__ == "__main__":
    main()
