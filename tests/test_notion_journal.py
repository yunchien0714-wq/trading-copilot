import json
import sqlite3

from copilot.event_engine.calendar import Event
from copilot.trade_journal import zh
from copilot.trade_journal.grader import SCHEMA as GRADE_SCHEMA, grade_trade, save
from copilot.trade_journal.notion_sync import USER_FIELDS, blocks, properties, sync

from test_grader import CFG, T, trade


def _all_grades():
    """Every improvement / lesson branch the grader can produce in tests."""
    ev = [Event("1", "CPI", "US", 1, T + 600, "BLS")]
    cases = [dict(), dict(qty=7), dict(qty=1, initial_stop=4160.0, initial_target=4250.0), dict(initial_target=4191.3),
             dict(qty=10, initial_stop=4183.3), dict(exit=4186.3, pnl_usd=-188.0),
             dict(entry_source="tradovate_performance_csv (x)", initial_stop=None, initial_target=None)]
    out = []
    for kw in cases:
        conn, t = trade(**kw)
        cap = (200.0, 260.0) if kw.get("qty") == 7 else 250.0
        out.append(grade_trade(conn, t, CFG, ev if not kw else [], 10.0, 0, cap))
    return out


def test_every_grader_sentence_has_a_fixed_translation():
    seen = set()
    for g in _all_grades():
        for s in g.improve + [g.lesson]:
            seen.add(s)
            assert zh.sentence(s) != s, f"untranslated: {s}"
    assert len(seen) >= 6


def test_unknown_text_is_kept_not_invented():
    assert zh.sentence("something new the grader says") == "something new the grader says"
    assert zh.sentence("Size 7 was above the recommended 6 for this stop -- size from the stop distance and the loss "
                       "cap before the entry.") == "口數 7 超過這個止損距離的建議口數 6——進場前先用止損距離和虧損上限算口數。"


def _db_with_grade(**kw):
    conn, t = trade(**kw)
    conn.executescript(GRADE_SCHEMA)
    conn.execute("ALTER TABLE t RENAME TO trades")
    conn.execute("ALTER TABLE trades ADD COLUMN r_multiple REAL")
    conn.execute("ALTER TABLE trades ADD COLUMN exit_source TEXT")
    g = grade_trade(conn, t, CFG, [], 10.0, 0, 250.0)
    save(conn, g, T + 1000)
    return conn


class FakeNotion:
    def __init__(self):
        self.calls, self.pages = [], {}

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/pages":
            self.pages["p1"] = body
            return {"id": "p1"}
        if method == "POST" and path.endswith("/query"):
            return {"results": []}
        if method == "GET":
            return {"results": [{"id": "b1"}, {"id": "b2"}], "has_more": False}
        return {}


def test_sync_creates_then_updates_without_touching_user_fields():
    conn = _db_with_grade()
    client = FakeNotion()
    assert sync(conn, client, "DB", T + 2000) == (1, [])
    body = client.pages["p1"]
    assert body["parent"] == {"database_id": "DB"}
    props = body["properties"]
    assert not set(props) & set(USER_FIELDS)
    assert props["評級"] == {"select": {"name": "A"}} and props["方向"] == {"select": {"name": "多"}}
    assert props["帳戶"] == {"select": {"name": "A"}}                       # no label configured -> account id
    assert props["交易"]["title"][0]["text"]["content"].startswith("#1 MGC 多")
    assert sync(conn, client, "DB", T + 2100) == (0, [])                       # nothing new -> no calls
    n = len(client.calls)
    conn.execute("UPDATE trade_grades SET graded_at = graded_at + 50")         # re-grade
    assert sync(conn, client, "DB", T + 2200) == (1, [])
    new = client.calls[n:]
    assert new[0][0] == "PATCH" and new[0][1] == "/pages/p1" and not set(new[0][2]["properties"]) & set(USER_FIELDS)
    assert [c[0] for c in new[1:]] == ["GET", "DELETE", "DELETE", "PATCH"]    # body replaced


def test_page_body_is_chinese_and_complete():
    conn = _db_with_grade(qty=4)
    conn.row_factory = sqlite3.Row
    t = conn.execute("SELECT * FROM trades").fetchone()
    g = conn.execute("SELECT * FROM trade_grades").fetchone()
    text = json.dumps(blocks(t, g), ensure_ascii=False)
    for s in ("檢查項目", "✅ 通過｜風控｜計畫 RR", "口數 vs 建議口數", "可改進", "數據", "目標出場"):
        assert s in text
    assert properties(t, g)["建議口數"] == {"number": 8.0}


class FakeQuery(FakeNotion):
    def __init__(self, pages):
        super().__init__()
        self.query_pages = pages

    def call(self, method, path, body=None):
        if method == "POST" and path.endswith("/query"):
            self.calls.append((method, path, body))
            return {"results": self.query_pages, "has_more": False}
        return super().call(method, path, body)


def _page(tid, reason=None, emo=None):
    return {"id": f"pg{tid}", "last_edited_time": "2026-10-05T15:00:00.000Z",
            "properties": {"交易編號": {"number": tid},
                           "進場理由": {"rich_text": [{"plain_text": reason}] if reason else []},
                           "情緒": {"select": {"name": emo} if emo else None}}}


def test_pull_notes_reads_user_fields_only_and_unfilled_list():
    from copilot.trade_journal.notion_sync import pull_notes, unfilled
    conn = _db_with_grade()
    sync(conn, FakeNotion(), "DB", T + 2000)
    assert [x[0] for x in unfilled(conn, T - 10)] == [1]                     # nothing filled yet
    client = FakeQuery([_page(1, "FVG+POC 拒絕K", "FOMO")])
    assert pull_notes(conn, client, "DB", T + 3000) == 1
    assert all(c[0] == "POST" and c[1].endswith("/query") for c in client.calls)   # read-only
    assert tuple(conn.execute("SELECT entry_reason, emotion FROM trade_notes").fetchone()) == ("FVG+POC 拒絕K", "FOMO")
    assert unfilled(conn, T - 10) == []
    assert pull_notes(conn, client, "DB", T + 3600) == 0                     # unchanged


def test_dashboard_emotion_section(tmp_path):
    from copilot.reports.dashboard import notes_section
    from copilot.trade_journal.notion_sync import pull_notes
    conn = _db_with_grade()
    sync(conn, FakeNotion(), "DB", T + 2000)
    pull_notes(conn, FakeQuery([_page(1, None, "平靜")]), "DB", T + 3000)
    conn.row_factory = sqlite3.Row
    html = notes_section(conn)
    assert "平靜" in html and "1/1 筆已填" in html and "還看不出規律" in html
