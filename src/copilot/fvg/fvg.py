"""Three-candle Fair Value Gaps. User definition : every size counts; quality is scored later.

Bullish FVG at bar i: low[i] > high[i-2]  -> zone [high[i-2], low[i]]
Bearish FVG at bar i: high[i] < low[i-2]  -> zone [high[i], low[i-2]]
The gap is knowable at the close of bar i. Lifecycle is evaluated on bars after i only:
  TOUCHED      price traded into the zone
  MITIGATED_50 price reached the midpoint (consequent encroachment)
  FILLED       price traded through the far edge (wick)
  INVALIDATED  a bar CLOSED beyond the far edge
"""
from __future__ import annotations

from dataclasses import dataclass

from ..market_data.models import Bar
from ..market_data.series import atr as atr_series

_ORDER = ["OPEN", "TOUCHED", "MITIGATED_50", "FILLED"]


@dataclass
class FVG:
    direction: str            # BULL | BEAR
    top: float
    bottom: float
    index: int                # bar i (third candle); knowable at its close
    time: int
    size: float
    size_atr: float | None    # size / ATR at formation (None = ATR not available yet)
    displacement_atr: float | None  # body of the middle candle / ATR
    status: str = "OPEN"
    first_touch_time: int | None = None
    max_fill_pct: float = 0.0
    invalidated_time: int | None = None

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2


def detect_fvgs(bars: list[Bar], atr_len: int = 14) -> list[FVG]:
    a = atr_series(bars, atr_len)
    out: list[FVG] = []
    for i in range(2, len(bars)):
        b0, b1, b2 = bars[i - 2], bars[i - 1], bars[i]
        if b2.low > b0.high:
            direction, top, bottom = "BULL", b2.low, b0.high
        elif b2.high < b0.low:
            direction, top, bottom = "BEAR", b0.low, b2.high
        else:
            continue
        size = top - bottom
        ref = a[i - 1]  # ATR known before the gap completed
        out.append(FVG(direction, top, bottom, i, b2.time, size,
                       size / ref if ref else None,
                       abs(b1.close - b1.open) / ref if ref else None))
    for g in out:
        _track(g, bars)
    return out


def _advance(g: FVG, status: str) -> None:
    if _ORDER.index(status) > _ORDER.index(g.status):
        g.status = status


def _track(g: FVG, bars: list[Bar]) -> None:
    for b in bars[g.index + 1:]:
        if g.direction == "BULL":
            depth = g.top - b.low                     # how far down into the zone
            closed_beyond = b.close < g.bottom
        else:
            depth = b.high - g.bottom
            closed_beyond = b.close > g.top
        if depth > 0:
            if g.first_touch_time is None:
                g.first_touch_time = b.time
            pct = min(depth / g.size, 1.0) if g.size > 0 else 1.0
            g.max_fill_pct = max(g.max_fill_pct, pct)
            _advance(g, "FILLED" if pct >= 1.0 else "MITIGATED_50" if pct >= 0.5 else "TOUCHED")
        if closed_beyond:
            g.status = "INVALIDATED"
            g.invalidated_time = b.time
            return
