"""Render results/report.html: a self-contained dark dashboard, no external libraries."""
from __future__ import annotations
import html
import json
from pathlib import Path

import pandas as pd

from .config import RESULTS_DIR
from .tradelog import load_result

CSS = """
:root{--bg:#0b1220;--panel:#111a2b;--line:#1f2b40;--text:#d6deea;--muted:#7f8ea6;--cyan:#22d3ee;--green:#34d399;--red:#f87171;--amber:#fbbf24}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:20px}
h1{font-size:15px;letter-spacing:.08em;text-transform:uppercase;margin:0 0 6px}h2{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:26px 0 10px}
.sub{color:var(--muted);margin-bottom:16px}.warn{color:var(--amber);margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px}.tile .k{color:var(--muted);font-size:10px;letter-spacing:.1em;text-transform:uppercase}.tile .v{font-size:17px;margin-top:4px}
.pos{color:var(--green)}.neg{color:var(--red)}.cyan{color:var(--cyan)}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:normal;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
td:first-child,th:first-child{text-align:left}tr.best td{background:#0f2233}
.wrap{overflow-x:auto}.chart{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px}
svg text{fill:var(--muted);font-size:11px}
"""


def _fmt_money(x: float) -> str:
    sign = "-" if x < 0 else "+"
    return f"{sign}${abs(x):,.0f}"


def _cls(x: float) -> str:
    return "pos" if x >= 0 else "neg"


