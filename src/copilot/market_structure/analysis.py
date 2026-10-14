"""Run the Phase 3 engines on one timeframe, and combine 5m context with 1m execution.

Spec section 3: the 1m chart must NOT override 5m context unless a regime transition has been identified.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..fvg.fvg import FVG, detect_fvgs
from ..liquidity.liquidity import (LiquidityEvent, LiquidityParams, Level, classify_interactions, equal_levels,
                                   levels_from_periods, levels_from_swings)
from ..market_data.models import RESOLUTION_SECONDS, Bar
from ..market_data.rolls import mark_roll_gaps
from ..market_data.series import atr as atr_series
from ..session_engine.sessions import PeriodLevel, killzone, period_levels
from ..volume.volume import classify, rv_n, rv_time_of_day
from .smc import SmcState
from .smc import run as run_smc
from .structure import BosEvent, Regime, StructureParams, Swing, classify_regime, detect_bos, detect_swings

ANALYSIS_VERSION = "phase3-0.2.0"   # 0.2: SMC structure replica added


@dataclass
class TimeframeAnalysis:
    resolution: str
    bars_used: int
    first_time: int | None
    last_closed_time: int | None
    atr: float | None
    swings: list[Swing]
    bos: list[BosEvent]
    regime: Regime
    periods: list[PeriodLevel]
    levels: list[Level]
    liquidity_events: list[LiquidityEvent]
    fvgs: list[FVG]
    rv20: float | None
    rv_tod: float | None
    volume_state: str
    killzone: str | None
    params: dict = field(default_factory=dict)
    version: str = ANALYSIS_VERSION
    # Directional structure = replica of the user's SMC indicator (persists until a CHoCH). The ATR-zigzag
    # swings/regime above remain for liquidity levels and as a second opinion.
    smc: SmcState | None = None


def analyze_timeframe(bars: list[Bar], resolution: str,
                      sp: StructureParams = StructureParams(), lp: LiquidityParams = LiquidityParams(),
                      roll_times: list[int] = ()) -> TimeframeAnalysis:
    """roll_times: contract-roll boundaries of the continuous symbol; FVGs straddling one are marked ROLL_GAP."""
    closed = [b for b in bars if b.is_closed]
    sec = RESOLUTION_SECONDS[resolution]
    swings = detect_swings(closed, sp)
    bos = detect_bos(closed, swings)
    regime = classify_regime(closed, swings, bos, sp)
    periods = period_levels(closed, sec)
    levels = levels_from_swings(swings, sec) + levels_from_periods(periods) + equal_levels(closed, swings, sec, lp)
    events = sorted(classify_interactions(closed, levels, lp), key=lambda e: e.resolved_time)
    fvgs = detect_fvgs(closed, sp.atr_len)
    mark_roll_gaps(fvgs, closed, list(roll_times))
    a = atr_series(closed, sp.atr_len)
    last = len(closed) - 1
    rv20 = rv_n(closed, last) if closed else None
    rvt = rv_time_of_day(closed, last) if closed else None
    return TimeframeAnalysis(
        resolution, len(closed), closed[0].time if closed else None, closed[-1].time if closed else None,
        a[-1] if a else None, swings, bos, regime, periods, levels, events, fvgs, rv20, rvt,
        classify(rvt if rvt is not None else rv20), killzone(closed[-1].time) if closed else None,
        {"structure": sp.__dict__, "liquidity": lp.__dict__},
        smc=run_smc(closed) if closed else None,
    )


@dataclass
class MtfContext:
    bias: str                    # from 5m regime: BULLISH | BEARISH | NEUTRAL | RANGE | TRANSITION | UNKNOWN
    execution_state: str         # 1m regime
    aligned: bool | None         # None when either side is UNKNOWN
    notes: list[str]


def mtf_context(ctx5: TimeframeAnalysis, exe1: TimeframeAnalysis) -> MtfContext:
    bias, exe = ctx5.regime.state, exe1.regime.state
    notes = []
    if bias == "UNKNOWN" or exe == "UNKNOWN":
        return MtfContext(bias, exe, None, ["5m or 1m structure is UNKNOWN -- no directional context"])
    directional = {"BULLISH": "UP", "BEARISH": "DOWN"}
    b, e = directional.get(bias), directional.get(exe)
    if b is None or e is None:
        notes.append(f"no directional comparison: 5m {bias} / 1m {exe} (alignment only applies to BULLISH/BEARISH)")
        return MtfContext(bias, exe, None, notes + (
            ["5m is in TRANSITION: 1m direction may be considered for the new direction (spec section 3)"]
            if bias == "TRANSITION" else []))
    aligned = b == e
    if b != e:
        notes.append(f"1m is {exe} against 5m {bias}: 1m does NOT override 5m (no 5m transition identified)")
    if bias == "TRANSITION":
        notes.append("5m is in TRANSITION: 1m direction may be considered for the new direction (spec section 3)")
    return MtfContext(bias, exe, aligned, notes)
