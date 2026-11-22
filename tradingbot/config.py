"""Paths and JSON config loading. Everything else imports paths from here."""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
VOTERS_DIR = ROOT / "tradingbot" / "voters"


def load_json(path: Path | str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(obj, path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)


def load_settings() -> dict:
    return load_json(CONFIG_DIR / "settings.json")


def load_params(path: Path | str | None = None) -> dict:
    return load_json(path or CONFIG_DIR / "params.json")


def load_universe() -> list[str]:
    return load_json(CONFIG_DIR / "universe.json")["symbols"]


def book_settings(settings: dict, book: dict | None) -> dict:
    """Settings as seen by one book: its own benchmark (e.g. BTC-USD for crypto), its own cost model (crypto fees are
    25 bps a side, stocks are free) and whether it sizes in fractional units. Everything else is shared."""
    if not book:
        return settings
    s = json.loads(json.dumps(settings))
    if book.get("benchmark"):
        s["benchmark"] = book["benchmark"]
    if book.get("costs"):
        s["costs"] = {**s.get("costs", {}), **book["costs"]}
    s.setdefault("account", {})["fractional"] = bool(book.get("fractional", False))
    return s


def find_book(settings: dict, name: str) -> dict:
    try:
        return next(b for b in settings.get("books", []) if b["name"] == name)
    except StopIteration:
        raise KeyError(f"no book named '{name}' in settings.books")


def resolve_universe(name: str, settings: dict) -> list[str]:
    """'stocks' -> config/universe.json, 'etf' -> settings.validation_universe, else settings['<name>_universe']."""
    if name in (None, "", "stocks"):
        return load_universe()
    if name == "etf":
        return list(settings["validation_universe"])
    key = f"{name}_universe"
    if key not in settings:
        raise KeyError(f"unknown universe '{name}': add '{key}' to config/settings.json")
    return list(settings[key])
