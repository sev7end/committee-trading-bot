"""Daily paper-trading runner for Alpaca, multi-book, two execution modes.

    python scripts/run_paper.py --auction --wait-for-open 150   # scheduled: before the open -> market-on-open orders
    python scripts/run_paper.py                                 # after the open -> marketable-limit bracket orders
    python scripts/run_paper.py --dry-run                       # compute and print, submit nothing
    python scripts/run_paper.py --sync                          # pull filled orders from Alpaca into results/paper/fills.csv

AUCTION mode (default for the scheduled task, which fires ~15 min before the New York open):
  signals come from yesterday's completed daily bars, exactly like the backtest; entries are submitted as
  market-on-open (OPG) orders so they fill in the opening auction at the official open, which is what the
  backtester assumes; time exits are OPG sells. Once the market opens the runner waits for the fills and
  attaches a one-cancels-other stop/target to each new position. If the runner starts after ~09:27 NY
  (too late for OPG) it falls back to the intraday mode automatically.

INTRADAY mode: marketable-limit bracket orders (see tradingbot/execution.py).

Books: settings.books lists the strategies trading side by side, each with its own params file, universe,
share of the sizing cap (paper.equity_cap) and positions file (results/paper/positions_<book>.json).
A symbol held by one book is off-limits to the others. Refuses to run against a non-paper account.
"""
from __future__ import annotations
import argparse
import datetime as dt
import sys
import time as _time

import pandas as pd

import _common  # noqa: F401

from tradingbot.committee import Committee
from tradingbot.config import CONFIG_DIR, DATA_DIR, RESULTS_DIR, ROOT, load_settings, load_universe, load_json, save_json, resolve_universe
from tradingbot.data import alpaca_keys, load_bars
from tradingbot.indicators import atr
from tradingbot.sizing import vol_scale_now
from tradingbot.earnings import is_blackout_now, refresh_earnings
from tradingbot.execution import (submit_entries, submit_auction_entries, submit_auction_exit, attach_brackets_after_open,
                                  attach_exit, cancel_open_orders, submit_market_sell)
from tradingbot.rotation_live import is_rebalance_day, target_weights, plan_orders
from tradingbot.voters import Context
from tradingbot import notify
from tradingbot import risklimits

PAPER_DIR = RESULTS_DIR / "paper"
ORDERS_FILE = PAPER_DIR / "orders.csv"
PLAN_FILE = PAPER_DIR / "pending_plan.json"   # planned entries, written BEFORE submission, deleted after bookkeeping
_NOTIFIED = {"exit": False}
ORDER_COLS = ["time", "book", "symbol", "action", "qty", "ref_price", "stop", "target", "order_id", "dry_run",
              "order_type", "limit_price", "bid", "ask", "fill_price", "slippage_bps", "status", "limit_base"]


def books_config(settings: dict) -> list[dict]:
    books = settings.get("books") or [{"name": "trend", "params": "config/params.json", "capital_pct": 100}]
    return [b for b in books if b.get("enabled", True)]


def positions_file(book: str):
    return PAPER_DIR / f"positions_{book}.json"


def load_all_tracked() -> dict[str, dict]:
    out = {}
    for p in PAPER_DIR.glob("positions_*.json"):
        book = p.stem.replace("positions_", "")
        for sym, v in load_json(p).items():
            out[sym] = {**v, "book": book}
    return out


def trading_client():
    from alpaca.trading.client import TradingClient

    keys = alpaca_keys()
    if not keys:
        sys.exit("No Alpaca keys in .env. Copy .env.example to .env and paste your PAPER keys.")
    last_err = None
    for attempt in range(20):  # the logon trigger can fire before Wi-Fi is up
        try:
            tc = TradingClient(keys[0], keys[1], paper=True)
            acct = tc.get_account()
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e).lower()
            if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
                break
            print(f"Alpaca unreachable ({type(e).__name__}); retry {attempt + 1}/20 in 30s")
            _time.sleep(30)
    else:
        last_err = last_err or RuntimeError("unreachable")
    if last_err is not None and "acct" not in locals():
        sys.exit(f"🔑 Alpaca rejected the API keys or is unreachable ({type(last_err).__name__}: {str(last_err)[:160]}). "
                 "Check the keys in .env on https://app.alpaca.markets (Paper Trading -> API Keys).")
    if not str(acct.account_number).upper().startswith("PA"):
        sys.exit(f"Account {acct.account_number} does not look like a paper account. Refusing to trade.")
    return tc, acct


def clock_safe(tc):
    for _ in range(10):
        try:
            return tc.get_clock()
        except Exception as e:  # noqa: BLE001
            print(f"clock check failed ({type(e).__name__}); retrying in 30s")
            _time.sleep(30)
    return tc.get_clock()


def completed_bars(settings: dict, symbols: list[str], today: pd.Timestamp) -> dict[str, pd.DataFrame]:
    live_settings = dict(settings, data_provider="yfinance")
    bars = load_bars(symbols, settings["data_start"], today.strftime("%Y-%m-%d"), live_settings, refresh=True, cache_dir=DATA_DIR / "live")
    return {s: df[df.index < today.normalize()] for s, df in bars.items()}


