import json
import sqlite3

from copilot.research.auto import ensure_schema, generate
from copilot.research.journal import lines, snapshot, sync

NOW = 1791300000


def db():
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    generate(c, NOW - 3 * 86400)
    c.executescript("CREATE TABLE IF NOT EXISTS research_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);")
    c.execute("INSERT INTO research_state VALUES ('baseline', ?)", (json.dumps(
        {"days": 29, "markets": "MGC 20 天＋MNQ 9 天", "in_n": 46, "in_exp": -0.2, "out_n": 26, "out_exp": 0.2}),))
    return c


def mark(c, hid, at, n, exp, verdict="INSUFFICIENT_DATA"):
    c.execute("UPDATE hypotheses SET last_tested_at=?, verdict=?, results=? WHERE id=?",
              (at, verdict, json.dumps({"out_sample": {"n": n, "expectancy": exp}}), hid))


def test_snapshot_covers_everything_since_the_previous_record():
    c = db()
    mark(c, "G:target=3R", NOW - 3 * 86400 - 60, 12, -0.3)        # before Friday's record -> Friday's
    snapshot(c, "2026-10-09", NOW - 3 * 86400, [])                # Friday record
    mark(c, "G:target=1.75R", NOW - 2 * 86400, 31, 0.15)         # Saturday
    mark(c, "G:target=2.5R", NOW - 3600, 8, 0.4)                 # Monday
    d = snapshot(c, "2026-10-12", NOW, [("2026-10-01", 700)])
    labels = [t["label"] for t in d["top"]]
    assert d["tested"] == 2 and "目標：1.75R" in labels             # the weekend run is not lost
    assert labels.index("目標：2.5R") < labels.index("目標：1.75R")   # best out-of-sample first
    assert d["enough"] == 1 and d["data"] == "MGC 20 天＋MNQ 9 天" and d["baseline"]["out_n"] == 26
    txt = "\n".join(lines(d))
    assert "上次紀錄後測了 2 個假設" in txt and "樣本外 26 筆" in txt and "2026-10-01（700 根）" in txt
    assert "目前策略不變" in txt


def test_snapshot_before_research_exists_records_nothing():
    assert snapshot(sqlite3.connect(":memory:"), "2026-10-12", NOW) is None


class FakeNotion:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/pages":
            return {"id": "page-1"}
        if method == "GET":
            return {"results": [], "has_more": False}
        return {}


def test_sync_creates_then_updates_one_page_per_day():
    c = db()
    snapshot(c, "2026-10-12", NOW)
    cl = FakeNotion()
    assert sync(c, cl, "db", NOW + 1) == (1, [])
    assert ("POST", "/pages") in cl.calls
    assert sync(c, cl, "db", NOW + 2) == (0, [])                  # nothing new
    snapshot(c, "2026-10-12", NOW + 10)                            # recorded again -> rewrite the same page
    cl2 = FakeNotion()
    assert sync(c, cl2, "db", NOW + 11) == (1, []) and ("PATCH", "/pages/page-1") in cl2.calls


def test_dashboard_shows_research_log():
    from copilot.reports.dashboard import research_log_section
    c = db()
    mark(c, "G:target=2.5R", NOW - 3600, 8, 0.4)
    snapshot(c, "2026-10-12", NOW)
    h = research_log_section(c)
    assert "研究紀錄" in h and "2026-10-12" in h and "26 筆 / +0.20R" in h and "目標：2.5R" in h


def test_monitor_records_once_per_day_after_the_close(tmp_path, monkeypatch):
    from datetime import datetime
    import copilot.monitor as m
    from copilot.session_engine.sessions import ET
    c = db()
    monkeypatch.setattr(m, "missing_days", lambda conn: [])
    st, cfg = m.LoopState(), {"notion": {}}
    t = lambda h, mi: int(datetime(2026, 10, 10, h, mi, tzinfo=ET).timestamp())   # a Saturday: recorded too
    assert m.research_journal(c, None, st, cfg, t(16, 0)) is False
    assert m.research_journal(c, None, st, cfg, t(17, 11)) is True
    assert m.research_journal(c, None, st, cfg, t(18, 0)) is False
    assert c.execute("SELECT COUNT(*) FROM research_log").fetchone()[0] == 1


