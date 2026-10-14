"""Alert outcome tracking (spec section 26): what WOULD have happened to every confirmed setup, traded or not.

A decision = a setup that produced entry / stop / target at a confirmation close:
  ALERTED   -- TRADE_SETUP sent (passed strategy + risk)
  BLOCKED   -- passed the strategy, stopped by the risk gate
  FILTERED  -- confirmed, but a strategy filter failed (structure, volume support, opposing FVG, RR)
Outcome, walking 1m bars AFTER the confirmation close:
  TARGET first -> WIN (+RR); STOP first -> LOSS (-1R); both inside one bar -> LOSS (order unknown: conservative);
  neither by the end of the trading day -> TIMEOUT, marked to the last close (R can be fractional).
Classification:
  ALERTED/BLOCKED + WIN  -> CORRECT_ALERT         ALERTED/BLOCKED + LOSS -> FALSE_POSITIVE
  FILTERED + LOSS        -> FILTERED_CORRECTLY    FILTERED + WIN         -> FILTERED_INCORRECTLY (missed)
The decision's conditions are kept, so "why did it work / fail" can be analysed across many alerts later.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..market_data.models import Bar
from ..session_engine.sessions import trading_day
from ..trade_monitor.monitor import root_of

OUTCOME_VERSION = "alert_outcomes-0.1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_outcomes (
    key TEXT NOT NULL, source TEXT NOT NULL,            -- live | replay
    symbol TEXT NOT NULL, direction TEXT NOT NULL, decision TEXT NOT NULL, filtered_by TEXT,
    decided_at INTEGER NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL, rr REAL,
    conditions TEXT NOT NULL, poc_sources TEXT,
    outcome TEXT, r REAL, resolved_at INTEGER, mfe_r REAL, mae_r REAL, classification TEXT,
    user_traded INTEGER, version TEXT NOT NULL,
    PRIMARY KEY (key, source)
);
"""


@dataclass
class Resolution:
    outcome: str          # WIN | LOSS | TIMEOUT | PENDING
    r: float | None
    resolved_at: int | None
    mfe_r: float
    mae_r: float


def resolve(direction: str, entry: float, stop: float, target: float, decided_at: int, bars: list[Bar],
            now: int) -> Resolution:
    """Walk closed 1m bars that OPEN at/after the decision (the confirmation bar's close)."""
    long = direction == "LONG"
    risk = abs(entry - stop)
    if risk <= 0:
        return Resolution("PENDING", None, None, 0.0, 0.0)
    day = trading_day(decided_at)
    mfe = mae = 0.0
    last = None
    for b in bars:
        if b.time < decided_at:
            continue
        if trading_day(b.time) != day:
            break
        last = b
        fav = (b.high - entry) if long else (entry - b.low)
        adv = (entry - b.low) if long else (b.high - entry)
        mfe, mae = max(mfe, fav / risk), max(mae, adv / risk)
        hit_t = b.high >= target if long else b.low <= target
        hit_s = b.low <= stop if long else b.high >= stop
        if hit_s:                                   # includes "both in one bar" -> conservative LOSS
            return Resolution("LOSS", -1.0, b.time + 60, mfe, mae)
        if hit_t:
            return Resolution("WIN", round(abs(target - entry) / risk, 2), b.time + 60, mfe, mae)
    if last is not None and trading_day(now) != day:
        r = ((last.close - entry) if long else (entry - last.close)) / risk
        return Resolution("TIMEOUT", round(r, 2), last.time + 60, mfe, mae)
    return Resolution("PENDING", None, None, mfe, mae)


def classify(decision: str, outcome: str, r: float | None) -> str | None:
    if outcome == "PENDING":
        return None
    good = outcome == "WIN" or (outcome == "TIMEOUT" and (r or 0) > 0)
    if decision in ("ALERTED", "BLOCKED", "QUALIFIED_UNGATED"):
        return "CORRECT_ALERT" if good else "FALSE_POSITIVE"
    if decision == "UNCONFIRMED":                 # hypothetical entry at the first zone touch, no confirmation
        return "MISSED_BY_CONFIRMATION" if good else "SAVED_BY_CONFIRMATION"
    return "FILTERED_INCORRECTLY" if good else "FILTERED_CORRECTLY"


def decision_of(state: str, risk_passed, invalidation: str | None) -> tuple[str, str | None] | None:
    if state == "QUALIFIED":
        if risk_passed is None:          # replay: the historical account / risk state is unknown
            return "QUALIFIED_UNGATED", None
        return ("ALERTED", None) if risk_passed else ("BLOCKED", "risk gate")
    if state == "INVALIDATED" and invalidation and invalidation.startswith("confirmed but failed:"):
        return "FILTERED", invalidation.split(":", 1)[1].strip()
    return None


