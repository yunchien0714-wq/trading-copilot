from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from copilot.fvg.fvg import detect_fvgs
from copilot.liquidity.liquidity import Level, LiquidityParams, classify_interactions, equal_levels
from copilot.market_data.models import Bar
from copilot.market_data.series import atr
from copilot.market_structure.analysis import MtfContext, analyze_timeframe, mtf_context
from copilot.market_structure.structure import (StructureParams, classify_regime, detect_bos, detect_swings)
from copilot.session_engine.sessions import killzone, period_levels, trading_day
from copilot.volume.volume import rv_n, rv_time_of_day

ET = ZoneInfo("America/New_York")
T0 = int(datetime(2026, 9, 29, 9, 30, tzinfo=ET).timestamp())


def bar(i, o, h, l, c, v=100.0, step=300, t0=T0):
    return Bar(t0 + i * step, o, h, l, c, v, True)


def path(closes, spread=0.5, step=300, t0=T0):
    """Bars whose open = previous close and high/low = body +/- spread."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(bar(i, o, max(o, c) + spread, min(o, c) - spread, c, step=step, t0=t0))
        prev = c
    return out


P = StructureParams(atr_len=3, retrace_atr=2.0)


# --- ATR -------------------------------------------------------------------

def test_atr_none_until_enough_bars_and_no_lookahead():
    bars = path([10, 11, 12, 13, 14])
    a = atr(bars, 3)
    assert a[:2] == [None, None] and a[2] is not None
    # changing a later bar must not change earlier ATR values
    b2 = bars[:4] + [bar(4, 14, 40, 0, 20)]
    assert atr(b2, 3)[:4] == a[:4]


# --- swings ----------------------------------------------------------------

def zigzag():
    # up to 20, down to 10, up to 25, down to 15, up to 30
    seq = list(range(10, 21)) + list(range(19, 9, -1)) + list(range(11, 26)) + list(range(24, 14, -1)) + list(range(16, 31))
    return path([float(x) for x in seq])


def test_swings_require_meaningful_retrace_and_are_labelled():
    sw = detect_swings(zigzag(), P)
    kinds = [(s.kind, s.label) for s in sw]
    assert kinds[:4] == [("HIGH", None), ("LOW", None), ("HIGH", "HH"), ("LOW", "HL")]
    assert sw[0].price == pytest.approx(20.5)          # high of the bar closing at 20
    for s in sw:
        assert s.confirmed_index > s.index             # knowable only after the extreme


def test_extreme_on_first_loaded_bar_is_not_a_swing():
    sw = detect_swings(zigzag(), P)
    assert all(s.index > 0 for s in sw)


def test_small_wiggles_are_not_swings():
    bars = path([10, 10.5, 10, 10.5, 10, 10.5, 10, 10.5])
    assert detect_swings(bars, P) == []


# --- BOS / regime -----------------------------------------------------------

def test_bos_on_close_only_and_regime_bullish():
    bars = zigzag()
    sw = detect_swings(bars, P)
    bos = detect_bos(bars, sw)
    ups = [e for e in bos if e.direction == "UP"]
    assert ups, "closing above the first swing high must be a BOS"
    first = ups[0]
    assert bars[first.index].close > first.swing.price
    assert first.swing.confirmed_index < first.index
    assert classify_regime(bars, sw, bos, P).state == "BULLISH"


def _shift_then(confirm: bool):
    # bearish structure (LH/LL), then an up-break of the last lower high; optionally confirmed by a higher low
    seq = list(range(30, 19, -1)) + list(range(21, 27)) + list(range(25, 14, -1)) + list(range(16, 23)) \
        + list(range(21, 11, -1)) + list(range(13, 26))
    if confirm:   # pull back to a higher low and bounce, WITHOUT a new break -> last event is still the shift
        seq += list(range(24, 18, -1)) + list(range(19, 24))
    return path([float(x) for x in seq])


def test_shift_is_transition_until_confirmed():
    bars = _shift_then(False)
    sw = detect_swings(bars, P)
    bos = detect_bos(bars, sw)
    assert bos[-1].direction == "UP" and bos[-1].shift
    assert classify_regime(bars, sw, bos, P).state == "TRANSITION"


def test_shift_confirmed_by_higher_low_is_bullish():
    bars = _shift_then(True)
    sw = detect_swings(bars, P)
    bos = detect_bos(bars, sw)
    assert bos[-1].shift, "test must exercise the 'confirmed shift' path"
    r = classify_regime(bars, sw, bos, P)
    assert r.state == "BULLISH", r.evidence
    assert any("confirmed by a HL" in e for e in r.evidence)


def test_wick_through_without_close_is_not_bos():
    base = zigzag()[:30]                        # up to 20, down to ~11
    sw = detect_swings(base, P)
    hi = [s for s in sw if s.kind == "HIGH"][0]
    n = len(base)
    poke = base + [bar(n, 15, hi.price + 2, 14.5, hi.price - 1)]   # wick above, close below
    assert not [e for e in detect_bos(poke, detect_swings(poke, P)) if e.direction == "UP"]


def test_regime_unknown_without_enough_swings():
    bars = path([10, 11, 12])
    assert classify_regime(bars, [], [], P).state == "UNKNOWN"


# --- liquidity ----------------------------------------------------------------

LV = Level("PDH", "BSL", 100.0, T0, "session")


def test_sweep_same_bar():
    bars = [bar(0, 99, 99.5, 98, 99), bar(1, 99, 101, 98.5, 99.5)]
    ev = classify_interactions(bars, [LV])
    assert ev[0].kind == "SWEEP_SAME_BAR" and ev[0].excursion == pytest.approx(1.0)


def test_sweep_reclaim_within_n():
    bars = [bar(0, 99, 99.5, 98, 99), bar(1, 99, 101, 98.5, 100.5), bar(2, 100.5, 101, 99, 99.5)]
    ev = classify_interactions(bars, [LV], LiquidityParams(reclaim_bars=3))
    assert ev[0].kind == "SWEEP_RECLAIM" and ev[0].bars_outside == 1


def test_break_when_no_reclaim():
    bars = [bar(0, 99, 99.5, 98, 99)] + [bar(i, 100.5, 102, 100.2, 101) for i in range(1, 6)]
    ev = classify_interactions(bars, [LV], LiquidityParams(reclaim_bars=3))
    assert ev[0].kind == "BREAK" and ev[0].resolved_index == 4


def test_pending_break_is_not_classified():
    bars = [bar(0, 99, 99.5, 98, 99), bar(1, 99, 101, 98.5, 100.5)]
    assert classify_interactions(bars, [LV], LiquidityParams(reclaim_bars=3)) == []


def test_level_not_tested_before_known():
    late = Level("PDH", "BSL", 100.0, T0 + 600, "session")
    bars = [bar(0, 99, 101, 98, 99), bar(1, 99, 99.5, 98, 99)]
    assert classify_interactions(bars, [late]) == []


def test_equal_highs():
    seq = [10, 12, 14, 16, 18, 20, 18, 16, 14, 12, 14, 16, 18, 20, 18, 16, 14, 12, 14]
    bars = path([float(x) for x in seq])
    sw = detect_swings(bars, P)
    eq = equal_levels(bars, sw, 300, LiquidityParams(equal_tol_atr=0.2))
    assert any(l.name == "EQH" and l.price == pytest.approx(20.5) for l in eq)


# --- FVG -----------------------------------------------------------------------

def test_bull_fvg_lifecycle():
    bars = [bar(0, 10, 11, 9.5, 10.8), bar(1, 10.8, 14, 10.7, 13.8), bar(2, 13.8, 15, 12, 14.5),
            bar(3, 14.5, 14.6, 11.6, 12.5),   # into zone 11..12, past midpoint 11.5? low 11.6 -> 40% only
            bar(4, 12.5, 12.6, 11.4, 12.0),   # 60% -> MITIGATED_50
            bar(5, 12.0, 12.2, 10.9, 11.2)]   # wick below 11 -> FILLED, close above 11
    g = detect_fvgs(bars, atr_len=2)[0]
    assert (g.direction, g.bottom, g.top) == ("BULL", 11, 12)
    assert g.status == "FILLED" and g.invalidated_time is None


def test_fvg_invalidated_on_close_beyond():
    bars = [bar(0, 10, 11, 9.5, 10.8), bar(1, 10.8, 14, 10.7, 13.8), bar(2, 13.8, 15, 12, 14.5),
            bar(3, 14.5, 14.6, 10, 10.5)]
    g = detect_fvgs(bars, atr_len=2)[0]
    assert g.status == "INVALIDATED"


def test_bear_fvg_any_size():
    bars = [bar(0, 20, 20.5, 19.0, 19.2), bar(1, 19.2, 19.3, 17, 17.1), bar(2, 17.1, 18.99, 16, 16.5)]
    g = detect_fvgs(bars, atr_len=2)[0]
    assert g.direction == "BEAR" and g.size == pytest.approx(0.01)


# --- sessions -------------------------------------------------------------------

def ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def test_trading_day_starts_at_18_et():
    assert str(trading_day(ts(2026, 10, 1, 17, 59))) == "2026-10-01"
    assert str(trading_day(ts(2026, 10, 1, 18, 0))) == "2026-10-02"


def test_killzones_follow_dst():
    assert killzone(ts(2026, 7, 1, 9, 45)) == "NY_AM"      # EDT
    assert killzone(ts(2026, 12, 1, 9, 45)) == "NY_AM"     # EST
    assert killzone(ts(2026, 12, 1, 23, 0)) == "ASIA"      # crosses midnight
    assert killzone(ts(2026, 12, 1, 0, 0)) is None
    assert killzone(ts(2026, 12, 1, 11, 0)) is None
    assert killzone(ts(2026, 12, 1, 12, 30)) is None       # lunch is not one of the user's sessions


def test_pdh_unknown_when_previous_day_partial_and_known_when_complete():
    def day_bars(start_ts, n, base):
        return [Bar(start_ts + i * 3600, base, base + 1 + (i == 5) * 10, base - 1, base, 10, True) for i in range(n)]
    d1 = day_bars(ts(2026, 9, 28, 18, 0), 23, 100)   # trading day 09-29
    d2 = day_bars(ts(2026, 9, 29, 18, 0), 23, 200)   # trading day 09-30
    d3 = day_bars(ts(2026, 9, 30, 18, 0), 5, 300)    # current
    lv = {p.name: p for p in period_levels(d2 + d3, 3600)}
    assert lv["PDH"].price is None and not lv["PDH"].complete
    lv = {p.name: p for p in period_levels(d1 + d2 + d3, 3600)}
    assert lv["PDH"].price == 211 and lv["PDL"].price == 199
    assert lv["PDH"].known_at == d2[-1].time + 3600


# --- volume ----------------------------------------------------------------------

def test_rv_n():
    bars = [bar(i, 1, 1, 1, 1, v=100) for i in range(20)] + [bar(20, 1, 1, 1, 1, v=300)]
    assert rv_n(bars, 20) == pytest.approx(3.0)
    assert rv_n(bars, 5) is None


def test_rv_time_of_day_matches_by_clock_and_needs_all_sessions():
    bars = []
    for d in range(7):
        t = ts(2026, 9, 21 + d, 10, 0)
        bars.append(Bar(t, 1, 1, 1, 1, 100 if d < 6 else 250, True))
    assert rv_time_of_day(bars, 6, sessions=6) == pytest.approx(2.5)
    assert rv_time_of_day(bars, 6, sessions=7) is None


# --- MTF ---------------------------------------------------------------------------

def test_1m_does_not_override_5m():
    a5 = analyze_timeframe(zigzag(), "5", P)
    a1 = analyze_timeframe(zigzag(), "1", P)
    a5.regime.state, a1.regime.state = "BULLISH", "BEARISH"
    ctx = mtf_context(a5, a1)
    assert ctx.bias == "BULLISH" and ctx.aligned is False
    assert any("does NOT override" in n for n in ctx.notes)


def test_non_directional_regimes_have_no_alignment():
    a5 = analyze_timeframe(zigzag(), "5", P)
    a1 = analyze_timeframe(zigzag(), "1", P)
    a5.regime.state, a1.regime.state = "RANGE", "RANGE"
    assert mtf_context(a5, a1).aligned is None
    a5.regime.state, a1.regime.state = "BULLISH", "BULLISH"
    assert mtf_context(a5, a1).aligned is True


def test_unknown_context_propagates():
    a5 = analyze_timeframe(path([10, 11]), "5", P)
    a1 = analyze_timeframe(zigzag(), "1", P)
    ctx = mtf_context(a5, a1)
    assert ctx.aligned is None and ctx.bias == "UNKNOWN"


def test_forming_bar_excluded_from_analysis():
    bars = zigzag()
    forming = bars[:-1] + [Bar(bars[-1].time, 30, 999, 30, 999, 1, False)]
    a = analyze_timeframe(forming, "5", P)
    assert a.bars_used == len(bars) - 1
    assert all(s.price != 999 for s in a.swings)
