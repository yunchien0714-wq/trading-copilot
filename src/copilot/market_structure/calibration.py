"""Swing-threshold calibration: how strong is each swing?

strength(swing) = the largest retrace multiple k (from K_GRID) at which the ATR zigzag still marks a swing of
the same kind at the same bar. A label "H 3" means: still a swing high even with a 3x-ATR retrace requirement.
The user looks at these on the chart and says which ones they consider meaningful -> that fixes retrace_atr.
"""
from __future__ import annotations

from dataclasses import replace

from ..market_data.models import Bar
from .structure import StructureParams, Swing, detect_swings

K_GRID = (1.0, 1.5, 2.0, 3.0, 4.0, 6.0)


def swing_strengths(bars: list[Bar], base: StructureParams = StructureParams()) -> list[tuple[Swing, float]]:
    present: dict[float, set[tuple[str, int]]] = {}
    for k in K_GRID:
        present[k] = {(s.kind, s.index) for s in detect_swings(bars, replace(base, retrace_atr=k))}
    out = []
    for s in detect_swings(bars, replace(base, retrace_atr=K_GRID[0])):
        best = max(k for k in K_GRID if (s.kind, s.index) in present[k]) if any(
            (s.kind, s.index) in present[k] for k in K_GRID) else K_GRID[0]
        out.append((s, best))
    return out


def counts_by_threshold(strengths: list[tuple[Swing, float]]) -> dict[float, int]:
    return {k: sum(1 for _, st in strengths if st >= k) for k in K_GRID}
