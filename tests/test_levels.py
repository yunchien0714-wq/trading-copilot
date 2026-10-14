import sqlite3

from copilot.chart_manager.drawings import ChartManager
from copilot.chart_manager.levels import select, sync
from copilot.fvg.fvg import FVG
from copilot.liquidity.liquidity import Level, LiquidityEvent
from copilot.market_structure.analysis import TimeframeAnalysis
from copilot.market_structure.structure import Regime
from copilot.session_engine.sessions import PeriodLevel

from test_chart_manager import FakeChart

NOW = 1_790_950_000
ATR = 10.0


def analysis(periods=(), levels=(), events=(), fvgs=()):
    return TimeframeAnalysis("5", 100, NOW - 86400, NOW, ATR, [], [], Regime("RANGE"), list(periods),
                             list(levels), list(events), list(fvgs), None, None, "UNKNOWN", None)


def pl(name, price, complete=True):
    return PeriodLevel(name, price, "x", NOW - 3600, complete)


def test_far_and_incomplete_levels_dropped():
    a = analysis([pl("PDH", 4222.8), pl("PWH", 4422.2), pl("PDL", 4169.4, complete=False)])
    names = [n for c in select(a, 4227.0) for n in c.names]
    assert names == ["PDH"]          # PWH is 19 ATR away, PDL incomplete


def test_nearby_levels_merge_into_one_line():
    a = analysis([pl("PDH", 4222.8), pl("LONDON_H", 4223.9)])
    sel = select(a, 4227.0)
    assert len(sel) == 1 and set(sel[0].names) == {"PDH", "LONDON_H"}


def test_swept_liquidity_not_drawn():
    swept = Level("SWING_HIGH", "BSL", 4240.0, NOW - 7200, "structure")
    fresh = Level("SWING_LOW", "SSL", 4210.0, NOW - 7200, "structure")
    ev = LiquidityEvent(swept, "SWEEP_SAME_BAR", 1, NOW - 600, 1, NOW - 600, 1.0, 0)
    sel = select(analysis(levels=[swept, fresh], events=[ev]), 4227.0)
    assert [c.names for c in sel] == [["SWING_LOW"]]


def test_filled_and_tiny_fvgs_not_drawn():
    ok = FVG("BULL", 4220, 4214, 1, NOW - 600, 6, 0.6, 1.0, "OPEN")
    tiny = FVG("BULL", 4219, 4218.5, 2, NOW - 600, 0.5, 0.05, 1.0, "OPEN")
    filled = FVG("BEAR", 4240, 4232, 3, NOW - 600, 8, 0.8, 1.0, "FILLED")
    sel = select(analysis(fvgs=[ok, tiny, filled]), 4227.0)
    assert len(sel) == 1 and sel[0].low == 4214


def test_cap_respected_and_heaviest_kept():
    periods = [pl(n, 4200 + i * 5) for i, n in enumerate(
        ["PDH", "PDL", "PWH", "PWL", "ASIA_H", "ASIA_L", "LONDON_H", "LONDON_L", "NY_AM_H", "NY_AM_L",
         "NY_PM_H", "NY_PM_L"])]
    sel = select(analysis(periods), 4227.0, max_objects=4)
    assert len(sel) == 4
    assert {n for c in sel for n in c.names} >= {"PWH", "PWL"}


def test_sync_keeps_unchanged_adds_new_removes_stale():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    fake = FakeChart()
    cm = ChartManager(conn, fake.evaluate)
    a1 = analysis([pl("PDH", 4222.8), pl("PDL", 4210.0)])
    r1 = sync(cm, select(a1, 4215.0), 0, "MGC", NOW)
    assert len(r1["added"]) == 2 and not r1["removed"]
    a2 = analysis([pl("PDH", 4222.8), pl("LONDON_L", 4205.0)])   # PDL gone, LONDON_L new
    r2 = sync(cm, select(a2, 4215.0), 0, "MGC", NOW + 300)
    assert len(r2["kept"]) == 1 and len(r2["added"]) == 1 and len(r2["removed"]) == 1
    assert "USER1" in fake.shapes                                  # user's own drawing untouched
    assert len(cm.owned(environment="PRODUCTION")) == 2


# --- do not redraw what the user's indicators already draw  ---------------------------

from copilot.chart_manager.indicator_objects import Occupied, parse as parse_objects


def test_parse_keeps_smc_fvg_and_structure_but_not_fib_or_other_studies():
    raw = {"SMC Structures and FVG": {"boxes": [[4220.0, 4214.0]],
                                      "lines": [4204.2, 4220.6014, 4259.0],
                                      "labels": [["BOS", 4204.2], ["0.786(4220.6014)", 4220.6014]]},
           "PoC Migration Map [BackQuant] - Fixed": {"boxes": [[4200, 4202]], "lines": [4210.0], "labels": []}}
    occ = parse_objects(raw)
    assert occ.zones == [(4214.0, 4220.0)]
    assert sorted(occ.lines) == [4204.2, 4259.0]          # fib line removed, PoC study ignored


def test_select_skips_indicator_duplicates_but_keeps_session_levels():
    fvg = FVG("BULL", 4220, 4214, 1, NOW - 600, 6, 0.6, 1.0, "OPEN")
    swing = Level("SWING_LOW", "SSL", 4204.2, NOW - 7200, "structure")
    a = analysis([pl("PDH", 4222.8)], levels=[swing], fvgs=[fvg])
    occ = Occupied(zones=[(4215.0, 4219.0)], lines=[4204.3])
    names = [n for c in select(a, 4215.0, occupied=occ) for n in c.names]
    assert names == ["PDH"]
    names_without = [n for c in select(a, 4215.0) for n in c.names]
    assert set(names_without) == {"PDH", "SWING_LOW", "FVG"}
