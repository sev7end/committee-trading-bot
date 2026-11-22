"""The backtest engine.

Timeline for one trading day (bar i), in this exact order:
  1. Fill entries that were decided at yesterday's close, at TODAY'S OPEN plus costs.
  2. For every open position, check today's high/low against stop and target.
     If both are hit in the same bar we assume the stop hit first (conservative).
  3. Mark equity = cash + value of open positions at today's close.
  4. Run the committee on bars up to and including today's close. Any "enter"
     decision becomes a pending order for tomorrow's open.

Signals are computed vectorised over the full history but every voter is
backward-looking only, and tests/test_no_lookahead.py verifies that. The
engine itself never reads a bar beyond the one it is processing.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import math

import numpy as np
import pandas as pd

from .committee import Committee
from .costs import CostModel
from .indicators import atr
from .metrics import compute_metrics
from .sizing import vol_scale_series
from . import memo
from .earnings import load_earnings_dates, blackout_mask
from .risklimits import limits as risk_limits, loss_block, theme_of, load_themes
from .voters import Context


@dataclass
class Position:
    symbol: str
    shares: int
    entry_date: pd.Timestamp
    entry_price: float
    stop: float
    target: float
    entry_fees: float
    signal_date: pd.Timestamp
    scores: dict
    bars_held: int = 0
    side: int = 1          # +1 long, -1 short


@dataclass
class BacktestResult:
    trades: list[dict]
    equity: pd.Series
    metrics: dict
    params: dict
    window: dict
    skipped: dict = field(default_factory=dict)


def run_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame | None,
    params: dict,
    settings: dict,
    start: str,
    end: str,
) -> BacktestResult:
    costs = CostModel(settings.get("costs", {}))
    account = settings.get("account", {})
    cash = float(account.get("starting_cash", 25_000))
    starting_cash = cash
    max_positions = int(account.get("max_positions", 5))
    fractional = bool(account.get("fractional", False))   # crypto: quantities in 1/10000 units instead of whole shares

    def qty_of(x: float):
        return math.floor(x * 1e4) / 1e4 if fractional else int(x)
    rl = risk_limits(settings)
    max_per_sector = int(account.get("max_per_sector", 0)) or int(rl.get("max_per_theme", 0))  # 0 = no theme cap
    max_open_risk_pct = float(rl.get("max_open_risk_pct", 0)) / 100
    sectors = settings.get("_sectors") or {}
    if max_per_sector and not sectors:
        sectors = load_themes()
    warmup = int(settings.get("warmup_bars", 60))

    exit_cfg = params.get("exit", {})
    stop_atr = float(exit_cfg.get("stop_atr", 1.0))
    target_atr = float(exit_cfg.get("target_atr", 2.0))
    max_hold = int(exit_cfg.get("max_hold_bars", 10))
    atr_period = int(exit_cfg.get("atr_period", 14))
    trail_atr = float(exit_cfg.get("trail_atr", 0) or 0)      # > 0: stop ratchets up to (high - trail_atr * ATR), never down
    if target_atr <= 0:
        target_atr = math.inf                                   # no profit target: let the trailing stop / time exit end the trade
    side = -1 if str(params.get("entry", {}).get("side", "long")).lower() == "short" else 1

    risk_cfg = params.get("risk", {})
    risk_pct = float(risk_cfg.get("risk_per_trade_pct", 1.0)) / 100
    max_pos_pct = float(risk_cfg.get("max_position_pct", 20)) / 100

    committee = Committee(params)
    ctx = Context(benchmark=benchmark)
    earn_cfg = params.get("entry", {}).get("earnings_blackout")
    skipped = {"no_cash": 0, "max_positions": 0, "already_in": 0, "zero_shares": 0}

    # Pre-compute signals and ATR for each symbol (vectorised, backward-looking).
    signals: dict[str, pd.DataFrame] = {}
    atrs: dict[str, pd.Series] = {}
    vscale: dict[str, pd.Series] = {}
    vol_cfg = risk_cfg.get("vol_target")
    for sym, df in bars_by_symbol.items():
        if len(df) < warmup + 5:
            continue
        sig = committee.score(df, ctx)
        sig.iloc[:warmup] = sig.iloc[:warmup].assign(enter=False)
        if earn_cfg and earn_cfg.get("enabled"):
            ed = load_earnings_dates(sym)
            if len(ed):
                blocked = blackout_mask(df.index, ed, int(earn_cfg.get("days_before", 3)), int(earn_cfg.get("days_after", 1)))
                skipped["earnings_blackout"] = skipped.get("earnings_blackout", 0) + int((sig["enter"] & blocked).sum())
                sig.loc[blocked, "enter"] = False
        signals[sym] = sig
        atrs[sym] = memo.get_or_compute(memo.key_for("atr", id(df), atr_period), lambda: atr(df, atr_period))
        vscale[sym] = memo.get_or_compute(memo.key_for("vscale", id(df), id(benchmark) if benchmark is not None else 0, vol_cfg or {}),
                                          lambda: vol_scale_series(df, benchmark, vol_cfg))

    # Master calendar: union of all trading dates inside the window.
    all_dates = sorted(set().union(*[set(df.index) for df in bars_by_symbol.values()]))
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    dates = [d for d in all_dates if start_ts <= d <= end_ts]
    n_days = len(dates)
    cal = pd.DatetimeIndex(dates)
    voter_names = committee.voter_names

    # Align every series to the master calendar ONCE, as plain numpy arrays. The day loop below then does
    # only integer indexing; per-(symbol, day) pandas label lookups used to be more than half the run time.
    A: dict[str, dict] = {}
    for sym in signals:
        df = bars_by_symbol[sym]
        px = df.reindex(cal)
        sig = signals[sym].reindex(cal)
        A[sym] = {
            "has": ~px["close"].isna().to_numpy(),
            "open": px["open"].to_numpy(dtype=float), "high": px["high"].to_numpy(dtype=float),
            "low": px["low"].to_numpy(dtype=float), "close": px["close"].to_numpy(dtype=float),
            # last close on or before each day, also across days the symbol did not trade (== close.asof(date))
            "close_ff": df["close"].reindex(cal, method="ffill").to_numpy(dtype=float),
            "enter": signals[sym]["enter_short" if side < 0 else "enter"].reindex(cal, fill_value=False).astype(bool).to_numpy(),
            "total": sig["total_short" if side < 0 else "total"].to_numpy(dtype=float),
            "scores": {v: sig[v].to_numpy(dtype=float) for v in voter_names},
            "atr": atrs[sym].reindex(cal).to_numpy(dtype=float),
            "vs": vscale[sym].reindex(cal).fillna(1.0).to_numpy(dtype=float),
        }

    def mark(pos, close: float) -> float:
        """Per-share value of a position at `close`: the price for a long; collateral plus the move for a short."""
        return close if pos.side > 0 else 2 * pos.entry_price - close

    positions: dict[str, Position] = {}
    pending: list[dict] = []  # entries decided yesterday, to fill at today's open
    trades: list[dict] = []
    equity_points: dict[pd.Timestamp, float] = {}

    def close_position(pos: Position, date: pd.Timestamp, raw_price: float, reason: str):
        nonlocal cash
        if pos.side > 0:
            px = costs.sell_price(raw_price)
            notional = px * pos.shares
            fees = costs.fees(pos.shares, notional, "sell")
            cash += notional - fees
        else:  # buy to cover: the collateral reserved at entry comes back plus/minus the move
            px = costs.buy_price(raw_price)
            notional = px * pos.shares
            fees = costs.fees(pos.shares, notional, "buy")
            cash += pos.shares * (2 * pos.entry_price - px) - fees
        pnl = (px - pos.entry_price) * pos.shares * pos.side - fees - pos.entry_fees
        trades.append(
            {
                "symbol": pos.symbol,
                "signal_date": pos.signal_date.date().isoformat(),
                "entry_date": pos.entry_date.date().isoformat(),
                "exit_date": date.date().isoformat(),
                "entry_price": round(pos.entry_price, 4),
                "exit_price": round(px, 4),
                "shares": pos.shares,
                "stop": round(pos.stop, 4),
                "target": None if math.isinf(pos.target) else round(pos.target, 4),
                "pnl": round(pnl, 2),
                "pnl_pct": round((px / pos.entry_price - 1) * 100 * pos.side, 3),
                "side": "short" if pos.side < 0 else "long",
                "bars_held": pos.bars_held,
                "exit_reason": reason,
                "fees": round(fees + pos.entry_fees, 2),
                **{f"score_{k}": (None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, 3)) for k, v in pos.scores.items()},
            }
        )

    for i, date in enumerate(dates):
        # 0. account-level loss limits: after a bad day/week, no new entries today (the same rule the live runner applies)
        if pending and (rl.get("daily_loss_pct") or rl.get("weekly_loss_pct")):
            hist = list(equity_points.values())
            why = loss_block(hist[-6:], hist[-1] if hist else starting_cash, rl)
            if why:
                skipped["loss_limit"] = skipped.get("loss_limit", 0) + len(pending)
                pending = []
        # 1. fill pending entries at today's open
        for order in pending:
            sym = order["symbol"]
            arr = A[sym]
            if not arr["has"][i]:
                continue  # symbol did not trade today; drop the order
            if sym in positions:
                skipped["already_in"] += 1
                continue
            if len(positions) >= max_positions:
                skipped["max_positions"] += 1
                continue
            if max_per_sector:
                sec = sectors.get(sym, sym)
                if sum(1 for p in positions.values() if sectors.get(p.symbol, p.symbol) == sec) >= max_per_sector:
                    skipped["sector_cap"] = skipped.get("sector_cap", 0) + 1
                    continue
            raw_open = float(arr["open"][i])
            a = order["atr"]
            if side > 0:
                px = costs.buy_price(raw_open)
                stop, target = px - stop_atr * a, px + target_atr * a
            else:
                px = costs.sell_price(raw_open)
                stop, target = px + stop_atr * a, px - target_atr * a
            per_share_risk = abs(stop - px)
            if per_share_risk <= 0:
                skipped["zero_shares"] += 1
                continue
            # Equity as it is known AT THE OPEN: yesterday's close for existing positions (today's close is still
            # in the future) and the entry price for positions opened this morning.
            j = i - 1 if i > 0 else i
            equity_now = cash + sum(p.shares * (p.entry_price if p.entry_date == date else mark(p, float(A[p.symbol]["close_ff"][j])))
                                    for p in positions.values())
            vs = float(order.get("vol_scale", 1.0))
            # vol_scale dials the risk budget only; max_position_pct stays a hard concentration cap
            shares = qty_of(min(equity_now * risk_pct * vs / per_share_risk, equity_now * max_pos_pct / px, cash / px))
            if shares <= 0:
                skipped["no_cash" if cash < px else "zero_shares"] += 1
                continue
            if max_open_risk_pct > 0:
                # money lost if every open stop is hit (at yesterday's close) plus this entry must fit the budget
                open_risk = sum(max((float(A[p.symbol]["close_ff"][j]) - p.stop) * p.side, 0.0) * p.shares for p in positions.values())
                room = max_open_risk_pct * equity_now - open_risk
                shares = qty_of(min(shares, max(room, 0.0) / per_share_risk))
                if shares <= 0:
                    skipped["risk_cap"] = skipped.get("risk_cap", 0) + 1
                    continue
            notional = px * shares
            fees = costs.fees(shares, notional, "buy")
            cash -= notional + fees
            positions[sym] = Position(
                symbol=sym, shares=shares, entry_date=date, entry_price=px, stop=stop, target=target,
                entry_fees=fees, signal_date=order["signal_date"], scores=order["scores"], side=side,
            )
        pending = []

        # 2. exits
        for sym in list(positions.keys()):
            pos = positions[sym]
            arr = A[sym]
            if not arr["has"][i]:
                continue
            o, h, l, c = float(arr["open"][i]), float(arr["high"][i]), float(arr["low"][i]), float(arr["close"][i])
            pos.bars_held += 1
            reason, raw_px = None, None
            if pos.side > 0:
                if l <= pos.stop:
                    reason, raw_px = "stop", min(o, pos.stop)  # gap below the stop fills at the open
                elif h >= pos.target:
                    reason, raw_px = "target", max(o, pos.target)
            else:
                if h >= pos.stop:
                    reason, raw_px = "stop", max(o, pos.stop)  # gap above the stop covers at the open
                elif l <= pos.target:
                    reason, raw_px = "target", min(o, pos.target)
            if reason:
                pass
            elif pos.bars_held >= max_hold:
                reason, raw_px = "time", c
            elif i == n_days - 1:
                reason, raw_px = "end_of_data", c
            if reason:
                close_position(pos, date, raw_px, reason)
                del positions[sym]
            elif trail_atr > 0:
                # ratchet AFTER today's exit check: the new stop is live from tomorrow, exactly like the pre-open runner
                a_now = arr["atr"][i]
                if not np.isnan(a_now) and a_now > 0:
                    pos.stop = max(pos.stop, h - trail_atr * float(a_now)) if pos.side > 0 else min(pos.stop, l + trail_atr * float(a_now))

        # 3. mark equity at close
        mkt_value = 0.0
        for pos in positions.values():
            mkt_value += pos.shares * mark(pos, float(A[pos.symbol]["close_ff"][i]))
        equity_points[date] = cash + mkt_value

        # 4. signals at today's close -> pending for tomorrow
        if i == n_days - 1:
            break
        for sym, arr in A.items():
            if sym in positions or not arr["has"][i] or not arr["enter"][i]:
                continue
            a = arr["atr"][i]
            if np.isnan(a) or a <= 0:
                continue
            pending.append(
                {
                    "symbol": sym,
                    "signal_date": date,
                    "atr": float(a),
                    "vol_scale": float(arr["vs"][i]),
                    "scores": {**{n: float(arr["scores"][n][i]) for n in voter_names}, "total": float(arr["total"][i])},
                }
            )
        # Fill the strongest signals first when there are more than slots.
        pending.sort(key=lambda o: -o["scores"]["total"])

    # A position whose symbol had no bar on the final date (missing data, delisting) would otherwise never be
    # closed and would vanish from the trade list. Close it at its last available close.
    for sym in list(positions.keys()):
        pos = positions[sym]
        js = np.nonzero(A[sym]["has"])[0]
        if len(js):
            j = int(js[-1])
            close_position(pos, dates[j], float(A[sym]["close"][j]), "end_of_data")
        del positions[sym]

    equity = pd.Series(equity_points, dtype=float).sort_index()
    metrics = compute_metrics(trades, equity, starting_cash)
    return BacktestResult(trades=trades, equity=equity, metrics=metrics, params=params, window={"start": start, "end": end}, skipped=skipped)
