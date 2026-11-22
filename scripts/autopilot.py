"""Autopilot: the machine grades itself, retrains itself, and pauses itself. No interaction needed.

    python scripts/autopilot.py                 # weekly run: sync, grade, kill-switch, retune if due, report
    python scripts/autopilot.py --force-retune  # retune now regardless of schedule
    python scripts/autopilot.py --dry-run       # do everything except change params / pause / write to Alpaca
    python scripts/autopilot.py --no-apply      # run the full retune but never replace the live params

Steps, each isolated so one failure does not stop the others:
  1. refresh research data (Yahoo) to today
  2. sync fills from Alpaca -> pair into trades -> live stats (results/paper/live_stats.json)
  3. reality gap: backtest the live params over the paper-trading period and compare with live
  4. kill switch: pause new entries if live drawdown / losses breach settings.autopilot.kill_switch
  5. if a retune is due (every retune_every_days, or forced, or paused):
       roll windows (train = 10y ending today - embargo_days, holdout = the embargo year)
       rerun baselines, run the compute sweep -> challenger
       champion vs challenger on the HOLDOUT year, both universes; challenger must win by replace_margin
       if the sweep stalls twice in a row, spend claude_rounds_on_stall Claude rounds (same gate)
  6. write results/reports/autopilot_<date>.md and results/autopilot_state.json

Honesty rules baked in: the challenger is never chosen on the holdout year it is judged on; a champion
is only replaced by a clear margin; live paper results are the final arbiter via the kill switch.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import json
import shutil
import subprocess
import sys
import traceback

import pandas as pd

import _common  # noqa: F401

from tradingbot.backtest import run_backtest
from tradingbot.config import CONFIG_DIR, RESULTS_DIR, ROOT, VOTERS_DIR, book_settings, load_json, load_settings, load_universe, save_json
from tradingbot.data import alpaca_keys, load_bars
from tradingbot.metrics import clean_for_json
from tradingbot.tradelog import load_result
from tradingbot import notify

STATE_PATH = RESULTS_DIR / "autopilot_state.json"
PAUSE_FLAG = RESULTS_DIR / "paper" / "PAUSED"
REPORTS = RESULTS_DIR / "reports"
PARAMS = CONFIG_DIR / "params.json"

DEFAULT_CFG = {
    "embargo_days": 365, "train_years": 10, "retune_every_days": 28, "sweep_samples": 800,
    "replace_margin": 1.15, "claude_rounds_on_stall": 3,
    "kill_switch": {"max_drawdown_pct": 10, "min_trades": 30, "min_profit_factor": 0.8, "resume_after_days": 28},
}


def log(msg: str):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(args, timeout=7200):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def load_state() -> dict:
    if STATE_PATH.exists():
        return load_json(STATE_PATH)
    return {"paper_start": dt.date.today().isoformat(), "last_retune": None, "stall_count": 0, "paused": False, "history": []}


# ---------------------------------------------------------------- 1. data
def refresh_data(settings: dict, today: dt.date):
    from tradingbot.config import resolve_universe
    extra = [sym for b in enabled_books(settings) for sym in resolve_universe(b.get("universe", "stocks"), settings)]
    syms = list(dict.fromkeys(load_universe() + settings["validation_universe"] + extra + [settings["benchmark"]]))
    bars = load_bars(syms, settings["data_start"], today.isoformat(), dict(settings, data_provider="yfinance"), refresh=True)
    last = max(df.index.max() for df in bars.values())
    log(f"data refreshed: {len(bars)} symbols, last bar {last.date()}")
    return last.date()


# ---------------------------------------------------------------- 2. live stats
def pair_trades(fills: pd.DataFrame) -> pd.DataFrame:
    """FIFO-match buys to sells per symbol -> one row per round trip."""
    rows = []
    if fills is None or len(fills) == 0:
        return pd.DataFrame(rows)
    fills = fills.sort_values("filled_at")
    for sym, g in fills.groupby("symbol"):
        open_lots = []
        for _, f in g.iterrows():
            qty, px = float(f["qty"]), float(f["avg_price"])
            if f["side"] == "buy":
                open_lots.append([qty, px, f["filled_at"]])
            else:
                while qty > 1e-9 and open_lots:
                    lot = open_lots[0]
                    take = min(qty, lot[0])
                    rows.append({"symbol": sym, "entry_time": lot[2], "exit_time": f["filled_at"], "qty": take,
                                 "entry_price": lot[1], "exit_price": px, "pnl": (px - lot[1]) * take})
                    lot[0] -= take
                    qty -= take
                    if lot[0] <= 1e-9:
                        open_lots.pop(0)
    return pd.DataFrame(rows)


def _pf_stats(pnls: list) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]
    pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else (999.0 if wins else 0.0)
    return {"trades": len(pnls), "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0, "total_pnl": sum(pnls), "profit_factor": pf}


def live_stats(tc, cap: float, since: str | None = None) -> dict:
    """Whole-history statistics for the report plus a 'recent' block (trades closed since `since`, the last resume
    date) that the kill switch judges, so a pause does not carry one bad month forever."""
    from run_paper import sync_fills  # reuse the runner's downloader

    sync_fills(tc)
    fills_p = RESULTS_DIR / "paper" / "fills.csv"
    fills = pd.read_csv(fills_p) if fills_p.exists() and fills_p.stat().st_size > 5 else pd.DataFrame()
    trades = pair_trades(fills)
    trades.to_csv(RESULTS_DIR / "paper" / "live_trades.csv", index=False)
    pnls = trades["pnl"].tolist() if len(trades) else []
    recent = trades[trades["exit_time"].astype(str) >= since] if (since and len(trades)) else trades
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]
    pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else (999.0 if wins else 0.0)
    # drawdown from the account's own equity history, relative to the sizing cap
    max_dd = 0.0
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        hist = tc.get_portfolio_history(GetPortfolioHistoryRequest(period="3M", timeframe="1D"))
        eq = pd.Series([float(x) for x in hist.equity if x is not None])
        if len(eq) > 1:
            max_dd = float((eq.cummax() - eq).max())
    except Exception as e:  # noqa: BLE001
        log(f"portfolio history unavailable: {e}")
    acct = tc.get_account()
    stats = {"as_of": dt.datetime.now().isoformat(timespec="seconds"), "trades": len(pnls), "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
             "total_pnl": sum(pnls), "profit_factor": pf, "max_drawdown": max_dd, "max_drawdown_pct_of_cap": max_dd / cap * 100,
             "account_equity": float(acct.equity), "open_positions": len(tc.get_all_positions()),
             "recent": {**_pf_stats(recent["pnl"].tolist() if len(recent) else []), "since": since}}
    save_json(stats, RESULTS_DIR / "paper" / "live_stats.json")
    log(f"live: {stats['trades']} trades, PF {pf:.2f}, P&L ${stats['total_pnl']:,.0f}, max DD ${max_dd:,.0f}, {stats['open_positions']} open")
    return stats


# ---------------------------------------------------------------- 3. reality gap
def eval_params(params: dict, settings: dict, start: str, end: str, universe: str) -> dict:
    from tradingbot.config import resolve_universe
    syms = resolve_universe(universe, settings)
    bars = load_bars(list(dict.fromkeys(syms + [settings["benchmark"]])), settings["data_start"], end, settings)
    bench = bars.get(settings["benchmark"])
    bars = {k: v for k, v in bars.items() if k in syms}   # same universe as the sweep: the benchmark is tradable if listed
    res = run_backtest(bars, bench, params, settings, start, end)
    m = clean_for_json(res.metrics)
    m["_equity"] = res.equity
    return m


def reality_gap(settings: dict, params: dict, paper_start: str, today: dt.date, live: dict | None) -> dict:
    sim = eval_params(params, settings, paper_start, today.isoformat(), "stocks")
    gap = {"window": [paper_start, today.isoformat()], "sim_trades": sim["trades"], "sim_pnl": sim["total_pnl"], "sim_pf": sim["profit_factor"]}
    if live:
        gap.update(live_trades=live["trades"], live_pnl=live["total_pnl"], live_pf=live["profit_factor"],
                   pnl_gap=live["total_pnl"] - sim["total_pnl"])
    save_json(gap, RESULTS_DIR / "paper" / "reality_gap.json")
    log(f"reality gap: sim {sim['trades']} trades ${sim['total_pnl']:,.0f} vs live {gap.get('live_trades')} trades ${gap.get('live_pnl', 0):,.0f}")
    return gap


# ---------------------------------------------------------------- 3b. health: missed runs
def missed_runs(tc, days: int = 7) -> list[str]:
    """Trading days in the last `days` with no signals_<date>.csv, i.e. the runner never ran."""
    from alpaca.trading.requests import GetCalendarRequest

    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days)
    cal = tc.get_calendar(GetCalendarRequest(start=start, end=end))
    missed = []
    for day in cal:
        d = day.date if isinstance(day.date, dt.date) else dt.date.fromisoformat(str(day.date)[:10])
        if d < dt.date.fromisoformat(load_state()["paper_start"]):
            continue
        if not (RESULTS_DIR / "paper" / f"signals_{d.isoformat()}.csv").exists():
            missed.append(d.isoformat())
    return missed



# ---------------------------------------------------------------- 3c. execution quality -> cost model
def execution_quality(settings: dict) -> dict | None:
    """Measure real fills against what the backtester assumes (fill at the day's open + costs).

    buy_slip_vs_open_bps : (fill - that day's open) / open, for filled entries. This is exactly the
                           quantity the backtest models as half-spread + slippage on the buy side.
    stop_slip_bps        : (stop price - fill) / stop price for stop exits (how far below the stop the sell filled).
    Returns None until there are fills to measure."""
    from tradingbot.config import resolve_universe
    orders_p = RESULTS_DIR / "paper" / "orders.csv"
    fills_p = RESULTS_DIR / "paper" / "fills.csv"
    if not orders_p.exists():
        return None
    o = pd.read_csv(orders_p)
    o = o[(o["action"] == "buy_bracket") & (o["dry_run"].astype(str).str.lower() != "true")]
    if "fill_price" not in o.columns:
        return None
    buys = o[o["fill_price"].notna()].copy()
    out = {"n_buys": int(len(buys)), "n_stops": 0}
    if len(buys):
        syms = sorted(set(buys["symbol"]))
        live_dir = RESULTS_DIR.parent / "data" / "live"
        slips, slips_base = [], []
        for _, r in buys.iterrows():
            day = pd.Timestamp(str(r["time"])[:10])
            f = live_dir / f"{r['symbol']}_1Day.csv"
            if str(r.get("order_type")) != "market_opg":
                if pd.notna(r.get("slippage_bps")):
                    slips_base.append(float(r["slippage_bps"]))
                continue   # an intraday fill is not comparable with the open; it is measured against its quote instead
            if f.exists():
                df = pd.read_csv(f, index_col="date", parse_dates=True)
                if day in df.index:
                    slips.append((float(r["fill_price"]) - float(df.at[day, "open"])) / float(df.at[day, "open"]) * 10_000)
            if pd.notna(r.get("slippage_bps")):
                slips_base.append(float(r["slippage_bps"]))
        if slips:
            out.update(median_buy_slip_vs_open_bps=float(pd.Series(slips).median()), mean_buy_slip_vs_open_bps=float(pd.Series(slips).mean()), n_buys_with_open=len(slips))
        if slips_base:
            out["median_slip_vs_quote_bps"] = float(pd.Series(slips_base).median())
    if fills_p.exists() and fills_p.stat().st_size > 5:
        fills = pd.read_csv(fills_p)
        stops = fills[(fills["side"] == "sell") & (fills["type"].astype(str).str.contains("stop"))]
        ss = []
        for _, f in stops.iterrows():
            ref = o[o["symbol"] == f["symbol"]].sort_values("time")
            ref = ref[ref["time"] <= str(f["filled_at"])]
            if len(ref) and pd.notna(ref.iloc[-1]["stop"]):
                stop_px = float(ref.iloc[-1]["stop"])
                ss.append((stop_px - float(f["avg_price"])) / stop_px * 10_000)
        out["n_stops"] = len(ss)
        if ss:
            out["median_stop_slip_bps"] = float(pd.Series(ss).median())
    save_json(out, RESULTS_DIR / "paper" / "execution_quality.json")
    return out


def calibrate_costs(settings: dict, eq: dict | None, cfg: dict, dry_run: bool) -> str | None:
    """Once enough real fills exist, set the backtester's slippage to what we actually pay (bounded)."""
    cc = cfg.get("calibrate_costs", {"enabled": True, "min_fills": 30, "min_bps": 2, "max_bps": 30})
    if not eq or not cc.get("enabled", True) or eq.get("n_buys_with_open", 0) < int(cc.get("min_fills", 30)):
        return None
    spread = float(settings["costs"].get("spread_bps", 3))
    measured = float(eq["median_buy_slip_vs_open_bps"])
    new_slip = round(min(max(measured - spread / 2, float(cc.get("min_bps", 2))), float(cc.get("max_bps", 30))), 1)
    old_slip = float(settings["costs"].get("slippage_bps", 5))
    if abs(new_slip - old_slip) < 0.5:
        return f"measured buy slippage {measured:.1f} bps vs open over {eq['n_buys_with_open']} fills; cost model already at {old_slip} bps, unchanged"
    if not dry_run:
        settings["costs"]["slippage_bps"] = new_slip
        save_json(settings, CONFIG_DIR / "settings.json")
        with open(ROOT / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write(f"\n### Cost model calibrated from live fills ({dt.date.today()})\nslippage_bps {old_slip} -> {new_slip} "
                    f"(median buy fill vs open {measured:.1f} bps over {eq['n_buys_with_open']} fills, minus half spread).\n")
    return f"cost model slippage {old_slip} -> {new_slip} bps from {eq['n_buys_with_open']} real fills (median fill vs open {measured:.1f} bps)"


# ---------------------------------------------------------------- 4. kill switch
def kill_switch(live: dict | None, cfg: dict) -> tuple[bool, str]:
    if not live:
        return False, "no live data"
    ks = cfg["kill_switch"]
    if live["max_drawdown_pct_of_cap"] >= ks["max_drawdown_pct"]:
        return True, f"live drawdown {live['max_drawdown_pct_of_cap']:.1f}% >= {ks['max_drawdown_pct']}%"
    r = live.get("recent") or live
    if r["trades"] >= ks["min_trades"] and r["profit_factor"] < ks["min_profit_factor"]:
        return True, f"live profit factor {r['profit_factor']:.2f} < {ks['min_profit_factor']} over the last {r['trades']} trades"
    return False, "ok"


# ---------------------------------------------------------------- 5. retune
def roll_windows(settings: dict, today: dt.date, cfg: dict, last_bar: dt.date) -> dict:
    hold_end = min(today - dt.timedelta(days=1), last_bar)
    train_end = today - dt.timedelta(days=cfg["embargo_days"])
    try:
        train_start = train_end.replace(year=train_end.year - cfg["train_years"])
    except ValueError:  # Feb 29
        train_start = train_end.replace(year=train_end.year - cfg["train_years"], day=28)
    settings["windows"] = {"train": {"start": train_start.isoformat(), "end": train_end.isoformat()},
                           "holdout": {"start": (train_end + dt.timedelta(days=1)).isoformat(), "end": hold_end.isoformat()}}
    # the slowest voters need ~250 bars (xa: SMA 250, momentum 180, vol median window 252) BEFORE the train start
    settings["data_start"] = (train_start - dt.timedelta(days=cfg.get("history_days_before_train", 450))).isoformat()
    save_json(settings, CONFIG_DIR / "settings.json")
    log(f"windows rolled: train {settings['windows']['train']}  holdout {settings['windows']['holdout']}")
    return settings


def holdout_scores(params: dict, settings: dict, primary: str = "stocks", secondary: str | None = None) -> dict:
    """Score a params set on the holdout year at several start dates (0, +14, +42 days).
    Keys 'stocks' (= primary) and 'etf' (= secondary) hold the FIRST start's metrics for the report/dashboard;
    'starts' holds every start; 'score' is the median across starts."""
    import statistics
    h = settings["windows"]["holdout"]
    secondary = secondary or ("stocks" if primary == "etf" else "etf")
    offsets = settings.get("autopilot", {}).get("holdout_start_offsets_days", [0, 14, 42])
    starts = []
    for off in offsets:
        st = (dt.date.fromisoformat(h["start"]) + dt.timedelta(days=int(off))).isoformat()
        if st >= h["end"] and off != 0:
            continue
        a = eval_params(params, settings, st, h["end"], primary)
        b = eval_params(params, settings, st, h["end"], secondary)
        for m in (a, b):
            m.pop("_equity", None)
        starts.append({"start": st, "stocks": a, "etf": b, "score": (max(a["recovery_ratio"], 0) * max(b["recovery_ratio"], 0)) ** 0.5})
    return {"stocks": starts[0]["stocks"], "etf": starts[0]["etf"], "primary": "stocks", "universes": [primary, secondary],
            "starts": starts, "score": statistics.median(x["score"] for x in starts)}


def challenger_wins(champ: dict, chal: dict, margin: float, min_abs_score: float = 0.5) -> tuple[bool, str]:
    """The challenger must beat the champion by `margin` on a MAJORITY of holdout start dates, be profitable
    (PF > 1 on both universes) on a majority of them, and never be below PF 0.9 on the primary at any start.
    Where the champion lost money on a start (score 0) the challenger must instead clear `min_abs_score`."""
    cs, hs = champ.get("starts") or [champ], chal.get("starts") or [chal]
    n = min(len(cs), len(hs))
    wins, profitable, pf_ok = 0, 0, True
    for c, h in zip(cs[:n], hs[:n]):
        prof = h["stocks"]["profit_factor"] > 1.0 and h["etf"]["profit_factor"] > 1.0
        profitable += prof
        if h["stocks"]["profit_factor"] < 0.9:
            pf_ok = False
        bar = c["score"] * margin if c["score"] > 0 else min_abs_score
        if prof and h["score"] >= bar and h["stocks"]["profit_factor"] >= c["stocks"]["profit_factor"]:
            wins += 1
    need = n // 2 + 1
    detail = f"{wins}/{n} starts won by >= {margin}x, profitable on {profitable}/{n}"
    if not pf_ok:
        return False, f"challenger below PF 0.9 at some start ({detail})"
    if profitable < need:
        return False, f"challenger not profitable on enough starts ({detail})"
    if wins < need:
        return False, f"challenger did not beat champion on enough starts ({detail}); median score {chal['score']:.2f} vs {champ['score']:.2f}"
    return True, f"challenger wins {detail}; median score {chal['score']:.2f} vs champion {champ['score']:.2f}"


def watch_books(settings: dict) -> list[dict]:
    """Research candidates: disabled, zero capital, but retuned and graded monthly so the report says when one is ready."""
    return [b for b in settings.get("books", []) if not b.get("enabled", True) and b.get("watch")]


def enabled_books(settings: dict) -> list[dict]:
    books = settings.get("books") or [{"name": "trend", "params": "config/params.json", "capital_pct": 100, "space": "trend"}]
    return [b for b in books if b.get("enabled", True)]


def snapshot_params(book: dict) -> dict:
    snap = RESULTS_DIR / "_champion" / book["name"]
    if snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True)
    src = ROOT / book["params"]
    shutil.copy2(src, snap / "params.json")
    shutil.copytree(VOTERS_DIR, snap / "voters", ignore=shutil.ignore_patterns("__pycache__"))
    return load_json(src)


def restore_params(book: dict | None = None):
    books = [book] if book else enabled_books(load_settings())
    for b in books:
        snap = RESULTS_DIR / "_champion" / b["name"]
        if not snap.exists():
            continue
        shutil.copy2(snap / "params.json", ROOT / b["params"])
        keep = {p.name for p in (snap / "voters").glob("*.py")}
        for p in VOTERS_DIR.glob("*.py"):
            if p.name not in keep:
                p.unlink()
        for p in (snap / "voters").glob("*.py"):
            shutil.copy2(p, VOTERS_DIR / p.name)


def run_baselines():
    for args in (["--params", "config/baseline_params.json", "--out", "baseline", "--label", "baseline stocks"],
                 ["--params", "config/baseline_params.json", "--out", "baseline_etf", "--universe", "etf", "--label", "baseline etf"]):
        r = sh([sys.executable, "scripts/run_backtest.py", *args])
        if r.returncode != 0:
            raise RuntimeError("baseline backtest failed: " + (r.stderr or r.stdout)[-800:])


def _rotation_eval(params: dict, uni: dict, bench, settings: dict, start: str, end: str) -> dict:
    from tradingbot.rotation import rotation_backtest
    r = rotation_backtest(uni, params, settings, start, end, benchmark=bench)
    m = clean_for_json(r.metrics)
    eq = r.equity
    yp = []
    if len(eq):
        prev = float(settings["account"]["starting_cash"])
        for _, s_ in eq.groupby(eq.index.year):
            if len(s_) >= 200:
                yp.append(float(s_.iloc[-1]) - prev)
            prev = float(s_.iloc[-1])
    m["positive_years_pct"] = (sum(1 for v in yp if v > 0) / len(yp) * 100) if yp else 0.0
    m["_result"] = r
    return m


def _save_rotation_result(res, out_name: str, label: str, version) -> None:
    out = RESULTS_DIR / out_name
    out.mkdir(parents=True, exist_ok=True)
    save_json({"label": label, "window": res.window, "metrics": clean_for_json(res.metrics), "skipped": {}, "params_version": version}, out / "metrics.json")
    pd.DataFrame(res.trades).to_csv(out / "trades.csv", index=False)
    res.equity.rename("equity").to_frame().rename_axis("date").to_csv(out / "equity.csv")
    save_json(res.params, out / "params.json")


def retune_rotation_book(book: dict, settings: dict, state: dict, cfg: dict, today: dt.date, apply: bool) -> dict:
    """Champion vs challenger for a momentum-rotation book: a small grid on the train window, judged on the holdout
    year at several start dates with the same majority rule as the committee books."""
    import itertools
    from tradingbot.config import resolve_universe
    from rotation_research import GRID
    name, params_path = book["name"], ROOT / book["params"]
    settings = book_settings(settings, book)
    champion = snapshot_params(book)
    primary, secondary = book.get("universe", "stocks"), book.get("secondary", "stocks2016")
    syms = list(dict.fromkeys(resolve_universe(primary, settings) + resolve_universe(secondary, settings) + [settings["benchmark"]]))
    bars = load_bars(syms, settings["data_start"], settings["windows"]["holdout"]["end"], settings)
    bench = bars[settings["benchmark"]]
    uni_p = {k: bars[k] for k in resolve_universe(primary, settings) if k in bars}
    uni_s = {k: bars[k] for k in resolve_universe(secondary, settings) if k in bars}
    tr, h = settings["windows"]["train"], settings["windows"]["holdout"]

    def hold(params):
        starts = []
        for off in settings.get("autopilot", {}).get("holdout_start_offsets_days", [0, 14, 42]):
            st = (dt.date.fromisoformat(h["start"]) + dt.timedelta(days=int(off))).isoformat()
            if st >= h["end"] and off != 0:
                continue
            a = _rotation_eval(params, uni_p, bench, settings, st, h["end"]); b = _rotation_eval(params, uni_s, bench, settings, st, h["end"])
            a.pop("_result"); b.pop("_result")
            starts.append({"start": st, "stocks": a, "etf": b, "score": (max(a["recovery_ratio"], 0) * max(b["recovery_ratio"], 0)) ** 0.5})
        import statistics
        return {"stocks": starts[0]["stocks"], "etf": starts[0]["etf"], "primary": "stocks", "universes": [primary, secondary], "starts": starts,
                "score": statistics.median(x["score"] for x in starts)}

    champ_hold = hold(champion)
    log(f"[{name}] champion v{champion.get('version')} holdout: PF {champ_hold['stocks']['profit_factor']:.2f} ratio {champ_hold['stocks']['recovery_ratio']:.2f} | bias set PF {champ_hold['etf']['profit_factor']:.2f} | score {champ_hold['score']:.2f}")
    outcome = {"book": name, "date": today.isoformat(), "champion_version": champion.get("version"), "champion_holdout": champ_hold, "replaced": False}
    keys = list(GRID)
    best = None
    for vals in itertools.product(*GRID.values()):
        g = {**{k: v for k, v in champion.items() if k not in ("version", "_comment")}, **dict(zip(keys, vals))}
        m = _rotation_eval(g, uni_p, bench, settings, tr["start"], tr["end"])
        if m["trades"] < 10 or m["profit_factor"] <= 1.0 or m["positive_years_pct"] < 60:
            continue
        if best is None or m["recovery_ratio"] > best[0]:
            best = (m["recovery_ratio"], g)
    challenger = best[1] if best else None
    strip = lambda d: {k: v for k, v in d.items() if k not in ("version", "_comment")}
    if challenger is None or strip(challenger) == strip(champion):
        outcome["reason"] = "grid found nothing better than the champion on the train window"
        log(f"[{name}] {outcome['reason']}")
    else:
        chal_hold = hold(challenger)
        win, reason = challenger_wins(champ_hold, chal_hold, cfg["replace_margin"])
        outcome.update(challenger_source="grid", challenger_holdout=chal_hold, reason=reason)
        log(f"[{name}] challenger holdout: PF {chal_hold['stocks']['profit_factor']:.2f} ratio {chal_hold['stocks']['recovery_ratio']:.2f} | score {chal_hold['score']:.2f} -> {reason}")
        if win and apply:
            new_version = int(champion.get("version", 1)) + 1
            p = dict(challenger); p["version"] = new_version
            save_json(p, params_path)
            snapshot_params(book)
            outcome.update(replaced=True, new_version=new_version)
            with open(ROOT / "CHANGELOG.md", "a", encoding="utf-8") as f:
                f.write(f"\n## [{name}] v{new_version} — autopilot retune ({today}) via grid\n{reason}\n")
            log(f"[{name}] REPLACED champion -> v{new_version}")
    live = load_json(params_path)
    for w, out_name, label in ((tr, f"best_{name}", f"{name} live params, train"), (h, f"holdout_{name}", f"{name} HOLDOUT")):
        m = _rotation_eval(live, uni_p, bench, settings, w["start"], w["end"])
        _save_rotation_result(m["_result"], out_name, label, live.get("version"))
    return outcome


def retune_book(book: dict, idx: int, settings: dict, state: dict, cfg: dict, today: dt.date, apply: bool, sweep_samples: int) -> dict:
    """Champion vs challenger for one book. idx 0 is the primary book (gets the baseline check and Claude escalation)."""
    name, params_path = book["name"], ROOT / book["params"]
    stalls = state.setdefault("stall", {})
    stall = int(stalls.get(name, 0))
    champion = snapshot_params(book)
    primary = book.get("universe", "stocks")
    secondary = book.get("secondary", "stocks" if primary == "etf" else "etf")
    settings = book_settings(settings, book)   # the book's own benchmark / costs / sizing for every evaluation below
    champ_hold = holdout_scores(champion, settings, primary, secondary)
    log(f"[{name}] champion v{champion.get('version')} holdout: stocks PF {champ_hold['stocks']['profit_factor']:.2f} ratio {champ_hold['stocks']['recovery_ratio']:.2f} | etf PF {champ_hold['etf']['profit_factor']:.2f} | score {champ_hold['score']:.2f}")
    outcome = {"book": name, "date": today.isoformat(), "champion_version": champion.get("version"), "champion_holdout": champ_hold, "replaced": False}

    sweep_dir = RESULTS_DIR / ("sweep" if idx == 0 else f"sweep_{name}")
    if sweep_dir.exists():
        shutil.rmtree(sweep_dir)
    sw_args = [sys.executable, "-u", "scripts/sweep.py", "--book", name, "--samples", str(sweep_samples), "--params", book.get("seed", book["params"]),
               *(["--workers", str(cfg["sweep_workers"])] if cfg.get("sweep_workers") else []),
               "--out", sweep_dir.name, "--space", book.get("space", "trend"), "--primary", primary, "--secondary", secondary]
    if idx > 0:
        sw_args.append("--no-baseline-check")
    r = sh(sw_args, timeout=4 * 3600)
    (REPORTS / f"sweep_{name}_{today.isoformat()}.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        outcome.update(error=True, reason=f"sweep crashed (exit {r.returncode}): {(r.stderr or r.stdout)[-300:].strip()}")
        log(f"[{name}] {outcome['reason']}")
        restore_params(book)
        return outcome  # a crash is not a stall: no stall count, no Claude escalation
    best_p = sweep_dir / "best_params.json"
    challenger = load_json(best_p)["params"] if best_p.exists() else None
    source = "sweep"

    if idx == 0 and (challenger is None or stall >= 2) and cfg.get("claude_rounds_on_stall", 0) > 0:
        from loop import claude_ready
        ok, why = claude_ready()
        if not ok:
            log(f"Claude preflight failed, skipping escalation: {why}")
            notify.send("🔑 Autopilot wanted to run Claude research rounds but the Claude CLI is not usable "
                        f"({why[:160]}). If it mentions login, run in PowerShell:\n& \"$env:USERPROFILE\\.local\\bin\\claude.exe\" login")
        else:
            log(f"[{name}] escalating: {cfg['claude_rounds_on_stall']} Claude rounds on the train window")
            r2 = sh([sys.executable, "scripts/loop.py", "--rounds", str(cfg["claude_rounds_on_stall"])], timeout=3 * 3600)
            (REPORTS / f"claude_{today.isoformat()}.log").write_text(r2.stdout + r2.stderr, encoding="utf-8")
            loop_result = load_json(params_path)
            if loop_result != champion:
                challenger, source = loop_result, "claude"
            # Not restored here: a Claude round may have added a voter module the challenger needs. The holdout
            # gate below scores it first; the win branch re-snapshots, the lose branch restores.

    def strip(d):
        return {k: v for k, v in d.items() if k != "version"}

    if challenger is not None and strip(challenger) == strip(champion):
        challenger = None
    if challenger is None:
        outcome["reason"] = "search found nothing better than the champion on the train window"
        stalls[name] = stall + 1
        restore_params(book)
        log(f"[{name}] {outcome['reason']}")
    else:
        chal_hold = holdout_scores(challenger, settings, primary, secondary)
        win, reason = challenger_wins(champ_hold, chal_hold, cfg["replace_margin"])
        outcome.update(challenger_source=source, challenger_holdout=chal_hold, reason=reason)
        log(f"[{name}] challenger ({source}) holdout: stocks PF {chal_hold['stocks']['profit_factor']:.2f} ratio {chal_hold['stocks']['recovery_ratio']:.2f} | etf PF {chal_hold['etf']['profit_factor']:.2f} | score {chal_hold['score']:.2f} -> {reason}")
        if win and apply:
            new_version = int(champion.get("version", 1)) + 1
            p = copy.deepcopy(challenger)
            p["version"] = new_version
            save_json(p, params_path)
            snapshot_params(book)  # the new champion (params + any new voter file) is now the restore point
            outcome.update(replaced=True, new_version=new_version)
            stalls[name] = 0
            with open(ROOT / "CHANGELOG.md", "a", encoding="utf-8") as f:
                f.write(f"\n## [{name}] v{new_version} — autopilot retune ({today}) via {source}\n{reason}\n"
                        f"Holdout {settings['windows']['holdout']['start']} -> {settings['windows']['holdout']['end']}: "
                        f"stocks PF {chal_hold['stocks']['profit_factor']:.2f} (champion {champ_hold['stocks']['profit_factor']:.2f}), "
                        f"ETF PF {chal_hold['etf']['profit_factor']:.2f} (champion {champ_hold['etf']['profit_factor']:.2f}).\n")
            log(f"[{name}] REPLACED champion -> v{new_version}")
        else:
            if not win:
                stalls[name] = stall + 1
            restore_params(book)
            if source == "claude":
                with open(ROOT / "CHANGELOG.md", "a", encoding="utf-8") as f:
                    f.write(f"\n_({today}) The Claude-loop candidate logged as KEPT above was rejected by the autopilot holdout gate "
                            f"({reason}); live [{name}] stays v{champion.get('version')}._\n")
            log(f"[{name}] champion kept" + ("" if apply else " (no-apply mode)"))

    # refresh dashboard runs for whichever params are now live in this book
    if idx == 0:
        runs = (["--out", "best", "--label", "live params, train"], ["--out", "best_etf", "--universe", "etf", "--label", "live params, ETFs train"],
                ["--window", "holdout", "--out", "holdout", "--label", "live params HOLDOUT"],
                ["--window", "holdout", "--params", "config/baseline_params.json", "--out", "baseline_holdout", "--label", "baseline HOLDOUT"])
    else:
        runs = (["--book", name, "--out", f"best_{name}", "--label", f"{name} live params, train"],
                ["--book", name, "--window", "holdout", "--out", f"holdout_{name}", "--label", f"{name} HOLDOUT"])
    for a in runs:
        sh([sys.executable, "scripts/run_backtest.py", *a])
    return outcome


def retune(settings: dict, state: dict, cfg: dict, today: dt.date, last_bar: dt.date, apply: bool, sweep_samples: int, only: set | None = None) -> dict:
    settings = roll_windows(settings, today, cfg, last_bar)
    run_baselines()
    outcomes = [(retune_rotation_book(b, settings, state, cfg, today, apply) if b.get("engine") == "rotation"
                 else retune_book(b, i, settings, state, cfg, today, apply, sweep_samples)) for i, b in enumerate(enabled_books(settings))
                if only is None or b["name"] in only]
    watched = []
    for b in watch_books(settings):
        if only is not None and b["name"] not in only:
            continue
        try:
            o = retune_book(b, 99, settings, state, cfg, today, apply, sweep_samples)   # idx 99: never the primary, no baseline check
            best = o.get("challenger_holdout") if o.get("replaced") else o.get("champion_holdout")
            starts = (best or {}).get("starts") or []
            o["ready"] = bool(starts) and all(x["stocks"]["profit_factor"] >= 1.1 for x in starts) and all(x["etf"]["profit_factor"] >= 1.0 for x in starts)
            watched.append(o)
            log(f"[{b['name']}] (watch) {'READY' if o['ready'] else 'not ready'}: " + ", ".join(f"PF {x['stocks']['profit_factor']:.2f}" for x in starts))
        except Exception as e:  # noqa: BLE001
            log(f"[{b['name']}] (watch) failed: {e}")
            watched.append({"book": b["name"], "error": str(e)})
    state["last_retune"] = today.isoformat()
    state["stall_count"] = int(state.get("stall", {}).get(outcomes[0]["book"], 0)) if outcomes else 0
    primary = dict(outcomes[0]) if outcomes else {}
    primary["books"] = outcomes
    primary["replaced"] = any(o.get("replaced") for o in outcomes)
    primary["replaced_books"] = [f"{o['book']} v{o['new_version']}" for o in outcomes if o.get("replaced")]
    primary["watched"] = watched
    return primary


# ---------------------------------------------------------------- 6. report
def write_report(today, live, gap, paused, pause_reason, retune_out, errors, eq_stats=None, cost_note=None, settings=None) -> str:
    settings = settings or load_settings()
    REPORTS.mkdir(parents=True, exist_ok=True)
    params = load_json(PARAMS)
    lines = [f"# Autopilot report — {today}", ""]
    lines.append(f"Live strategy: params v{params.get('version')}. New entries are {'PAUSED — ' + pause_reason if paused else 'active'}.")
    lines.append("")
    if live:
        lines += ["## Paper account so far",
                  f"- {live['trades']} completed trades, win rate {live['win_rate']:.0f}%, profit factor {live['profit_factor']:.2f}",
                  f"- realised P&L ${live['total_pnl']:,.0f}, worst drawdown ${live['max_drawdown']:,.0f} ({live['max_drawdown_pct_of_cap']:.1f}% of the $25k sizing cap)",
                  f"- {live['open_positions']} positions open, account equity ${live['account_equity']:,.0f}", ""]
    if gap:
        lines += ["## Reality check (simulation vs. what actually happened over the same days)",
                  f"- simulation: {gap['sim_trades']} trades, ${gap['sim_pnl']:,.0f}",
                  f"- live: {gap.get('live_trades', 0)} trades, ${gap.get('live_pnl', 0):,.0f}",
                  "- a gap that keeps widening means the simulation is too optimistic about fills or costs", ""]
    if eq_stats and eq_stats.get("n_buys"):
        lines += ["## Execution quality (real fills vs. what the backtest assumes)",
                  f"- {eq_stats['n_buys']} entry fills; median fill vs that day's open: {eq_stats.get('median_buy_slip_vs_open_bps', float('nan')):.1f} bps "
                  f"(backtest assumes {settings['costs'].get('spread_bps', 3) / 2 + settings['costs'].get('slippage_bps', 5):.1f} bps)",
                  f"- median fill vs the quote the limit was built on: {eq_stats.get('median_slip_vs_quote_bps', float('nan')):.1f} bps",
                  f"- {eq_stats.get('n_stops', 0)} stop exits; median fill below the stop: {eq_stats.get('median_stop_slip_bps', float('nan')):.1f} bps"]
        if cost_note:
            lines.append(f"- {cost_note}")
        lines.append("")
    if retune_out:
        lines.append("## Monthly retune")
        for o in retune_out.get("books", [retune_out]):
            ch = o.get("champion_holdout", {}).get("stocks", {})
            lines.append(f"- [{o.get('book', 'trend')}] champion v{o.get('champion_version')} on the held-out year: PF {ch.get('profit_factor', 0):.2f}, P&L/DD {ch.get('recovery_ratio', 0):.2f}")
            if o.get("challenger_holdout"):
                cs = o["challenger_holdout"]["stocks"]
                lines.append(f"  challenger ({o.get('challenger_source')}): PF {cs['profit_factor']:.2f}, P&L/DD {cs['recovery_ratio']:.2f}")
            lines.append(f"  decision: {'REPLACED, now v' + str(o.get('new_version')) if o.get('replaced') else 'champion kept'} — {o.get('reason', '')}")
        lines.append("")
    if retune_out and retune_out.get("watched"):
        lines += ["## Candidate books on watch (not trading)"]
        for o in retune_out["watched"]:
            if o.get("error"):
                lines.append(f"- [{o['book']}] failed: {o['error']}")
                continue
            best = o.get("challenger_holdout") if o.get("replaced") else o.get("champion_holdout")
            pfs = ", ".join(f"{x['stocks']['profit_factor']:.2f}" for x in (best or {}).get("starts", []))
            lines.append(f"- [{o['book']}] {'READY to enable' if o.get('ready') else 'not ready'}: holdout PF by start date {pfs}"
                         + (f"; params replaced -> v{o['new_version']}" if o.get("replaced") else ""))
        lines.append("")
    if errors:
        lines += ["## Errors this run"] + [f"- {e}" for e in errors] + [""]
    text = "\n".join(lines)
    (REPORTS / f"autopilot_{today}.md").write_text(text, encoding="utf-8")
    (REPORTS / "latest.md").write_text(text, encoding="utf-8")
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-retune", action="store_true")
    ap.add_argument("--books", default=None, help="comma-separated book names to retune (default all), e.g. --books trend")
    ap.add_argument("--dry-run", action="store_true", help="no pause flag, no param changes, no Alpaca writes")
    ap.add_argument("--no-apply", action="store_true", help="run the retune but never replace live params")
    ap.add_argument("--sweep-samples", type=int, default=0)
    ap.add_argument("--no-claude", action="store_true", help="never escalate to Claude rounds (testing)")
    ap.add_argument("--skip-if-recent", type=int, default=0, metavar="DAYS", help="exit quietly if the last run was within DAYS days (for the logon trigger)")
    args = ap.parse_args()
    if args.skip_if_recent:
        st = load_state()
        if st.get("last_run") and (dt.datetime.now() - dt.datetime.fromisoformat(st["last_run"])).days < args.skip_if_recent:
            print(f"last autopilot run {st['last_run']} is recent, nothing to do")
            return

    settings = load_settings()
    cfg = {**DEFAULT_CFG, **settings.get("autopilot", {})}
    if args.no_claude:
        cfg["claude_rounds_on_stall"] = 0
    cfg["kill_switch"] = {**DEFAULT_CFG["kill_switch"], **settings.get("autopilot", {}).get("kill_switch", {})}
    state = load_state()
    today = dt.date.today()
    errors, live, gap, retune_out = [], None, None, None
    paused, pause_reason = state.get("paused", False), state.get("pause_reason", "")
    REPORTS.mkdir(parents=True, exist_ok=True)
    log(f"autopilot start (dry_run={args.dry_run})")
    try:
        from sweep import lower_priority
        lower_priority()
    except Exception:  # noqa: BLE001
        pass

    try:
        last_bar = refresh_data(settings, today)
    except Exception as e:  # noqa: BLE001
        errors.append(f"data refresh: {e}")
        last_bar = today - dt.timedelta(days=1)

    tc = None
    if alpaca_keys():
        try:
            from alpaca.trading.client import TradingClient
            tc = TradingClient(*alpaca_keys(), paper=True)
            live = live_stats(tc, float(settings.get("paper", {}).get("equity_cap", 25000)), since=state.get("stats_since"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"live stats: {e}")
    else:
        errors.append("no Alpaca keys in .env; skipped live stats")

    if tc is not None:
        try:
            miss = missed_runs(tc)
            if miss:
                errors.append(f"runner did not run on {len(miss)} trading day(s): {', '.join(miss)}")
                notify.send(f"⚠️ The daily paper runner did not run on {len(miss)} trading day(s): {', '.join(miss)}. "
                            "Was the laptop off at 17:35? Check the task 'TradingBot Paper Runner' and results/paper/runner.log.")
        except Exception as e:  # noqa: BLE001
            errors.append(f"missed-run check: {e}")

    try:
        gap = reality_gap(settings, load_json(PARAMS), state["paper_start"], today, live)
    except Exception as e:  # noqa: BLE001
        errors.append(f"reality gap: {e}\n{traceback.format_exc()[-400:]}")

    eq_stats, cost_note = None, None
    try:
        eq_stats = execution_quality(settings)
        if eq_stats:
            log(f"execution: {eq_stats}")
        cost_note = calibrate_costs(settings, eq_stats, cfg, args.dry_run)
        if cost_note:
            log(cost_note)
    except Exception as e:  # noqa: BLE001
        errors.append(f"execution quality: {e}")

    try:
        trip, reason = kill_switch(live, cfg)
        if trip and not paused:
            paused, pause_reason = True, reason
            log(f"KILL SWITCH: {reason}")
            state["paused_since"] = today.isoformat()
            if not args.dry_run:
                PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                PAUSE_FLAG.write_text(reason, encoding="utf-8")
                notify.send(f"🛑 KILL SWITCH engaged: {reason}. New entries paused until a retrained strategy passes "
                            f"or, after {cfg['kill_switch'].get('resume_after_days', 28)} days, the current one still passes its holdout exam.")
    except Exception as e:  # noqa: BLE001
        errors.append(f"kill switch: {e}")

    due = args.force_retune or paused or state.get("last_retune") is None or \
        (today - dt.date.fromisoformat(state["last_retune"])).days >= cfg["retune_every_days"]
    if due and not args.dry_run:
        try:
            retune_out = retune(settings, state, cfg, today, last_bar, apply=not args.no_apply, sweep_samples=args.sweep_samples or cfg["sweep_samples"],
                                only=set(args.books.split(",")) if args.books else None)
            state.setdefault("history", []).append({k: v for k, v in retune_out.items() if k not in ("champion_holdout", "challenger_holdout")})
            books_out = retune_out.get("books") or []
            if paused and books_out and books_out[0].get("replaced"):  # the primary book carries the account drawdown
                paused, pause_reason = False, ""
                PAUSE_FLAG.unlink(missing_ok=True)
                log("resumed trading with the new primary champion")
            elif paused and retune_out.get("replaced"):
                log("a secondary book was replaced but the primary was not; staying paused")
            if paused and books_out:
                days = (today - dt.date.fromisoformat(state.get("paused_since", today.isoformat()))).days
                champ_pf = (books_out[0].get("champion_holdout") or {}).get("stocks", {}).get("profit_factor", 0.0)
                if days >= int(cfg["kill_switch"].get("resume_after_days", 28)) and champ_pf > 1.0:
                    paused, pause_reason = False, ""
                    PAUSE_FLAG.unlink(missing_ok=True)
                    state["stats_since"] = today.isoformat()
                    log(f"resumed after {days} days paused: the champion still passes its holdout exam (PF {champ_pf:.2f}); fresh statistics window")
                    notify.send(f"▶️ Trading resumed after {days} days paused: no better strategy was found, but the current one still "
                                f"passes its holdout exam (PF {champ_pf:.2f}). The kill switch now judges trades from today on.")
                else:
                    log(f"still paused ({days} days; champion holdout PF {champ_pf:.2f}); resume needs {cfg['kill_switch'].get('resume_after_days', 28)} days and PF > 1")
        except Exception as e:  # noqa: BLE001
            errors.append(f"retune: {e}\n{traceback.format_exc()[-600:]}")
            try:
                restore_params()
            except Exception:  # noqa: BLE001
                pass
    elif due:
        log("retune due but skipped (dry run)")

    state.update(paused=paused, pause_reason=pause_reason, last_run=dt.datetime.now().isoformat(timespec="seconds"), last_errors=errors)
    if not args.dry_run:
        save_json(state, STATE_PATH)
    text = write_report(today, live, gap, paused, pause_reason, retune_out, errors, eq_stats, cost_note, settings)
    print("\n" + text)
    if not args.dry_run:
        head = f"🤖 Autopilot {today}" + ("  ⏸ PAUSED" if paused else "")
        if retune_out and retune_out.get("replaced"):
            head += "\n🔁 Strategy replaced: " + ", ".join(retune_out.get("replaced_books", []))
        body = text.replace("# ", "").replace("## ", "").replace("- ", "• ")
        notify.send(head + "\n\n" + body)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        notify.send(f"❌ Autopilot crashed: {type(e).__name__}: {e}")
        raise
