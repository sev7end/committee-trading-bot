"""The live runner's safety paths, exercised against a fake Alpaca client (no network, no real orders).

Covers the defects found in the 2026-09-22 review: a fill must always end up tracked AND protected by a
stop/target, whether the bracket attached, the poll timed out, or the previous run died before bookkeeping.
"""
import pathlib
import sys
import types
import datetime as dt

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from tradingbot import execution  # noqa: E402
import run_paper  # noqa: E402


class FakeOrder:
    def __init__(self, oid, symbol, side="buy", status="new", filled_qty=0, filled_avg_price=None, order_class=None, **kw):
        self.id = oid
        self.symbol = symbol
        self.side = types.SimpleNamespace(value=side)
        self.status = types.SimpleNamespace(value=status)
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.filled_at = dt.datetime(2026, 9, 22, 13, 30) if filled_qty else None
        self.order_class = order_class
        self.type = types.SimpleNamespace(value=kw.get("type", "market"))
        self.legs = []


class FakePosition:
    def __init__(self, symbol, qty, avg):
        self.symbol, self.qty, self.avg_entry_price, self.market_value = symbol, str(qty), str(avg), str(qty * avg)


class FakeTC:
    """Just enough of alpaca-py's TradingClient for the runner's order paths."""

    def __init__(self, fail_submit_for=()):
        self.orders = {}
        self.positions = {}
        self.n = 0
        self.fail_submit_for = set(fail_submit_for)
        self.is_open = True
        self.submitted = []

    def _new(self, **kw):
        self.n += 1
        o = FakeOrder(f"o{self.n}", **kw)
        self.orders[o.id] = o
        return o

    def get_clock(self):
        now = dt.datetime(2026, 9, 22, 9, 31)
        return types.SimpleNamespace(is_open=self.is_open, timestamp=now, next_open=now + dt.timedelta(days=1))

    def submit_order(self, req):
        sym = req.symbol
        if sym in self.fail_submit_for:
            raise RuntimeError(f"rejected {sym}")
        side = req.side.value if hasattr(req.side, "value") else str(req.side)
        cls = getattr(req, "order_class", None)
        self.submitted.append((sym, side, cls.value if cls is not None and hasattr(cls, "value") else cls, getattr(req, "limit_price", None),
                               getattr(getattr(req, "stop_loss", None), "stop_price", None)))
        return self._new(symbol=sym, side=side, status="new", order_class=cls)

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def cancel_order_by_id(self, oid):
        o = self.orders[oid]
        if o.status.value in ("new", "accepted", "partially_filled"):
            o.status = types.SimpleNamespace(value="canceled")

    def get_orders(self, req):
        st = req.status.value if hasattr(req.status, "value") else str(req.status)
        out = [o for o in self.orders.values() if (o.status.value in ("new", "accepted", "partially_filled")) == (st == "open")]
        if getattr(req, "symbols", None):
            out = [o for o in out if o.symbol in req.symbols]
        return out

    def get_all_positions(self):
        return list(self.positions.values())

    def close_position(self, sym):
        return self._new(symbol=sym, side="sell", status="new")


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)
    monkeypatch.setattr(run_paper._time, "sleep", lambda s: None)
    # never touch the real results/paper state from a test
    monkeypatch.setattr(run_paper, "PAPER_DIR", tmp_path)
    monkeypatch.setattr(run_paper, "ORDERS_FILE", tmp_path / "orders.csv")
    monkeypatch.setattr(run_paper, "PLAN_FILE", tmp_path / "pending_plan.json")
    monkeypatch.setattr(run_paper, "positions_file", lambda book: tmp_path / f"positions_{book}.json")
    monkeypatch.setattr(run_paper, "append_order", lambda row: None)


def test_opg_fill_gets_bracket_rebuilt_from_fill_price():
    tc = FakeTC()
    e = {"symbol": "AAA", "qty": 10, "stop": 97.0, "target": 102.0, "ref_price": 100.0, "atr": 1.0, "stop_atr": 3.0, "target_atr": 2.0}
    execution.submit_auction_entries(tc, [e], "trend", dt.date(2026, 9, 22))
    o = tc.orders[e["order_id"]]
    o.status, o.filled_qty, o.filled_avg_price = types.SimpleNamespace(value="filled"), 10, 101.0   # gapped up at the open
    execution.attach_brackets_after_open(tc, [e], deadline_seconds_after_open=1)
    assert e["status"] == "filled" and e["oco_id"]
    assert (e["stop"], e["target"]) == (98.0, 103.0)                     # from the FILL, not yesterday's close
    sym, side, cls, limit, stop = tc.submitted[-1]
    assert (sym, side, cls, limit, stop) == ("AAA", "sell", "oco", 103.0, 98.0)


