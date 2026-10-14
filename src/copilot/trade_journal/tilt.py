"""Emotional-trading (tilt) warnings -- rules from config [personal] tilt_*). Warning only.

Patterns, all within the current trading day (18:00 ET start):
* STREAK   -- the losing streak reached tilt_consecutive_losses (and every further loss in the streak)
* REENTRY  -- a trade started within tilt_reentry_minutes of a losing exit
* SWITCH   -- the trade right after a loss is in a different instrument
* LAST     -- the last allowed trade of the day started (max_trades_per_day); OVER -- a trade beyond it

Facts only: every message states what happened and which of the user's own rules it touches. Events older than
FRESH_SECONDS when first seen (e.g. trades recovered from fills after a disconnect) are recorded without a message.
A trade with unknown P&L is never counted as a loss or a win.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..session_engine.sessions import to_et, trading_day, trading_day_start
from ..trade_monitor.monitor import root_of

TILT_VERSION = "tilt-0.1.0"
FRESH_SECONDS = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS tilt_events (
    key TEXT PRIMARY KEY, trade_id INTEGER, kind TEXT NOT NULL, at INTEGER NOT NULL, detail TEXT NOT NULL,
    alerted INTEGER NOT NULL, recorded_at INTEGER NOT NULL, version TEXT NOT NULL
);
"""


@dataclass
class TiltEvent:
    key: str
    trade_id: int
    kind: str
    at: int
    detail: str


def _hm(t: int) -> str:
    return f"{to_et(t):%H:%M}"


def detect(conn: sqlite3.Connection, now: int, cfg: dict) -> list[TiltEvent]:
    p = cfg.get("personal", {})
    need = int(p.get("tilt_consecutive_losses", 0) or 0)
    reentry = int(p.get("tilt_reentry_minutes", 0) or 0) * 60
    max_n = p.get("max_trades_per_day")
    start = trading_day_start(now)
    day = trading_day(now)
    rows = conn.execute("SELECT id, symbol, direction, qty, entry_time, exit_time, pnl_usd, status FROM trades"
                        " WHERE entry_time >= ? ORDER BY entry_time, id", (start,)).fetchall()
    ev: list[TiltEvent] = []
    # entry-time patterns: compare each trade with the last trade that had CLOSED before it started
    for i, (tid, sym, d, qty, et, xt, pnl, status) in enumerate(rows):
        n = i + 1
        if p.get("tilt_warn_last_trade") and isinstance(max_n, int):
            if n == max_n:
                ev.append(TiltEvent(f"LAST:{day}", tid, "LAST", et, f"第 {n} 筆：今天最後一筆（你的上限是每天 {max_n} 筆）"))
            elif n > max_n:
                ev.append(TiltEvent(f"OVER:{tid}", tid, "OVER", et, f"第 {n} 筆：超過你每天 {max_n} 筆的上限"))
        prev = [r for r in rows[:i] if r[5] is not None and r[5] <= et]
        if not prev:
            continue
        last = max(prev, key=lambda r: r[5])
        if last[6] is None or last[6] >= 0:
            continue                                         # previous result unknown or not a loss
        gap = et - last[5]
        if reentry and gap <= reentry:
            ev.append(TiltEvent(f"REENTRY:{tid}", tid, "REENTRY", et,
                                f"上一筆虧損 ${-last[6]:.0f} 在 {_hm(last[5])} 出場，{gap // 60} 分鐘後就又進場"
                                f"（你設的冷靜時間是 {reentry // 60} 分鐘）"))
        if p.get("tilt_warn_switch_after_loss") and root_of(sym) != root_of(last[1]):
            ev.append(TiltEvent(f"SWITCH:{tid}", tid, "SWITCH", et,
                                f"上一筆 {root_of(last[1])} 虧損後，換去做 {root_of(sym)}"))
    # losing streak, in exit order
    if need:
        streak, total = 0, 0.0
        for tid, sym, d, qty, et, xt, pnl, status in sorted((r for r in rows if r[5] is not None), key=lambda r: r[5]):
            if pnl is None:
                continue
            if pnl < 0:
                streak, total = streak + 1, total + pnl
                if streak >= need:
                    ev.append(TiltEvent(f"STREAK:{tid}", tid, "STREAK", xt,
                                        f"今天連續虧損 {streak} 筆，合計 −${-total:.0f}"))
            else:
                streak, total = 0, 0.0
    return ev


def check(conn: sqlite3.Connection, now: int, cfg: dict) -> list[TiltEvent]:
    """Record new events; return the fresh ones that should be sent now."""
    conn.executescript(SCHEMA)
    fresh = []
    for e in detect(conn, now, cfg):
        if conn.execute("SELECT 1 FROM tilt_events WHERE key = ?", (e.key,)).fetchone():
            continue
        send = now - e.at <= FRESH_SECONDS
        conn.execute("INSERT INTO tilt_events VALUES (?,?,?,?,?,?,?,?)",
                     (e.key, e.trade_id, e.kind, e.at, e.detail, int(send), now, TILT_VERSION))
        if send:
            fresh.append(e)
    conn.commit()
    return fresh


def message(events: list[TiltEvent], conn: sqlite3.Connection, now: int) -> str:
    start = trading_day_start(now)
    n, pnl = conn.execute("SELECT COUNT(*), SUM(pnl_usd) FROM trades WHERE entry_time >= ?", (start,)).fetchone()
    lines = "\n".join(f"- {e.detail}" for e in events)
    day = f"今天：{n} 筆" + (f"，已知損益 ${pnl:+.0f}" if pnl is not None else "")
    return (f"⚠️ 情緒交易警訊\n\n{lines}\n\n{day}\n\n"
            "這只是提醒，系統不會動你的單。下一筆之前，先確認它是不是你的 setup（5m FVG + POC、"
            "順 5m 結構、等 1m 反應）。")
