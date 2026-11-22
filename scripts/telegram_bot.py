"""Telegram command listener. Answers only the chat in TELEGRAM_CHAT_ID.

    python scripts/telegram_bot.py        # long-polls forever; registered as the 'TradingBot Telegram' task at logon

Commands (also shown in Telegram's "/" menu):
  /balance    account equity, cash, today's change, sizing cap
  /positions  open positions with entry, current price, P&L, stop and target
  /status     live params version, paused?, last run, next scheduled runs, live stats
  /signals    latest committee scores (top 8) from the last run
  /report     latest autopilot report
  /pause      stop opening new positions (existing stops/targets keep working)
  /resume     allow new positions again
  /run        run the paper runner now if the market is open (skips if today already ran)
  /help
"""
from __future__ import annotations
import datetime as dt
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request

import pandas as pd

import _common  # noqa: F401
from dotenv import load_dotenv

from tradingbot.config import CONFIG_DIR, RESULTS_DIR, ROOT, load_json

load_dotenv(ROOT / ".env")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
OFFSET_FILE = RESULTS_DIR / "telegram_offset.txt"
PAUSE_FLAG = RESULTS_DIR / "paper" / "PAUSED"

COMMANDS = [("balance", "Account equity, cash, today's change"), ("positions", "Open positions with P&L, stop, target"),
            ("status", "What is live, paused?, last and next runs"), ("signals", "Latest committee scores"),
            ("report", "Latest autopilot report"), ("pause", "Stop opening new positions"), ("resume", "Allow new positions"),
            ("run", "Run the paper runner now if the market is open"), ("help", "List commands")]