def compute_signals(params: dict, bars: dict[str, pd.DataFrame], bench: pd.DataFrame | None) -> pd.DataFrame:
    committee = Committee(params)
    ctx = Context(benchmark=bench)
    rows = []
    atr_n = int(params.get("exit", {}).get("atr_period", 14))
    for sym, df in bars.items():
        if len(df) < 60:
            continue
        sc = committee.score(df, ctx).iloc[-1]
        a = atr(df, atr_n).iloc[-1]
        rows.append({"symbol": sym, "date": df.index[-1].date().isoformat(), "close": float(df["close"].iloc[-1]),
                     "atr": float(a), "total": float(sc["total"]), "agree": int(sc["agree"]), "enter": bool(sc["enter"]),
                     **{f"score_{n}": float(sc[n]) for n in committee.voter_names}})
    return pd.DataFrame(rows).sort_values("total", ascending=False)


def append_order(row: dict):
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    if ORDERS_FILE.exists():
        old = pd.read_csv(ORDERS_FILE)
        changed = False
        if "book" not in old.columns:
            old.insert(1, "book", "trend")
            changed = True
        for c in ORDER_COLS:
            if c not in old.columns:
                old[c] = None
                changed = True
        if changed:
            old[ORDER_COLS].to_csv(ORDERS_FILE, index=False)
    pd.DataFrame([{k: row.get(k) for k in ORDER_COLS}]).to_csv(ORDERS_FILE, mode="a", header=not ORDERS_FILE.exists(), index=False)


def sync_fills(tc):
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    orders = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, nested=True))
    rows, seen = [], set()

    def add(o, parent=""):
        if o.filled_at is None or str(o.id) in seen or float(o.filled_qty or 0) <= 0:
            return
        seen.add(str(o.id))
        rows.append({"filled_at": o.filled_at, "symbol": o.symbol, "side": o.side.value, "qty": float(o.filled_qty or 0),
                     "avg_price": float(o.filled_avg_price or 0), "type": o.type.value, "order_id": str(o.id), "parent_id": parent})

    for o in orders:
        add(o)
        for leg in (o.legs or []):  # bracket stop/target sells are returned as legs of the buy, not as top-level orders
            add(leg, str(o.id))
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["filled_at", "symbol", "side", "qty", "avg_price", "type", "order_id", "parent_id"]
    df = pd.DataFrame(rows, columns=cols)
    out_p = PAPER_DIR / "fills.csv"
    if out_p.exists() and out_p.stat().st_size > 5:  # keep fills older than the API's 500-order page; dedupe on order_id
        old = pd.read_csv(out_p)
        df = pd.concat([old[~old["order_id"].astype(str).isin(set(df["order_id"].astype(str)))], df], ignore_index=True)
    if len(df):
        df["filled_at"] = df["filled_at"].astype(str)
        df = df.sort_values("filled_at")
    df.to_csv(out_p, index=False)
    print(f"{len(df)} fills -> {out_p}")


def fail(msg: str, args) -> None:
    """Alert once (not again via the __main__ handler) and stop with a non-zero exit code."""
    if not args.dry_run:
        notify.send(msg)
        _NOTIFIED["exit"] = True
    sys.exit(msg)


def previous_session(tc, today: dt.date):
    """The last NYSE session strictly before `today` (None if the calendar call fails)."""
    try:
        from alpaca.trading.requests import GetCalendarRequest
        cal = tc.get_calendar(GetCalendarRequest(start=today - dt.timedelta(days=12), end=today - dt.timedelta(days=1)))
        days = [d.date if isinstance(d.date, dt.date) else dt.date.fromisoformat(str(d.date)[:10]) for d in cal]
        days = [d for d in days if d < today]
        return max(days) if days else None
    except Exception as e:  # noqa: BLE001
        print(f"calendar unavailable ({type(e).__name__}); skipping the stale-data check")
        return None


def reenter_unfilled(tc, summaries: list[dict], settings: dict, today) -> list[str]:
    """Alpaca's paper venue lets many market-on-open orders EXPIRE unfilled (ETFs and thinner names especially: 5 of
    8 in the first week). Right after the open, re-enter every planned entry that got no fill at all with a
    marketable-limit bracket, so the signal is not lost. Partial auction fills are kept as they are."""
    redone = []
    for s in summaries:
        left = [e for e in s.get("planned", []) if e.get("status") in ("unfilled", "unfilled_timeout") and not e.get("fill_price")]
        if not left:
            continue
        for e in left:
            e["opg_order_id"], e["auction_status"] = e.get("order_id"), e.get("status")
        print(f"[auction] {len(left)} order(s) expired unfilled in the auction; re-entering with limit brackets: {', '.join(e['symbol'] for e in left)}")
        submit_entries(tc, left, settings.get("paper", {}).get("entry_orders"), s["book"], today.date())
        redone += [f"{e['symbol']} ({e.get('status')})" for e in left]
    return redone


def fill_missing_session(bars: dict, last_session, symbols: list[str]) -> int:
    """Yahoo occasionally publishes yesterday's daily bar late. If every symbol is exactly one session behind, take
    that one day from Alpaca's daily bars (IEX feed; volume is a fraction of consolidated, prices are fine) so the run
    can proceed on current prices. Returns the number of symbols patched."""
    try:
        from tradingbot.data import _fetch_alpaca
        start = pd.Timestamp(last_session).strftime("%Y-%m-%d")
        end = (pd.Timestamp(last_session) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        got = _fetch_alpaca(symbols, start, end, "1Day")
    except Exception as e:  # noqa: BLE001
        print(f"Alpaca daily bars unavailable ({type(e).__name__}); cannot patch the missing session")
        return 0
    n = 0
    day = pd.Timestamp(last_session)
    for sym in symbols:
        df = bars.get(sym)
        if df is None or sym not in got or day not in got[sym].index or df.index[-1] >= day:
            continue
        row = got[sym].loc[[day]]
        bars[sym] = pd.concat([df, row[df.columns.intersection(row.columns)]])
        n += 1
    return n


def _open_sell_symbols(tc) -> set:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    return {o.symbol for o in tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)) if o.side.value == "sell"}


