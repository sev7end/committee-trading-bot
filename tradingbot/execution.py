"""Order execution for the paper runner.

Entry orders are "marketable limits": a buy limit a small buffer ABOVE the current ask. In normal
conditions it fills immediately like a market order, but it caps the worst price you can pay when the
spread is momentarily wide or a bad print flashes at the open. If it has not filled within
`wait_seconds` it is cancelled and (optionally) replaced by a market order, so no signal is missed.

Every fill is recorded with the quote at submission so execution quality can be measured later:
  slippage_bps = (fill - ask_at_submit) / ask_at_submit * 10_000   (negative = better than the ask)

settings.paper.entry_orders:
  {"type": "marketable_limit" | "market", "buffer_bps": 20, "wait_seconds": 120, "fallback_to_market": true}
"""
from __future__ import annotations
import datetime as dt
import time

DEFAULT = {"type": "marketable_limit", "buffer_bps": 20, "wait_seconds": 120, "fallback_to_market": True}


MAX_SPREAD_PCT = 0.5   # a quote wider than this is stale or pre-market: do not trust it


def choose_base(bid, ask, last, ref_price: float) -> tuple[float, str]:
    """Price to build the limit on, and which source was used.
    1. a sane quote (both sides present, spread <= MAX_SPREAD_PCT of mid) -> the ask
    2. otherwise the latest trade price, if any
    3. otherwise the reference price (previous close) plus 0.1%"""
    if bid and ask and bid > 0 and ask >= bid:
        mid = (bid + ask) / 2
        if (ask - bid) / mid * 100 <= MAX_SPREAD_PCT:
            return float(ask), "ask"
    if last and last > 0:
        return float(last), "last_trade"
    return float(ref_price) * 1.001, "ref_close"


def limit_price_for(ask, ref_price: float, buffer_bps: float, bid=None, last=None) -> float:
    base, _ = choose_base(bid, ask, last, ref_price)
    return round(base * (1 + buffer_bps / 10_000), 2)


def latest_quotes(symbols: list[str]) -> dict[str, dict]:
    """{symbol: {'bid','ask','last'}} from Alpaca's free IEX feed; empty dict on any failure."""
    out: dict[str, dict] = {}
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
        from alpaca.data.enums import DataFeed
        from .data import alpaca_keys

        client = StockHistoricalDataClient(*alpaca_keys())
        q = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX))
        for sym, v in q.items():
            out[sym] = {"bid": float(v.bid_price or 0) or None, "ask": float(v.ask_price or 0) or None, "last": None}
        try:
            t = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX))
            for sym, v in t.items():
                out.setdefault(sym, {"bid": None, "ask": None})["last"] = float(v.price or 0) or None
        except Exception as e:  # noqa: BLE001
            print(f"[execution] latest trades unavailable ({type(e).__name__})")
    except Exception as e:  # noqa: BLE001
        print(f"[execution] quotes unavailable ({type(e).__name__}); using reference prices")
    return out


def _bracket(symbol, qty, stop, target, client_id, limit=None):
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    if not target:   # trailing exits have no target; a bracket still needs one -> park it far away (the trailing step replaces the exit later)
        target = round(float(stop) * 3, 2)
    common = dict(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC, order_class=OrderClass.BRACKET,
                  client_order_id=client_id, take_profit=TakeProfitRequest(limit_price=round(float(target), 2)),
                  stop_loss=StopLossRequest(stop_price=round(float(stop), 2)))
    return LimitOrderRequest(limit_price=limit, **common) if limit is not None else MarketOrderRequest(**common)


