import sqlite3

import pytest

from copilot.trade_journal.tradovate_csv import auto_import, explain_gap, import_file, parse

HEADER = ("symbol,_priceFormat,_priceFormatType,_tickSize,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,"
          "boughtTimestamp,soldTimestamp,duration\n")
# same shape as a real export: a long, a short, and a short entered in two lots and closed by one fill
ROWS = [
    "MGCZ6,-1,0,0.1,11,12,4,4287.9,4282.0,$(236.00),10/05/2026 10:15:12,10/05/2026 10:34:57,19min 44sec",
    "MGCZ6,-1,0,0.1,21,22,4,4276.0,4282.1,$244.00,10/05/2026 10:49:09,10/05/2026 10:40:29,8min 39sec",
    "MGCZ6,-1,0,0.1,31,32,2,4264.5,4263.7,$(16.00),10/05/2026 13:12:25,10/05/2026 13:11:25,1min ",
    "MGCZ6,-1,0,0.1,31,33,2,4264.5,4263.7,$(16.00),10/05/2026 13:12:25,10/05/2026 13:11:06,1min 19sec",
]


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "Performance.csv"
    p.write_text(HEADER + "\n".join(ROWS) + "\n", encoding="utf-8")
    return p


def test_parse_groups_split_fills_into_one_trade(csv_file):
    t = parse(csv_file, "UTC")
    assert [(x.direction, x.qty, x.pnl_gross) for x in t] == [("LONG", 4, -236), ("SHORT", 4, 244), ("SHORT", 4, -32)]
    assert t[2].entry == pytest.approx(4263.7) and t[2].exit == pytest.approx(4264.5)
    assert t[1].entry_time < t[1].exit_time                      # short: opened by the sell
    from datetime import datetime, timezone
    assert t[0].entry_time == int(datetime(2026, 10, 5, 10, 15, 12, tzinfo=timezone.utc).timestamp())  # 10:15:12 UTC


def test_import_is_idempotent_and_net_of_commission(csv_file):
    conn = sqlite3.connect(":memory:")
    ids = import_file(conn, csv_file, "UTC", "ACC", 1.34)
    assert len(ids) == 3 and import_file(conn, csv_file, "UTC", "ACC", 1.34) == []
    net = [r[0] for r in conn.execute("SELECT pnl_usd FROM trades ORDER BY entry_time")]
    assert net == pytest.approx([-236 - 5.36, 244 - 5.36, -32 - 5.36])
    assert sum(net) == pytest.approx(-40.08)


def test_wrong_file_is_rejected(tmp_path):
    p = tmp_path / "Performance.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse(p, "UTC")


def test_explain_gap_reconciles_balance_change(csv_file):
    conn = sqlite3.connect(":memory:")
    import_file(conn, csv_file, "UTC", "ACC", 1.34)
    conn.executescript("CREATE TABLE IF NOT EXISTS chart_snapshots (feed_symbol TEXT, point_value REAL, fetched_at INT);"
                       "CREATE TABLE IF NOT EXISTS bars (feed_symbol TEXT, resolution TEXT, time INT, open REAL,"
                       " high REAL, low REAL, close REAL, volume REAL)")
    t0, t1 = 1791188800, 1791208800                 # covers all three exits (10:34-13:12 UTC)
    ok, peak, why = explain_gap(conn, t0, t1, 25000.0, 24959.92)
    assert ok and peak is None                      # reconciled; no bars stored here -> peak UNKNOWN (no guess)
    ok, _, why = explain_gap(conn, t0, t1, 25000.0, 24900.00)
    assert not ok and "trades missing" in why


def test_auto_import_once_per_file_version(csv_file):
    conn = sqlite3.connect(":memory:")
    first = auto_import(conn, [str(csv_file.parent)], "UTC", "ACC", 1.34, 1)
    assert len(first) == 1 and len(first[0][1]) == 3
    assert auto_import(conn, [str(csv_file.parent)], "UTC", "ACC", 1.34, 2) == []