def do_exits(tc, summaries: list[dict], mode: str, today) -> None:
    """Submit the planned time exits. A position leaves its book only once its sell order exists; if the sell fails
    after the stop/target were cancelled, the bracket is re-attached and the position is kept and reported."""
    for s in summaries:
        for x in s.get("exits", []):
            sym, info, qty = x["symbol"], x["info"], x["qty"]
            order_id = None
            partial = bool(x.get("partial"))
            try:
                if partial:   # a rotation trim: sell part, keep the rest protected
                    cancel_open_orders(tc, sym)
                    order_id = submit_market_sell(tc, sym, qty, s["book"], today.date(), opg=(mode == "auction"))
                    remaining = int(float(info.get("qty", 0)) - qty)
                    if remaining > 0:
                        info["qty"] = remaining
                        info["oco_id"] = attach_exit(tc, sym, remaining, float(info["stop"]), info.get("target"))
                elif mode == "auction":
                    order_id = submit_auction_exit(tc, sym, qty, s["book"], today.date())
                else:
                    cancel_open_orders(tc, sym)
                    order_id = str(tc.close_position(sym).id)
            except Exception as ex:  # noqa: BLE001
                print(f"{'trim' if partial else 'time exit'} {sym} failed: {str(ex)[:160]}")
            if order_id and partial:
                s["closed"].append(f"{sym} (trim {int(qty)})")
                append_order({"time": dt.datetime.now().isoformat(timespec="seconds"), "book": s["book"], "symbol": sym, "action": "trim",
                              "qty": qty, "dry_run": False, "order_id": order_id, "order_type": "market_opg" if mode == "auction" else "market"})
            elif order_id:
                s["tracked"].pop(sym, None)
                s["closed"].append(sym)
                append_order({"time": dt.datetime.now().isoformat(timespec="seconds"), "book": s["book"], "symbol": sym, "action": "close_time",
                              "qty": qty, "dry_run": False, "order_id": order_id, "order_type": "market_opg" if mode == "auction" else "market"})
            else:
                s.setdefault("exit_failed", []).append(sym)
                try:  # the stop/target may have been cancelled: put them back
                    if sym not in _open_sell_symbols(tc):
                        info["oco_id"] = attach_exit(tc, sym, int(qty), float(info["stop"]), info.get("target"))
                except Exception as ex:  # noqa: BLE001
                    print(f"re-attaching the bracket for {sym} failed: {str(ex)[:160]}")


def reconcile(tc, held: dict, books: list[dict], bars: dict, bench, settings, today) -> list[str]:
    """Make the books agree with Alpaca. Every held position must (a) be tracked by exactly one book and (b) have a
    working sell order (its stop/target). Repairs: adopt untracked positions into the book whose universe holds the
    symbol (levels from the pending plan if the previous run died mid-way, else from the current ATR), and attach a
    fresh one-cancels-other stop/target wherever none is open. Returns human-readable notes for the alert."""
    notes = []
    if not held:
        return notes
    try:
        protected = _open_sell_symbols(tc)
    except Exception as e:  # noqa: BLE001
        print(f"reconcile: cannot list open orders ({type(e).__name__}); skipped")
        return notes
    tracked_all = load_all_tracked()
    plan = load_json(PLAN_FILE) if PLAN_FILE.exists() else {}
    planned = {e["symbol"]: {**e, "book": b} for b, es in (plan.get("books") or {}).items() for e in es}
    changed = {}
    for sym, pos in held.items():
        if sym in tracked_all and sym in protected:
            continue
        qty = int(float(pos.qty))
        avg = float(pos.avg_entry_price)
        info = tracked_all.get(sym)
        if info is None:
            owner = planned[sym]["book"] if sym in planned else next(
                (b["name"] for b in books if sym in resolve_universe(b.get("universe", "stocks"), settings)), books[0]["name"])
            if sym in planned and planned[sym].get("atr"):
                p = planned[sym]
                stop = round(avg - float(p["stop_atr"]) * float(p["atr"]), 2)
                target = round(avg + float(p["target_atr"]) * float(p["atr"]), 2) if float(p.get("target_atr", 0)) > 0 else None
            else:
                owner_book = next(b for b in books if b["name"] == owner)
                params = load_json(ROOT / owner_book["params"])
                exit_cfg = params.get("exit") or {}
                if owner_book.get("engine") == "rotation" or not exit_cfg:   # rank books: a wide disaster stop, no target
                    stop, target = round(avg * (1 - float(owner_book.get("disaster_stop_pct", 20)) / 100), 2), None
                else:
                    a = float(atr(bars[sym], int(exit_cfg.get("atr_period", 14))).iloc[-1]) if sym in bars else avg * 0.02
                    stop = round(avg - float(exit_cfg["stop_atr"]) * a, 2)
                    target = round(avg + float(exit_cfg["target_atr"]) * a, 2) if float(exit_cfg.get("target_atr", 0)) > 0 else None
            entry_date = plan.get("date") if sym in planned else today.date().isoformat()
            info = {"entry_date": entry_date, "qty": qty, "stop": stop, "target": target, "order_id": planned.get(sym, {}).get("order_id"),
                    "fill_price": avg, "adopted": True}
            tracked_all[sym] = {**info, "book": owner}
            changed.setdefault(owner, {})[sym] = dict(info)
            notes.append(f"{sym} x{qty} was held but untracked -> adopted into '{owner}'")
        if sym not in protected:
            try:
                info["oco_id"] = attach_exit(tc, sym, qty, float(info["stop"]), info.get("target"))
                changed.setdefault(tracked_all[sym]["book"], {})[sym] = {k: v for k, v in info.items() if k != "book"}
                notes.append(f"{sym}: no stop order was open -> stop {info['stop']} / target {info['target']} attached")
            except Exception as ex:  # noqa: BLE001
                notes.append(f"⚠️ {sym}: could not attach a stop ({str(ex)[:100]}); position is UNPROTECTED")
    for book_name, upd in changed.items():
        p = positions_file(book_name)
        cur = load_json(p) if p.exists() else {}
        for sym, info in upd.items():
            cur[sym] = {**cur.get(sym, {}), **{k: v for k, v in info.items() if k != "book"}}
        save_json(cur, p)
    for n in notes:
        print(f"[reconcile] {n}")
    return notes


