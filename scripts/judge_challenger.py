"""Judge a proposed parameter set against the live champion with the autopilot's own holdout gate, and apply it if it wins.

    python scripts/judge_challenger.py --book trend --params results/_cand_chop_0.3.json --apply

Same rules as a retune: the challenger must beat the champion by replace_margin on a majority of holdout start dates, be
profitable (PF > 1 on both universes) on a majority, and never fall below PF 0.9 on the primary. If it wins and --apply is
given: version bump, CHANGELOG entry, champion snapshot refreshed, dashboard runs refreshed, and if the book is the primary
and trading is paused by the kill switch, trading resumes with a fresh statistics window.
"""
from __future__ import annotations
import argparse
import datetime as dt
import subprocess
import sys

import _common  # noqa: F401

from tradingbot.config import ROOT, RESULTS_DIR, book_settings, find_book, load_json, load_settings, save_json
from tradingbot import notify
import autopilot as ap_mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="trend")
    ap.add_argument("--params", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    settings = load_settings()
    book = find_book(settings, args.book)
    bs = book_settings(settings, book)
    cfg = {**ap_mod.DEFAULT_CFG, **settings.get("autopilot", {})}
    primary = book.get("universe", "stocks")
    secondary = book.get("secondary", "stocks" if primary == "etf" else "etf")
    champion = ap_mod.snapshot_params(book)
    challenger = load_json(args.params)
    today = dt.date.today()
    ch = ap_mod.holdout_scores(champion, bs, primary, secondary)
    cl = ap_mod.holdout_scores(challenger, bs, primary, secondary)
    win, reason = ap_mod.challenger_wins(ch, cl, cfg["replace_margin"])
    fmt = lambda h: " | ".join(f"{x['start']}: PF {x['stocks']['profit_factor']:.2f}/{x['etf']['profit_factor']:.2f} score {x['score']:.2f}" for x in h["starts"])
    print(f"champion v{champion.get('version')}: {fmt(ch)}\nchallenger      : {fmt(cl)}\n-> {reason}")
    if not (win and args.apply):
        return
    new_version = int(champion.get("version", 1)) + 1
    p = dict(challenger)
    p["version"] = new_version
    save_json(p, ROOT / book["params"])
    ap_mod.snapshot_params(book)
    with open(ROOT / "CHANGELOG.md", "a", encoding="utf-8") as f:
        f.write(f"\n## [{args.book}] v{new_version} — proposed challenger judged by the holdout gate ({today})\n{reason}\n"
                f"Holdout {bs['windows']['holdout']['start']} -> {bs['windows']['holdout']['end']}: primary PF {cl['stocks']['profit_factor']:.2f} "
                f"(champion {ch['stocks']['profit_factor']:.2f}), bias set PF {cl['etf']['profit_factor']:.2f} (champion {ch['etf']['profit_factor']:.2f}). Source: {args.params}\n")
    runs = [["--book", args.book, "--out", "best" if args.book == settings["books"][0]["name"] else f"best_{args.book}", "--label", f"{args.book} live params, train"],
            ["--book", args.book, "--window", "holdout", "--out", "holdout" if args.book == settings["books"][0]["name"] else f"holdout_{args.book}", "--label", f"{args.book} HOLDOUT"]]
    for a in runs:
        subprocess.run([sys.executable, "scripts/run_backtest.py", *a], cwd=ROOT, capture_output=True)
    state = ap_mod.load_state()
    state.setdefault("history", []).append({"date": today.isoformat(), "book": args.book, "champion_version": champion.get("version"), "replaced": True,
                                            "challenger_source": "manual", "reason": reason, "new_version": new_version})
    msg = f"🔁 [{args.book}] replaced by v{new_version} after passing the holdout gate: {reason}"
    if args.book == settings["books"][0]["name"] and state.get("paused"):
        state.update(paused=False, pause_reason="", stats_since=today.isoformat())
        ap_mod.PAUSE_FLAG.unlink(missing_ok=True)
        msg += "\n▶️ Trading resumes with the new primary strategy; the kill switch judges trades from today on."
    save_json(state, ap_mod.STATE_PATH)
    notify.send(msg)
    print(msg.encode("ascii", "replace").decode())   # the Windows console may not render emoji


if __name__ == "__main__":
    main()
