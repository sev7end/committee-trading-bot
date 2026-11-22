"""Opt-in memo for expensive per-symbol series during parameter sweeps.

Inside one sweep worker the price DataFrames never change, so a voter's score series (or an ATR series, or a
volatility-scale series) for a given (data object, parameters) pair is identical across the thousands of
candidate parameter sets the worker evaluates. Each voter depends only on its own knobs, and each knob has a
handful of grid values, so a few thousand entries cover the whole search: after warm-up the signal computation
becomes nearly free and an evaluation is just the day-by-day portfolio loop.

Off by default (plain function call). sweep.py enables it in every worker. Keys use id() of the data objects,
which is only safe while those objects stay alive and unchanged, as they do for the lifetime of a worker.
"""
from __future__ import annotations
import json
from collections import OrderedDict

_STORE: OrderedDict | None = None
_MAX = 0
stats = {"hits": 0, "misses": 0}


def enable(max_entries: int = 5000) -> None:
    global _STORE, _MAX
    _STORE, _MAX = OrderedDict(), int(max_entries)
    stats.update(hits=0, misses=0)


def disable() -> None:
    global _STORE
    _STORE = None


def enabled() -> bool:
    return _STORE is not None


def key_for(*parts) -> tuple:
    return tuple(json.dumps(p, sort_keys=True, default=str) if isinstance(p, (dict, list)) else p for p in parts)


def get_or_compute(key: tuple, fn):
    if _STORE is None:
        return fn()
    if key in _STORE:
        _STORE.move_to_end(key)
        stats["hits"] += 1
        return _STORE[key]
    v = fn()
    stats["misses"] += 1
    _STORE[key] = v
    if len(_STORE) > _MAX:
        _STORE.popitem(last=False)
    return v
