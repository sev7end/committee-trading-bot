"""The live-only runner steps (trailing stops, reconciliation) must work with EVERY configured book, including the
rotation book that has no 'exit' section. A dry run never executes these steps, which is how a KeyError got into
production for ten days (2026-10-27..11-06)."""
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import run_paper  # noqa: E402
from tradingbot.config import load_settings  # noqa: E402
from test_runner_paths import FakeTC, FakePosition  # noqa: E402


def _bars(n=80):
    idx = pd.bdate_range("2026-07-01", periods=n)
    df = pd.DataFrame({"close": np.linspace(100, 120, n)}, index=idx)
    df["open"] = df["close"] - 0.2; df["high"] = df["close"] + 0.5; df["low"] = df["close"] - 0.5; df["volume"] = 1e6
    return df


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(run_paper, "PAPER_DIR", tmp_path)
    monkeypatch.setattr(run_paper, "PLAN_FILE", tmp_path / "pending_plan.json")
    monkeypatch.setattr(run_paper, "positions_file", lambda book: tmp_path / f"positions_{book}.json")
    monkeypatch.setattr(run_paper, "append_order", lambda row: None)
    return tmp_path


def test_trail_stops_tolerates_every_book(sandbox):
    settings = load_settings()
    books = run_paper.books_config(settings)
    assert any(b.get("engine") == "rotation" for b in books), "the rotation book must be part of this test"
    tc = FakeTC()
    bars = {"GE": _bars(), "AAPL": _bars()}
    # a trailing position in the trend book and a rotation position; neither must raise
    run_paper.save_json({"AAPL": {"entry_date": "2026-10-01", "qty": 5, "stop": 90.0, "target": None, "trail_atr": 3.0}}, sandbox / "positions_trend.json")
    run_paper.save_json({"GE": {"entry_date": "2026-10-01", "qty": 10, "stop": 80.0, "target": None}}, sandbox / "positions_rotation.json")
    tc.positions = {"AAPL": FakePosition("AAPL", 5, 100.0), "GE": FakePosition("GE", 10, 100.0)}
    notes = run_paper.trail_stops(tc, books, bars, tc.positions, dry_run=False)
    assert any(n.startswith("AAPL") for n in notes)          # the trailing one moved up
    assert not any(n.startswith("GE") for n in notes)        # the rotation one was left alone


def test_reconcile_adopts_into_the_rotation_book_with_a_disaster_stop(sandbox, monkeypatch):
    settings = load_settings()
    books = run_paper.books_config(settings)
    monkeypatch.setattr(run_paper, "load_all_tracked", lambda: {})
    tc = FakeTC()
    sym = next(s for s in settings["stocks2016_universe"] if s not in run_paper.load_universe())   # only the rotation universe holds it
    tc.positions = {sym: FakePosition(sym, 10, 50.0)}
    notes = run_paper.reconcile(tc, tc.positions, books, {sym: _bars()}, None, settings, pd.Timestamp("2026-11-06"))
    assert any("adopted into 'rotation'" in n for n in notes) and any("attached" in n for n in notes)
    saved = run_paper.load_json(sandbox / "positions_rotation.json")
    assert saved[sym]["stop"] == pytest.approx(40.0) and saved[sym]["target"] is None