def record_decision(conn: sqlite3.Connection, key: str, source: str, symbol: str, direction: str, decision: str,
                    filtered_by: str | None, decided_at: int, entry: float, stop: float, target: float, rr,
                    conditions: dict | str, poc_sources) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO alert_outcomes (key, source, symbol, direction, decision, filtered_by, decided_at,"
                 " entry, stop, target, rr, conditions, poc_sources, version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (key, source, symbol, direction, decision, filtered_by, decided_at, entry, stop, target, rr,
                  conditions if isinstance(conditions, str) else json.dumps(conditions),
                  poc_sources if isinstance(poc_sources, str) else json.dumps(poc_sources), OUTCOME_VERSION))


TOUCH_NOTE = ("hypothetical: entry at the zone's near edge on the first touch, stop at the far edge, 2R target "
              "(never confirmed -- measures what the confirmation rule did)")


def decisions_from(src: sqlite3.Connection, risk_known: bool) -> list[tuple]:
    """Every setup that reached a final state -> (row, decision, filtered_by, decided_at, entry, stop, target, rr,
    conditions). Confirmed setups keep their real prices; setups that touched the zone but never confirmed
    (EXPIRED, or closed beyond the zone first) get the hypothetical touch entry, so no potential setup is lost
    (record every potential setup to learn which rules are wrong)."""
    out = []
    for s in src.execute("SELECT * FROM setups").fetchall():
        if s["entry"] is not None and s["stop"] is not None and s["confirmed_at"] is not None:
            d = decision_of(s["state"], s["risk_passed"] if risk_known or s["state"] != "QUALIFIED" else None,
                            s["invalidation"])
            if d:
                out.append((s, d[0], d[1], s["confirmed_at"] + 60, s["entry"], s["stop"], s["target"], s["rr"],
                            s["conditions"]))
            continue
        if s["state"] not in ("EXPIRED", "INVALIDATED"):
            continue                                       # still live -> decided later
        touch = src.execute("SELECT MIN(at) FROM setup_transitions WHERE key = ? AND to_state IN"
                            " ('POTENTIAL_SETUP', 'CONFIRMING')", (s["key"],)).fetchone()[0]
        if touch is None:
            continue                                       # price never reached the zone: nothing to measure
        long = s["direction"] == "LONG"
        entry, stop = (s["zone_high"], s["zone_low"]) if long else (s["zone_low"], s["zone_high"])
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        why = ("expired: no 1m confirmation" if s["state"] == "EXPIRED"
               else "closed beyond the zone before any confirmation")
        cond = json.loads(s["conditions"]) if s["conditions"] else {}
        cond["_model"] = TOUCH_NOTE
        out.append((s, "UNCONFIRMED", why, touch, entry, stop, entry + 2 * risk if long else entry - 2 * risk,
                    2.0, json.dumps(cond)))
    return out


def sync_live_decisions(conn: sqlite3.Connection) -> int:
    """Copy confirmed decisions from the live tracker's setups table."""
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT name FROM sqlite_master WHERE name='setups'").fetchone():
        return 0
    n = 0
    prev, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        rows = decisions_from(conn, risk_known=True)
    finally:
        conn.row_factory = prev
    for s, dec, why, at, entry, stop, target, rr, cond in rows:
        before = conn.total_changes
        record_decision(conn, s["key"], "live", s["symbol"], s["direction"], dec, why, at, entry, stop, target, rr,
                        cond, s["poc_sources"])
        n += conn.total_changes - before
    conn.commit()
    return n


def resolve_pending(conn: sqlite3.Connection, bars_for, now: int) -> list[sqlite3.Row]:
    """bars_for(symbol, t_from, t_to) -> closed 1m bars. Returns rows resolved in this call."""
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    done = []
    for a in conn.execute("SELECT * FROM alert_outcomes WHERE outcome IS NULL OR outcome = 'PENDING'").fetchall():
        res = resolve(a["direction"], a["entry"], a["stop"], a["target"], a["decided_at"],
                      bars_for(a["symbol"], a["decided_at"], a["decided_at"] + 86400), now)
        traded = None
        if conn.execute("SELECT name FROM sqlite_master WHERE name='trades'").fetchone():
            root = root_of(a["symbol"])
            late = 1800 if a["decision"] == "UNCONFIRMED" else 900
            traded = int(any(root_of(r[0]) == root for r in conn.execute(
                "SELECT symbol FROM trades WHERE direction = ? AND entry_time BETWEEN ? AND ?",
                (a["direction"], a["decided_at"] - 300, a["decided_at"] + late))))
        conn.execute("UPDATE alert_outcomes SET outcome=?, r=?, resolved_at=?, mfe_r=?, mae_r=?, classification=?,"
                     " user_traded=? WHERE key=? AND source=?",
                     (res.outcome, res.r, res.resolved_at, round(res.mfe_r, 2), round(res.mae_r, 2),
                      classify(a["decision"], res.outcome, res.r), traded, a["key"], a["source"]))
        if res.outcome != "PENDING":
            done.append(a)
    conn.commit()
    return done


def db_bars(conn: sqlite3.Connection):
    """bars_for() backed by the local bar store (realtime feed preferred over the delayed one)."""
    from ..trade_journal.grader import _bars
    from ..trade_monitor.monitor import root_of
    return lambda symbol, a, b: _bars(conn, root_of(symbol), "1", a, b)