def account_risk(tc, acct, held: dict, settings: dict) -> risklimits.RiskState:
    """Account-wide risk state from every book's tracked positions, live prices and the equity history."""
    base = float(settings.get("paper", {}).get("equity_cap", settings["account"]["starting_cash"]))
    tracked_all = {s: v for s, v in load_all_tracked().items() if s in held}
    prices = {s: float(held[s].current_price) for s in tracked_all}
    hist = None
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        h = tc.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
        hist = [float(x) for x in h.equity if x is not None]
    except Exception as e:  # noqa: BLE001
        print(f"portfolio history unavailable ({type(e).__name__}); loss limits not checked today")
    st = risklimits.from_positions(settings, base, tracked_all, prices, hist)
    print(f"risk limits: {st.describe()}")
    return st


def trail_stops(tc, books: list[dict], bars: dict, held: dict, dry_run: bool) -> list[str]:
    """Trailing exits: before the open, raise each tracked stop to (yesterday's high - trail_atr * ATR) when that is
    higher than the current stop, replacing the exit order. Mirrors the engine, which ratchets after each day's bar."""
    notes = []
    for b in books:
        params = load_json(ROOT / b["params"])
        exit_cfg = params.get("exit") or {}          # a rotation book has no exit section: nothing to trail
        trail = float(exit_cfg.get("trail_atr", 0) or 0)
        pf = positions_file(b["name"])
        tracked = load_json(pf) if pf.exists() else {}
        changed = False
        for sym, info in tracked.items():
            t = float(info.get("trail_atr", trail) or 0)
            if t <= 0 or sym not in bars or (held and sym not in held):
                continue
            df = bars[sym]
            a = float(atr(df, int(exit_cfg.get("atr_period", 14))).iloc[-1])
            new_stop = round(float(df["high"].iloc[-1]) - t * a, 2)
            if not (a > 0) or new_stop <= float(info["stop"]) + 0.005:
                continue
            note = f"{sym}: stop {info['stop']} -> {new_stop}"
            if not dry_run:
                try:
                    cancel_open_orders(tc, sym)
                    info["oco_id"] = attach_exit(tc, sym, int(float(held[sym].qty)), new_stop, info.get("target"))
                except Exception as ex:  # noqa: BLE001
                    notes.append(f"⚠️ {sym}: trailing stop update failed ({str(ex)[:80]})")
                    continue
            info["stop"] = new_stop
            changed = True
            notes.append(note)
        if changed and not dry_run:
            save_json(tracked, pf)
    for n in notes:
        print(f"[trail] {n}")
    return notes


def plan_rotation_book(book: dict, idx: int, tc, acct, bars, bench, settings, today, args, held: dict, claimed: set, mode: str,
                       risk: risklimits.RiskState | None = None) -> dict:
    """Momentum rotation: on the first session of each month, sell what left the top ranks and buy what entered,
    sized to the book's capital. Any other day: nothing (the wide disaster stops stay in place)."""
    name = book["name"]
    params = load_json(ROOT / book["params"])
    cap_total = float(settings.get("paper", {}).get("equity_cap", settings["account"]["starting_cash"]))
    cap = cap_total * float(book.get("capital_pct", 100)) / 100
    tracked = load_json(positions_file(name)) if positions_file(name).exists() else {}
    if tc is not None:
        tracked = {s: v for s, v in tracked.items() if s in held}
    summary = {"book": name, "version": params.get("version"), "signals": 0, "universe": len(bars), "top": "", "placed": [], "closed": [],
               "open_before": len(tracked), "params": params, "tracked": tracked, "planned": [], "exits": [], "rotation": True}
    sessions = bench.index if bench is not None else pd.DatetimeIndex([])
    due = is_rebalance_day(today.date(), sessions, str(params.get("rebalance", "M"))) or not tracked   # an empty book starts at the next run
    print(f"\n=== book '{name}' (rotation v{params.get('version')}, cap ${cap:,.0f}, {mode}) — {'REBALANCE day' if due else 'not a rebalance day, holding'}")
    if not due:
        return summary
    others = set(claimed) - set(tracked)                       # symbols another book holds are off-limits
    targets = target_weights(bars, params, bench, exclude=others)
    prices = {s: float(df["close"].iloc[-1]) for s, df in bars.items() if len(df)}
    for s in tracked:
        if s in held and s not in prices:
            prices[s] = float(held[s].current_price)
    holdings = {s: (float(held[s].qty) if s in held else float(v.get("qty", 0))) for s, v in tracked.items()}
    min_notional = float(settings.get("paper", {}).get("min_position_notional", 0) or 0)
    sells, buys = plan_orders(targets, holdings, prices, cap, min_notional)
    summary["signals"] = len(targets)
    summary["top"] = ", ".join(f"{s} {w * 100:.0f}%" for s, w in sorted(targets.items(), key=lambda x: -x[1])[:6])
    print(f"targets: {summary['top'] or 'none (cash)'}")
    for x in sells:
        info = tracked.get(x["symbol"], {})
        full = x["qty"] >= holdings.get(x["symbol"], 0) - 1e-9
        print(f"SELL [{name}] {x['symbol']} x{x['qty']} ({x['reason']})")
        summary["exits"].append({"symbol": x["symbol"], "qty": float(x["qty"]), "info": info, "partial": not full})
    if (PAPER_DIR / "PAUSED").exists():
        summary["paused"] = (PAPER_DIR / "PAUSED").read_text(encoding="utf-8").strip()
        return summary
    if risk is None:
        risk = risklimits.RiskState(cfg=risklimits.limits(settings), base=cap, themes=risklimits.load_themes())
    if risk.blocked:
        summary["blocked"] = risk.blocked
        return summary
    dsp = float(book.get("disaster_stop_pct", 20)) / 100
    for b in buys:
        px = b["ref_price"]
        stop = round(px * (1 - dsp), 2)
        qty = risk.fit_qty(int(b["qty"]), px - stop)           # no theme cap for a diversified rank book; the open-risk budget still applies
        if qty <= 0:
            summary.setdefault("risk_capped", []).append(b["symbol"])
            continue
        print(f"BUY [{name}] {b['symbol']} x{qty} ~{px:.2f}  weight {b['weight'] * 100:.0f}%  disaster stop {stop:.2f}")
        summary["planned"].append({"symbol": b["symbol"], "qty": qty, "stop": stop, "target": None, "ref_price": px, "book": name,
                                   "atr": None, "stop_atr": None, "target_atr": 0.0, "trail_atr": 0.0, "weight": b["weight"]})
        claimed.add(b["symbol"])
        risk.add(b["symbol"], qty, px - stop)
    return summary


