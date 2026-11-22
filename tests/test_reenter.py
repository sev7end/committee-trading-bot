"""A market-on-open order that expires unfilled must be re-entered with a limit bracket after the open."""
import datetime as dt
import pathlib
import sys
import types

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from tradingbot import execution  # noqa: E402
import run_paper  # noqa: E402
from test_runner_paths import FakeTC  # noqa: E402


def test_expired_opg_is_reentered_with_limit_bracket(monkeypatch, tmp_path):
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)
    monkeypatch.setattr(run_paper, "append_order", lambda row: None)
    tc = FakeTC()
    e = {"symbol": "XLP", "qty": 12, "stop": 79.86, "target": 83.82, "ref_price": 82.06, "book": "mr"}
    execution.submit_auction_entries(tc, [e], "mr", dt.date(2026, 9, 28))
    tc.orders[e["order_id"]].status = types.SimpleNamespace(value="expired")      # the auction never filled it
    execution.attach_brackets_after_open(tc, [e], deadline_seconds_after_open=1)
    assert e["status"] == "unfilled"
    settings = {"paper": {"entry_orders": {"type": "marketable_limit", "buffer_bps": 20, "wait_seconds": 0, "fallback_to_market": True}}}
    monkeypatch.setattr(execution, "latest_quotes", lambda syms: {"XLP": {"bid": 82.0, "ask": 82.05, "last": 82.02}})
    redone = run_paper.reenter_unfilled(tc, [{"book": "mr", "planned": [e]}], settings, pd.Timestamp("2026-09-28"))
    assert redone and redone[0].startswith("XLP")
    assert e["opg_order_id"] and e["order_id"] != e["opg_order_id"]                 # a new order went out
    assert e["status"] in ("new", "fallback", "filled")
    kinds = [(s, side, cls) for s, side, cls, *_ in tc.submitted]
    assert ("XLP", "buy", "bracket") in kinds                                      # and it carries its stop/target


def test_partial_auction_fill_is_not_reentered(monkeypatch):
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)
    tc = FakeTC()
    e = {"symbol": "XLU", "qty": 2, "stop": 38.0, "target": 40.63, "ref_price": 39.51, "book": "mr"}
    execution.submit_auction_entries(tc, [e], "mr", dt.date(2026, 9, 28))
    o = tc.orders[e["order_id"]]
    o.status, o.filled_qty, o.filled_avg_price = types.SimpleNamespace(value="expired"), 1, 39.46
    execution.attach_brackets_after_open(tc, [e], deadline_seconds_after_open=1)
    assert e["status"] == "filled" and e["qty"] == 1
    assert run_paper.reenter_unfilled(tc, [{"book": "mr", "planned": [e]}], {}, pd.Timestamp("2026-09-28")) == []
