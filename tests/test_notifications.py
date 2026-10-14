import sqlite3
from types import SimpleNamespace

import pytest

from copilot.event_engine.calendar import Event
from copilot.monitor import event_reminders, is_micro
from copilot.notifications.formatter import render
from copilot.notifications.telegram import MockSender, Outbox, TelegramSender
from copilot.setup_engine.poc_fvg import evaluate_all

from test_setup_engine import TICK, a5, approach_and_reject, profile


def qualified_ev():
    return evaluate_all(a5(), profile(), approach_and_reject(), TICK, 1.5)[0]


def note(kind, **kw):
    return SimpleNamespace(kind=kind, key="k", evaluation=kw.get("ev", qualified_ev()), risk=kw.get("risk"),
                           reason=kw.get("reason", ""))


GATE_OK = SimpleNamespace(passed=True, failures=[], max_contracts=10, risk_usd_at_max=210.0, loss_cap_usd=250.0)
GATE_NO = SimpleNamespace(passed=False, max_contracts=None, risk_usd_at_max=None, loss_cap_usd=None,
                          failures=[SimpleNamespace(rule="data quality", detail="data is DELAYED")])


def test_trade_setup_text_has_prices_risk_and_no_promises():
    t = render(note("TRADE_SETUP", risk=GATE_OK), "MGC1!", "REALTIME")
    for s in ("交易 setup", "進場：4214.5", "止損：4208.9", "RR：2.0", "10 口 → 打到止損約虧", "失效條件"):
        assert s in t
    assert "會賺" not in t and "資料" not in t                # no promises; realtime -> no data warning
    assert "把握度：未知" in t                                 # no fabricated confidence


def test_delayed_data_is_always_flagged():
    t = render(note("RISK_BLOCKED", risk=GATE_NO), "MGC1!", "DELAYED")
    assert "即時資料：data is DELAYED" in t and "[資料DELAYED" in t and "不做這筆" in t


def test_potential_and_invalidated_texts():
    assert "潛在 setup" in render(note("POTENTIAL"), "MGC1!", "REALTIME")
    assert "收盤低於 4209" in render(note("POTENTIAL"), "MGC1!", "REALTIME")
    assert "setup 失效" in render(note("INVALIDATED", ev=None, reason="zone gone"), "MGC1!", "REALTIME")


def test_outbox_statuses():
    conn = sqlite3.connect(":memory:")
    ok, bad = MockSender(), MockSender(fail=True)
    assert Outbox(conn, ok).post("X", "a", "hi", 1) == "SENT" and ok.sent == ["hi"]
    assert Outbox(conn, bad).post("X", "b", "hi", 1) == "FAILED"
    assert Outbox(conn, None).post("X", "c", "hi", 1) == "DRY_RUN"
    rows = dict(conn.execute("SELECT key, status FROM outbox").fetchall())
    assert rows == {"a": "SENT", "b": "FAILED", "c": "DRY_RUN"}
    retry = Outbox(conn, MockSender())
    assert retry.retry_failed(now=100) == 1
    assert conn.execute("SELECT status FROM outbox WHERE key='b'").fetchone()[0] == "SENT"
    assert Outbox(conn, MockSender(fail=True)).post("X", "d", "x", 2000) == "FAILED"
    assert Outbox(conn, MockSender()).retry_failed(now=2000 + 901) == 0     # too old to be worth sending


def test_event_reminders_once_per_stage():
    conn = sqlite3.connect(":memory:")
    ob = Outbox(conn, MockSender())
    ev = [Event("9", "CPI", "US", 1, 10_000, "BLS"), Event("8", "Fed Speech", "US", 0, 10_000, "Fed")]
    assert event_reminders(ob, ev, 10_000 - 50 * 60) == 2          # both at the 60-min stage
    assert event_reminders(ob, ev, 10_000 - 40 * 60) == 0          # already sent
    assert event_reminders(ob, ev, 10_000 - 10 * 60) == 1          # 15-min stage: HIGH only
    assert "10 分鐘後" in ob.sender.sent[-1]


def test_micro_detection():
    assert is_micro("MGC1!") is True and is_micro("GC1!") is False and is_micro("ES1!") is None


def test_sender_requires_token(monkeypatch):
    import copilot.notifications.telegram as tg
    monkeypatch.setattr(tg, "_user_env", lambda name: None)     # ignore any real token on this machine
    with pytest.raises(RuntimeError):
        TelegramSender()


# --- monitoring health alerts (observed: broker disconnect went unnoticed) ---------------------------

