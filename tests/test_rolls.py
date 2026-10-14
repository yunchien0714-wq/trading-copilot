import sqlite3
from types import SimpleNamespace

from copilot.market_data.models import Bar
from copilot.market_data.rolls import boundaries, front_at, mark_roll_gaps, observe, spans_roll
from copilot.session_engine.sessions import trading_day_start

NOW = 1791230000
Z, G = "COMEX_MINI:MGCZ2026", "COMEX_MINI:MGCG2027"


def test_first_contract_is_baseline_then_change_is_a_roll():
    c = sqlite3.connect(":memory:")
    assert observe(c, "MGC", Z, NOW) is None                         # baseline, start unknown
    assert observe(c, "MGC", Z, NOW + 30) is None                    # unchanged
    later = NOW + 50 * 86400
    old, new, b = observe(c, "MGC", G, later)
    assert (old, new, b) == (Z, G, trading_day_start(later))
    assert observe(c, "MGC", G, later + 30) is None
    assert boundaries(c, "MGC") == [b]
    assert front_at(c, "MGC", NOW - 10) is None                      # before anything was observed: UNKNOWN
    assert front_at(c, "MGC", NOW + 100) == Z and front_at(c, "MGC", b + 1) == G
    assert boundaries(c, "MNQ") == []


def test_fvg_across_the_roll_is_marked_and_others_are_not():
    bars = [Bar(1000 + 300 * i, 0, 0, 0, 0, 1, True) for i in range(10)]
    b = bars[5].time                                                  # first bar of the new contract
    # an FVG at index i uses candles i-2, i-1, i
    gaps = [SimpleNamespace(index=i, status="OPEN") for i in (4, 5, 6, 7, 8)]
    assert mark_roll_gaps(gaps, bars, [b]) == 2                      # only i = 5, 6 mix both contracts
    assert [g.status for g in gaps] == ["OPEN", "ROLL_GAP", "ROLL_GAP", "OPEN", "OPEN"]
    assert spans_roll(b - 1, b, [b]) and not spans_roll(b, b + 10, [b])


def test_roll_gap_fvg_never_becomes_a_zone():
    from copilot.setup_engine.poc_fvg import find_zones
    node = SimpleNamespace(low=100.0, high=101.0)
    g = SimpleNamespace(status="ROLL_GAP", bottom=100.2, top=100.8, direction="BULL")
    a5 = SimpleNamespace(fvgs=[g])
    prof = SimpleNamespace(poc=node, segment_pocs=[], fixed_segment_pocs=[])
    assert find_zones(a5, prof) == []
    g.status = "OPEN"
    assert len(find_zones(a5, prof)) == 1