def equity_svg(equity: pd.Series, width: int = 1100, height: int = 320) -> str:
    if equity is None or len(equity) < 2:
        return "<div class='sub'>No equity data.</div>"
    vals = equity.values.astype(float)
    start = vals[0]
    lo, hi = float(min(vals.min(), start)), float(max(vals.max(), start))
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    L, R, T, B = 70, 20, 16, 30
    W, H = width - L - R, height - T - B

    def x(i):
        return L + i / (len(vals) - 1) * W

    def y(v):
        return T + (hi - v) / (hi - lo) * H

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = f"{L:.1f},{y(start):.1f} " + pts + f" {x(len(vals)-1):.1f},{y(start):.1f}"
    grid = ""
    for frac in (0.0, 0.5, 1.0):
        v = lo + pad + (hi - lo - 2 * pad) * frac
        grid += f"<line x1='{L}' x2='{width-R}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='#1f2b40' stroke-dasharray='3 4'/>"
        grid += f"<text x='{L-8}' y='{y(v)+4:.1f}' text-anchor='end'>{_fmt_money(v-start)}</text>"
    dates = equity.index
    labels = ""
    for i in range(0, len(dates), max(1, len(dates) // 6)):
        labels += f"<text x='{x(i):.1f}' y='{height-8}' text-anchor='middle'>{pd.Timestamp(dates[i]).strftime('%Y-%m-%d')}</text>"
    return (
        f"<svg viewBox='0 0 {width} {height}' width='100%' preserveAspectRatio='none' style='height:{height}px'>"
        f"<defs><linearGradient id='g' x1='0' x2='0' y1='0' y2='1'><stop offset='0' stop-color='#22d3ee' stop-opacity='.35'/><stop offset='1' stop-color='#22d3ee' stop-opacity='0'/></linearGradient></defs>"
        f"{grid}<polygon points='{area}' fill='url(#g)'/><polyline points='{pts}' fill='none' stroke='#22d3ee' stroke-width='2'/>"
        f"<line x1='{L}' x2='{width-R}' y1='{y(start):.1f}' y2='{y(start):.1f}' stroke='#7f8ea6' stroke-width='.7'/>{labels}</svg>"
    )


def _row(label: str, m: dict, best: bool = False) -> str:
    pf = m["profit_factor"]
    return (
        f"<tr class='{'best' if best else ''}'><td>{html.escape(label)}</td><td>{m['trades']}</td><td>{m['win_rate']:.0f}%</td>"
        f"<td class='{_cls(m['total_pnl'])}'>{_fmt_money(m['total_pnl'])}</td><td class='{_cls(m['avg_pnl'])}'>{_fmt_money(m['avg_pnl'])}</td>"
        f"<td>{pf:.2f}</td><td class='neg'>-${m['max_drawdown']:,.0f}</td><td>{m['recovery_ratio']:.2f}</td><td>{m['sharpe']:.2f}</td></tr>"
    )


def build_report(results_dir: Path = RESULTS_DIR, out_path: Path | None = None) -> Path:
    results_dir = Path(results_dir)
    out_path = out_path or results_dir / "report.html"
    baseline = load_result(results_dir / "baseline")
    current = load_result(results_dir / "current")
    best = load_result(results_dir / "best")
    holdout = load_result(results_dir / "holdout")
    baseline_holdout = load_result(results_dir / "baseline_holdout")

    focus = best or current
    rounds = []
    rounds_dir = results_dir / "rounds"
    if rounds_dir.exists():
        for p in sorted(rounds_dir.glob("round_*.json"), key=lambda p: int(p.stem.split("_")[1])):
            rounds.append(json.loads(p.read_text(encoding="utf-8")))

    parts = [f"<style>{CSS}</style>"]
    if focus:
        m = focus["metrics"]
        w = focus["window"]
        parts.append(f"<h1>Results — committee bot {w['start']} → {w['end']} <span class='cyan'>· {html.escape(focus.get('label',''))} · params v{focus.get('params_version')}</span></h1>")
        parts.append("<div class='sub'>Train window only. The holdout window is shown separately below and must never be used for tuning.</div>")
        tiles = [
            ("Starting balance", f"${m['starting_cash']:,.0f}", ""),
            ("Ending balance", f"${m['ending_equity']:,.0f}", _cls(m["total_pnl"])),
            ("Return", f"{m['return_pct']:+.1f}%", _cls(m["return_pct"])),
            ("Profit factor", f"{m['profit_factor']:.2f}", "cyan"),
            ("Win / loss ratio", f"{m['win_loss_ratio']:.2f}×", ""),
            ("Max drawdown", f"-${m['max_drawdown']:,.0f} ({m['max_drawdown_pct']:.1f}%)", "neg"),
            ("Trades", f"{m['trades']} · {m['trades_per_day']:.1f}/day", ""),
            ("Win rate", f"{m['win_rate']:.0f}%", ""),
            ("Avg win / avg loss", f"{_fmt_money(m['avg_win'])} / {_fmt_money(m['avg_loss'])}", ""),
            ("Sharpe", f"{m['sharpe']:.2f}", ""),
            ("Longest streak", f"{m['longest_win_streak']}W · {m['longest_loss_streak']}L", ""),
            ("Best / worst", f"{(m.get('best_trade') or {}).get('symbol','-')} {_fmt_money((m.get('best_trade') or {}).get('pnl',0))} / {(m.get('worst_trade') or {}).get('symbol','-')} {_fmt_money((m.get('worst_trade') or {}).get('pnl',0))}", ""),
        ]
        parts.append("<div class='grid'>" + "".join(f"<div class='tile'><div class='k'>{k}</div><div class='v {c}'>{v}</div></div>" for k, v, c in tiles) + "</div>")
        parts.append("<div class='warn'>⚠ Costs modelled: spread, slippage, sell fees, cash cap. Still a simulation. Treat as an upper bound until paper trading agrees.</div>")
        parts.append("<h2>Equity curve — train window</h2><div class='chart'>" + equity_svg(focus["equity"]) + "</div>")

    parts.append("<h2>Comparison</h2><div class='wrap'><table><tr><th>Run</th><th>Trades</th><th>Win rate</th><th>Total P&L</th><th>Avg P&L</th><th>PF</th><th>Max DD</th><th>P&L / DD</th><th>Sharpe</th></tr>")
    if baseline:
        parts.append(_row("Baseline (dumb breakout) — train", baseline["metrics"]))
    if current:
        parts.append(_row("Current params — train", current["metrics"]))
    if best:
        parts.append(_row(f"Best so far (v{best.get('params_version')}) — train", best["metrics"], best=True))
    if baseline_holdout:
        parts.append(_row("Baseline — HOLDOUT", baseline_holdout["metrics"]))
    if holdout:
        parts.append(_row(f"Best (v{holdout.get('params_version')}) — HOLDOUT", holdout["metrics"]))
    parts.append("</table></div>")
    if holdout:
        parts.append("<h2>Equity curve — holdout window (never tuned on)</h2><div class='chart'>" + equity_svg(holdout["equity"]) + "</div>")

    if rounds:
        parts.append("<h2>Iteration loop</h2><div class='wrap'><table><tr><th>Round</th><th>Result</th><th>Version</th><th>Trades</th><th>Total P&L</th><th>PF</th><th>Max DD</th><th>P&L / DD</th><th>Hypothesis</th></tr>")
        for r in rounds:
            m = r.get("metrics") or {}
            ok = r.get("accepted")
            parts.append(
                f"<tr><td>{r['round']}</td><td class='{'pos' if ok else 'neg'}'>{'kept' if ok else 'reverted'}</td><td>v{r.get('version','')}</td>"
                f"<td>{m.get('trades','-')}</td><td class='{_cls(m.get('total_pnl',0))}'>{_fmt_money(m.get('total_pnl',0))}</td><td>{m.get('profit_factor',0):.2f}</td>"
                f"<td class='neg'>-${m.get('max_drawdown',0):,.0f}</td><td>{m.get('recovery_ratio',0):.2f}</td><td style='text-align:left'>{html.escape(str(r.get('hypothesis','')))[:160]}</td></tr>"
            )
        parts.append("</table></div>")

    if focus is not None and len(focus["trades"]):
        t = focus["trades"]
        parts.append("<h2>Worst 10 trades (train)</h2><div class='wrap'><table><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Shares</th><th>Entry px</th><th>Exit px</th><th>P&L</th><th>Reason</th><th>Score</th></tr>")
        for _, r in t.nsmallest(10, "pnl").iterrows():
            parts.append(f"<tr><td>{r['symbol']}</td><td>{r['entry_date']}</td><td>{r['exit_date']}</td><td>{r['shares']}</td><td>{r['entry_price']:.2f}</td><td>{r['exit_price']:.2f}</td><td class='neg'>{_fmt_money(r['pnl'])}</td><td>{r['exit_reason']}</td><td>{r.get('score_total', 0):.2f}</td></tr>")
        parts.append("</table></div>")

    out_path.write_text("<title>Committee Bot Results</title>" + "".join(parts), encoding="utf-8")
    return out_path
