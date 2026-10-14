import sqlite3

import pytest

from copilot.event_engine.calendar import Event
from copilot.trade_journal.grader import grade_trade, render

from test_risk_gate import CFG

T = 1_791_198_000
CFG = CFG | {"account": CFG["account"] | {"size_usd": 25000},
             "personal": CFG["personal"] | {"news_blackout_minutes_before": 30, "news_blackout_minutes_after": 30}}


def trade(**kw):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE bars (feed_symbol TEXT, resolution TEXT, time INT, open REAL, high REAL,"
                       " low REAL, close REAL, volume REAL);"
                       "CREATE TABLE t (id INT, account TEXT, symbol TEXT, direction TEXT, qty REAL, entry REAL,"
                       " entry_time INT, entry_source TEXT, initial_stop REAL, initial_target REAL, balance_before REAL,"
                       " status TEXT, exit REAL, exit_time INT, pnl_usd REAL, pnl_gross REAL)")
    row = dict(id=1, account="A", symbol="COMEX_MINI:MGCZ2026", direction="LONG", qty=6, entry=4189.3,
               entry_time=T, entry_source="broker position", initial_stop=4186.3, initial_target=4195.3,
               balance_before=25000.0, status="CLOSED", exit=4195.3, exit_time=T + 900, pnl_usd=352.0, pnl_gross=360.0)
    row.update(kw)
    conn.execute(f"INSERT INTO t VALUES ({','.join('?' * len(row))})", list(row.values()))
    return conn, conn.execute("SELECT * FROM t").fetchone()


def checks(g):
    return {n: s for _, n, s, _ in g.checks}


def test_no_market_history_means_unknown_and_provisional_not_a_guess():
    conn, t = trade()
    g = grade_trade(conn, t, CFG, [], 10.0, 0, 250.0)
    assert checks(g)["5m structure aligned"] == "UNKNOWN" and g.provisional
    assert g.metrics["planned_rr"] == 2.0 and g.metrics["r"] == 2.0 and g.outcome == "WIN"


def test_rr_below_minimum_is_d():
    conn, t = trade(initial_target=4191.3)                     # RR 0.67
    g = grade_trade(conn, t, CFG, [], 10.0, 0, 250.0)
    assert checks(g)["planned RR"] == "FAIL" and g.grade == "D" and g.quality == "BAD"


def test_risk_over_loss_cap_is_d():
    conn, t = trade(qty=10, initial_stop=4183.3)               # 6 pts * $10 * 10 + fees > 250
    g = grade_trade(conn, t, CFG, [], 10.0, 0, 250.0)
    assert checks(g)["loss cap (1/4 of buffer)"] == "FAIL" and g.grade == "D"
    assert any("1/4 of the remaining" in x for x in g.improve)


def test_news_window_and_trade_count():
    conn, t = trade()
    ev = [Event("1", "CPI", "US", 1, T + 600, "BLS")]
    g = grade_trade(conn, t, CFG, ev, 10.0, 5, 250.0)
    c = checks(g)
    assert c["outside news window"] == "FAIL" and c["trades per day"] == "FAIL" and g.grade == "D"


def test_size_below_recommended_is_recorded_only():
    conn, t = trade(qty=4)                                     # stop 3 pts -> $31/contract (test fee $1), cap 250 -> 8
    g = grade_trade(conn, t, CFG, [], 10.0, 0, 250.0)
    assert checks(g)["size vs recommended"] == "PASS" and g.metrics["recommended_contracts"] == 8
    assert g.grade == "A"


def test_size_above_conservative_recommendation_is_a_warning():
    conn, t = trade(qty=7)                                     # risk $217: over the 200 cap, within observed 260
    g = grade_trade(conn, t, CFG, [], 10.0, 0, (200.0, 260.0))
    c = checks(g)
    assert c["size vs recommended"] == "WARN" and g.metrics["recommended_contracts"] == 6 and g.grade == "B"
    assert any("recommended 6" in x for x in g.improve)


def test_stop_too_wide_for_even_one_contract():
    conn, t = trade(qty=1, initial_stop=4160.0, initial_target=4250.0)   # 29.3 pts -> $294 > 250
    g = grade_trade(conn, t, CFG, [], 10.0, 0, 250.0)
    assert checks(g)["size vs recommended"] == "WARN" and g.metrics["recommended_contracts"] == 0
    assert checks(g)["loss cap (1/4 of buffer)"] == "FAIL" and g.grade == "D"


def test_good_trade_bad_outcome_is_not_called_a_mistake():
    conn, t = trade(exit=4186.3, pnl_usd=-188.0)
    g = grade_trade(conn, t, CFG, [], 10.0, 0, 250.0)
    assert g.classification == "Good trade / Bad outcome"
    assert "variance" in g.lesson


def test_imported_trade_without_stop():
    conn, t = trade(entry_source="tradovate_performance_csv (Performance.csv)", initial_stop=None,
                    initial_target=None, balance_before=None)
    g = grade_trade(conn, t, CFG, None, 10.0, 0, None)
    c = checks(g)
    assert c["stop defined"] == "UNKNOWN" and c["outside news window"] == "UNKNOWN" and g.provisional
    assert "r" not in g.metrics


def test_render_has_all_sections():
    conn, t = trade(qty=7)                                     # over the conservative recommendation -> B
    txt = render(grade_trade(conn, t, CFG, [], 10.0, 0, (200.0, 260.0)), t)
    for s in ("交易日誌 #1", "評級：B", "分類：", "檢查項目：", "可改進：", "主要教訓："):
        assert s in txt


def test_loss_cap_three_tiers():
    conn, t = trade(qty=6, initial_stop=4186.3)              # risk 3 * 10 * 6 + 6 * 1.0 = 186
    c = lambda cap: {n: s for _, n, s, _ in grade_trade(conn, t, CFG, [], 10.0, 0, cap).checks}["loss cap (1/4 of buffer)"]
    assert c((200.0, 240.0)) == "PASS"                      # within the conservative cap
    assert c((180.0, 240.0)) == "WARN"                      # between: true value unknown -> warning, not a D
    assert c((150.0, 170.0)) == "FAIL"                      # over even the observed cap


def test_stop_after_reaching_1r_is_an_observation_not_a_rule():
    conn, t = trade(exit=4186.3, pnl_usd=-188.0)
    conn.execute("INSERT INTO bars VALUES ('COMEX_MINI:MGC1!', '1', ?, 4189, 4193.0, 4186.0, 4187, 10)", (T - T % 60,))
    g = grade_trade(conn, t, CFG, [], 10.0, 0, (250.0, 250.0))
    assert g.grade == "A" and any("hypothesis" in x for x in g.improve)   # an observation never lowers the grade
