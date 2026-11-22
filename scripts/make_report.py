"""Rebuild results/report.html from whatever runs exist in results/."""
import _common  # noqa: F401

from tradingbot.report import build_report

if __name__ == "__main__":
    p = build_report()
    print(f"report -> {p}")