def submit_entries(tc, entries: list[dict], cfg: dict | None, book: str, today) -> list[dict]:
    """entries: [{symbol, qty, stop, target, ref_price}] -> same dicts + order_id, order_type, limit_price, bid, ask, fill_price, status."""
    c = {**DEFAULT, **(cfg or {})}
    if not entries:
        return []
    quotes = latest_quotes([e["symbol"] for e in entries]) if c["type"] == "marketable_limit" else {}
    live = []
    for e in entries:
        q = quotes.get(e["symbol"], {})
        e.update(bid=q.get("bid"), ask=q.get("ask"), last=q.get("last"), limit_price=None, fill_price=None, status="new")
        cid = f"{book}-{e['symbol']}-{today}"
        try:
            if c["type"] == "marketable_limit":
                base, src = choose_base(q.get("bid"), q.get("ask"), q.get("last"), e["ref_price"])
                e["limit_price"] = round(base * (1 + float(c["buffer_bps"]) / 10_000), 2)
                e["limit_base"] = src
                e["order_type"] = "limit"
                o = tc.submit_order(_bracket(e["symbol"], e["qty"], e["stop"], e["target"], cid, e["limit_price"]))
            else:
                e["order_type"] = "market"
                o = tc.submit_order(_bracket(e["symbol"], e["qty"], e["stop"], e["target"], cid))
            e["order_id"] = str(o.id)
            live.append(e)
        except Exception as ex:  # noqa: BLE001
            e.update(order_id=None, status=f"rejected: {type(ex).__name__}: {str(ex)[:120]}")
            print(f"[execution] {e['symbol']} order rejected: {str(ex)[:160]}")

    # poll for fills
    deadline = time.time() + float(c["wait_seconds"])
    pending = list(live)
    while pending and time.time() < deadline:
        time.sleep(5)
        for e in list(pending):
            try:
                o = tc.get_order_by_id(e["order_id"])
            except Exception:  # noqa: BLE001
                continue
            st = o.status.value
            if st == "filled":
                e.update(status="filled", fill_price=float(o.filled_avg_price or 0), filled_at=str(o.filled_at)[:19])
                pending.remove(e)
            elif st in ("canceled", "expired", "rejected"):
                e.update(status=st)
                pending.remove(e)

    # unfilled limits: cancel and fall back to market so the signal is not lost
    for e in pending:
        try:
            tc.cancel_order_by_id(e["order_id"])
        except Exception:  # noqa: BLE001
            pass
        try:  # a PARTIAL fill keeps its bracket legs (Alpaca sizes them to the filled qty); never add a full-size market order on top
            time.sleep(2)
            o = tc.get_order_by_id(e["order_id"])
            got = int(float(o.filled_qty or 0))
            if got > 0:
                e.update(status="filled", qty=got, fill_price=float(o.filled_avg_price or 0), filled_at=str(o.filled_at)[:19], partial=True)
                print(f"[execution] {e['symbol']} partial fill {got} of {e['qty']}; remainder cancelled, no fallback")
                continue
        except Exception:  # noqa: BLE001
            pass
        if c.get("fallback_to_market", True) and e["order_type"] == "limit":
            try:
                o = tc.submit_order(_bracket(e["symbol"], e["qty"], e["stop"], e["target"], f"{book}-{e['symbol']}-{today}-mkt"))
                e.update(order_id=str(o.id), order_type="market_fallback", status="fallback")
                time.sleep(10)
                o2 = tc.get_order_by_id(o.id)
                if o2.status.value == "filled":
                    e.update(status="filled", fill_price=float(o2.filled_avg_price or 0), filled_at=str(o2.filled_at)[:19])
            except Exception as ex:  # noqa: BLE001
                e.update(status=f"fallback rejected: {str(ex)[:100]}")
        else:
            e["status"] = "cancelled_unfilled"

    for e in live:
        base = e["ask"] if e.get("limit_base") == "ask" else (e.get("last") or None)
        if e.get("fill_price") and base:
            e["slippage_bps"] = round((e["fill_price"] - base) / base * 10_000, 1)
        tag = f"{e.get('order_type')} {e.get('status')}"
        px = f"filled @ {e['fill_price']:.2f}" if e.get("fill_price") else ""
        print(f"[execution] {e['symbol']} x{e['qty']} {tag} limit {e.get('limit_price')} ({e.get('limit_base')}) ask {e.get('ask')} last {e.get('last')} {px} slip {e.get('slippage_bps', '')}bps")
    return entries


