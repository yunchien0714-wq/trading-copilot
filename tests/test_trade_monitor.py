import sqlite3
from types import SimpleNamespace

import pytest

from copilot.market_data.models import Bar
from copilot.trade_monitor.monitor import TradeMonitor, brackets, direction_of, root_of

T0 = 1_790_000_000


def snap(positions=(), orders=(), balance=25000.0):
    return SimpleNamespace(connected=True, account_name="PROP-TEST", balance=balance,
                           raw={"positions": list(positions), "orders": list(orders), "allExecutions": []})


POS = {"id": "p1", "symbol": "COMEX_MINI:MGCZ2026", "qty": 5, "side": 1, "avgPrice": 4210.0}
ORDERS = [{"symbol": "MGCZ2026", "side": -1, "type": 3, "stopPrice": 4207.0, "qty": 5},
          {"symbol": "MGCZ2026", "side": -1, "type": 1, "limitPrice": 4216.0, "qty": 5}]


def bar(i, h, l, c):
    return Bar(T0 + i * 60, c, h, l, c, 100, True)


def tm():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return TradeMonitor(conn), conn


def test_root_and_direction_parsing():
    assert root_of("COMEX_MINI:MGC1!") == "MGC" and root_of("MGCZ2026") == "MGC" and root_of("NQZ26") == "NQ"
    assert direction_of({"side": 1}) == "LONG" and direction_of({"qty": -2}) == "SHORT" and direction_of({}) is None
    assert brackets(ORDERS, "MGC1!", "LONG") == (4207.0, 4216.0)
    assert brackets(ORDERS, "MGC1!", "SHORT") == (None, None)       # those orders are on the wrong side


def test_full_lifecycle_long():
    m, conn = tm()
    n = m.update(snap([POS], ORDERS), T0, {}, {"MGC": 10.0}, {"smc_5m": "BULLISH"})
    assert [x.kind for x in n] == ["TRADE_STARTED"] and "止損：4207.0" in n[0].text
    # price runs +3.5 pts = more than 1R (risk 3 pts)
    bars = [bar(1, 4212, 4209.5, 4211), bar(2, 4213.5, 4211, 4213)]
    n = m.update(snap([POS], ORDERS), T0 + 180, {"MGC": bars}, {"MGC": 10.0}, {"smc_5m": "BULLISH"})
    assert [x.kind for x in n] == ["TRADE_UPDATE"]
    assert m.update(snap([POS], ORDERS), T0 + 240, {"MGC": bars}, {"MGC": 10.0}, {"smc_5m": "BULLISH"}) == []
    assert m.peak_equity_hint({"MGC": 10.0}) == pytest.approx(25000 + 3.5 * 10 * 5)
    # closed at +$290 (incl. fees) -> derived exit ~4215.8, R = 290 / (3 * 10 * 5)
    n = m.update(snap([], [], balance=25290.0), T0 + 600, {"MGC": bars}, {"MGC": 10.0}, {"smc_5m": "BULLISH"})
    assert [x.kind for x in n] == ["TRADE_CLOSED"]
    row = conn.execute("SELECT * FROM trades").fetchone()
    assert row["status"] == "CLOSED" and row["exit"] == pytest.approx(4215.8)
    assert row["r_multiple"] == pytest.approx(290 / 150) and "derived" in row["exit_source"]
    assert row["mfe_points"] == pytest.approx(3.5) and row["mae_points"] == pytest.approx(0.5)


def test_no_brackets_means_unknown_risk_not_guessed():
    m, conn = tm()
    n = m.update(snap([POS], []), T0, {}, {"MGC": 10.0}, {})
    assert "未知（沒看到止損單）" in n[0].text
    n = m.update(snap([], [], balance=25100.0), T0 + 60, {}, {"MGC": 10.0}, {})
    assert conn.execute("SELECT r_multiple FROM trades").fetchone()[0] is None


def test_thesis_invalidated_once_when_structure_flips():
    m, _ = tm()
    m.update(snap([POS], ORDERS), T0, {}, {"MGC": 10.0}, {"smc_5m": "BULLISH"})
    n = m.update(snap([POS], ORDERS), T0 + 60, {}, {"MGC": 10.0}, {"smc_5m": "BEARISH", "smc_last": "CHOCH DOWN @ 4205"})
    assert [x.kind for x in n] == ["THESIS_INVALIDATED"] and "CHOCH DOWN" in n[0].text
    assert m.update(snap([POS], ORDERS), T0 + 120, {}, {"MGC": 10.0}, {"smc_5m": "BEARISH"}) == []


def test_reversal_entered_against_structure_is_not_invalidated_at_entry():
    # a reversal entered while the 5m structure was already against it
    m, _ = tm()
    ctx = {"smc_5m": "BEARISH", "smc_last": "CHOCH DOWN @ 4205"}
    m.update(snap([POS], ORDERS), T0, {}, {"MGC": 10.0}, ctx)                 # LONG against BEARISH
    assert m.update(snap([POS], ORDERS), T0 + 60, {}, {"MGC": 10.0}, ctx) == []
    n = m.update(snap([POS], ORDERS), T0 + 120, {}, {"MGC": 10.0},
                 {"smc_5m": "BEARISH", "smc_last": "BOS DOWN @ 4198"})          # a new break continues against it
    assert [x.kind for x in n] == ["THESIS_INVALIDATED"] and "逆向的結構突破" in n[0].text


def test_first_raw_objects_kept_for_format_verification():
    m, conn = tm()
    m.update(snap([POS], ORDERS), T0, {}, {"MGC": 10.0}, {})
    kinds = {r[0] for r in conn.execute("SELECT kind FROM broker_raw")}
    assert kinds == {"position", "order"}


def test_disconnected_broker_does_nothing():
    m, _ = tm()
    assert m.update(SimpleNamespace(connected=False), T0, {}, {}, {}) == []


def test_no_stop_order_alert_once_then_late_stop_is_adopted():
    # 10 contracts with no stop order seen
    m, conn = tm()
    kinds = lambda n: [x.kind for x in n]
    assert kinds(m.update(snap([POS], []), T0, {}, {"MGC": 10.0}, {})) == ["TRADE_STARTED"]
    assert m.update(snap([POS], []), T0 + 30, {}, {"MGC": 10.0}, {}) == []          # < 60 s: maybe placing it now
    n = m.update(snap([POS], []), T0 + 60, {}, {"MGC": 10.0}, {})
    assert kinds(n) == ["NO_STOP"] and "沒有止損單" in n[0].text and "5 口" in n[0].text
    assert m.update(snap([POS], []), T0 + 90, {}, {"MGC": 10.0}, {}) == []          # no repeat
    n = m.update(snap([POS], ORDERS), T0 + 120, {}, {"MGC": 10.0}, {})
    assert kinds(n) == ["STOP_OK"] and "4207.0" in n[0].text
    row = conn.execute("SELECT initial_stop, initial_target, brackets_source FROM trades").fetchone()
    assert row[0] == 4207.0 and row[1] == 4216.0 and "120s after entry" in row[2]


def test_stop_seen_at_entry_never_alerts():
    m, _ = tm()
    m.update(snap([POS], ORDERS), T0, {}, {"MGC": 10.0}, {})
    assert m.update(snap([POS], ORDERS), T0 + 300, {}, {"MGC": 10.0}, {}) == []
