"""Line-by-line replica of the user's "SMC Structures and FVG" (LudoGH68, pine digest b134b36c...)
structure engine, so the co-pilot's BOS / CHoCH / direction match what the user reads on the chart.

User definition : a structure stays in force until a BOS or CHoCH appears -- no timeout.
Indicator settings read from the chart: "Break with candle's body" = true (close-based breaks), lookback 10.

Faithful on purpose, including a quirk of the original: get_structure_highest/lowest_bar keep their result in a
`var` (persists across bars), so when no local peak is found in the last 10 bars the PREVIOUS bar's relative
offset is reused and now points one bar further back (source lines 10-32).
Verified: on 7,000 MGC 5m bars the replica reproduces all 10 BOS/CHoCH the indicator had drawn
(same order, same prices) and its current structure (BULLISH 4259.0 / 4204.2). Regression test:
tests/fixtures/mgc_5m_smc_2026-10-02.json. Tie resolution of ta.highestbars is undocumented; both choices gave
identical results on that data, most-recent is kept.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..market_data.models import Bar

LOOKBACK = 10


@dataclass(frozen=True)
class SmcEvent:
    kind: str          # BOS | CHOCH
    direction: str     # UP | DOWN
    index: int         # bar whose close broke the level
    time: int
    level: float       # the structure high/low that was broken
    level_index: int   # bar where that structure level was set


@dataclass
class SmcState:
    direction: str     # BULLISH | BEARISH | UNDEFINED
    structure_high: float
    structure_low: float
    high_index: int
    low_index: int
    events: list[SmcEvent]


TIES_RECENT = True   # undocumented in Pine; no observed case where it matters (cross-check)


def _highest_bars(h: list[float], i: int, length: int) -> int:
    """Offset (<= 0) of the highest high in the window ending at i."""
    lo = max(0, i - length + 1)
    best = i if TIES_RECENT else lo
    for k in (range(i, lo - 1, -1) if TIES_RECENT else range(lo, i + 1)):
        if h[k] > h[best]:
            best = k
    return best - i


def _lowest_bars(l: list[float], i: int, length: int) -> int:
    lo = max(0, i - length + 1)
    best = i if TIES_RECENT else lo
    for k in (range(i, lo - 1, -1) if TIES_RECENT else range(lo, i + 1)):
        if l[k] < l[best]:
            best = k
    return best - i


def run(bars: list[Bar], body_break: bool = True) -> SmcState:
    n = len(bars)
    if n == 0:
        return SmcState("UNDEFINED", float("nan"), float("nan"), 0, 0, [])
    H = [b.high for b in bars]
    L = [b.low for b in bars]
    C = [b.close for b in bars]
    hi_brk = C if body_break else H
    lo_brk = C if body_break else L

    s_high, s_low = H[0], L[0]
    s_high_i = s_low_i = 0
    direction = 0                  # 0 undefined, 1 bearish, 2 bullish (as in the source)
    idx_hi_var = 0                 # the persistent `var int idx` of each helper function
    idx_lo_var = 0
    events: list[SmcEvent] = []

    def g(arr, i, k):              # arr[k bars ago] with Pine's na for out-of-range
        j = i - k
        return arr[j] if j >= 0 else None

    for i in range(n):
        # --- get_structure_highest_bar(10) ---
        max_bar = _highest_bars(H, i, LOOKBACK if i > LOOKBACK else i + 1)
        for k in range(LOOKBACK):
            a, b1, c = g(H, i, k + 1), g(H, i, k + 2), g(H, i, k)
            if a is not None and b1 is not None and c is not None and a > b1 and c <= a and -(k + 1) >= max_bar:
                idx_hi_var = -(k + 1)
        idx_hi_var = max_bar if idx_hi_var == 0 else idx_hi_var
        structure_max_bar = i + idx_hi_var
        # --- get_structure_lowest_bar(10) ---
        min_bar = _lowest_bars(L, i, LOOKBACK if i > LOOKBACK else i + 1)
        for k in range(LOOKBACK):
            a, b1, c = g(L, i, k + 1), g(L, i, k + 2), g(L, i, k)
            if a is not None and b1 is not None and c is not None and a < b1 and c >= a and -(k + 1) >= min_bar:
                idx_lo_var = -(k + 1)
        idx_lo_var = min_bar if idx_lo_var == 0 else idx_lo_var
        structure_min_bar = i + idx_lo_var

        def prev_ok(arr, cmp, lvl, start):
            vals = [g(arr, i, k) for k in (1, 2, 3)]
            return all(v is not None and cmp(v, lvl) for v in vals) and (i - 3) > start

        # Source lines 298-299: the unconditional clause belongs to the OPPOSITE structure -- in a bullish
        # structure (2) any close below the protected low breaks at once (CHoCH), and vice versa. Breaks in
        # the structure's own direction (BOS) need the three-prior-closes condition.
        low_broken = (lo_brk[i] < s_low and prev_ok(lo_brk, lambda v, x: v >= x, s_low, s_low_i)) or \
                     (direction == 2 and lo_brk[i] < s_low)
        high_broken = (hi_brk[i] > s_high and prev_ok(hi_brk, lambda v, x: v <= x, s_high, s_high_i)) or \
                      (direction == 1 and hi_brk[i] > s_high)

        if low_broken:
            events.append(SmcEvent("BOS" if direction == 1 else "CHOCH", "DOWN", i, bars[i].time, s_low, s_low_i))
            direction = 1
            s_high_i = max(0, structure_max_bar)
            s_low_i = i
            s_high = H[s_high_i]
            s_low = L[i]
        elif high_broken:
            events.append(SmcEvent("BOS" if direction == 2 else "CHOCH", "UP", i, bars[i].time, s_high, s_high_i))
            direction = 2
            s_high_i = i
            s_low_i = max(0, structure_min_bar)
            s_high = H[i]
            s_low = L[s_low_i]
        else:
            if H[i] > s_high and direction in (0, 2):
                if not body_break or not (i - 3 > s_high_i):
                    s_high, s_high_i = H[i], i
            elif L[i] < s_low and direction in (0, 1):
                if not body_break or not (i - 3 > s_low_i):
                    s_low, s_low_i = L[i], i

    name = {0: "UNDEFINED", 1: "BEARISH", 2: "BULLISH"}[direction]
    return SmcState(name, s_high, s_low, s_high_i, s_low_i, events)
