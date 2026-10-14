import sqlite3

from copilot.trade_journal.tilt import check, detect, message

CFG = {"personal": {"max_trades_per_day": 5, "tilt_consecutive_losses": 2, "tilt_reentry_minutes": 10,
                    "tilt_warn_last_trade": True, "tilt_warn_switch_after_loss": True}}
Z, NQ = "COMEX_MINI:MGCZ2026", "CME_MINI:MNQZ2026"
# a sample night of trades: id, symbol, entry, exit, net P&L
NIGHT = [(5, Z, 1788222749, 1788222801, -98.30), (6, Z, 1788232505, 1788235055, -71.20),
         (7, Z, 1788239342, 1788239821, -149.90), (8, Z, 1788240963, 1788241025, -35.40),
         (10, NQ, 1788241646, 1788241907, -104.60), (9, Z, 1788242183, 1788242184, -49.80)]


def _db(rows):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, qty REAL, entry_time INT,"
              " exit_time INT, pnl_usd REAL, status TEXT)")
    for tid, sym, et, xt, pnl in rows:
        c.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)", (tid, sym, "SHORT", 5, et, xt, pnl, "CLOSED"))
    return c


def test_last_night_replayed_live_warns_where_it_should():
    c = _db([])
    sent = []
    for tid, sym, et, xt, pnl in NIGHT:                       # as if watched live: entry seen, then exit seen
        c.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)", (tid, sym, "SHORT", 5, et, None, None, "ACTIVE"))
        sent += [(tid, e.kind) for e in check(c, et + 30, CFG)]
        c.execute("UPDATE trades SET exit_time = ?, pnl_usd = ?, status = 'CLOSED' WHERE id = ?", (xt, pnl, tid))
        sent += [(tid, e.kind) for e in check(c, xt + 30, CFG)]
    assert sent == [
        (6, "STREAK"),                       # 2nd loss in a row (23:57)
        (7, "STREAK"),                       # 3rd
        (8, "STREAK"),                       # 4th
        (10, "LAST"), (10, "SWITCH"),        # 5th trade of the day, MGC -> MNQ right after a loss
        (10, "STREAK"),
        (9, "OVER"), (9, "REENTRY"), (9, "SWITCH"),   # 6th trade, 4.6 min after a loss, back to MGC
        (9, "STREAK")]


def test_old_events_are_recorded_silently_and_never_resent():
    c = _db(NIGHT)
    later = NIGHT[-1][3] + 6 * 3600                          # recovered from fills hours later
    assert check(c, later, CFG) == []
    assert c.execute("SELECT COUNT(*), SUM(alerted) FROM tilt_events").fetchone() == (10, 0)
    assert check(c, later + 30, CFG) == []


def test_unknown_pnl_is_not_a_loss_and_a_win_resets_the_streak():
    c = _db([(1, Z, 1000, 1100, -50.0), (2, Z, 5000, 5100, None), (3, Z, 9000, 9100, 20.0), (4, Z, 13000, 13100, -10.0)])
    kinds = [e.kind for e in detect(c, 13200, CFG)]
    assert "STREAK" not in kinds and "REENTRY" not in kinds


def test_message_is_factual():
    c = _db(NIGHT[:2])
    ev = detect(c, NIGHT[1][3] + 10, CFG)
    txt = message(ev, c, NIGHT[1][3] + 10)
    assert "連續虧損 2 筆" in txt and "系統不會動你的單" in txt and "今天：2 筆" in txt
