import pytest

from copilot.database.store import connect, save_snapshot
from copilot.market_data.models import DataQuality
from copilot.market_data.quality import assess, find_gaps, mark_closed
from copilot.tradingview.chart import build_snapshot_js, parse_snapshot

NOW = 1_790_896_192  # page clock at capture (seconds)


def raw_bars(start, n, step=300, price=4200.0):
    return [{"time": start + i * step, "open": price, "high": price + 1, "low": price - 1,
             "close": price + 0.5, "volume": 100 + i} for i in range(n)]


def raw_snapshot(delay=600, bars=None, symbol_info=True):
    return {
        "now_ms": NOW * 1000,
        "symbol": "COMEX_MINI_DL:MGC1!",
        "resolution": "5",
        "chart_type": 1,
        "studies": [{"id": "abc", "name": "Some Indicator"}],
        "symbol_info": {
            "full_name": "COMEX_MINI_DL:MGC1!", "pro_name": "COMEX_MINI:MGC1!", "name": "MGC1!",
            "exchange": "COMEX", "type": "futures", "delay": delay, "minmov": 1, "pricescale": 10,
            "pointvalue": 10, "session": "1800-1700", "timezone": "America/New_York", "currency_code": "USD",
        } if symbol_info else None,
        "bars": bars if bars is not None else raw_bars(1_790_895_300 - 300 * 9, 10),
    }


# --- data quality -------------------------------------------------------------

def test_delayed_feed_is_not_live_usable():
    snap = parse_snapshot(raw_snapshot(delay=600))
    assert snap.data_quality is DataQuality.DELAYED
    assert not snap.data_quality.live_usable
    assert any("600s" in r for r in snap.quality_reasons)


def test_missing_symbol_info_is_unknown_not_guessed():
    snap = parse_snapshot(raw_snapshot(symbol_info=False))
    assert snap.data_quality is DataQuality.UNKNOWN
    assert snap.symbol.min_tick is None and snap.symbol.point_value is None
    assert snap.symbol.delay_seconds is None


def test_realtime_when_no_delay_and_current_bar_fresh():
    bars = [b for b in mark_closed(raw_bars(NOW - 300 * 5 - 60, 6), "5", NOW, 0)]
    q, _ = assess(bars, "5", NOW, 0)
    assert q is DataQuality.REALTIME


def test_stale_when_realtime_feed_has_old_last_bar():
    bars = mark_closed(raw_bars(NOW - 3600 * 3, 5), "5", NOW, 0)
    q, reasons = assess(bars, "5", NOW, 0)
    assert q is DataQuality.STALE and reasons


def test_no_bars_is_unknown():
    assert assess([], "5", NOW, 0)[0] is DataQuality.UNKNOWN


def test_unsupported_resolution_rejected():
    with pytest.raises(ValueError):
        mark_closed(raw_bars(NOW, 2), "7", NOW, 0)


# --- closed vs forming --------------------------------------------------------

def test_last_bar_always_forming():
    bars = mark_closed(raw_bars(NOW - 300 * 20, 3), "5", NOW, 0)  # all ancient
    assert [b.is_closed for b in bars] == [True, True, False]


def test_delay_shifts_closed_boundary():
    # bar opened 7 min ago on 5m: closed in realtime terms, but with a 10 min delay its end is still "in the future"
    bars = mark_closed(raw_bars(NOW - 420, 2, step=1), "5", NOW, 600)
    assert not bars[0].is_closed


def test_find_gaps():
    bars = mark_closed([*raw_bars(0, 3), *raw_bars(3600, 2)], "5", 10_000, 0)
    assert find_gaps(bars, "5") == [(600, 3600)]


# --- symbol metadata ----------------------------------------------------------

def test_tick_and_point_value_from_symbol_info():
    s = parse_snapshot(raw_snapshot()).symbol
    assert s.min_tick == pytest.approx(0.1)
    assert s.point_value == 10
    assert s.pro_name == "COMEX_MINI:MGC1!"


def test_snapshot_js_embeds_bar_limit():
    js = build_snapshot_js(123)
    assert "})(123)" in js and "_activeChartWidgetWV" in js


# --- storage ------------------------------------------------------------------

def test_store_only_closed_bars_and_flag_discrepancies(tmp_path):
    conn = connect(tmp_path / "t.sqlite3")
    snap = parse_snapshot(raw_snapshot())
    first = save_snapshot(conn, snap)
    assert first == {"bars_inserted": 9, "discrepancies": 0}  # 10 bars, last one forming

    # Same bars again -> nothing new, nothing flagged
    assert save_snapshot(conn, snap) == {"bars_inserted": 0, "discrepancies": 0}

    # Source later reports a different close for an already-stored closed bar -> flagged, not overwritten
    changed = raw_snapshot()
    changed["bars"][0]["close"] = 9999.0
    result = save_snapshot(conn, parse_snapshot(changed))
    assert result["discrepancies"] == 1
    t0 = changed["bars"][0]["time"]
    assert conn.execute("SELECT close FROM bars WHERE time=?", (t0,)).fetchone()[0] == 4200.5
    assert conn.execute("SELECT COUNT(*) FROM chart_snapshots").fetchone()[0] == 3
    # The same revision seen again on the next poll is not logged twice
    assert save_snapshot(conn, parse_snapshot(changed))["discrepancies"] == 0
    assert conn.execute("SELECT COUNT(*) FROM data_discrepancies").fetchone()[0] == 1