# ---------------------------------------------------------------- opening-auction execution
def submit_auction_entries(tc, entries: list[dict], book: str, today) -> list[dict]:
    """Market-on-open buys (time_in_force OPG) submitted BEFORE the open. Alpaca rejects OPG after ~09:28 New York.
    No bracket can be attached to an OPG order; attach_brackets_after_open() adds the stop/target once filled."""
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    for e in entries:
        e.update(order_type="market_opg", status="new", fill_price=None, bid=None, ask=None, last=None, limit_price=None, limit_base="auction")
        try:
            o = tc.submit_order(MarketOrderRequest(symbol=e["symbol"], qty=e["qty"], side=OrderSide.BUY, time_in_force=TimeInForce.OPG,
                                                   client_order_id=f"{book}-{e['symbol']}-{today}-opg"))
            e.update(order_id=str(o.id), status="opg_submitted")
            print(f"[auction] {e['symbol']} x{e['qty']} market-on-open submitted")
        except Exception as ex:  # noqa: BLE001
            e.update(order_id=None, status=f"rejected: {type(ex).__name__}: {str(ex)[:120]}")
            print(f"[auction] {e['symbol']} OPG rejected: {str(ex)[:160]}")
    return entries


def submit_auction_exit(tc, symbol: str, qty: float, book: str, today) -> str | None:
    """Cancel any open orders on the symbol and sell the whole position in the opening auction."""
    from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

    try:
        cancel_open_orders(tc, symbol)   # and wait for the cancels to settle, else Alpaca rejects the sell ("insufficient qty")
        o = tc.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.OPG,
                                               client_order_id=f"{book}-{symbol}-{today}-exit"))
        return str(o.id)
    except Exception as ex:  # noqa: BLE001
        print(f"[auction] exit {symbol} failed: {str(ex)[:160]}")
        return None


def submit_market_sell(tc, symbol: str, qty, book: str, today, opg: bool = False) -> str:
    """Sell `qty` of a position (a rotation trim): market-on-open before the open, plain market otherwise."""
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    o = tc.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.OPG if opg else TimeInForce.DAY,
                                           client_order_id=f"{book}-{symbol}-{today}-trim"))
    return str(o.id)


def cancel_open_orders(tc, symbol: str, settle_seconds: int = 24) -> None:
    """Cancel every open order on `symbol` and wait until none is left (a pending cancel blocks a sell)."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    for o in tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
        try:
            tc.cancel_order_by_id(o.id)
        except Exception:  # noqa: BLE001
            pass
    for _ in range(max(1, settle_seconds // 2)):
        if not tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            return
        time.sleep(2)


def attach_oco(tc, symbol: str, qty: int, stop: float, target: float) -> str:
    """One-cancels-other SELL for a held position: limit at the target, stop at the stop. Returns the order id; raises on failure."""
    from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    # Alpaca's OCO: a limit sell (the target) plus a stop leg; the client insists on take_profit.limit_price as well.
    o = tc.submit_order(LimitOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                          order_class=OrderClass.OCO, limit_price=round(float(target), 2),
                                          take_profit=TakeProfitRequest(limit_price=round(float(target), 2)),
                                          stop_loss=StopLossRequest(stop_price=round(float(stop), 2))))
    return str(o.id)


def attach_stop(tc, symbol: str, qty: int, stop: float) -> str:
    """Plain GTC stop sell for a position that has no profit target (trailing-stop exits)."""
    from alpaca.trading.requests import StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    o = tc.submit_order(StopOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                         stop_price=round(float(stop), 2)))
    return str(o.id)


def attach_exit(tc, symbol: str, qty: int, stop: float, target) -> str:
    """The position's exit order(s): OCO stop/target when there is a target, a plain stop otherwise."""
    if target is None or (isinstance(target, float) and target != target) or float(target) <= 0:
        return attach_stop(tc, symbol, qty, stop)
    return attach_oco(tc, symbol, qty, stop, target)


