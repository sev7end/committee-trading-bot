"""Account-level risk limits: insurance, not a return engine.

Three limits, all relative to the sizing base (the $25k paper cap live; current equity in a backtest):

  daily_loss_pct    if the account lost more than this yesterday, no new entries today (a cooling-off day)
  weekly_loss_pct   same over the last five sessions
  max_open_risk_pct the money lost if EVERY open stop is hit, plus today's new entries, may not exceed this
  max_per_theme     at most this many open positions in one theme (sector for stocks, asset class for ETFs),
                    counted across all books: eight tech stocks are one bet, not eight

Settings: "risk_limits" in config/settings.json; themes in config/sectors.json ("sectors" map, unknown symbols are
their own theme). The backtest engine applies the same rules so research numbers include the insurance cost.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from .config import CONFIG_DIR, load_json

DEFAULTS = {"daily_loss_pct": 0.0, "weekly_loss_pct": 0.0, "max_open_risk_pct": 0.0, "max_per_theme": 0}


def limits(settings: dict) -> dict:
    return {**DEFAULTS, **(settings.get("risk_limits") or {})}


def load_themes() -> dict:
    try:
        return load_json(CONFIG_DIR / "sectors.json").get("sectors", {})
    except Exception:  # noqa: BLE001
        return {}


def theme_of(sym: str, themes: dict) -> str:
    return themes.get(sym, sym)


def loss_block(equity_history, base: float, cfg: dict) -> str | None:
    """Reason to block new entries today given the end-of-day equity history (oldest first), or None."""
    eq = [float(x) for x in equity_history if x is not None]
    d, wk = float(cfg.get("daily_loss_pct", 0)), float(cfg.get("weekly_loss_pct", 0))
    if d > 0 and len(eq) >= 2 and eq[-1] - eq[-2] <= -d / 100 * base:
        return f"lost ${eq[-2] - eq[-1]:,.0f} yesterday (limit {d}% of ${base:,.0f})"
    if wk > 0 and len(eq) >= 6 and eq[-1] - eq[-6] <= -wk / 100 * base:
        return f"lost ${eq[-6] - eq[-1]:,.0f} over 5 sessions (limit {wk}% of ${base:,.0f})"
    return None


@dataclass
class RiskState:
    """Mutable budget for one planning pass: shared by every book so the limits are account-wide."""
    cfg: dict
    base: float
    themes: dict
    theme_counts: dict = field(default_factory=dict)
    open_risk: float = 0.0
    blocked: str | None = None
    notes: list = field(default_factory=list)

    @property
    def budget(self) -> float:
        cap = float(self.cfg.get("max_open_risk_pct", 0))
        return float("inf") if cap <= 0 else cap / 100 * self.base - self.open_risk

    def theme_full(self, sym: str) -> bool:
        m = int(self.cfg.get("max_per_theme", 0))
        return m > 0 and self.theme_counts.get(theme_of(sym, self.themes), 0) >= m

    def fit_qty(self, qty: int, per_share_risk: float) -> int:
        """Shrink a planned quantity so its stop-loss risk fits the remaining budget (0 if nothing fits)."""
        if per_share_risk <= 0:
            return qty
        b = self.budget
        if b == float("inf"):
            return qty
        return int(min(qty, max(b, 0.0) // per_share_risk))

    def add(self, sym: str, qty: int, per_share_risk: float) -> None:
        self.theme_counts[theme_of(sym, self.themes)] = self.theme_counts.get(theme_of(sym, self.themes), 0) + 1
        self.open_risk += max(per_share_risk, 0.0) * qty

    def describe(self) -> str:
        parts = []
        if self.blocked:
            parts.append(f"NEW ENTRIES BLOCKED: {self.blocked}")
        cap = float(self.cfg.get("max_open_risk_pct", 0))
        if cap > 0:
            parts.append(f"open risk ${self.open_risk:,.0f} of ${cap / 100 * self.base:,.0f}")
        m = int(self.cfg.get("max_per_theme", 0))
        if m > 0:
            full = [t for t, n in self.theme_counts.items() if n >= m]
            parts.append(f"themes at cap ({m}): {', '.join(full) if full else 'none'}")
        return " · ".join(parts) if parts else "no limits configured"


def from_positions(settings: dict, base: float, positions: dict, prices: dict, equity_history=None) -> RiskState:
    """Build the state from tracked positions {sym: {qty, stop}} and current prices {sym: price}."""
    cfg = limits(settings)
    st = RiskState(cfg=cfg, base=float(base), themes=load_themes())
    for sym, info in positions.items():
        px = float(prices.get(sym, info.get("fill_price") or 0) or 0)
        st.add(sym, int(info.get("qty", 0)), max(px - float(info.get("stop", px)), 0.0))
    if equity_history is not None:
        st.blocked = loss_block(equity_history, base, cfg)
    return st
