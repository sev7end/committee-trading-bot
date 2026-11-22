"""Fill cost model. The backtest is only as honest as this file.

bps = basis points = 1/100 of a percent. 5 bps = 0.05%.
- spread: you buy at the higher "ask" and sell at the lower "bid". We charge half
  the spread on each side.
- slippage: the price moves against you between deciding and filling.
- commission: broker fee per share (Alpaca is 0 for stocks).
- sell_fee_bps: small regulatory fees charged on sells.
"""
from __future__ import annotations


class CostModel:
    def __init__(self, cfg: dict):
        self.spread_bps = float(cfg.get("spread_bps", 0))
        self.slippage_bps = float(cfg.get("slippage_bps", 0))
        self.commission_per_share = float(cfg.get("commission_per_share", 0))
        self.min_commission = float(cfg.get("min_commission", 0))
        self.sell_fee_bps = float(cfg.get("sell_fee_bps", 0))
        self.fee_bps = float(cfg.get("fee_bps", 0))   # charged on the notional of EVERY fill (Alpaca crypto: 25 bps taker)

    @property
    def _side_penalty(self) -> float:
        return (self.spread_bps / 2 + self.slippage_bps) / 10_000

    def buy_price(self, raw: float) -> float:
        return raw * (1 + self._side_penalty)

    def sell_price(self, raw: float) -> float:
        return raw * (1 - self._side_penalty)

    def fees(self, shares: int, notional: float, side: str) -> float:
        c = max(self.commission_per_share * shares, self.min_commission if shares > 0 else 0.0)
        if side == "sell":
            c += notional * self.sell_fee_bps / 10_000
        c += notional * self.fee_bps / 10_000
        return c

    @classmethod
    def zero(cls) -> "CostModel":
        return cls({})
