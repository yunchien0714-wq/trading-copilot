import json
import sqlite3

from copilot.alert_tracking.outcomes import SCHEMA as ALERT_SCHEMA
from copilot.broker.tradingview_broker import SCHEMA as BROKER_SCHEMA
from copilot.reports.dashboard import build
from copilot.trade_journal.grader import SCHEMA as GRADE_SCHEMA

CFG = {"account": {"size_usd": 25000, "profit_target_usd": 1500, "drawdown": {"max_drawdown_usd": 1000}}}
T0 = 1791198000


def db():
    c = sqlite3.connect(":memory:")
    c.executescript(BROKER_SCHEMA + GRADE_SCHEMA + ALERT_SCHEMA)
    c.executescript("""CREATE TABLE trades (id INTEGER PRIMARY KEY, account TEXT, symbol TEXT, direction TEXT, qty REAL,
        entry REAL, entry_time INTEGER, entry_source TEXT, initial_stop REAL, initial_target REAL, brackets_source TEXT,
        balance_before REAL, context TEXT, mfe_points REAL, mae_points REAL, last_price REAL, status TEXT, exit REAL,
        exit_time INTEGER, exit_source TEXT, pnl_usd REAL, r_multiple REAL, notified TEXT, version TEXT,
        fill_key TEXT, pnl_gross REAL)""")
    return c


def test_untouched_account_buffer_and_cap(tmp_path):
    c = db()
    c.executemany("INSERT INTO equity_marks VALUES (?,?,?,?,?)",
                  [(T0, "A", 25000, 25000, "s"), (T0 + 60, "A", 25000, 25200, "s"), (T0 + 120, "A", 24900, 24900, "s")])
    out = build(c, CFG, tmp_path / "none.toml", T0 + 200)
    # high-water 25200 -> threshold 24200 -> buffer 700 -> per-trade cap 175
    assert "$24,200.00" in out and "$700.00" in out and "$175.00" in out
    assert "<svg" in out


def test_unknown_high_water_is_shown_not_guessed(tmp_path):
    c = db()
    c.execute("INSERT INTO equity_marks VALUES (?,?,?,?,?)", (T0, "A", 24700, 24700, "s"))
    out = build(c, CFG, tmp_path / "none.toml", T0)
    assert "UNKNOWN" in out and "not an untouched account" in out


def test_trades_grades_and_mistakes(tmp_path):
    c = db()
    c.execute("INSERT INTO equity_marks VALUES (?,?,?,?,?)", (T0, "A", 25000, 25000, "s"))
    c.execute("INSERT INTO trades (id, account, direction, qty, entry, entry_time, status, exit, pnl_usd, r_multiple) "
              "VALUES (1,'A','LONG',5,100,?, 'CLOSED', 99, -200, -1.0)", (T0,))
    c.execute("INSERT INTO trades (id, account, direction, qty, entry, entry_time, status, exit, pnl_usd, r_multiple) "
              "VALUES (2,'OLD','LONG',5,100,?, 'CLOSED', 90, -900, -9.0)", (T0 - 9000,))   # other account: excluded
    c.execute("INSERT INTO trade_grades VALUES (1,?,?,0,'GOOD','LOSS','Good trade / Bad outcome',?,'[]','[]','','{}','v')",
              (T0, "B", json.dumps([["risk", "loss cap", "FAIL", "x"], ["strategy", "structure", "PASS", "y"]])))
    out = build(c, CFG, tmp_path / "none.toml", T0)
    assert "Good trade / Bad outcome" in out and "loss cap" in out and "structure" not in out.split("重複出現的錯誤")[1].split("系統訊號")[0]
    assert "只有 1 筆交易" in out
