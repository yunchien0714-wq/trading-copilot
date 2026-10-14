import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

from copilot.database.store import connect
from copilot.session_engine.sessions import ET

DAY0 = int(datetime(2026, 9, 13, 18, 0, tzinfo=ET).timestamp())      # Sunday open -> trading day Mon 09-14


def fill(conn, start, minutes, sym="COMEX_MINI:MGC1!"):
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [(sym, "1", start + 60 * i, 1, 1, 1, 1, 1, "t", 0, "REALTIME") for i in range(minutes)])
    conn.commit()


def context5(conn, sym="COMEX_MINI:MGC1!"):
    conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [(sym, "5", DAY0 - 300 * (i + 1), 1, 1, 1, 1, 1, "t", 0, "REALTIME") for i in range(2500)])


def day_open(k):
    """18:00 ET opening trading day k (0 = Mon 09-14), skipping weekends."""
    d = DAY0 + 86400 * (k + 2 * (k // 5))
    return d


def test_first_day_is_kept_when_it_starts_at_the_open(tmp_path):
    from copilot.__main__ import research_days
    c = connect(tmp_path / "a.sqlite3")
    for k in range(3):
        fill(c, day_open(k), 1380)
    now = day_open(3) + 3600                                     # day 3 still forming
    assert len(research_days(c, "MGC", now)) == 0                # no 5m context before the days -> not usable
    context5(c)
    assert len(research_days(c, "MGC", now)) == 3
    c2 = connect(tmp_path / "b.sqlite3")
    context5(c2)
    fill(c2, day_open(0) + 600 * 60, 780)                        # history started mid-session
    for k in (1, 2):
        fill(c2, day_open(k), 1380)
    assert len(research_days(c2, "MGC", now)) == 2


def test_missing_days_lists_incomplete_weekdays(tmp_path):
    from copilot.monitor import missing_days
    c = connect(tmp_path / "a.sqlite3")
    for k in range(5):
        fill(c, day_open(k), 700 if k == 2 else 1380)
    gaps = missing_days(c)
    assert gaps == [("2026-09-16", 700)]


def test_sample_progress_lines():
    from copilot.research.auto import ensure_schema, generate, sample_progress
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    generate(c, 100)
    c.executescript("CREATE TABLE IF NOT EXISTS research_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);")
    c.execute("INSERT INTO research_state VALUES ('baseline', ?)",
              (json.dumps({"days": 20, "in_n": 46, "in_exp": -0.2, "out_n": 26, "out_exp": 0.2, "at": 1}),))
    c.execute("UPDATE hypotheses SET last_tested_at=1, results=? WHERE id='G:target=3R'",
              (json.dumps({"out_sample": {"n": 31, "expectancy": 0.1}}),))
    txt = "\n".join(sample_progress(c, [("2026-09-16", 700)]))
    assert "資料 20 個交易日" in txt and "目前策略樣本外 26 筆" in txt and "已達 30 筆的假設：1 / 1" in txt
    assert "2026-09-16（700 根）" in txt


def test_history_fill_runs_once_per_day_only_when_closed(tmp_path, monkeypatch):
    import copilot.monitor as m
    monkeypatch.setattr(m, "HISTORY_FILL_MARKER", tmp_path / "marker.txt")
    calls = []
    monkeypatch.setattr(m, "ensure_history", lambda ev, t: calls.append(t) or [{"resolution": "1", "bars": 20700}])
    monkeypatch.setattr(m, "read_panes", lambda ev, n: [])
    monkeypatch.setattr(m, "secondary_targets", lambda: [])
    c = connect(tmp_path / "a.sqlite3")
    st = m.LoopState()
    sat = int(datetime(2026, 10, 10, 12, 0, tzinfo=ET).timestamp())
    mon = int(datetime(2026, 10, 12, 10, 0, tzinfo=ET).timestamp())
    cdp = SimpleNamespace(evaluate=lambda js, await_promise=False: None)
    m.history_fill(cdp, c, st, mon)                              # market open: never
    m.history_fill(cdp, c, st, sat)
    m.history_fill(cdp, c, st, sat + 600)                        # same day: once
    assert len(calls) == 1 and calls[0]["1"] >= 20700


def test_main_chart_is_the_configured_layout_even_when_another_tab_is_first(monkeypatch):
    import pytest
    import copilot.tradingview.cdp as cdp
    tabs = [{"url": "https://tw.tradingview.com/chart/MNQtab/"}, {"url": "https://tw.tradingview.com/chart/MGCtab/"}]
    monkeypatch.setattr(cdp, "chart_targets", lambda *a, **k: tabs)
    monkeypatch.setattr(cdp, "_primary_layout", lambda: "MGCtab")
    assert cdp.find_chart_target()["url"].endswith("/MGCtab/")
    assert [t["url"] for t in cdp.secondary_targets()] == [tabs[0]["url"]]
    monkeypatch.setattr(cdp, "chart_targets", lambda *a, **k: tabs[:1])
    with pytest.raises(cdp.TradingViewUnavailable):            # never silently watch the wrong instrument
        cdp.find_chart_target()
    monkeypatch.setattr(cdp, "_primary_layout", lambda: None)
    assert cdp.secondary_targets() == [] and cdp.find_chart_target()["url"].endswith("/MNQtab/")
