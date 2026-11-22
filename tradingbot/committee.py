"""The committee: run every enabled voter and combine their votes into one decision per bar."""
from __future__ import annotations
import pandas as pd

from .voters import discover, Context
from . import memo


class Committee:
    def __init__(self, params: dict, registry: dict | None = None):
        self.params = params
        registry = registry or discover()
        self.voters = []
        for name, cfg in params.get("voters", {}).items():
            if not cfg.get("enabled", True) or float(cfg.get("weight", 1.0)) <= 0:
                continue  # weight 0 means "off": a voter with no weight must not gate entries through the agree count
            if name not in registry:
                raise KeyError(f"params.json references voter '{name}' but no module in tradingbot/voters defines it")
            self.voters.append((name, float(cfg.get("weight", 1.0)), registry[name](cfg), {k: v for k, v in cfg.items() if k not in ("weight", "enabled")}))
        if not self.voters:
            raise ValueError("no voters enabled")
        entry = params.get("entry", {})
        self.min_score = float(entry.get("min_score", 1.0))
        self.min_agree = int(entry.get("min_voters_agree", 1))

    @property
    def voter_names(self) -> list[str]:
        return [v[0] for v in self.voters]

    def score(self, bars: pd.DataFrame, ctx: Context) -> pd.DataFrame:
        """Return one row per bar: each voter's score, weighted total, agree count, and enter flag."""
        out = pd.DataFrame(index=bars.index)
        total = pd.Series(0.0, index=bars.index)
        agree = pd.Series(0, index=bars.index)
        total_s = pd.Series(0.0, index=bars.index)   # short side: directional votes flipped, filters as-is
        agree_s = pd.Series(0, index=bars.index)
        for name, weight, voter, cfg_key in self.voters:
            # weight/enabled do not change what a voter computes, so the series is shared across candidates that
            # differ only there (memo is a no-op unless a sweep worker enabled it)
            s = memo.get_or_compute(memo.key_for("voter", name, cfg_key, id(bars), id(ctx.benchmark) if ctx.benchmark is not None else 0),
                                    lambda: voter.compute(bars, ctx).reindex(bars.index))
            out[name] = s
            total += s.fillna(0.0) * weight
            agree += (s > 0).astype(int)
            ss = -s if getattr(voter, "directional", True) else s
            total_s += ss.fillna(0.0) * weight
            agree_s += (ss > 0).astype(int)
        out["total"] = total
        out["agree"] = agree
        out["enter"] = (total >= self.min_score) & (agree >= self.min_agree)
        out["total_short"] = total_s
        out["agree_short"] = agree_s
        out["enter_short"] = (total_s >= self.min_score) & (agree_s >= self.min_agree)
        return out