def api(method: str, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(f"{API}/{method}", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def reply(text: str):
    for i in range(0, max(len(text), 1), 4000):
        api("sendMessage", chat_id=CHAT, text=text[i:i + 4000], disable_web_page_preview=True)


def money(v) -> str:
    v = float(v)
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def client():
    from alpaca.trading.client import TradingClient
    from tradingbot.data import alpaca_keys
    return TradingClient(*alpaca_keys(), paper=True)


# ---------------------------------------------------------------- commands
def cmd_balance() -> str:
    tc = client()
    a = tc.get_account()
    settings = load_json(CONFIG_DIR / "settings.json")
    cap = settings.get("paper", {}).get("equity_cap", settings["account"]["starting_cash"])
    eq, last = float(a.equity), float(a.last_equity or a.equity)
    clock = tc.get_clock()
    return (f"💰 Paper account {a.account_number}\n"
            f"Equity: {money(eq)}\nCash: {money(a.cash)}\nBuying power: {money(a.buying_power)}\n"
            f"Change since previous close: {money(eq - last)} ({(eq / last - 1) * 100:+.2f}%)\n"
            f"Bot sizes positions as if the account were {money(cap)}.\n"
            f"Market: {'OPEN' if clock.is_open else 'closed'} · next {'close' if clock.is_open else 'open'} {str(clock.next_close if clock.is_open else clock.next_open)[:16]} NY")


def cmd_positions() -> str:
    tc = client()
    pos = tc.get_all_positions()
    if not pos:
        return "No open positions." + ("\n⏸ New entries are PAUSED." if PAUSE_FLAG.exists() else "")
    from tradingbot.paperstate import load_all_tracked
    tracked = load_all_tracked()
    lines, total = [], 0.0
    for p in sorted(pos, key=lambda p: p.symbol):
        upl = float(p.unrealized_pl)
        total += upl
        t = tracked.get(p.symbol, {})
        extra = f" · stop {t['stop']} / target {t['target']} · since {t['entry_date']} · {t.get('book', '')}" if t else ""
        lines.append(f"{'🟢' if upl >= 0 else '🔴'} {p.symbol} x{int(float(p.qty))} @ {float(p.avg_entry_price):.2f} → {float(p.current_price):.2f}  {money(upl)} ({float(p.unrealized_plpc) * 100:+.2f}%){extra}")
    return f"📊 {len(pos)} open position(s), unrealised {money(total)}\n" + "\n".join(lines)


def cmd_status() -> str:
    params = load_json(CONFIG_DIR / "params.json")
    sig_files = sorted(p for p in (RESULTS_DIR / "paper").glob("signals_*.csv") if len(p.stem) == len("signals_2026-01-01"))  # live runs, primary book
    last_run = sig_files[-1].stem.replace("signals_", "") if sig_files else "never"
    st_p = RESULTS_DIR / "autopilot_state.json"
    st = load_json(st_p) if st_p.exists() else {}
    live_p = RESULTS_DIR / "paper" / "live_stats.json"
    live = load_json(live_p) if live_p.exists() else None
    out = [f"🤖 Live strategy: params v{params.get('version')} · {'⏸ PAUSED' if PAUSE_FLAG.exists() else '● trading'}",
           f"Last trading run: {last_run}", f"Autopilot last run: {str(st.get('last_run', 'never'))[:16]} · last retune {st.get('last_retune', '-')}",
           "Scheduled: runner weekdays 17:35 · autopilot Saturdays 12:00 (local time)"]
    if live and live.get("trades"):
        out.append(f"Live so far: {live['trades']} trades, PF {live['profit_factor']:.2f}, realised {money(live['total_pnl'])}, max DD {money(live['max_drawdown'])}")
    try:
        sched = subprocess.run(["schtasks", "/query", "/tn", "TradingBot Paper Runner", "/fo", "csv", "/v"], capture_output=True, text=True, timeout=20)
        rows = list(pd.read_csv(pd.io.common.StringIO(sched.stdout)).to_dict(orient="records"))
        if rows:
            out.append(f"Runner task: {rows[0].get('Status')} · last result {rows[0].get('Last Result')} · next {rows[0].get('Next Run Time')}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(out)


def cmd_signals() -> str:
    files = sorted(p for p in (RESULTS_DIR / "paper").glob("signals_*.csv") if len(p.stem) == len("signals_2026-01-01"))
    if not files:
        return "No signals recorded yet."
    df = pd.read_csv(files[-1]).sort_values("total", ascending=False)
    n_enter = int(df["enter"].sum())
    lines = [f"📡 {files[-1].stem.replace('signals_', '')}: {n_enter} of {len(df)} stocks qualify"]
    for r in df.head(8).itertuples():
        lines.append(f"{'✅' if r.enter else '·'} {r.symbol:<5} score {r.total:.1f} agree {r.agree} close {r.close:.2f}")
    return "\n".join(lines)


def cmd_report() -> str:
    p = RESULTS_DIR / "reports" / "latest.md"
    return p.read_text(encoding="utf-8").replace("# ", "").replace("## ", "") if p.exists() else "No autopilot report yet (first one Saturday 12:00)."


def cmd_pause(arg: str) -> str:
    PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FLAG.write_text(f"paused via Telegram {dt.datetime.now():%Y-%m-%d %H:%M}" + (f": {arg}" if arg else ""), encoding="utf-8")
    return "⏸ Paused. No new positions will be opened. Existing stops and targets stay active. /resume to continue."


def cmd_resume() -> str:
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
        return "▶️ Resumed. The runner may open new positions on its next run."
    return "Not paused."


def cmd_run() -> str:
    subprocess.Popen([sys.executable, "scripts/run_paper.py", "--skip-if-done"], cwd=ROOT,
                     stdout=open(RESULTS_DIR / "paper" / "runner.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)
    return "▶️ Runner started. If the market is open and today has not run yet, you will get its summary shortly; otherwise it exits quietly."


def cmd_help() -> str:
    return "Commands:\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS)


def handle(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    table = {"/balance": cmd_balance, "/positions": cmd_positions, "/status": cmd_status, "/signals": cmd_signals,
             "/report": cmd_report, "/resume": cmd_resume, "/run": cmd_run, "/help": cmd_help, "/start": cmd_help}
    if cmd == "/pause":
        return cmd_pause(arg)
    fn = table.get(cmd)
    return fn() if fn else f"Unknown command {cmd}.\n\n" + cmd_help()


# ---------------------------------------------------------------- loop
def main():
    if "--log" in sys.argv:
        RESULTS_DIR.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(RESULTS_DIR / "telegram_bot.log", "a", encoding="utf-8", buffering=1)
        print(f"--- start {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    if not TOKEN or not CHAT:
        print("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env). Exiting.")
        return
    try:
        api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except Exception as e:  # noqa: BLE001
        print(f"setMyCommands failed: {e}")
    offset = int(OFFSET_FILE.read_text().strip() or 0) if OFFSET_FILE.exists() else 0
    print(f"listening for chat {CHAT} (offset {offset})", flush=True)
    while True:
        try:
            res = api("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                OFFSET_FILE.write_text(str(offset), encoding="utf-8")
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != CHAT:
                    continue  # ignore anyone else
                text = msg.get("text", "")
                if not text.startswith("/"):
                    continue
                try:
                    reply(handle(text))
                except Exception as e:  # noqa: BLE001
                    reply(f"❌ {type(e).__name__}: {str(e)[:300]}")
                    traceback.print_exc()
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            print(f"poll error: {e}", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
