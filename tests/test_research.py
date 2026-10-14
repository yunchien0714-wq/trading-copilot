from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from copilot.market_data.models import Bar
from copilot.research.backtest import (MIN_TRADES, SimTrade, Stats, bootstrap_p_better, resolve_managed,
                                       walk_forward)
from copilot.research.hypotheses import SEED, variants
from copilot.setup_engine.poc_fvg import PRODUCTION

ET = ZoneInfo("America/New_York")
T = int(datetime(2026, 10, 6, 10, 0, tzinfo=ET).timestamp())


def bar(i, h, l, c=None):
    return Bar(T + i * 60, (h + l) / 2, h, l, c if c is not None else (h + l) / 2, 1, True)


def trades(day, rs):
    return [SimTrade(day, T + i, "LONG", 100, 99, 102, "WIN" if r > 0 else "LOSS", r) for i, r in enumerate(rs)]


def test_stats():
    s = Stats.of(trades(date(2026, 10, 1), [2, -1, -1, 2, -1]))
    assert (s.n, s.wins, s.total_r) == (5, 2, 1) and s.expectancy == pytest.approx(0.2)
    assert s.max_dd_r == 2 and s.profit_factor == pytest.approx(4 / 3)


def test_breakeven_only_after_the_bar_closes():
    # bar0 reaches +1.2R and comes back to entry inside the same bar: BE not yet armed -> still at original stop
    bars = [bar(0, 101.2, 99.9), bar(1, 100.5, 98.9)]
    assert resolve_managed("LONG", 100, 99, 102, T, bars, T + 600, 1.0).outcome == "BREAKEVEN"
    bars2 = [bar(0, 101.2, 98.9)]                         # same bar hits original stop -> loss
    assert resolve_managed("LONG", 100, 99, 102, T, bars2, T + 600, 1.0).r == -1.0
    bars3 = [bar(0, 101.2, 99.9), bar(1, 102.1, 100.5)]
    assert resolve_managed("LONG", 100, 99, 102, T, bars3, T + 600, 1.0).r == 2.0


def test_bootstrap_direction():
    days = [date(2026, 10, d) for d in range(1, 11)]
    better = [t for d in days for t in trades(d, [1])]
    worse = [t for d in days for t in trades(d, [-1])]
    assert bootstrap_p_better(better, worse, days) == 1.0
    assert bootstrap_p_better(worse, better, days) == 0.0


def test_walk_forward_requires_enough_trades():
    days = [date(2026, 10, d) for d in range(1, 7)]
    res = {"production": [t for d in days for t in trades(d, [-1, 2])],
           "cand": [t for d in days for t in trades(d, [2, 2])]}
    v = {x.variant: x for x in walk_forward(res, days)}
    assert v["production"].verdict == "BASELINE"
    assert v["cand"].verdict == "INSUFFICIENT_DATA"          # 2 trades/day * 2 OOS days < MIN_TRADES


def test_walk_forward_candidate_and_rejected():
    days = [date(2026, 9, 1 + d) for d in range(30)]
    per_day = MIN_TRADES // 5 + 1
    res = {"production": [t for d in days for t in trades(d, [-1, 2] * per_day)],
           "better": [t for d in days for t in trades(d, [2, 2] * per_day)],
           "worse": [t for d in days for t in trades(d, [-1, -1] * per_day)]}
    v = {x.variant: x for x in walk_forward(res, days)}
    assert v["better"].verdict == "CANDIDATE"
    assert v["worse"].verdict == "REJECTED"


def test_hypotheses_never_alter_production():
    assert PRODUCTION.name == "production" and PRODUCTION.use_volume_support and PRODUCTION.target_r == 2.0
    for hid, _, _, var in SEED:
        assert var.rules != PRODUCTION or var.breakeven_at_r is not None, hid
    assert set(variants()) == {"H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9"}
