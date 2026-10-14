import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from copilot.alert_tracking.outcomes import classify, decision_of, record_decision, resolve, resolve_pending
from copilot.market_data.models import Bar

ET = ZoneInfo("America/New_York")
T = int(datetime(2026, 10, 6, 10, 0, tzinfo=ET).timestamp())     # decision time (confirmation close)


def b(i, h, l, c=None):
    return Bar(T + i * 60, (h + l) / 2, h, l, c if c is not None else (h + l) / 2, 100, True)


def test_target_first_is_win_with_rr():
    r = resolve("LONG", 100.0, 98.0, 104.0, T, [b(0, 101, 99.5), b(1, 104.5, 101)], T + 600)
    assert (r.outcome, r.r) == ("WIN", 2.0) and r.mfe_r >= 2


def test_stop_first_is_loss():
    r = resolve("SHORT", 100.0, 102.0, 96.0, T, [b(0, 101, 99), b(1, 102.5, 100)], T + 600)
    assert (r.outcome, r.r) == ("LOSS", -1.0)


def test_both_in_one_bar_counts_as_loss():
    r = resolve("LONG", 100.0, 98.0, 104.0, T, [b(0, 104.5, 97.5)], T + 600)
    assert r.outcome == "LOSS"


def test_bars_before_decision_are_ignored():
    r = resolve("LONG", 100.0, 98.0, 104.0, T, [Bar(T - 60, 100, 105, 99, 104, 1, True), b(0, 101, 99.5)], T + 600)
    assert r.outcome == "PENDING"


def test_timeout_at_end_of_trading_day_marks_to_market():
    next_day = int(datetime(2026, 10, 7, 10, 0, tzinfo=ET).timestamp())
    r = resolve("LONG", 100.0, 98.0, 104.0, T, [b(0, 101, 99.5, 101.0)], next_day)
    assert r.outcome == "TIMEOUT" and r.r == 0.5


def test_classification_and_decisions():
    assert classify("ALERTED", "WIN", 2.0) == "CORRECT_ALERT"
    assert classify("BLOCKED", "LOSS", -1.0) == "FALSE_POSITIVE"
    assert classify("FILTERED", "WIN", 2.0) == "FILTERED_INCORRECTLY"
    assert classify("FILTERED", "LOSS", -1.0) == "FILTERED_CORRECTLY"
    assert classify("FILTERED", "PENDING", None) is None
    assert decision_of("QUALIFIED", 1, None) == ("ALERTED", None)
    assert decision_of("QUALIFIED", 0, None) == ("BLOCKED", "risk gate")
    assert decision_of("QUALIFIED", None, None) == ("QUALIFIED_UNGATED", None)
    assert decision_of("INVALIDATED", None, "confirmed but failed: 5m structure, volume support") == \
        ("FILTERED", "5m structure, volume support")
    assert decision_of("INVALIDATED", None, "1m close beyond zone edge") is None     # never had prices


def test_resolve_pending_links_user_trade():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trades (symbol TEXT, direction TEXT, entry_time INT)")
    conn.execute("INSERT INTO trades VALUES ('COMEX_MINI:MGCZ2026', 'LONG', ?)", (T + 120,))
    conn.execute("INSERT INTO trades VALUES ('CME_MINI:MNQZ2026', 'SHORT', ?)", (T + 120,))
    record_decision(conn, "k1", "live", "COMEX_MINI:MGC1!", "LONG", "ALERTED", None, T, 100.0, 98.0, 104.0, 2.0, {}, [])
    record_decision(conn, "k2", "live", "COMEX_MINI:MGC1!", "SHORT", "FILTERED", "volume support", T, 100.0, 102.0,
                    96.0, 2.0, {}, [])                     # an MNQ short at the same time is NOT this MGC setup
    done = resolve_pending(conn, lambda s, a, z: [b(0, 104.5, 99)], T + 600)
    rows = dict(conn.execute("SELECT key, user_traded FROM alert_outcomes").fetchall())
    row = conn.execute("SELECT outcome, classification, user_traded FROM alert_outcomes WHERE key='k1'").fetchone()
    assert len(done) == 2 and tuple(row) == ("WIN", "CORRECT_ALERT", 1) and rows["k2"] == 0


def test_every_potential_setup_is_recorded_including_unconfirmed_touches():
    from copilot.alert_tracking.outcomes import decisions_from
    from copilot.alert_tracking.rules_report import build, text
    from copilot.setup_engine.tracker import SCHEMA as TRACKER_SCHEMA
    src = sqlite3.connect(":memory:")
    src.row_factory = sqlite3.Row
    src.executescript(TRACKER_SCHEMA)
    row = ("INSERT INTO setups (key, symbol, setup, direction, zone_low, zone_high, state, first_seen, updated_at,"
           " entry, stop, target, rr, confirmed_at, invalidation, conditions, poc_sources, engine_version)"
           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    src.execute(row, ("a", "MGC", "S", "LONG", 99.0, 100.0, "EXPIRED", T, T, None, None, None, None, None,
                      "no confirmation", "{}", "[]", "v"))                                  # touched, never confirmed
    src.execute(row, ("b", "MGC", "S", "LONG", 99.0, 100.0, "EXPIRED", T, T, None, None, None, None, None,
                      "no confirmation", "{}", "[]", "v"))                                  # never touched -> skipped
    src.execute(row, ("c", "MGC", "S", "SHORT", 101.0, 102.0, "INVALIDATED", T, T, 101.0, 102.5, 98.0, 2.0, T,
                      "confirmed but failed: volume support", "{}", "[]", "v"))           # filtered by one rule
    src.execute("INSERT INTO setup_transitions VALUES ('a', ?, 'WATCHING', 'POTENTIAL_SETUP')", (T + 60,))
    got = {d[0]["key"]: d[1:4] for d in decisions_from(src, risk_known=True)}
    assert got == {"a": ("UNCONFIRMED", "expired: no 1m confirmation", T + 60),
                   "c": ("FILTERED", "volume support", T + 60)}
    a = [d for d in decisions_from(src, True) if d[0]["key"] == "a"][0]
    assert a[4:7] == (100.0, 99.0, 102.0)                 # touch entry at the near edge, stop far edge, 2R

    conn = sqlite3.connect(":memory:")
    for s, dec, why, at, entry, stop, target, rr, cond in decisions_from(src, True):
        record_decision(conn, s["key"], "live", "MGC", s["direction"], dec, why, at, entry, stop, target, rr, cond, "[]")
    conn.execute("UPDATE alert_outcomes SET outcome='WIN', r=2.0 WHERE key='a'")
    conn.execute("UPDATE alert_outcomes SET outcome='LOSS', r=-1.0 WHERE key='c'")
    rep = build(conn, "live")
    assert rep.decisions["UNCONFIRMED"].n == 1 and rep.only["volume support"].avg == -1.0
    assert "等不到 1m 確認" in text(rep) and "樣本不足" in text(rep)
    assert classify("UNCONFIRMED", "WIN", 2.0) == "MISSED_BY_CONFIRMATION"
    assert classify("UNCONFIRMED", "LOSS", -1.0) == "SAVED_BY_CONFIRMATION"