def test_a_day_slept_through_is_recorded_on_wake_up(monkeypatch):
    from datetime import datetime
    import copilot.monitor as m
    from copilot.session_engine.sessions import ET
    c = db()
    monkeypatch.setattr(m, "missing_days", lambda conn: [])
    st, cfg = m.LoopState(), {"notion": {}}
    t = lambda d, h: int(datetime(2026, 10, d, h, 15, tzinfo=ET).timestamp())
    assert m.research_journal(c, None, st, cfg, t(12, 17))                  # Mon close recorded
    # asleep over Tuesday's close; wakes Wednesday 05:53 ET -> Tuesday is recorded right away
    assert m.research_journal(c, None, st, cfg, t(14, 5))
    assert m.research_journal(c, None, st, cfg, t(14, 9)) is False
    assert m.research_journal(c, None, st, cfg, t(14, 17))                  # Wednesday as usual
    assert [r[0] for r in c.execute("SELECT day FROM research_log ORDER BY day")] == \
        ["2026-10-12", "2026-10-13", "2026-10-14"]


def test_backup_catches_up_after_sleeping_through_the_close(tmp_path, monkeypatch):
    import os
    from datetime import datetime
    import copilot.monitor as m
    from copilot.session_engine.sessions import ET
    monkeypatch.setattr(m, "ROOT", tmp_path)
    (tmp_path / "data" / "backups").mkdir(parents=True)
    made = []
    monkeypatch.setattr(m, "backup", lambda conn, dest, now: made.append(now) or
                        type("R", (), {"ok": True, "path": dest / "x", "size_mb": 1, "detail": "", "pruned": 0})())
    old = tmp_path / "data" / "backups" / "copilot-20261012-1705.sqlite3"
    old.write_text("x")
    mon = int(datetime(2026, 10, 12, 17, 5, tzinfo=ET).timestamp())
    os.utime(old, (mon, mon))
    m.daily_backup(None, None, mon + 3600 * 12)                             # Tue 05:05, last copy 12 h old: wait
    assert made == []
    m.daily_backup(None, None, int(datetime(2026, 10, 14, 5, 53, tzinfo=ET).timestamp()))   # 37 h -> catch up
    assert len(made) == 1


def test_other_systems_get_their_own_tagged_pages():
    from copilot.research import orb
    from copilot.research.backtest import SimTrade
    c = db()
    v = orb.variants(5)[0]
    orb.record(c, "ORB", v.name, orb.evaluate([SimTrade(date_(), 0, "LONG", 1, 0, 2, "WIN", 1.0)], [date_()]),
               {"markets": {"MGC": 77}, "days": 77, "first": "2026-06-25"}, NOW)
    d = snapshot(c, "2026-10-14", NOW)
    assert list(d["systems"]) == ["開盤區間突破（5 分鐘K線）"]
    from copilot.research.journal import sync_systems
    cl = FakeNotion()
    assert sync_systems(c, cl, "db", NOW + 1) == (1, [])                  # the ORB page, right away
    assert sync_systems(c, cl, "db", NOW + 2) == (0, [])                  # nothing new
    sync(c, cl, "db", NOW + 3)
    assert [m for m, p in cl.calls].count("POST") == 2                    # + the main page
    from copilot.research.journal import _props, _system_props
    assert _props(d)["策略"]["select"]["name"] == "POC/FVG（目前策略）"
    sp = _system_props("2026-10-14", "開盤區間突破（5 分鐘K線）", d, d["systems"]["開盤區間突破（5 分鐘K線）"])
    assert sp["策略"]["select"]["name"] == "開盤區間突破（5 分鐘K線）" and "MGC 77 天" in sp["資料"]["rich_text"][0]["text"]["content"]


def date_():
    from datetime import date
    return date(2026, 10, 1)
