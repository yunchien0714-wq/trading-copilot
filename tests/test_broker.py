import re
import sqlite3
from datetime import date

from copilot.broker.tradingview_broker import (FORBIDDEN, READ_METHODS, build_read_js, gate_state, high_water,
                                               parse, record)

RAW = {"connected": True, "connectionStatus": 1, "currentAccount": 1239643, "currentAccountType": "demo",
       "accountsMetainfo": [{"id": 1239643, "name": "PROP-TEST-0005", "currency": "USD", "type": "demo"}],
       "positions": [], "orders": [], "allExecutions": [],
       "summary": [["帳戶餘額", 25000], ["權益", 25000], ["獲利", 0]],
       "account_table": [{"id": "0", "netLiq": "25000.00", "openPL": "0.00", "totalPL": "0.00"}]}


def acct(net_liq, open_pl):
    return [{"id": "0", "netLiq": f"{net_liq:.2f}", "openPL": f"{open_pl:.2f}"}]


def test_js_is_read_only():
    js = build_read_js()
    for name in FORBIDDEN:
        assert not re.search(rf"\b{name}\b", js), name
    assert set(READ_METHODS).isdisjoint(FORBIDDEN)


def test_parse_fresh_account():
    s = parse(RAW, 100)
    assert s.connected and s.account_name == "PROP-TEST-0005"
    assert (s.balance, s.equity, s.profit, s.open_positions, s.trades_today) == (25000, 25000, 0, 0, 0)
    assert s.problems == []


def test_missing_account_table_is_unknown_not_guessed_from_labels():
    raw = dict(RAW, account_table=None, account_table_error="no page")
    s = parse(raw, 100)
    assert s.balance is None and s.equity is None and any("UNKNOWN" in p for p in s.problems)


def test_equity_is_net_liq_even_when_the_labels_say_otherwise():
    # observed during an open trade: the row labelled 帳戶餘額 moved with open P/L, 權益 stayed flat
    raw = dict(RAW, summary=[["帳戶餘額", 24930.00], ["權益", 24962.00], ["獲利", -32]], account_table=acct(24930.00, -32))
    s = parse(raw, 100)
    assert (s.equity, s.balance, s.profit) == (24930.00, 24962.00, -32)


def test_unverified_executions_make_trade_count_unknown():
    s = parse(dict(RAW, allExecutions=[{"id": 1}]), 100)
    assert s.trades_today is None


def test_not_connected():
    assert not parse({"connected": False}, 1).connected


def test_high_water_seeded_only_from_untouched_account():
    conn = sqlite3.connect(":memory:")
    s = parse(RAW, 100)
    assert high_water(conn, s.account_name, 25000, s)[0] is None          # nothing recorded yet
    record(conn, s)
    record(conn, parse(dict(RAW, account_table=acct(25180, 180)), 200))
    assert high_water(conn, s.account_name, 25000, s)[0] == 25180
    conn2 = sqlite3.connect(":memory:")
    used = parse(dict(RAW, account_table=acct(24700, 0)), 100)
    record(conn2, used)
    assert high_water(conn2, used.account_name, 25000, used)[0] is None   # history unknown -> UNKNOWN


def test_gate_state_unknowns_propagate():
    s = parse(dict(RAW, allExecutions=[{"id": 1}]), 100)
    st = gate_state(s, None, date(2026, 10, 5))
    assert st["high_water_usd"] == "UNKNOWN" and st["trades_today"] == "UNKNOWN"
    assert st["balance_usd"] == 25000 and st["as_of"] == date(2026, 10, 5)


def test_broker_object_present_but_session_down_is_not_connected():
    for status in (2, 3, 4, None):
        s = parse(dict(RAW, connectionStatus=status), 100)
        assert not s.connected and s.balance is None


def _mark(conn, at, bal, eq=None):
    from copilot.broker.tradingview_broker import SCHEMA
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO equity_marks VALUES (?,?,?,?,?)", (at, "PROP-TEST-0005", bal, eq if eq is not None else bal, "t"))


def test_unobserved_balance_change_makes_high_water_unknown_until_anchor():
    conn = sqlite3.connect(":memory:")
    s = parse(RAW, 100)
    record(conn, s)
    _mark(conn, 130, 25000.0)
    _mark(conn, 130 + 17 * 3600, 24959.92)                # traded while disconnected (17 h gap)
    hw, why = high_water(conn, s.account_name, 25000, s, 1000)
    assert hw is None and "unobserved" in why
    # user reads the trailing threshold 24050 at t=70000 -> high-water 25050; later observations count again
    _mark(conn, 70030, 24959.92, 24990.0)
    hw, why = high_water(conn, s.account_name, 25000, s, 1000, anchor=(24050.0, 70000))
    assert hw == 25050 and "anchor" in why


def test_continuous_observation_keeps_high_water_known():
    conn = sqlite3.connect(":memory:")
    s = parse(RAW, 100)
    record(conn, s)
    for i in range(1, 5):
        _mark(conn, 100 + 30 * i, 25000 + 20 * i, 25000 + 25 * i)   # balance moves, but watched every 30 s
    assert high_water(conn, s.account_name, 25000, s, 1000)[0] == 25100


def test_load_anchor(tmp_path):
    from copilot.broker.tradingview_broker import load_anchor
    p = tmp_path / "s.toml"
    p.write_text("trailing_threshold_usd = 24050.5\nthreshold_as_of = 2026-10-05T06:30:00-04:00\n", encoding="utf-8")
    from datetime import datetime, timedelta, timezone
    thr, at = load_anchor(p)
    expected = int(datetime(2026, 10, 5, 6, 30, tzinfo=timezone(timedelta(hours=-4))).timestamp())
    assert thr == 24050.5 and at == expected
    p.write_text("# nothing yet\n", encoding="utf-8")
    assert load_anchor(p) is None


def test_trades_today_unknown_after_unobserved_change_unless_reported():
    from copilot.broker.tradingview_broker import trades_today
    conn = sqlite3.connect(":memory:")
    _mark(conn, 1000, 25000.0)
    _mark(conn, 1000 + 17 * 3600, 24959.92)
    day_start, today = 2000, date(2026, 10, 5)
    assert trades_today(conn, "PROP-TEST-0005", day_start, None, today)[0] is None
    assert trades_today(conn, "PROP-TEST-0005", day_start, (2, date(2026, 10, 5)), today)[0] == 2
    assert trades_today(conn, "PROP-TEST-0005", day_start, (2, date(2026, 10, 4)), today)[0] is None  # stale report


def test_trades_today_zero_when_watched_all_day():
    from copilot.broker.tradingview_broker import trades_today
    conn = sqlite3.connect(":memory:")
    for i in range(10):
        _mark(conn, 1000 + 60 * i, 25000.0)
    assert trades_today(conn, "PROP-TEST-0005", 1100, None, date(2026, 10, 5))[0] == 0
