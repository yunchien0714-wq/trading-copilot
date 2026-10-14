from datetime import date

from copilot.market_data.models import Bar
from copilot.research.orb import OrbRules, _ts, evaluate, prev_ny_direction, simulate_day

D = date(2026, 10, 13)


def session(path):
    """78 5m bars 09:30-16:00 ET; path = {bar index: (o, h, l, c)}, others flat at the previous close."""
    out, last = [], 100.0
    for i in range(78):
        o, h, l, c = path.get(i, (last, last + 0.2, last - 0.2, last))
        out.append(Bar(_ts(D, (9, 30)) + 300 * i, o, h, l, c, 10, True))
        last = c
    return out


RANGE = {0: (100, 101, 99, 100), 1: (100, 100.5, 99.5, 100), 2: (100, 100.8, 99.2, 100)}   # range 99-101


def test_long_breakout_hits_target_and_pays_commission():
    bars = session({**RANGE, 4: (100, 101.6, 100, 101.5), 8: (101.5, 107, 101.4, 106)})
    t = simulate_day(bars, D, OrbRules("x", target_r=2.0), 0.1, 10.0, 1.34, None)
    # entry 101.5 (first close above 101), stop 99 - 0.1 = 98.9, risk 2.6, target 106.7 -> not reached (high 107? yes)
    assert t.direction == "LONG" and t.entry == 101.5 and abs(t.stop - 98.9) < 1e-9
    assert t.outcome == "WIN" and t.gross_r == 2.0 and abs(t.r - (2.0 - 1.34 / (2.6 * 10))) < 1e-3


def test_stop_and_target_in_the_same_bar_is_a_loss():
    bars = session({**RANGE, 4: (100, 101.6, 100, 101.5), 6: (101.5, 110, 95, 101)})
    assert simulate_day(bars, D, OrbRules("x"), 0.1, 10.0, None, None).outcome == "LOSS"


def test_no_breakout_before_11_means_no_trade_and_timeout_exits_at_16():
    late = session({**RANGE, 20: (100, 102, 100, 101.9)})            # bar 20 closes 11:15 -> too late
    assert simulate_day(late, D, OrbRules("x"), 0.1, 10.0, None, None) is None
    drift = session({**RANGE, 4: (100, 101.6, 100, 101.5), 77: (101.5, 102.6, 101.4, 102.8)})
    t = simulate_day(drift, D, OrbRules("x"), 0.1, 10.0, None, None)
    assert t.outcome == "TIMEOUT" and abs(t.gross_r - (102.8 - 101.5) / 2.6) < 1e-3


def test_fade_filter_takes_only_the_allowed_direction():
    bars = session({**RANGE, 4: (100, 101.6, 100, 101.5), 6: (101.5, 101.5, 98, 98.5)})
    t = simulate_day(bars, D, OrbRules("x", fade_prev_ny=True), 0.1, 10.0, None, "SHORT")
    assert t.direction == "SHORT" and t.entry == 98.5                  # the earlier long breakout is ignored


def test_previous_new_york_direction():
    up = session({0: (100, 100, 100, 100), 77: (100, 103, 100, 103)})
    days = {date(2026, 10, 12): up, D: up}
    assert prev_ny_direction(days, D) == "UP" and prev_ny_direction(days, date(2026, 10, 12)) is None


def test_evaluate_requires_enough_profitable_trades():
    from copilot.research.backtest import SimTrade
    days = [date(2026, 7, 1 + i % 28) for i in range(1)]
    ev = evaluate([SimTrade(days[0], 0, "LONG", 1, 0, 2, "WIN", 1.0)], days)
    assert ev["verdict"] == "樣本不足"


def test_15m_bars_are_built_from_complete_5m_buckets():
    from copilot.research.orb import aggregate
    bars = session({0: (100, 101, 99, 100.5), 1: (100.5, 102, 100, 101), 2: (101, 101.5, 98, 99)})
    agg = aggregate(bars, _ts(D, (9, 30)), 15)
    assert len(agg) == 26 and (agg[0].open, agg[0].high, agg[0].low, agg[0].close) == (100, 102, 98, 99)
    assert len(aggregate(bars[1:], _ts(D, (9, 30)), 15)) == 25          # first bucket incomplete -> dropped
