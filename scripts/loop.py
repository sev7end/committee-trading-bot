"""The iteration loop: backtest -> Claude proposes ONE change -> retest -> keep or revert.

    python scripts/loop.py --rounds 5        # run five rounds
    python scripts/loop.py                    # run forever (Ctrl+C to stop)
    python scripts/loop.py --dry-run          # print the prompt Claude would get, change nothing

Each round:
  1. Snapshot config/params.json and tradingbot/voters/.
  2. Send Claude Code (headless) the current params, the best-so-far numbers, the
     baseline numbers, the worst trades, and the history of past rounds. It may edit
     params.json or add/edit one voter file. It writes a one-line hypothesis.
  3. Run the test suite (catches look-ahead bugs in any new voter) and the backtest
     on the TRAIN window only.
  4. Accept if: trades >= min_trades, profit factor > 1, beats the baseline P&L, and
     recovery ratio (P&L / max drawdown) improves by >= min_improvement_pct.
     Otherwise revert to the snapshot.
  5. Log the round to results/rounds/, append accepted changes to CHANGELOG.md,
     rebuild results/report.html.

The loop never reads or runs the holdout window. That is your final exam; see
scripts/final_exam.py.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import time

import _common  # noqa: F401

from tradingbot.config import CONFIG_DIR, RESULTS_DIR, ROOT, VOTERS_DIR, load_json, load_settings, save_json
from tradingbot.report import build_report
from tradingbot.tradelog import format_metrics_table, load_result, worst_trades
from tradingbot.voters import discover

PARAMS = CONFIG_DIR / "params.json"
BEST_DIR = RESULTS_DIR / "best"
CURRENT_DIR = RESULTS_DIR / "current"
BASELINE_DIR = RESULTS_DIR / "baseline"
ROUNDS_DIR = RESULTS_DIR / "rounds"
BACKUP_DIR = RESULTS_DIR / "_backup"
CHANGELOG = ROOT / "CHANGELOG.md"


# ---------- helpers ----------------------------------------------------------

def sh(args: list[str], timeout: int = 900, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, input=input_text)


def backtest(out: str, params_path=PARAMS, label: str = "") -> tuple[dict | None, str]:
    r = sh([sys.executable, "scripts/run_backtest.py", "--params", str(params_path), "--out", out, "--label", label])
    if r.returncode != 0:
        return None, (r.stdout or "") + (r.stderr or "")
    return load_result(RESULTS_DIR / out), r.stdout


def run_tests() -> tuple[bool, str]:
    r = sh([sys.executable, "-m", "pytest", "tests", "-q", "-x"])
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def snapshot() -> None:
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)
    BACKUP_DIR.mkdir(parents=True)
    shutil.copy2(PARAMS, BACKUP_DIR / "params.json")
    shutil.copytree(VOTERS_DIR, BACKUP_DIR / "voters", ignore=shutil.ignore_patterns("__pycache__"))


def restore() -> None:
    shutil.copy2(BACKUP_DIR / "params.json", PARAMS)
    keep = {p.name for p in (BACKUP_DIR / "voters").glob("*.py")}
    for p in VOTERS_DIR.glob("*.py"):
        if p.name not in keep:
            p.unlink()
    for p in (BACKUP_DIR / "voters").glob("*.py"):
        shutil.copy2(p, VOTERS_DIR / p.name)


def set_status(**kw) -> None:
    """Write results/loop_status.json so the live dashboard can show what the loop is doing."""
    p = RESULTS_DIR / "loop_status.json"
    cur = load_json(p) if p.exists() else {}
    cur.update(kw)
    cur["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    save_json(cur, p)


def objective(m: dict) -> float:
    return float(m.get("recovery_ratio", 0.0))


def load_rounds() -> list[dict]:
    if not ROUNDS_DIR.exists():
        return []
    out = []
    for p in sorted(ROUNDS_DIR.glob("round_*.json"), key=lambda p: int(p.stem.split("_")[1])):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def promote_current_to_best(version: int) -> None:
    if BEST_DIR.exists():
        shutil.rmtree(BEST_DIR)
    shutil.copytree(CURRENT_DIR, BEST_DIR)
    m = load_json(BEST_DIR / "metrics.json")
    m["params_version"] = version
    m["label"] = f"best so far (v{version})"
    save_json(m, BEST_DIR / "metrics.json")
    p = load_json(BEST_DIR / "params.json")
    p["version"] = version
    save_json(p, BEST_DIR / "params.json")


# ---------- the prompt --------------------------------------------------------

def build_prompt(round_no: int, best: dict, baseline: dict, rounds: list[dict], settings: dict) -> str:
    params = load_json(PARAMS)
    reg = discover()
    voter_lines = "\n".join(f"  - {name}: {cls.description}" for name, cls in sorted(reg.items()))
    hist_lines = []
    for r in rounds[-12:]:
        m = r.get("metrics") or {}
        hist_lines.append(
            f"  round {r['round']}: {'KEPT' if r.get('accepted') else 'reverted'} | "
            f"PF {m.get('profit_factor', 0):.2f} | P&L ${m.get('total_pnl', 0):,.0f} | DD ${m.get('max_drawdown', 0):,.0f} | "
            f"P&L/DD {m.get('recovery_ratio', 0):.2f} | {r.get('hypothesis', '')}"
        )
    hist = "\n".join(hist_lines) if hist_lines else "  (none yet)"
    streak = 0
    for r in reversed(rounds):
        if r.get("accepted"):
            break
        streak += 1
    if streak >= 3:
        hist += (f"\n  NOTE: the last {streak} rounds were all reverted. Small tweaks are not working. Make a bolder,"
                 " structurally different change this round (a new voter that captures something the current ones miss,"
                 " or a different exit design), still as ONE coherent change.")
    worst = worst_trades(best["trades"], 10)
    cols = [c for c in worst.columns if c in ("symbol", "entry_date", "exit_date", "pnl", "exit_reason", "bars_held") or c.startswith("score_")]
    worst_txt = worst[cols].to_string(index=False) if len(worst) else "(no trades)"
    loop_cfg = settings["loop"]
    win = settings["windows"]["train"]
    hyp_path = f"results/rounds/round_{round_no}.hypothesis.txt"
    eq = best.get("equity")
    halves_txt = ""
    if eq is not None and len(eq) > 10:
        mid = eq.index[len(eq) // 2]
        halves_txt = (f"\nP&L by half of the window: first half (to {mid.date()}) ${float(eq.loc[mid] - eq.iloc[0]):,.0f}, "
                      f"second half ${float(eq.iloc[-1] - eq.loc[mid]):,.0f}. A robust strategy is positive in both.")

    return f"""You are improving a rule-based US-stock trading strategy. Round {round_no}.

