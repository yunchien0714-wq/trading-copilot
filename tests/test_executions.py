import sqlite3
from datetime import date

from copilot.broker.executions import build_trades, candidate_contracts, reconcile, store
from copilot.trade_monitor.monitor import SCHEMA as TRADES_SCHEMA

S = "COMEX_MINI:MGCZ2026"
# sample fills in the TradingView broker.executions format
FILLS = [("400000000011", S, 1, 4, 4287.9, 1788142512), ("400000000052", S, -1, 4, 4282.0, 1788143697),
        ("400000000061", S, -1, 4, 4282.1, 1788144029), ("400000000080", S, 1, 4, 4276.0, 1788144549),
        ("400000000096", S, -1, 2, 4263.7, 1788153066), ("400000000111", S, -1, 2, 4263.7, 1788153085),
        ("400000000131", S, 1, 4, 4264.5, 1788153145), ("400000000161", S, 1, 1, 4289.3, 1788174098),
        ("400000000142", S, 1, 3, 4289.3, 1788174098), ("400000000172", S, -1, 4, 4283.9, 1788176570)]


def test_sample_fills_rebuild_the_same_trades_and_keys_as_the_csv():
    closed, open_ = build_trades(FILLS)
    assert open_ is None and len(closed) == 4
    assert [t.direction for t in closed] == ["LONG", "SHORT", "SHORT", "LONG"]
    assert closed[0].key == "B400000000011|S400000000052"                       # == CSV import key of trade 2
    assert closed[2].key == "B400000000131|S400000000096|S400000000111"         # == trade 4 (two partial fills)
    t1 = closed[3]
    assert (t1.qty, t1.entry, t1.exit, t1.entry_time, t1.exit_time) == (4, 4289.3, 4283.9, 1788174098, 1788176570)
    assert round(t1.gross(10.0), 2) == -216.0                                   # 5.4 pts x 4 x $10


def test_flip_splits_into_two_trades_and_open_position_is_not_a_trade():
    fills = [("1", S, 1, 2, 100.0, 10), ("2", S, -1, 3, 101.0, 20), ("3", S, 1, 1, 99.0, 30), ("4", S, 1, 1, 98.0, 40)]
    closed, open_ = build_trades(fills)
    assert [(t.direction, t.qty, t.entry, t.exit) for t in closed] == [("LONG", 2, 100.0, 101.0), ("SHORT", 1, 101.0, 99.0)]
    assert open_.direction == "LONG" and open_.qty == 1


def test_candidate_contracts_follow_listed_months():
    assert candidate_contracts(["MGC", "MNQ"], date(2026, 10, 5)) == [
        "COMEX_MINI:MGCV2026", "COMEX_MINI:MGCZ2026", "CME_MINI:MNQZ2026", "CME_MINI:MNQH2027"]
    assert candidate_contracts(["XYZ"], date(2026, 10, 5)) == []


def _db():
    c = sqlite3.connect(":memory:")
    c.executescript(TRADES_SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    for col, typ in (("fill_key", "TEXT"), ("pnl_gross", "FILLS")):
        if col not in cols:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
    c.execute("CREATE TABLE trade_grades (trade_id INTEGER PRIMARY KEY)")
    return c


def _insert(c, **kw):
    row = dict(account="A", symbol=S, direction="LONG", qty=4, entry=4289.3, entry_time=1788174113,
               entry_source="broker position avgPrice; time = first seen by poll", context="{}", status="CLOSED",
               notified="[]", version="v", initial_stop=4283.9, initial_target=4201.9, exit=4283.833,
               exit_time=1788176587, pnl_usd=-218.68, fill_key=None)
    row.update(kw)
    cur = c.execute(f"INSERT INTO trades ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    return cur.lastrowid


def test_reconcile_corrects_live_trade_skips_csv_trades_and_recovers_the_rest():
    c = _db()
    live = _insert(c)                                                           # trade 1 as seen live
    _insert(c, direction="LONG", entry=4287.9, entry_time=1788142512, fill_key="B400000000011|S400000000052",
            entry_source="tradovate_performance_csv (x)")                       # trade 2 from the CSV
    c.execute("INSERT INTO trade_grades VALUES (?)", (live,))
    assert store(c, "A", {S: [dict(id=i, symbol=s, side=sd, qty=q, price=p, time=t * 1000)
                              for i, s, sd, q, p, t in FILLS]}, 1788186000) == 10
    assert store(c, "A", {S: [dict(id=FILLS[0][0], symbol=S, side=1, qty=4, price=4287.9, time=1)]}, 1788186001) == 0
    enriched, recovered = reconcile(c, "A", lambda r: 10.0, 1.34, 1788186000)
    assert enriched == [live] and len(recovered) == 2                          # #3 and #4 were not in the DB
    e = c.execute("SELECT entry, entry_time, exit, exit_time, pnl_usd, r_multiple, fill_key FROM trades WHERE id=?",
                  (live,)).fetchone()
    assert e[:4] == (4289.3, 1788174098, 4283.9, 1788176570)
    assert round(e[4], 2) == -221.36 and round(e[5], 2) == -1.0   # fills + both commission sides
    assert c.execute("SELECT COUNT(*) FROM trade_grades").fetchone()[0] == 0    # re-grade queued
    rec = c.execute("SELECT direction, initial_stop, entry_source FROM trades WHERE id IN (?,?)", recovered).fetchall()
    assert all(r[1] is None and "not seen live" in r[2] for r in rec)
    assert reconcile(c, "A", lambda r: 10.0, 1.34, 1788186100) == ([], [])     # idempotent


def test_open_live_trade_is_not_duplicated_before_it_closes():
    c = _db()
    _insert(c, status="ACTIVE", exit=None, exit_time=None, pnl_usd=None)
    store(c, "A", {S: [dict(id=i, symbol=s, side=sd, qty=q, price=p, time=t * 1000) for i, s, sd, q, p, t in FILLS[7:]]}, 1)
    assert reconcile(c, "A", lambda r: 10.0, 1.34, 1788186000) == ([], [])
