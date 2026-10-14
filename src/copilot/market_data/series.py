"""Basic series math on closed bars. Values that cannot be computed yet are None (UNKNOWN), never 0."""
from __future__ import annotations

from .models import Bar


def true_ranges(bars: list[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    """Wilder ATR. atr[i] uses bars[0..i] only (no lookahead). None until n bars exist."""
    tr = true_ranges(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(bars)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out
