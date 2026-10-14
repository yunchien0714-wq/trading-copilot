"""Liquidity pools and what price did to them.

User definition : a sweep counts in BOTH forms, labelled separately --
  SWEEP_SAME_BAR : a bar trades through the level and closes back on the original side in the same bar
  SWEEP_RECLAIM  : a bar closes through, but a close back on the original side follows within reclaim_bars
  BREAK          : closed through and no reclaim within reclaim_bars (acceptance beyond the level)
Only levels that were already knowable before the bar are tested (no lookahead).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..market_data.models import Bar
from ..market_data.series import atr as atr_series
from ..market_structure.structure import Swing
from ..session_engine.sessions import PeriodLevel


@dataclass(frozen=True)
class LiquidityParams:
    reclaim_bars: int = 3          # UNCALIBRATED: "N bars" for SWEEP_RECLAIM
    equal_tol_atr: float = 0.10    # UNCALIBRATED: two swings within this * ATR are "equal"
    equal_lookback_swings: int = 10
    calibrated: bool = False


@dataclass(frozen=True)
class Level:
    name: str          # e.g. SWING_HIGH, EQH, PDH, LONDON_L
    side: str          # BSL (above price, buy stops) | SSL (below price, sell stops)
    price: float
    known_time: int    # CLOSE time of the bar at which the level became knowable; tested on bars opening >= this
    source: str        # structure | session | equal_levels


@dataclass(frozen=True)
class LiquidityEvent:
    level: Level
    kind: str          # SWEEP_SAME_BAR | SWEEP_RECLAIM | BREAK
    pierce_index: int  # first bar that traded through
    pierce_time: int
    resolved_index: int  # bar at whose close the classification became known
    resolved_time: int
    excursion: float   # how far beyond the level price traded before resolving (points)
    bars_outside: int


def levels_from_swings(swings: list[Swing], bar_seconds: int) -> list[Level]:
    return [Level("SWING_HIGH" if s.kind == "HIGH" else "SWING_LOW", "BSL" if s.kind == "HIGH" else "SSL",
                  s.price, s.confirmed_time + bar_seconds, "structure") for s in swings]


def levels_from_periods(periods: list[PeriodLevel]) -> list[Level]:
    out = []
    for p in periods:
        if p.price is None or not p.complete or p.known_at is None:
            continue
        side = "BSL" if p.name.endswith("H") else "SSL"
        out.append(Level(p.name, side, p.price, p.known_at, "session"))
    return out


def equal_levels(bars: list[Bar], swings: list[Swing], bar_seconds: int,
                 p: LiquidityParams = LiquidityParams()) -> list[Level]:
    """Pairs of same-kind swings within tolerance, with no swing between them beyond both (pool still intact)."""
    a = atr_series(bars)
    out = []
    for kind in ("HIGH", "LOW"):
        ss = [s for s in swings if s.kind == kind]
        for j, s in enumerate(ss):
            tol_base = a[s.confirmed_index]
            if tol_base is None:
                continue
            tol = p.equal_tol_atr * tol_base
            for k in range(max(0, j - p.equal_lookback_swings), j):
                t = ss[k]
                if abs(s.price - t.price) > tol:
                    continue
                between = ss[k + 1:j]
                if kind == "HIGH" and any(x.price > max(s.price, t.price) for x in between):
                    continue
                if kind == "LOW" and any(x.price < min(s.price, t.price) for x in between):
                    continue
                price = max(s.price, t.price) if kind == "HIGH" else min(s.price, t.price)
                out.append(Level("EQH" if kind == "HIGH" else "EQL", "BSL" if kind == "HIGH" else "SSL",
                                 price, s.confirmed_time + bar_seconds, "equal_levels"))
                break
    return out


def classify_interactions(bars: list[Bar], levels: list[Level],
                          p: LiquidityParams = LiquidityParams()) -> list[LiquidityEvent]:
    """First interaction of each level. A level that has not been traded through yet produces no event."""
    events = []
    for lv in levels:
        start = next((i for i, b in enumerate(bars) if b.time >= lv.known_time), None)
        if start is None:
            continue
        up = lv.side == "BSL"
        for i in range(start, len(bars)):
            b = bars[i]
            pierced = b.high > lv.price if up else b.low < lv.price
            if not pierced:
                continue
            closed_beyond = b.close > lv.price if up else b.close < lv.price
            if not closed_beyond:
                exc = (b.high - lv.price) if up else (lv.price - b.low)
                events.append(LiquidityEvent(lv, "SWEEP_SAME_BAR", i, b.time, i, b.time, exc, 0))
                break
            # closed beyond: look for a reclaim close within reclaim_bars
            last = min(len(bars) - 1, i + p.reclaim_bars)
            exc = 0.0
            resolved = None
            for k in range(i, last + 1):
                c = bars[k]
                exc = max(exc, (c.high - lv.price) if up else (lv.price - c.low))
                if k > i and ((c.close <= lv.price) if up else (c.close >= lv.price)):
                    resolved = ("SWEEP_RECLAIM", k)
                    break
            if resolved is None:
                if i + p.reclaim_bars > len(bars) - 1:
                    break  # window still open -> not classifiable yet (no event rather than a guess)
                resolved = ("BREAK", i + p.reclaim_bars)
            kind, k = resolved
            events.append(LiquidityEvent(lv, kind, i, b.time, k, bars[k].time, exc, k - i))
            break
    return events
