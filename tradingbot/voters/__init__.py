"""Voter registry. Any module in this folder that defines VOTER is auto-loaded."""
from __future__ import annotations
import importlib
import pkgutil

from .base import Voter, Context  # noqa: F401


def discover() -> dict[str, type[Voter]]:
    """Import every sibling module and collect their VOTER classes by name."""
    registry: dict[str, type[Voter]] = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name in ("base", "__init__"):
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        cls = getattr(module, "VOTER", None)
        if cls is not None:
            registry[cls.name] = cls
    return registry