def plan_book(book: dict, idx: int, tc, acct, bars, bench, settings, today, args, held: dict, claimed: set, mode: str,
              risk: risklimits.RiskState | None = None) -> dict:
    """Compute one book's signals, do its time exits, and return the entries it wants (not yet submitted)."""
    name = book["name"]
    params = load_json(ROOT / book["params"])
    cap_total = float(settings.get("paper", {}).get("equity_cap", settings["account"]["starting_cash"]))
    cap = cap_total * float(book.get("capital_pct", 100)) / 100
    tracked = load_json(positions_file(name)) if positions_file(name).exists() else {}
    if tc is not None:
        tracked = {s: v for s, v in tracked.items() if s in held}

    sig = compute_signals(params, bars, bench)
    tag = "_dryrun" if args.dry_run else ""
    sig.to_csv(PAPER_DIR / (f"signals_{today.date()}{tag}.csv" if idx == 0 else f"signals_{today.date()}{tag}_{name}.csv"), index=False)
    print(f"\n=== book '{name}' (params v{params.get('version')}, cap ${cap:,.0f}, {mode}) — {int(sig['enter'].sum())} of {len(sig)} qualify")
    print(sig.head(6).to_string(index=False))

    invested = sum(float(held[s].market_value) for s in tracked if s in held) if tc is not None else 0.0
    equity = cap
    cash = max(0.0, min(float(acct.cash) if tc is not None else cap, cap - invested))
    summary = {"book": name, "version": params.get("version"), "signals": int(sig["enter"].sum()), "universe": len(sig),
               "top": ", ".join(f"{r.symbol} {r.total:.1f}" for r in sig.head(3).itertuples()), "placed": [], "closed": [], "open_before": len(tracked),
               "params": params, "tracked": tracked, "planned": [], "exits": []}

    # Time exits are planned here and submitted by do_exits() once the execution mode is final.
    # Backtest: the entry day is bar 1 and the position is sold at the CLOSE of bar max_hold. The closest a pre-open
    # runner can do is sell at the next open, so count sessions from the entry day through yesterday.
    max_hold = int(params["exit"].get("max_hold_bars", 10))
    bench_dates = bench.index if bench is not None else pd.DatetimeIndex([])
    for sym, info in list(tracked.items()):
        held_bars = int((bench_dates >= pd.Timestamp(info["entry_date"])).sum())
        if held_bars >= max_hold:
            print(f"TIME EXIT {sym}: held {held_bars} bars >= {max_hold}")
            summary["exits"].append({"symbol": sym, "qty": float(held[sym].qty) if sym in held else float(info.get("qty", 0)), "info": info})

    if (PAPER_DIR / "PAUSED").exists():
        summary["paused"] = (PAPER_DIR / "PAUSED").read_text(encoding="utf-8").strip()
        return summary

    exit_cfg, risk_cfg = params["exit"], params["risk"]
    max_positions = int(book.get("max_positions", settings["account"]["max_positions"]))
    slots = max_positions - (len(tracked) - len(summary["exits"]))  # a symbol leaving today frees its slot (it stays claimed today)
    candidates = sig[sig["enter"] & ~sig["symbol"].isin(claimed)]
    print(f"open in book: {len(tracked)}  free slots: {slots}  candidates: {len(candidates)}  cash ${cash:,.0f}")
    if risk is None:
        risk = risklimits.RiskState(cfg=risklimits.limits(settings), base=cap, themes=risklimits.load_themes())
    if risk.blocked:
        print(f"no new entries for [{name}]: {risk.blocked}")
        summary["blocked"] = risk.blocked
        return summary
    earn_cfg = params.get("entry", {}).get("earnings_blackout")
    taken = 0
    for _, r in candidates.iterrows():
        if taken >= max(slots, 0):
            break
        sym = r["symbol"]
        if risk.theme_full(sym):
            print(f"skip {sym}: theme '{risklimits.theme_of(sym, risk.themes)}' already at its cap")
            summary.setdefault("theme_capped", []).append(sym)
            continue
        if earn_cfg and earn_cfg.get("enabled") and sym in bars and is_blackout_now(sym, bars[sym].index, earn_cfg):
            print(f"skip {sym}: earnings blackout")
            summary.setdefault("blackout", []).append(sym)
            continue
        px, a = r["close"], r["atr"]
        stop = px - float(exit_cfg["stop_atr"]) * a
        target = (px + float(exit_cfg["target_atr"]) * a) if float(exit_cfg.get("target_atr", 0)) > 0 else None
        per_share_risk = px - stop
        if per_share_risk <= 0 or a <= 0:
            continue
        vs = vol_scale_now(bars[sym], bench, risk_cfg.get("vol_target")) if sym in bars else 1.0
        qty = int(min(equity * float(risk_cfg["risk_per_trade_pct"]) / 100 * vs / per_share_risk,
                      equity * float(risk_cfg["max_position_pct"]) / 100 / px, cash / px))  # the cap is a cap; vol_scale dials risk only
        if abs(vs - 1.0) > 0.01:
            print(f"  vol scale for {sym}: x{vs:.2f}")
        if qty <= 0:
            print(f"skip {sym}: not enough cash for one share at ~{px:.2f}")
            continue
        min_notional = float(settings.get("paper", {}).get("min_position_notional", 0) or 0)
        if min_notional and qty * px < min_notional:
            print(f"skip {sym}: ${qty * px:,.0f} position is below the ${min_notional:,.0f} minimum (book nearly full)")
            continue
        fitted = risk.fit_qty(qty, per_share_risk)
        if fitted <= 0:
            print(f"skip {sym}: open-risk budget exhausted (${risk.budget:,.0f} left)")
            summary.setdefault("risk_capped", []).append(sym)
            continue
        if fitted < qty:
            print(f"  {sym}: size cut {qty} -> {fitted} to fit the open-risk budget")
            qty = fitted
        print(f"BUY [{name}] {sym} x{qty} ~{px:.2f}  stop {stop:.2f}  target {'none (trailing)' if target is None else f'{target:.2f}'}  score {r['total']:.2f}")
        summary["planned"].append({"symbol": sym, "qty": qty, "stop": round(stop, 2), "target": (round(target, 2) if target is not None else None),
                                   "ref_price": round(px, 2), "book": name, "atr": round(float(a), 4),
                                   "stop_atr": float(exit_cfg["stop_atr"]), "target_atr": float(exit_cfg.get("target_atr", 0) or 0),
                                   "trail_atr": float(exit_cfg.get("trail_atr", 0) or 0)})
        cash -= qty * px
        taken += 1
        claimed.add(sym)
        risk.add(sym, qty, per_share_risk)
    return summary