def test_opg_timeout_with_fill_is_not_marked_unfilled(monkeypatch):
    tc = FakeTC()
    e = {"symbol": "AAA", "qty": 10, "stop": 97.0, "target": 102.0, "ref_price": 100.0}
    execution.submit_auction_entries(tc, [e], "trend", dt.date(2026, 9, 22))
    o = tc.orders[e["order_id"]]
    # the poll never sees it (deadline already passed), but the order DID fill in the auction
    o.status, o.filled_qty, o.filled_avg_price = types.SimpleNamespace(value="filled"), 10, 100.2
    monkeypatch.setattr(execution.time, "time", lambda: 10 ** 12)
    execution.attach_brackets_after_open(tc, [e], deadline_seconds_after_open=-1)
    assert e["status"] == "filled" and e.get("oco_id")


def test_partial_fill_on_cancelled_opg_is_a_position():
    tc = FakeTC()
    e = {"symbol": "AAA", "qty": 10, "stop": 97.0, "target": 102.0, "ref_price": 100.0}
    execution.submit_auction_entries(tc, [e], "trend", dt.date(2026, 9, 22))
    o = tc.orders[e["order_id"]]
    o.status, o.filled_qty, o.filled_avg_price = types.SimpleNamespace(value="canceled"), 4, 100.0
    execution.attach_brackets_after_open(tc, [e], deadline_seconds_after_open=1)
    assert e["status"] == "filled" and e["qty"] == 4 and e.get("oco_id")


def test_bracket_failure_is_tracked_and_reported():
    tc = FakeTC()
    e = {"symbol": "AAA", "qty": 10, "stop": 97.0, "target": 102.0, "ref_price": 100.0}
    execution.submit_auction_entries(tc, [e], "trend", dt.date(2026, 9, 22))
    tc.fail_submit_for.add("AAA")                                        # the OCO gets rejected
    o = tc.orders[e["order_id"]]
    o.status, o.filled_qty, o.filled_avg_price = types.SimpleNamespace(value="filled"), 10, 100.0
    execution.attach_brackets_after_open(tc, [e], deadline_seconds_after_open=1)
    assert e["status"] == "filled_no_bracket"
    summary = {"book": "trend", "tracked": {}, "placed": [], "planned": [e]}
    run_paper.append_order = lambda row: None
    run_paper.record_entries(summary, types.SimpleNamespace(dry_run=False), pd.Timestamp("2026-09-22"))
    assert "AAA" in summary["tracked"] and summary["unprotected"] == ["AAA"]


def test_reconcile_adopts_and_protects_untracked_position(tmp_path, monkeypatch):
    monkeypatch.setattr(run_paper, "PAPER_DIR", tmp_path)
    monkeypatch.setattr(run_paper, "PLAN_FILE", tmp_path / "pending_plan.json")
    monkeypatch.setattr(run_paper, "positions_file", lambda book: tmp_path / f"positions_{book}.json")
    monkeypatch.setattr(run_paper, "load_all_tracked", lambda: {})
    tc = FakeTC()
    tc.positions["XLF"] = FakePosition("XLF", 17, 56.0)                  # held on Alpaca, no stop order, not in any book
    idx = pd.bdate_range("2026-06-01", periods=60)
    bars = {"XLF": pd.DataFrame({"open": 55.0, "high": 56.0, "low": 54.0, "close": 55.0, "volume": 1e6}, index=idx)}
    books = [{"name": "trend", "params": "config/params.json", "universe": "stocks"}, {"name": "mr", "params": "config/params_mr.json", "universe": "etf"}]
    settings = run_paper.load_settings()
    notes = run_paper.reconcile(tc, tc.positions, books, bars, None, settings, pd.Timestamp("2026-09-22"))
    assert any("adopted into 'mr'" in n for n in notes) and any("attached" in n for n in notes)
    saved = run_paper.load_json(tmp_path / "positions_mr.json")
    assert saved["XLF"]["qty"] == 17 and saved["XLF"]["stop"] < 56.0 < saved["XLF"]["target"] and saved["XLF"]["oco_id"]
    assert tc.submitted[-1][:3] == ("XLF", "sell", "oco")


def test_failed_time_exit_keeps_position_and_reattaches_stop():
    tc = FakeTC(fail_submit_for={"T"})                                    # the OPG sell is rejected
    tc.positions["T"] = FakePosition("T", 157, 26.76)
    s = {"book": "trend", "tracked": {"T": {"entry_date": "2026-09-15", "qty": 157, "stop": 24.93, "target": 27.57}}, "closed": [],
         "exits": [{"symbol": "T", "qty": 157.0, "info": {"entry_date": "2026-09-15", "qty": 157, "stop": 24.93, "target": 27.57}}]}
    run_paper.append_order = lambda row: None
    run_paper.do_exits(tc, [s], "auction", pd.Timestamp("2026-09-22"))
    assert s["exit_failed"] == ["T"] and "T" in s["tracked"] and s["closed"] == []
    tc.fail_submit_for.clear()
    s2 = {"book": "trend", "tracked": {"T": {"entry_date": "2026-09-15", "qty": 157, "stop": 24.93, "target": 27.57}}, "closed": [],
          "exits": [{"symbol": "T", "qty": 157.0, "info": {"entry_date": "2026-09-15", "qty": 157, "stop": 24.93, "target": 27.57}}]}
    run_paper.do_exits(tc, [s2], "auction", pd.Timestamp("2026-09-22"))
    assert s2["closed"] == ["T"] and "T" not in s2["tracked"]
