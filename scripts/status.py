"""One-screen status of the research loop: best-so-far, every round, and what is running.

    python scripts/status.py
"""
import json
import _common  # noqa: F401

from tradingbot.config import RESULTS_DIR, load_json
from tradingbot.tradelog import load_result


def main():
    best = load_result(RESULTS_DIR / "best")
    base = load_result(RESULTS_DIR / "baseline")
    lock = RESULTS_DIR / "loop.lock"
    print(f"loop running: {'yes (pid ' + lock.read_text().strip() + ')' if lock.exists() else 'no'}")
    if base:
        m = base["metrics"]
        print(f"baseline : PF {m['profit_factor']:.2f}  P&L ${m['total_pnl']:,.0f}  DD ${m['max_drawdown']:,.0f}  ratio {m['recovery_ratio']:.2f}  trades {m['trades']}")
    if best:
        m = best["metrics"]
        eq = best["equity"]
        mid = eq.index[len(eq) // 2]
        print(f"best v{best.get('params_version')}: PF {m['profit_factor']:.2f}  P&L ${m['total_pnl']:,.0f}  DD ${m['max_drawdown']:,.0f}  ratio {m['recovery_ratio']:.2f}  trades {m['trades']}"
              f"  | halves ${float(eq.loc[mid]-eq.iloc[0]):,.0f} / ${float(eq.iloc[-1]-eq.loc[mid]):,.0f}")
    rounds_dir = RESULTS_DIR / "rounds"
    files = sorted(rounds_dir.glob("round_*.json"), key=lambda p: int(p.stem.split("_")[1])) if rounds_dir.exists() else []
    kept = sum(1 for p in files if json.loads(p.read_text(encoding="utf-8")).get("accepted"))
    print(f"\nrounds: {len(files)} total, {kept} kept\n")
    for p in files:
        r = json.loads(p.read_text(encoding="utf-8"))
        m = r.get("metrics") or {}
        tag = "KEPT" if r.get("accepted") else "rev "
        print(f"{r['round']:>3} {r['time'][11:16]} {tag} v{r.get('version')}  PF {m.get('profit_factor',0):.2f}  P&L ${m.get('total_pnl',0):>7,.0f}  DD ${m.get('max_drawdown',0):>6,.0f}  ratio {m.get('recovery_ratio',0):.2f}  | {r.get('hypothesis','')[:95]}")
        if not r.get("accepted"):
            print(f"{'':20}-> {r.get('reason','')[:110]}")
    hold = load_result(RESULTS_DIR / "holdout")
    if hold:
        m = hold["metrics"]
        print(f"\nHOLDOUT (final exam) v{hold.get('params_version')}: PF {m['profit_factor']:.2f}  P&L ${m['total_pnl']:,.0f}  DD ${m['max_drawdown']:,.0f}  trades {m['trades']}")


if __name__ == "__main__":
    main()
