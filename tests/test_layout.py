import sqlite3

from copilot.database.store import SCHEMA_VERSION, connect, save_snapshot
from copilot.tradingview.chart import build_panes_js, check_mtf_layout, parse_snapshot

from test_market_data import raw_snapshot


def pane(idx, res, full="COMEX_MINI_DL:MGC1!", pro="COMEX_MINI:MGC1!"):
    raw = raw_snapshot()
    raw.update(pane_index=idx, resolution=res, symbol=full)
    raw["symbol_info"] = {**raw["symbol_info"], "full_name": full, "pro_name": pro}
    return parse_snapshot(raw)


def test_same_instrument_5m_and_1m_is_ok():
    r = check_mtf_layout([pane(0, "5"), pane(1, "1")])
    assert r["ok"] and r["pairs"] == {"COMEX_MINI:MGC1!": {"5": 0, "1": 1}}


def test_mixed_instruments_are_not_paired():
    # a mixed layout: 5m MGC on the left, 1m NQ on the right
    r = check_mtf_layout([pane(0, "5"), pane(1, "1", "CME_MINI_DL:NQ1!", "CME_MINI:NQ1!")])
    assert not r["ok"] and r["pairs"] == {}
    assert len(r["issues"]) == 2


def test_delayed_and_realtime_feed_of_same_instrument_pair_by_pro_name():
    r = check_mtf_layout([pane(0, "5"), pane(1, "1", full="COMEX_MINI:MGC1!")])
    assert r["pairs"] == {"COMEX_MINI:MGC1!": {"5": 0, "1": 1}}


def test_duplicate_timeframe_flagged():
    r = check_mtf_layout([pane(0, "5"), pane(1, "5")])
    assert not r["ok"] and any("two 5m panes" in i for i in r["issues"])


def test_panes_js_reads_every_widget_and_skips_builtin_markers():
    js = build_panes_js(50)
    assert "_chartWidgetCollection.getAll()" in js and "ESD$" in js and "})(50)" in js


def test_pane_index_persisted(tmp_path):
    conn = connect(tmp_path / "t.sqlite3")
    save_snapshot(conn, pane(1, "1"))
    assert conn.execute("SELECT pane_index FROM chart_snapshots").fetchone()[0] == 1


def test_v1_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE chart_snapshots (id INTEGER PRIMARY KEY, source TEXT NOT NULL, fetched_at INTEGER NOT NULL,
            feed_symbol TEXT NOT NULL, pro_symbol TEXT, resolution TEXT NOT NULL, delay_seconds INTEGER,
            data_quality TEXT NOT NULL, quality_reasons TEXT NOT NULL, min_tick REAL, point_value REAL,
            studies TEXT NOT NULL, bar_count INTEGER NOT NULL, last_bar_time INTEGER, last_close REAL,
            last_bar_closed INTEGER, feature_version TEXT NOT NULL);
    """)
    old.close()
    conn = connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    save_snapshot(conn, pane(0, "5"))
    assert conn.execute("SELECT pane_index FROM chart_snapshots").fetchone()[0] == 0


# --- realtime inferred from measured freshness when the feed reports no delay  ----------------

from copilot.market_data.models import DataQuality
from copilot.tradingview.chart import propagate_inferred_realtime

from test_market_data import NOW, raw_bars


def nodelay(idx, res, last_age, full="COMEX_MINI:MGC1!"):
    step = 60 if res == "1" else 300
    raw = raw_snapshot(delay=None, bars=raw_bars(NOW - last_age - step * 9, 10, step=step))
    raw.update(pane_index=idx, resolution=res, symbol=full)
    raw["symbol_info"] = {**raw["symbol_info"], "full_name": full, "pro_name": "COMEX_MINI:MGC1!", "delay": None}
    return parse_snapshot(raw)


def test_fresh_1m_without_delay_field_is_inferred_realtime():
    p = nodelay(1, "1", last_age=30)
    assert p.data_quality is DataQuality.REALTIME and "INFERRED" in p.quality_reasons[0]


def test_stale_or_delayed_symbol_is_not_inferred():
    assert nodelay(1, "1", last_age=200).data_quality is DataQuality.UNKNOWN
    assert nodelay(1, "1", last_age=30, full="COMEX_MINI_DL:MGC1!").data_quality is DataQuality.UNKNOWN


def test_5m_alone_cannot_prove_realtime_but_inherits_from_same_feed_1m():
    p5 = nodelay(0, "5", last_age=200)
    assert p5.data_quality is DataQuality.UNKNOWN
    panes = propagate_inferred_realtime([p5, nodelay(1, "1", last_age=30)])
    assert [p.data_quality for p in panes] == [DataQuality.REALTIME, DataQuality.REALTIME]


def test_no_inheritance_across_different_feeds():
    panes = propagate_inferred_realtime([nodelay(0, "5", 200, full="CME_MINI:MNQ1!"), nodelay(1, "1", 30)])
    assert panes[0].data_quality is DataQuality.UNKNOWN