from copilot.monitor import LoopState, health


def test_short_broker_drop_stays_quiet():
    conn = sqlite3.connect(":memory:")
    ob, st = Outbox(conn, MockSender()), LoopState()
    health(ob, st, 1, broker_connected=True)
    health(ob, st, 100, broker_connected=False)
    health(ob, st, 190, broker_connected=False)         # 90 s down: the usual self-healing drop
    health(ob, st, 220, broker_connected=True)
    assert ob.sender.sent == []


def test_long_broker_outage_keeps_reminding_until_fixed():
    conn = sqlite3.connect(":memory:")
    ob, st = Outbox(conn, MockSender()), LoopState()
    t0 = 1000
    for dt in range(0, 5 * 3600 + 1, 30):               # down for 5 h, polled every 30 s
        health(ob, st, t0 + dt, broker_connected=False)
    sent = ob.sender.sent
    # alerts at 3 min, 30 min, then every 2 h after the second one: 2h30, 4h30
    assert len(sent) == 4 and "連接" in sent[0] and "仍未恢復" in sent[1] and "4 小時 30 分鐘" in sent[3]
    health(ob, st, t0 + 5 * 3600 + 30, broker_connected=True)
    assert len(ob.sender.sent) == 5 and "重新連線" in ob.sender.sent[4]
    health(ob, st, t0 + 6 * 3600, broker_connected=True)
    assert len(ob.sender.sent) == 5


def test_tv_down_alert_after_grace_period_then_recovery():
    conn = sqlite3.connect(":memory:")
    ob, st = Outbox(conn, MockSender()), LoopState()
    health(ob, st, 1000, tv_ok=False, detail="ConnectionClosedError")
    health(ob, st, 1100, tv_ok=False)                   # < 5 min: brief blips stay quiet
    assert ob.sender.sent == []
    health(ob, st, 1000 + 301, tv_ok=False)
    health(ob, st, 1000 + 400, tv_ok=False)
    assert len(ob.sender.sent) == 1 and "5 分鐘連不到" in ob.sender.sent[0]
    health(ob, st, 2000, tv_ok=True)
    assert "已恢復連線" in ob.sender.sent[-1]
    health(ob, st, 2100, tv_ok=False)
    health(ob, st, 2150, tv_ok=True)                    # short blip after recovery: silent
    assert len(ob.sender.sent) == 2


def test_market_hours_calendar():
    from datetime import datetime
    from copilot.session_engine.sessions import ET, market_open
    t = lambda d, h, m=0: int(datetime(2026, 10, d, h, m, tzinfo=ET).timestamp())
    assert not market_open(t(10, 12))                      # Saturday
    assert not market_open(t(11, 17, 59)) and market_open(t(11, 18))   # Sunday open
    assert market_open(t(12, 10)) and not market_open(t(12, 17, 30))    # Monday daily break
    assert market_open(t(9, 16, 59)) and not market_open(t(9, 17))      # Friday close


def test_no_system_alerts_while_market_closed_and_timers_restart_at_open():
    from datetime import datetime
    from copilot.session_engine.sessions import ET
    conn = sqlite3.connect(":memory:")
    ob, st = Outbox(conn, MockSender()), LoopState()
    sat = int(datetime(2026, 10, 10, 1, 35, tzinfo=ET).timestamp())
    for dt in range(0, 6 * 3600, 30):                      # broker down all Saturday night
        health(ob, st, sat + dt, broker_connected=False)
    health(ob, st, sat + 3600, tv_ok=False, detail="x")
    assert ob.sender.sent == []
    sun = int(datetime(2026, 10, 11, 18, 0, tzinfo=ET).timestamp())
    for dt in range(0, 200, 30):                           # still down after Sunday's open -> alert after 3 min
        health(ob, st, sun + dt, broker_connected=False)
    assert len(ob.sender.sent) == 1 and "3 分鐘" in ob.sender.sent[0]


def test_stale_bars_on_a_weekday_count_as_closed_holiday():
    from datetime import datetime
    from copilot.session_engine.sessions import ET
    conn = sqlite3.connect(":memory:")
    ob, st = Outbox(conn, MockSender()), LoopState()
    now = int(datetime(2026, 11, 26, 14, 0, tzinfo=ET).timestamp())    # a Thursday (exchange holiday)
    st.last_bar_close = now - 3 * 3600
    for dt in range(0, 900, 30):
        health(ob, st, now + dt, broker_connected=False)
    assert ob.sender.sent == []
