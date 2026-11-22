"""Live dashboard server. Serves dashboard/index.html and a JSON snapshot of results/.

    python scripts/serve.py            # then open http://localhost:8765
    python scripts/serve.py --port 9000

Read-only: it never writes to results/ and never touches the loop. Safe to leave
running while the loop works; the page polls /api/state every 10 seconds.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

import _common  # noqa: F401

from tradingbot.config import CONFIG_DIR, RESULTS_DIR, ROOT, load_json

DASH = ROOT / "dashboard"


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _run(name: str) -> dict | None:
    d = RESULTS_DIR / name
    if not (d / "metrics.json").exists():
        return None
    out = load_json(d / "metrics.json")
    eq_p, tr_p = d / "equity.csv", d / "trades.csv"
    if eq_p.exists():
        eq = pd.read_csv(eq_p)
        out["equity"] = {"dates": eq["date"].astype(str).tolist(), "values": [round(float(x), 2) for x in eq["equity"]]}
    if tr_p.exists() and tr_p.stat().st_size > 5:
        tr = pd.read_csv(tr_p)
        tr = tr.where(pd.notnull(tr), None)
        out["trades"] = [{k: _clean(v) for k, v in row.items()} for row in tr.to_dict(orient="records")]
        score_cols = [c for c in tr.columns if c.startswith("score_") and c != "score_total"]
        if score_cols and len(tr):
            win, loss = tr[tr["pnl"] > 0], tr[tr["pnl"] <= 0]
            out["voter_diag"] = [
                {"voter": c[6:], "winners": _clean(float(win[c].mean())) if len(win) else None,
                 "losers": _clean(float(loss[c].mean())) if len(loss) else None}
                for c in score_cols
            ]
        tr2 = tr.copy()
        tr2["month"] = pd.to_datetime(tr2["exit_date"]).dt.strftime("%Y-%m")
        out["monthly_pnl"] = [{"month": m, "pnl": round(float(v), 2)} for m, v in tr2.groupby("month")["pnl"].sum().items()]
    return out


def _rounds() -> list[dict]:
    d = RESULTS_DIR / "rounds"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("round_*.json"), key=lambda p: int(p.stem.split("_")[1])):
        r = json.loads(p.read_text(encoding="utf-8"))
        r["metrics"] = {k: _clean(v) for k, v in (r.get("metrics") or {}).items()} if r.get("metrics") else None
        out.append(r)
    return out


def _paper() -> dict:
    d = RESULTS_DIR / "paper"
    out = {"orders": [], "positions": {}, "signals": None}
    if not d.exists():
        return out
    if (d / "orders.csv").exists():
        o = pd.read_csv(d / "orders.csv").where(lambda x: pd.notnull(x), None)
        out["orders"] = [{k: _clean(v) for k, v in row.items()} for row in o.tail(50).to_dict(orient="records")]
    try:
        from tradingbot.paperstate import load_all_tracked
        out["positions"] = load_all_tracked()
    except Exception:  # noqa: BLE001
        pass
    sigs = sorted(p for p in d.glob("signals_*.csv") if len(p.stem) == len("signals_2026-01-01"))  # primary book, live runs only
    if sigs:
        s = pd.read_csv(sigs[-1]).where(lambda x: pd.notnull(x), None)
        out["signals"] = {"file": sigs[-1].name, "rows": [{k: _clean(v) for k, v in row.items()} for row in s.head(15).to_dict(orient="records")]}
    return out


def _sweep() -> dict | None:
    d = RESULTS_DIR / "sweep"
    if not (d / "log.csv").exists():
        return None
    try:
        log = pd.read_csv(d / "log.csv")
    except Exception:
        return None
    if len(log) == 0:
        return None
    passed = log["fails"].isna() | (log["fails"].astype(str).str.strip() == "")
    sc = log["score"].where(passed, other=float("nan"))
    running = sc.cummax().ffill().fillna(0.0)
    fails = log["fails"].fillna("PASS").astype(str).str.split(";").explode().value_counts().to_dict()
    best = load_json(d / "best_params.json") if (d / "best_params.json").exists() else None
    top = log[passed].nlargest(5, "score")[["i", "score", "s_pf", "s_ratio", "s_pnl", "s_dd", "s_pos_years", "e_pf", "e_ratio"]]
    top = top.where(pd.notnull(top), None).to_dict(orient="records")
    step = max(1, len(running) // 300)
    return {"evals": int(len(log)), "passed": int(passed.sum()), "fails": fails,
            "running_best": [round(float(x), 3) for x in running.iloc[::step]], "best": best, "top": top,
            "log_mtime": dt.datetime.fromtimestamp((d / "log.csv").stat().st_mtime).isoformat(timespec="seconds")}


def _autopilot() -> dict:
    out = {"state": None, "live": None, "gap": None, "report": None, "paused": (RESULTS_DIR / "paper" / "PAUSED").exists()}
    for key, path in (("state", RESULTS_DIR / "autopilot_state.json"), ("live", RESULTS_DIR / "paper" / "live_stats.json"), ("gap", RESULTS_DIR / "paper" / "reality_gap.json")):
        if path.exists():
            try:
                out[key] = load_json(path)
            except Exception:
                pass
    rep = RESULTS_DIR / "reports" / "latest.md"
    if rep.exists():
        out["report"] = rep.read_text(encoding="utf-8")
    return out


_live_cache = {"t": 0.0, "data": None}


def _alpaca_live() -> dict | None:
    """Current account and positions straight from Alpaca, cached 60 s so the 10 s page poll is cheap."""
    if _live_cache["data"] is not None and time.time() - _live_cache["t"] < 60:
        return _live_cache["data"]
    try:
        from tradingbot.data import alpaca_keys
        keys = alpaca_keys()
        if not keys:
            return None
        from alpaca.trading.client import TradingClient
        tc = TradingClient(*keys, paper=True)
        a = tc.get_account()
        clock = tc.get_clock()
        from tradingbot.paperstate import load_all_tracked
        tracked = load_all_tracked()
        pos = []
        for p in tc.get_all_positions():
            t = tracked.get(p.symbol, {})
            pos.append({"symbol": p.symbol, "qty": float(p.qty), "avg_entry": float(p.avg_entry_price), "price": float(p.current_price),
                        "market_value": float(p.market_value), "unrealized_pl": float(p.unrealized_pl), "unrealized_plpc": float(p.unrealized_plpc) * 100,
                        "stop": t.get("stop"), "target": t.get("target"), "entry_date": t.get("entry_date"), "book": t.get("book", "")})
        data = {"as_of": dt.datetime.now().isoformat(timespec="seconds"), "account": a.account_number, "equity": float(a.equity),
                "cash": float(a.cash), "last_equity": float(a.last_equity or a.equity), "market_open": bool(clock.is_open),
                "next_open": str(clock.next_open)[:16], "next_close": str(clock.next_close)[:16],
                "positions": sorted(pos, key=lambda x: x["symbol"]), "unrealized_total": sum(x["unrealized_pl"] for x in pos)}
    except Exception as e:  # noqa: BLE001
        data = {"error": f"{type(e).__name__}: {str(e)[:200]}", "as_of": dt.datetime.now().isoformat(timespec="seconds")}
    _live_cache.update(t=time.time(), data=data)
    return data


def state() -> dict:
    settings = load_json(CONFIG_DIR / "settings.json")
    lock = RESULTS_DIR / "loop.lock"
    status_p = RESULTS_DIR / "loop_status.json"
    loop_status = load_json(status_p) if status_p.exists() else {}
    rounds = _rounds()
    return {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "settings": {"windows": settings["windows"], "account": settings["account"], "costs": settings["costs"], "loop": settings["loop"], "autopilot": settings.get("autopilot", {})},
        "params": load_json(CONFIG_DIR / "params.json"),
        "loop": {"running": lock.exists(), "pid": lock.read_text(encoding="utf-8").strip() if lock.exists() else None,
                 "next_round": (rounds[-1]["round"] + 1) if rounds else 1, **loop_status},
        "baseline": _run("baseline"),
        "best": _run("best"),
        "current": _run("current"),
        "holdout": _run("holdout"),
        "baseline_holdout": _run("baseline_holdout"),
        "rounds": rounds,
        "paper": _paper(),
        "sweep": _sweep(),
        "autopilot": _autopilot(),
        "alpaca": _alpaca_live(),
        "best_etf": _run("best_etf"),
        "baseline_etf": _run("baseline_etf"),
        "changelog": (ROOT / "CHANGELOG.md").read_text(encoding="utf-8") if (ROOT / "CHANGELOG.md").exists() else "",
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DASH), **kw)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            try:
                body = json.dumps(state(), default=str).encode("utf-8")
            except Exception as e:  # noqa: BLE001  (a CSV mid-write by the runner, a half-saved JSON)
                body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, fmt, *args):
        if "/api/state" not in str(args[0] if args else ""):
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--log", action="store_true", help="append stdout/stderr to results/dashboard.log (for the scheduled task)")
    args = ap.parse_args()
    if args.log:
        import sys
        RESULTS_DIR.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(RESULTS_DIR / "dashboard.log", "a", encoding="utf-8", buffering=1)
        print(f"--- start {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard -> http://localhost:{args.port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