def levels_from_fill(fill: float, entry: dict) -> tuple[float, float | None]:
    """Stop/target rebuilt from the actual fill price, as the backtester does. Falls back to the planned levels.
    A target_atr of 0 means no target (None)."""
    if entry.get("atr") and entry.get("stop_atr") is not None and entry.get("target_atr") is not None and fill > 0:
        a = float(entry["atr"])
        t = float(entry["target_atr"])
        return round(fill - float(entry["stop_atr"]) * a, 2), (round(fill + t * a, 2) if t > 0 else None)
    return float(entry["stop"]), (float(entry["target"]) if entry.get("target") else None)


def _settle_fill(tc, e: dict, o) -> bool:
    """Any filled quantity (full or partial) is a position: record it and attach the stop/target. False if nothing filled."""
    qty = int(float(o.filled_qty or 0))
    fill = float(o.filled_avg_price or 0)
    if qty <= 0 or fill <= 0:
        return False
    e.update(status="filled", fill_price=fill, filled_at=str(o.filled_at)[:19], qty=qty)
    e["stop"], e["target"] = levels_from_fill(fill, e)
    try:
        e["oco_id"] = attach_exit(tc, e["symbol"], qty, e["stop"], e["target"])
        print(f"[auction] {e['symbol']} x{qty} filled @ {fill:.2f}; stop {e['stop']} / target {e['target']} attached")
    except Exception as ex:  # noqa: BLE001
        e["status"], e["error"] = "filled_no_bracket", str(ex)[:160]
        print(f"[auction] {e['symbol']} x{qty} filled @ {fill:.2f} but bracket FAILED: {str(ex)[:160]}")
    return True


def attach_brackets_after_open(tc, entries: list[dict], deadline_seconds_after_open: int = 300) -> list[dict]:
    """Wait for the open, then for each filled OPG buy submit a one-cancels-other sell (limit at the target, stop at
    the stop), both rebuilt from the fill price. Partial fills are positions too. On timeout the order is cancelled
    and its fill status re-read, so a fill that happened is never mistaken for 'unfilled'."""
    live = [e for e in entries if e.get("status") == "opg_submitted"]
    if not live:
        return entries
    # wait for the market to open (the OPG fill happens in the opening cross)
    while True:
        try:
            clock = tc.get_clock()
            if clock.is_open:
                break
            secs = (clock.next_open - clock.timestamp).total_seconds()
            print(f"[auction] market opens in {secs / 60:.0f} min; waiting")
            time.sleep(min(max(secs - 5, 5), 300))
        except Exception as ex:  # noqa: BLE001
            print(f"[auction] clock check failed ({type(ex).__name__}); retrying")
            time.sleep(30)
    deadline = time.time() + deadline_seconds_after_open
    pending = list(live)
    while pending and time.time() < deadline:
        time.sleep(5)
        for e in list(pending):
            try:
                o = tc.get_order_by_id(e["order_id"])
            except Exception:  # noqa: BLE001
                continue
            st = o.status.value
            if st == "filled" or (st == "partially_filled" and time.time() > deadline - 30):
                if not _settle_fill(tc, e, o):
                    e["status"] = "unfilled"
                pending.remove(e)
            elif st in ("canceled", "expired", "rejected", "done_for_day"):
                if not _settle_fill(tc, e, o):  # a cancelled OPG order can still carry a partial fill
                    e["status"] = "unfilled"
                pending.remove(e)
    for e in pending:  # timed out (slow poll, sleep, network): cancel what is left and record any fill that DID happen
        try:
            tc.cancel_order_by_id(e["order_id"])
        except Exception:  # noqa: BLE001
            pass
        try:
            time.sleep(2)
            o = tc.get_order_by_id(e["order_id"])
            if not _settle_fill(tc, e, o):
                e["status"] = "unfilled_timeout"
        except Exception:  # noqa: BLE001
            e["status"] = "unfilled_timeout"  # unknown: the runner's reconciliation step protects any position found held
    for e in live:
        if e.get("fill_price") and e.get("ref_price"):
            e["slippage_bps"] = round((e["fill_price"] - e["ref_price"]) / e["ref_price"] * 10_000, 1)  # vs previous close: the gap, not execution cost
    return entries
