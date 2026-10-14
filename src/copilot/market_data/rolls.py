"""Contract rolls of the continuous chart symbols (e.g. COMEX_MINI:MGC1!).

Facts :
* Both chart panes have back-adjustment OFF -> a roll does NOT rewrite history; the continuous series simply switches
  to the next contract, which leaves a price jump (the calendar spread) at the switch.
* TradingView switches when the next contract's daily volume exceeds the current one (not on a fixed date), so the
  roll is detected, never predicted: the chart's symbolInfo().front_contract changes.

The roll boundary is INFERRED as the start of the trading day (18:00 ET) on which the change is first seen, because
TradingView's volume rule is evaluated per day. Consequences handled elsewhere:
* FVGs whose three candles straddle a boundary are artifacts of the jump (status ROLL_GAP) -> never a setup.
* A trade in a contract other than the chart's front contract at that time cannot be compared with chart prices.
"""
from __future__ import annotations

import sqlite3

from ..session_engine.sessions import trading_day_start

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_rolls (
    root TEXT NOT NULL, contract TEXT NOT NULL, first_seen_at INTEGER NOT NULL,
    boundary INTEGER,               -- NULL for the first contract ever seen (its start is unknown)
    boundary_source TEXT NOT NULL,
    PRIMARY KEY (root, contract)
);
"""

FRONT_JS = """
(function() {
  var out = [];
  window.TradingViewApi._chartWidgetCollection.getAll().forEach(function(w) {
    var si = w.model().mainSeries().symbolInfo();
    if (si && si.front_contract && si.root) out.push([si.root, si.full_name.split(':')[0] + ':' + si.front_contract]);
  });
  return out;
})()
"""


def observe(conn: sqlite3.Connection, root: str, contract: str, now: int) -> tuple[str, str, int] | None:
    """Record the chart's front contract. Returns (old, new, boundary) when it changed, else None."""
    conn.executescript(SCHEMA)
    last = conn.execute("SELECT contract FROM contract_rolls WHERE root = ? ORDER BY first_seen_at DESC LIMIT 1",
                        (root,)).fetchone()
    if last is not None and last[0] == contract:
        return None
    if conn.execute("SELECT 1 FROM contract_rolls WHERE root = ? AND contract = ?", (root, contract)).fetchone():
        return None                                   # seen before (e.g. a pane briefly on an old symbol)
    if last is None:
        conn.execute("INSERT INTO contract_rolls VALUES (?,?,?,NULL,?)",
                     (root, contract, now, "first contract observed; its start is UNKNOWN"))
        conn.commit()
        return None
    boundary = trading_day_start(now)
    conn.execute("INSERT INTO contract_rolls VALUES (?,?,?,?,?)",
                 (root, contract, now, boundary, "INFERRED: start of the trading day the change was first seen"))
    conn.commit()
    return last[0], contract, boundary


def boundaries(conn: sqlite3.Connection, root: str) -> list[int]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='contract_rolls'").fetchone():
        return []
    return [r[0] for r in conn.execute("SELECT boundary FROM contract_rolls WHERE root = ? AND boundary IS NOT NULL"
                                       " ORDER BY boundary", (root,))]


def front_at(conn: sqlite3.Connection, root: str, t: int) -> str | None:
    """The chart's front contract at time t, or None if t is before anything was observed (UNKNOWN)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='contract_rolls'").fetchone():
        return None
    rows = conn.execute("SELECT contract, boundary, first_seen_at FROM contract_rolls WHERE root = ?"
                        " ORDER BY first_seen_at", (root,)).fetchall()
    cur = None
    for contract, boundary, first_seen in rows:
        start = boundary if boundary is not None else first_seen
        if t >= start:
            cur = contract
    return cur


def mark_roll_gaps(fvgs: list, bars: list, roll_times: list[int]) -> int:
    """Flag FVGs formed across a roll boundary (the jump between two contracts, not a real imbalance)."""
    if not roll_times:
        return 0
    n = 0
    for g in fvgs:
        if g.index < 2 or g.index >= len(bars):
            continue
        t0, t2 = bars[g.index - 2].time, bars[g.index].time
        if any(t0 < b <= t2 for b in roll_times):
            g.status = "ROLL_GAP"
            n += 1
    return n


def spans_roll(t_from: int, t_to: int, roll_times: list[int]) -> bool:
    return any(t_from < b <= t_to for b in roll_times)