THE STRATEGY
A committee of voters scores each stock each day (+1 bullish .. -1 bearish). The bot buys at the
next open when the weighted total >= entry.min_score AND at least entry.min_voters_agree voters are
positive. Exits are mechanical: stop at exit.stop_atr ATRs below entry, target at exit.target_atr ATRs
above, or after exit.max_hold_bars days. Position size risks risk.risk_per_trade_pct of equity.
Backtest window (TRAIN): {win['start']} -> {win['end']}. Daily bars, 30 large US stocks, realistic costs.

AVAILABLE VOTERS (tradingbot/voters/*.py)
{voter_lines}

CURRENT PARAMS (config/params.json)
{json.dumps(params, indent=2)}

BEST SO FAR (params v{best.get('params_version')}) on the train window
{format_metrics_table(best['metrics'])}{halves_txt}

DUMB BASELINE (single 20-day breakout rule, must be beaten)
{format_metrics_table(baseline['metrics'])}

WORST 10 TRADES OF THE BEST RUN (score_* = each voter's vote at signal time)
{worst_txt}

HISTORY OF PREVIOUS ROUNDS (do not repeat a reverted idea)
{hist}

ACCEPTANCE RULE (applied automatically after you finish)
  trades >= {loop_cfg['min_trades']}, profit factor > 1, total P&L above the baseline, AND
  recovery ratio (total P&L / max drawdown) improves by at least {loop_cfg['min_improvement_pct']}% over best-so-far,
  AND the equity curve is up in both the first and the second half of the window (robustness check).
  Otherwise your change is reverted.

YOUR TASK: make exactly ONE change, then stop.
  Allowed changes (pick one):
    a) change one or two related numbers in config/params.json, or
    b) enable/disable a voter or change its weight in config/params.json, or
    c) add ONE new voter: create tradingbot/voters/<name>.py following tradingbot/voters/base.py
       (vectorised, backward-looking only, VOTER = YourClass, name matches the key) and add its
       entry under "voters" in config/params.json.
  Rules:
    - Do NOT edit tradingbot/backtest.py, committee.py, costs.py, metrics.py, data.py, or the tests.
    - Do NOT read anything under results/holdout or results/baseline_holdout. Do not change config/settings.json.
    - Prefer simple, universal rules over symbol-specific tweaks. More rules = more overfitting.
      Look at WHY the worst trades lost (which voter was wrong) rather than chasing the P&L number.
    - Do not change the "version" field; the loop manages it.
  Finally, write ONE line (max 200 chars) stating your hypothesis to the file {hyp_path}
  in the form: "<what you changed> because <why it should help>". Then finish.
"""


def find_claude(cfg: dict) -> str:
    """Resolve the claude CLI. PATH first, then the standard install spots."""
    import shutil as _sh
    from pathlib import Path

    want = cfg.get("claude_cmd", "claude")
    if Path(want).is_absolute() and Path(want).exists():
        return want
    # Prefer the native installer's binary: npm/nvm shims on Windows are often broken.
    home = Path.home()
    for cand in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"):
        if cand.exists():
            return str(cand)
    found = _sh.which(want)
    if found:
        return found
    for cand in (home / "AppData" / "Roaming" / "npm" / "claude.cmd", Path(want)):
        if cand.exists():
            return str(cand)
    return want


def call_claude(prompt: str, settings: dict) -> tuple[str, int]:
    cfg = settings["loop"]
    cmd = [
        find_claude(cfg), "-p",
        "--output-format", "text",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Write,Glob,Grep",
        "--disallowedTools", "Bash",
        "--max-turns", str(cfg.get("max_turns", 40)),
    ]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    try:
        r = sh(cmd, timeout=1800, input_text=prompt)
    except FileNotFoundError:
        out, rc = "claude CLI not found. Set loop.claude_cmd in config/settings.json to its full path.", 127
    except subprocess.TimeoutExpired:
        out, rc = "claude timed out", 124
    else:
        out, rc = (r.stdout or "") + (r.stderr or ""), r.returncode
    if rc != 0:
        from tradingbot import notify
        low = out.lower()
        if any(k in low for k in ("authenticate", "not logged in", "401", "/login", "oauth")):
            notify.send("🔑 Claude login required. The research loop cannot run until you sign in.\n"
                        "In PowerShell run:\n& \"$env:USERPROFILE\\.local\\bin\\claude.exe\" login")
        else:
            notify.send(f"⚠️ Claude CLI failed (exit {rc}): {out.strip()[:300]}")
    return out, rc


def claude_ready() -> tuple[bool, str]:
    """Cheap preflight: can the CLI answer at all? Used before spending real rounds."""
    cfg = load_settings()["loop"]
    try:
        r = sh([find_claude(cfg), "-p", "--output-format", "text", "--max-turns", "1"], timeout=180, input_text="Reply with OK")
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out.strip()[:300]


# ---------- main --------------------------------------------------------------

def ensure_reference_runs() -> tuple[dict, dict]:
    win = load_settings()["windows"]["train"]

    def stale(res) -> bool:  # missing, or computed on a previous train window (the autopilot rolls windows)
        return res is None or (res.get("window") or {}) != win

    if stale(load_result(BASELINE_DIR)):
        print("[loop] running baseline ...")
        res, log = backtest("baseline", CONFIG_DIR / "baseline_params.json", "dumb breakout baseline")
        if res is None:
            sys.exit(f"baseline failed:\n{log}")
    if stale(load_result(BEST_DIR)):
        print("[loop] running current params to seed best-so-far ...")
        res, log = backtest("current", PARAMS, "initial")
        if res is None:
            sys.exit(f"initial backtest failed:\n{log}")
        promote_current_to_best(load_json(PARAMS).get("version", 1))
    return load_result(BEST_DIR), load_result(BASELINE_DIR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=0, help="number of rounds (0 = forever)")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and exit")
    ap.add_argument("--sleep", type=int, default=3, help="seconds between rounds")
    args = ap.parse_args()

    settings = load_settings()
    ROUNDS_DIR.mkdir(parents=True, exist_ok=True)

    # Only one loop at a time. Two loops would edit params.json and restore each
    # other's snapshots, corrupting every round.
    import os
    lock = RESULTS_DIR / "loop.lock"
    if lock.exists() and not args.dry_run:
        pid = lock.read_text(encoding="utf-8").strip()
        sys.exit(f"Another loop appears to be running (pid {pid}). Stop it first, or delete {lock} if it crashed.")
    if not args.dry_run:
        lock.write_text(str(os.getpid()), encoding="utf-8")
        import atexit
        atexit.register(lambda: lock.unlink(missing_ok=True))
        atexit.register(lambda: set_status(phase="stopped"))

    best, baseline = ensure_reference_runs()
    rounds = load_rounds()
    round_no = (rounds[-1]["round"] + 1) if rounds else 1

    if args.dry_run:
        print(build_prompt(round_no, best, baseline, rounds, settings))
        return

    done = 0
    while args.rounds == 0 or done < args.rounds:
        t0 = time.time()
        print(f"\n===== ROUND {round_no}  (best so far: P&L/DD {objective(best['metrics']):.2f}, PF {best['metrics']['profit_factor']:.2f}, v{best.get('params_version')}) =====")
        snapshot()
        set_status(round=round_no, phase="claude proposing a change", phase_started=dt.datetime.now().isoformat(timespec="seconds"), started=dt.datetime.now().isoformat(timespec="seconds"))
        hyp_path = ROUNDS_DIR / f"round_{round_no}.hypothesis.txt"
        if hyp_path.exists():
            hyp_path.unlink()

        prompt = build_prompt(round_no, best, baseline, rounds, settings)
        out, rc = call_claude(prompt, settings)
        (ROUNDS_DIR / f"round_{round_no}.claude.txt").write_text(out, encoding="utf-8")
        hypothesis = hyp_path.read_text(encoding="utf-8").strip().splitlines()[0][:200] if hyp_path.exists() and hyp_path.read_text(encoding="utf-8").strip() else "(no hypothesis written)"
        print(f"[claude rc={rc}] {hypothesis}")

        record = {"round": round_no, "time": dt.datetime.now().isoformat(timespec="seconds"), "hypothesis": hypothesis,
                  "claude_rc": rc, "accepted": False, "reason": "", "metrics": None, "version": best.get("params_version")}

        if rc != 0:
            record["reason"] = "claude call failed"
        else:
            set_status(phase="running tests", phase_started=dt.datetime.now().isoformat(timespec="seconds"))
            ok, test_log = run_tests()
            if not ok:
                record["reason"] = "tests failed (look-ahead or broken voter)"
                (ROUNDS_DIR / f"round_{round_no}.tests.txt").write_text(test_log, encoding="utf-8")
            else:
                set_status(phase="backtesting candidate", phase_started=dt.datetime.now().isoformat(timespec="seconds"))
                res, log = backtest("current", PARAMS, f"round {round_no}")
                if res is None:
                    record["reason"] = "backtest crashed"
                    (ROUNDS_DIR / f"round_{round_no}.error.txt").write_text(log, encoding="utf-8")
                else:
                    m, bm = res["metrics"], best["metrics"]
                    record["metrics"] = m
                    cfg = settings["loop"]
                    needed = objective(bm) * (1 + cfg["min_improvement_pct"] / 100)
                    checks = {
                        "enough trades": m["trades"] >= cfg["min_trades"],
                        "profit factor > 1": m["profit_factor"] > 1.0,
                        "beats baseline": m["total_pnl"] > baseline["metrics"]["total_pnl"],
                        f"P&L/DD {m['recovery_ratio']:.2f} >= {needed:.2f}": objective(m) >= needed,
                    }
                    # Robustness: the strategy must make money in BOTH halves of the
                    # train window, not just ride one lucky stretch.
                    eq = res["equity"]
                    if len(eq) > 10:
                        mid = eq.index[len(eq) // 2]
                        first_half = float(eq.loc[mid] - eq.iloc[0])
                        second_half = float(eq.iloc[-1] - eq.loc[mid])
                        checks[f"profitable in both halves ({first_half:,.0f} / {second_half:,.0f})"] = first_half > 0 and second_half > 0
                    failed = [k for k, v in checks.items() if not v]
                    record["accepted"] = not failed
                    record["reason"] = "accepted" if not failed else "failed: " + "; ".join(failed)
                    print(format_metrics_table(m))

        if record["accepted"]:
            new_version = int(best.get("params_version") or 1) + 1
            p = load_json(PARAMS)
            p["version"] = new_version
            save_json(p, PARAMS)
            promote_current_to_best(new_version)
            record["version"] = new_version
            old, new = best["metrics"], record["metrics"]
            with open(CHANGELOG, "a", encoding="utf-8") as f:
                f.write(
                    f"\n## v{new_version} — round {round_no} ({record['time'][:10]})\n{hypothesis}\n\n"
                    f"| | before | after |\n|---|---|---|\n"
                    f"| trades | {old['trades']} | {new['trades']} |\n"
                    f"| total P&L | ${old['total_pnl']:,.0f} | ${new['total_pnl']:,.0f} |\n"
                    f"| profit factor | {old['profit_factor']:.2f} | {new['profit_factor']:.2f} |\n"
                    f"| max drawdown | ${old['max_drawdown']:,.0f} | ${new['max_drawdown']:,.0f} |\n"
                    f"| P&L / DD | {old['recovery_ratio']:.2f} | {new['recovery_ratio']:.2f} |\n"
                )
            best = load_result(BEST_DIR)
            print(f"[round {round_no}] KEPT -> v{new_version}")
        else:
            restore()
            # re-run so results/current reflects the restored state
            backtest("current", PARAMS, f"current (v{best.get('params_version')})")
            print(f"[round {round_no}] REVERTED: {record['reason']}")

        set_status(phase="idle between rounds", phase_started=dt.datetime.now().isoformat(timespec="seconds"))
        save_json(record, ROUNDS_DIR / f"round_{round_no}.json")
        rounds.append(record)
        build_report()
        print(f"[round {round_no}] {time.time()-t0:.0f}s, report -> results/report.html")
        round_no += 1
        done += 1
        if args.rounds == 0 or done < args.rounds:
            time.sleep(args.sleep)


if __name__ == "__main__":
    main()
