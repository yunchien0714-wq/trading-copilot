import sqlite3

from copilot.event_engine.calendar import Event
from copilot.notifications.commands import answer, muted_until, poll
from copilot.notifications.telegram import MockSender, Outbox

NOW = 1_791_200_000


class FakeTelegram(MockSender):
    def __init__(self, updates):
        super().__init__()
        self.chat_id = "111"
        self.updates = updates

    def _call(self, method, payload=None):
        assert method == "getUpdates"
        return [u for u in self.updates if u["update_id"] >= (payload or {}).get("offset", 0)]


def msg(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_only_the_users_chat_is_answered_and_updates_not_repeated():
    conn = sqlite3.connect(":memory:")
    tg = FakeTelegram([msg(1, 999, "/status"), msg(2, 111, "/help"), msg(3, 111, "hello")])
    assert poll(tg, conn, {}, NOW) == 1 and "指令" in tg.sent[0]
    assert poll(tg, conn, {}, NOW) == 0                     # offset remembered


def test_mute_silences_noncritical_only():
    conn = sqlite3.connect(":memory:")
    answer("/mute", "20", conn, {}, NOW)
    assert muted_until(conn) == NOW + 1200
    ob = Outbox(conn, MockSender())
    assert ob.post("POTENTIAL", "a", "x", NOW + 60) == "MUTED"
    assert ob.post("HEALTH", "b", "x", NOW + 60) == "SENT"
    assert ob.post("TRADE_SETUP", "c", "x", NOW + 60) == "SENT"
    answer("/unmute", "", conn, {}, NOW)
    assert ob.post("POTENTIAL", "d", "x", NOW + 120) == "SENT"


def test_account_and_events_answers_show_unknowns():
    conn = sqlite3.connect(":memory:")
    a = answer("/account", "", conn, {"gate_state": {"balance_usd": 24962.00, "high_water_usd": "UNKNOWN"}}, NOW)
    assert "最高權益：未知" in a and "單筆虧損上限：未知" in a
    assert "讀不到" in answer("/events", "", conn, {"events": None}, NOW)
    ev = [Event("1", "CPI", "US", 1, NOW + 3600, "BLS")]
    assert "重大 CPI" in answer("/events", "", conn, {"events": ev}, NOW)


def test_today_without_trades():
    conn = sqlite3.connect(":memory:")
    assert answer("/today", "", conn, {}, NOW) == "還沒有交易紀錄。"


def test_account_shows_max_stop_per_size():
    from copilot.notifications.commands import sizing_table
    txt = sizing_table(116.33, {"MGC": 10.0}, 1.34, (1, 10))
    # 1 contract: (116.33 - 1.34) / 10 = 11.5 pts; 5 contracts: (23.27 - 1.34) / 10 = 2.2 pts
    assert "MGC（每點 $10）：1 口 ≤ 11.5 點" in txt and "5 口 ≤ 2.2 點" in txt and "10 口 ≤ 1.0 點" in txt
    assert sizing_table(None, {"MGC": 10.0}, 1.34, (1, 10)) == ""          # unknown cap -> no table, no guess
