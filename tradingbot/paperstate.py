"""Shared read helpers for the paper-trading state files (used by the runner, dashboard, Telegram bot)."""
from __future__ import annotations

from .config import RESULTS_DIR, load_json

PAPER_DIR = RESULTS_DIR / "paper"


def load_all_tracked() -> dict[str, dict]:
    """{symbol: {entry_date, qty, stop, target, order_id, book}} across every book's positions file."""
    out: dict[str, dict] = {}
    for p in sorted(PAPER_DIR.glob("positions_*.json")):
        book = p.stem.replace("positions_", "")
        try:
            for sym, v in load_json(p).items():
                out[sym] = {**v, "book": book}
        except Exception:  # noqa: BLE001
            continue
    legacy = PAPER_DIR / "positions.json"
    if legacy.exists() and not out:
        try:
            out = {s: {**v, "book": "trend"} for s, v in load_json(legacy).items()}
        except Exception:  # noqa: BLE001
            pass
    return out