def record_entries(summary: dict, args, today):
    """After submission (either mode): update the book's positions file, the order log and the summary lines."""
    tracked = summary["tracked"]
    for e in summary["planned"]:
        st = e.get("status", "dry_run" if args.dry_run else None) or ""
        # anything that filled (even without its bracket) or is still working is a position we own and must track
        ok = args.dry_run or bool(e.get("fill_price")) or st in ("filled", "fallback", "new", "opg_submitted") or st.startswith("filled")
        if ok:
            tracked[e["symbol"]] = {"entry_date": today.date().isoformat(), "qty": e["qty"], "stop": e["stop"], "target": e["target"],
                                    "order_id": e.get("order_id"), "fill_price": e.get("fill_price"), "oco_id": e.get("oco_id"),
                                    "trail_atr": e.get("trail_atr", 0)}
            fill = f" filled {e['fill_price']:.2f}" if e.get("fill_price") else ""
            summary["placed"].append(f"{e['symbol']} x{e['qty']}{fill} (stop {e['stop']}, target {e['target']})")
            if st == "filled_no_bracket":
                summary.setdefault("unprotected", []).append(e["symbol"])
        else:
            summary.setdefault("failed", []).append(f"{e['symbol']} ({st})")
        if not args.dry_run:
            append_order({"time": dt.datetime.now().isoformat(timespec="seconds"), "book": summary["book"], "symbol": e["symbol"], "action": "buy_bracket",
                          "qty": e["qty"], "ref_price": e["ref_price"], "stop": e["stop"], "target": e["target"], "order_id": e.get("order_id"),
                          "dry_run": False, "order_type": e.get("order_type"), "limit_price": e.get("limit_price"), "bid": e.get("bid"), "ask": e.get("ask"),
                          "fill_price": e.get("fill_price"), "slippage_bps": e.get("slippage_bps"), "status": st, "limit_base": e.get("limit_base")})
    if not args.dry_run:
        save_json(tracked, positions_file(summary["book"]))
    summary["open_after"] = len(tracked)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sync", action="store_true", help="download filled orders and exit")
    ap.add_argument("--force", action="store_true", help="run even if the market is closed (intraday mode)")
    ap.add_argument("--auction", action="store_true", help="before the open: submit market-on-open orders and attach stops/targets after the fill")
    ap.add_argument("--skip-if-done", action="store_true", help="exit quietly if today's run already happened (for the logon trigger)")
    ap.add_argument("--wait-for-open", type=int, default=0, metavar="MINUTES", help="intraday mode: wait up to N minutes for the market to open")
    ap.add_argument("--max-minutes-before-open", type=int, default=420, metavar="MINUTES",
                    help="auction mode: if the open is further away than this, do nothing (a logon run far from the open leaves the day to the scheduled run)")
    args = ap.parse_args()

    settings = load_settings()
    today = pd.Timestamp(dt.datetime.now().date())
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    legacy = PAPER_DIR / "positions.json"
    if legacy.exists() and not positions_file("trend").exists():
        legacy.rename(positions_file("trend"))
    done_flag = PAPER_DIR / f"done_{today.date()}.flag"   # written only by a completed LIVE run (a --dry-run must not block the real one)
    if args.skip_if_done and done_flag.exists() and not args.dry_run:
        print(f"already ran today ({today.date()}), nothing to do")
        return

    tc = acct = None
    if not args.dry_run or args.sync:
        tc, acct = trading_client()
    if args.sync:
        sync_fills(tc)
        return

    # ---- choose the execution mode
    mode = "intraday"
    if tc is not None:
        clock = clock_safe(tc)
        ny_now = clock.timestamp
        if args.auction and not clock.is_open and clock.next_open.date() == ny_now.date() and (clock.next_open - ny_now).total_seconds() > 180:
            mins = (clock.next_open - ny_now).total_seconds() / 60
            if mins > args.max_minutes_before_open and not args.force:
                # A logon run hours before the open would submit OPG orders and then have to wait past the task's
                # time limit to attach the stops. Leave the day to the scheduled 17:15 run instead (no signals file
                # is written, so --skip-if-done will not block it).
                print(f"auction mode: market opens in {mins:.0f} min, more than {args.max_minutes_before_open}; leaving today to the scheduled run")
                return
            mode = "auction"
            print(f"auction mode: market opens at {str(clock.next_open)[:16]} NY, in {mins:.0f} min")
        else:
            waited = 0
            while not clock.is_open and not args.force and waited < args.wait_for_open:
                if waited % 10 == 0:
                    print(f"market closed, next open {clock.next_open}; waiting ... ({waited} min)")
                _time.sleep(60)
                waited += 1
                clock = clock_safe(tc)
            if not clock.is_open and not args.force:
                print(f"Market is closed (next open {clock.next_open}); nothing to do. Use --force to submit anyway.")
                return
    elif args.auction:
        mode = "auction"

    books = books_config(settings)
    symbols = list(dict.fromkeys([sym for b in books for sym in resolve_universe(b.get("universe", "stocks"), settings)]))
    bench_sym = settings["benchmark"]
    bars = completed_bars(settings, list(dict.fromkeys(symbols + [bench_sym])), today)
    bench = bars.get(bench_sym)   # kept in `bars` too: SPY is a tradable member of the ETF universe
    if bench is None or len(bars) < 0.8 * len(symbols):
        fail(f"⚠️ Price feed incomplete: {len(bars)} of {len(symbols)} symbols loaded, benchmark {'ok' if bench is not None else 'MISSING'}. "
             "Yahoo may be down; no trades placed today.", args)
    if tc is not None:
        last_session = previous_session(tc, today.date())
        tries = 0
        while last_session and bench.index[-1].date() < last_session and tries < 2:   # Yahoo sometimes lags by a few minutes
            tries += 1
            print(f"benchmark's last bar is {bench.index[-1].date()} but the last session was {last_session}; retrying Yahoo in 90s ({tries}/2)")
            _time.sleep(90)
            bars = completed_bars(settings, list(dict.fromkeys(symbols + [bench_sym])), today)
            bench = bars.get(bench_sym, bench)
        if last_session and bench.index[-1].date() < last_session:
            n = fill_missing_session(bars, last_session, list(bars.keys()))
            bench = bars.get(bench_sym, bench)
            print(f"patched the missing session {last_session} from Alpaca daily bars for {n} symbols")
            if bench.index[-1].date() < last_session:
                fail(f"⚠️ Price data is stale: the benchmark's last bar is {bench.index[-1].date()} but the last session was {last_session}, "
                     "and Alpaca could not fill the gap. Signals would use old prices; no trades placed today.", args)
            notify.send(f"ℹ️ Yahoo had not published {last_session} by run time; used Alpaca's daily bar for that day ({n} symbols).", silent=True)
        stale = [s for s, df in bars.items() if s != bench_sym and last_session and df.index[-1].date() < last_session]
        if stale:
            print(f"dropping {len(stale)} symbols with stale bars: {', '.join(stale[:10])}{' ...' if len(stale) > 10 else ''}")
            for s in stale:
                bars.pop(s, None)
            if len(bars) < 0.8 * len(symbols):
                fail(f"⚠️ {len(stale)} of {len(symbols)} symbols have stale bars (last session {last_session}). No trades placed today.", args)
    st_p = RESULTS_DIR / "autopilot_state.json"
    if st_p.exists() and not args.dry_run:
        last = load_json(st_p).get("last_run")
        if last and (dt.datetime.now() - dt.datetime.fromisoformat(last)).days > 9:
            notify.send(f"⚠️ The Saturday autopilot has not run since {last[:10]}. Check the task 'TradingBot Autopilot' and results/reports/autopilot_runner.log.", silent=True)
    try:
        stock_syms = [sym for b in books if b.get("universe", "stocks") == "stocks" and (load_json(ROOT / b["params"]).get("entry", {}).get("earnings_blackout") or {}).get("enabled")
                      for sym in resolve_universe("stocks", settings)]
        if stock_syms:
            refresh_earnings(sorted(set(stock_syms)), max_age_days=7, verbose=False)
    except Exception as e:  # noqa: BLE001
        print(f"earnings refresh skipped: {e}")

    held = {p.symbol: p for p in tc.get_all_positions()} if tc is not None else {}
    repaired = []
    if tc is not None and not args.dry_run:
        repaired += reconcile(tc, held, books, bars, bench, settings, today)   # yesterday's leftovers first
    claimed = set(load_all_tracked().keys()) | set(held.keys())
    if tc is not None:
        print(f"account equity {float(acct.equity):,.0f}, cash {float(acct.cash):,.0f}, {len(held)} positions held")

    trailed = trail_stops(tc, books, bars, held, args.dry_run) if tc is not None else []
    risk = account_risk(tc, acct, held, settings) if tc is not None else None
    summaries = []
    for i, b in enumerate(books):
        uni = set(resolve_universe(b.get("universe", "stocks"), settings))
        book_bars = {k: v for k, v in bars.items() if k in uni}
        planner = plan_rotation_book if b.get("engine") == "rotation" else plan_book
        summaries.append(planner(b, i, tc, acct, book_bars, bench, settings, today, args, held, claimed, mode, risk))

    # ---- final mode check: the data download and scoring take minutes; Alpaca rejects OPG orders after ~09:28 NY
    if tc is not None and not args.dry_run and mode == "auction":
        clock = clock_safe(tc)
        secs = 0 if clock.is_open else (clock.next_open - clock.timestamp).total_seconds()
        if clock.is_open or secs < 150:
            print(f"too close to the open ({secs / 60:.1f} min) for market-on-open orders; switching to intraday execution")
            mode = "intraday"
            while not clock.is_open:
                _time.sleep(20)
                clock = clock_safe(tc)

    # ---- submit
    reentered = []
    if not args.dry_run:
        save_json({"date": today.date().isoformat(), "mode": mode, "books": {s["book"]: s["planned"] for s in summaries}}, PLAN_FILE)
        do_exits(tc, summaries, mode, today)
        if mode == "auction":
            for s in summaries:
                if s["planned"]:
                    submit_auction_entries(tc, s["planned"], s["book"], today.date())
            all_entries = [e for s in summaries for e in s["planned"]]
            if all_entries:
                attach_brackets_after_open(tc, all_entries, deadline_seconds_after_open=300)
                reentered = reenter_unfilled(tc, summaries, settings, today)
        else:
            for s in summaries:
                if s["planned"]:
                    s["planned"] = submit_entries(tc, s["planned"], settings.get("paper", {}).get("entry_orders"), s["book"], today.date())
    for s in summaries:
        record_entries(s, args, today)

    if not args.dry_run:
        held = {p.symbol: p for p in tc.get_all_positions()}
        repaired += reconcile(tc, held, books, bars, bench, settings, today)   # anything today left unprotected
        PLAN_FILE.unlink(missing_ok=True)
        done_flag.write_text(dt.datetime.now().isoformat(timespec="seconds"), encoding="utf-8")

        lines = [f"📈 Paper run {today.date()} ({'opening auction' if mode == 'auction' else 'intraday limits'})"]
        loud = bool(repaired)
        for s in summaries:
            if "paused" in s:
                lines.append(f"[{s['book']} v{s['version']}] ⏸ PAUSED ({s['paused']}); {s['open_before']} open kept")
                continue
            loud = loud or bool(s["placed"]) or bool(s.get("failed")) or bool(s["closed"]) or bool(s.get("exit_failed"))
            line = f"[{s['book']} v{s['version']}] {s['signals']}/{s['universe']} qualify · top: {s['top'] or '-'} · placed {len(s['placed'])}"
            if s["placed"]:
                line += ": " + "; ".join(s["placed"])
            if s.get("failed"):
                line += f" · failed: {', '.join(s['failed'])}"
            if s["closed"]:
                line += f" · time-exit: {', '.join(s['closed'])}"
            if s.get("exit_failed"):
                line += f" · ⚠️ time-exit FAILED (kept, stop re-attached): {', '.join(s['exit_failed'])}"
            if s.get("unprotected"):
                line += f" · ⚠️ filled without bracket: {', '.join(s['unprotected'])}"
            if s.get("blocked"):
                line += f" · ⛔ no entries: {s['blocked']}"
            if s.get("theme_capped") or s.get("risk_capped"):
                line += f" · skipped by limits: {', '.join(s.get('theme_capped', []) + s.get('risk_capped', []))}"
            lines.append(line)
        if repaired:
            lines.append("🛡 reconciliation: " + "; ".join(repaired))
        if trailed:
            lines.append("📈 trailing stops raised: " + "; ".join(trailed))
        if mode == "auction" and reentered:
            lines.append("🔁 expired in the auction, re-entered with limit brackets: " + ", ".join(reentered))
        if risk is not None:
            lines.append("🧯 risk limits: " + risk.describe())
        lines.append(f"Open positions: {len(load_all_tracked())} · account equity ${float(acct.equity):,.0f}")
        notify.send("\n".join(lines), silent=not loud)
    print("\ndone" + (" (dry run, nothing submitted)" if args.dry_run else ""))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None) and not _NOTIFIED["exit"]:
            notify.send(f"⚠️ Paper run stopped: {e.code}")
        raise
    except Exception as e:  # noqa: BLE001
        notify.send(f"❌ Paper run crashed: {type(e).__name__}: {e}")
        raise
