"""Volume context from chart bars. TradingView bar volume only -- no bid/ask, so NO order-flow claims here.

rv_n          : volume / mean of the previous n bars
rv_time_of_day: volume / mean volume of the bar at the SAME ET clock time on the previous `sessions`
                trading days, matched by timestamp (unlike the TRVO indicator, which matches by bar offset
                and drifts when bars are missing). UNKNOWN unless every one of those sessions has that bar.
"""
from __future__ import annotations

from ..market_data.models import Bar
from ..session_engine.sessions import to_et, trading_day

EXPANSION_RV = 1.5    # UNCALIBRATED
CONTRACTION_RV = 0.6  # UNCALIBRATED


def rv_n(bars: list[Bar], i: int, n: int = 20) -> float | None:
    if i < n:
        return None
    base = sum(b.volume for b in bars[i - n:i]) / n
    return bars[i].volume / base if base > 0 else None


def rv_time_of_day(bars: list[Bar], i: int, sessions: int = 6) -> float | None:
    target = bars[i]
    clock = to_et(target.time).strftime("%H:%M")
    day = trading_day(target.time)
    by_day: dict = {}
    for b in bars[:i]:
        if to_et(b.time).strftime("%H:%M") == clock:
            d = trading_day(b.time)
            if d != day:
                by_day[d] = b.volume
    days = sorted(by_day)[-sessions:]
    if len(days) < sessions:
        return None
    base = sum(by_day[d] for d in days) / sessions
    return target.volume / base if base > 0 else None


def classify(rv: float | None) -> str:
    if rv is None:
        return "UNKNOWN"
    if rv >= EXPANSION_RV:
        return "EXPANSION"
    if rv <= CONTRACTION_RV:
        return "CONTRACTION"
    return "NORMAL"