def replay_backfill(conn: sqlite3.Connection, feed_root: str, tick: float, min_rr: float, days: list) -> int:
    """Re-run the live engines over stored bars, bar by bar with only data known at each close, and record every
    confirmed decision with source='replay'. Risk state is not reconstructed (QUALIFIED_UNGATED)."""
    from types import SimpleNamespace
    from ..market_data.series import atr as atr_series
    from ..market_data.rolls import boundaries as roll_boundaries
    from ..market_structure.analysis import analyze_timeframe
    from ..setup_engine.poc_fvg import evaluate_all
    from ..setup_engine.tracker import SetupTracker
    from ..trade_journal.grader import _bars
    from ..volume.profile import ProfileParams, build_profile, fixed_segment_pocs
    from ..session_engine.sessions import trading_day_start

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    tracker = SetupTracker(mem, feed_root)
    gate = lambda ev: SimpleNamespace(passed=True, rules=[], failures=[], version="replay-ungated")
    for day in days:
        from datetime import datetime
        from ..session_engine.sessions import ET
        start = trading_day_start(int(datetime(day.year, day.month, day.day, 12, tzinfo=ET).timestamp()))
        b1 = _bars(conn, feed_root, "1", start - 600 * 60, start + 23 * 3600)
        b5 = _bars(conn, feed_root, "5", start - 2600 * 300, start + 23 * 3600)
        cache = {}
        for i, b in enumerate(b1):
            if b.time < start:
                continue
            t_close = b.time + 60
            k5 = sum(1 for x in b5 if x.time + 300 <= t_close)
            if k5 not in cache:
                hist = b5[max(0, k5 - 2500):k5]
                prof = build_profile(hist, ProfileParams())
                if prof is not None:
                    prof.fixed_segment_pocs = fixed_segment_pocs(hist, 300, tick, atr_series(hist))
                cache = {k5: (analyze_timeframe(hist, "5", roll_times=roll_boundaries(conn, feed_root)), prof)}
            a5, prof = cache[k5]
            tracker.update(evaluate_all(a5, prof, b1[max(0, i - 600):i + 1], tick, min_rr), t_close, gate)
    n = 0
    for s, dec, why, at, entry, stop, target, rr, cond in decisions_from(mem, risk_known=False):
        before = conn.total_changes
        record_decision(conn, s["key"], "replay", f"{feed_root}", s["direction"], dec, why, at, entry, stop, target,
                        rr, cond, s["poc_sources"])
        n += conn.total_changes - before
    conn.commit()
    return n


def daily_summary(conn: sqlite3.Connection, day, start: int, end: int) -> str:
    """One end-of-day message: alert outcomes + trades + grades (instead of a message per resolution)."""
    from ..session_engine.sessions import to_et
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT decision, outcome, r, classification FROM alert_outcomes WHERE source='live'"
                        " AND decided_at >= ? AND decided_at < ?", (start, end)).fetchall()
    cls_zh = {"CORRECT_ALERT": "提醒正確", "FALSE_POSITIVE": "提醒錯誤", "FILTERED_CORRECTLY": "過濾正確",
              "FILTERED_INCORRECTLY": "過濾錯誤（擋掉了會賺的）", "MISSED_BY_CONFIRMATION": "沒等到確認、錯過會賺的",
              "SAVED_BY_CONFIRMATION": "沒等到確認、避開會賠的", "PENDING": "結果未定"}
    lines = [f"每日總結 {day}", ""]
    if rows:
        from collections import Counter
        cls = Counter(r[3] or "PENDING" for r in rows)
        lines += [f"今天的潛在 setup：{len(rows)} 個"] + [f"- {cls_zh.get(k, k)}：{v}" for k, v in sorted(cls.items())]
        alerted = [r for r in rows if r[0] == "ALERTED" and r[2] is not None]
        if alerted:
            lines.append(f"系統發出的交易提醒，如果都照做合計 {sum(r[2] for r in alerted):+.2f}R（未扣手續費）")
    else:
        lines.append("今天沒有確認的 setup（沒交易也是正常的結果）。")
    has = lambda name: conn.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone()
    if has("trades") and has("trade_grades"):
        tr = conn.execute("SELECT t.id, t.direction, t.pnl_usd, g.grade FROM trades t LEFT JOIN trade_grades g"
                          " ON g.trade_id = t.id WHERE t.entry_time >= ? AND t.entry_time < ? ORDER BY t.entry_time",
                          (start, end)).fetchall()
        if tr:
            side = {"LONG": "多", "SHORT": "空"}
            net = sum(r[2] for r in tr if r[2] is not None)
            lines += ["", f"你的交易：{len(tr)} 筆，淨損益 ${net:.2f}"]
            lines += [f"- #{r[0]} {side.get(r[1], r[1])} {f'${r[2]:.2f}' if r[2] is not None else '?'} 評級 {r[3] or '?'}"
                      for r in tr]
    return "\n".join(lines)
