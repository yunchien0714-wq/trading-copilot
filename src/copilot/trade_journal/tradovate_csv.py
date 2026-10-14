"""Import Tradovate "Performance" CSV exports -- the authoritative fill record (backfill for trades the live
monitor could not see, e.g. while the TradingView broker session was down).

Format (verified on the user's export):
  symbol,_priceFormat,_priceFormatType,_tickSize,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,
  boughtTimestamp,soldTimestamp,duration
  * one row per matched buy/sell lot; a fill split across lots shares its fill id between rows
  * pnl like "$244.00" or "$(236.00)" (gross, before commissions)
  * timestamps "MM/DD/YYYY HH:MM:SS" in the account's display timezone -- set [broker].tradovate_report_timezone, verified by
    matching all six fills to stored 1m bars (5 inside the bar range, 1 off by one tick); config: costs/report tz
Rows connected through a shared fill id form ONE trade (e.g. two 2-lot entries closed by one 4-lot exit).
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..trade_monitor.monitor import SCHEMA as TRADES_SCHEMA, TRADE_MONITOR_VERSION, root_of

RECONCILE_TOL = 0.5   # $; balance change vs sum of imported net P&L must agree within this

SOURCE = "tradovate_performance_csv"
REQUIRED = {"symbol", "buyFillId", "sellFillId", "qty", "buyPrice", "sellPrice", "pnl", "boughtTimestamp", "soldTimestamp"}


@dataclass
class ImportedTrade:
    key: str               # sorted fill ids -> stable identity (re-importing the same file adds nothing)
    symbol: str
    direction: str
    qty: float
    entry: float           # qty-weighted average of the opening fills
    exit: float
    entry_time: int
    exit_time: int
    pnl_gross: float
    fills: list


def _money(s: str) -> float:
    s = s.strip()
    neg = s.startswith("$(") or s.startswith("(") or s.startswith("-")
    v = float(re.sub(r"[^\d.]", "", s))
    return -v if neg else v


def _ts(s: str, tz: ZoneInfo) -> int:
    return int(datetime.strptime(s.strip(), "%m/%d/%Y %H:%M:%S").replace(tzinfo=tz).timestamp())


def parse(path: Path, tz_name: str) -> list[ImportedTrade]:
    tz = ZoneInfo(tz_name)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    missing = REQUIRED - set(rows[0])
    if missing:
        raise ValueError(f"not a Tradovate Performance export (missing columns: {sorted(missing)})")
    # union-find over fill ids
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for r in rows:
        a, b = "B" + r["buyFillId"], "S" + r["sellFillId"]
        parent[find(a)] = find(b)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(find("B" + r["buyFillId"]), []).append(r)
    trades = []
    for rs in groups.values():
        buys = {r["buyFillId"]: (_ts(r["boughtTimestamp"], tz), float(r["buyPrice"])) for r in rs}
        sells = {r["sellFillId"]: (_ts(r["soldTimestamp"], tz), float(r["sellPrice"])) for r in rs}
        long = min(t for t, _ in buys.values()) <= min(t for t, _ in sells.values())
        qty = sum(float(r["qty"]) for r in rs)
        wavg = lambda side: sum(float(r["qty"]) * float(r[side]) for r in rs) / qty
        entry, exit_ = (wavg("buyPrice"), wavg("sellPrice")) if long else (wavg("sellPrice"), wavg("buyPrice"))
        opens, closes = (buys, sells) if long else (sells, buys)
        trades.append(ImportedTrade(
            key="|".join(sorted({"B" + r["buyFillId"] for r in rs} | {"S" + r["sellFillId"] for r in rs})),
            symbol=rs[0]["symbol"], direction="LONG" if long else "SHORT", qty=qty,
            entry=round(entry, 6), exit=round(exit_, 6),
            entry_time=min(t for t, _ in opens.values()), exit_time=max(t for t, _ in closes.values()),
            pnl_gross=sum(_money(r["pnl"]) for r in rs),
            fills=[{k: r[k] for k in ("buyFillId", "sellFillId", "qty", "buyPrice", "sellPrice",
                                      "boughtTimestamp", "soldTimestamp")} for r in rs]))
    return sorted(trades, key=lambda t: t.entry_time)


def import_file(conn: sqlite3.Connection, path: Path, tz_name: str, account: str | None,
                commission_rt: float | None) -> list[int]:
    """Insert trades not seen before (by fill-id key). Returns new trade ids."""
    conn.executescript(TRADES_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "fill_key" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN fill_key TEXT")
        conn.execute("ALTER TABLE trades ADD COLUMN pnl_gross REAL")
    new = []
    for t in parse(path, tz_name):
        if conn.execute("SELECT 1 FROM trades WHERE fill_key = ?", (t.key,)).fetchone():
            continue
        net = t.pnl_gross - commission_rt * t.qty if commission_rt is not None else None
        cur = conn.execute(
            "INSERT INTO trades (account, symbol, direction, qty, entry, entry_time, entry_source, initial_stop,"
            " initial_target, brackets_source, balance_before, context, status, exit, exit_time, exit_source,"
            " pnl_usd, r_multiple, notified, version, fill_key, pnl_gross) VALUES"
            " (?,?,?,?,?,?,?,NULL,NULL,?,NULL,?,?,?,?,?,?,NULL,'[]',?,?,?)",
            (account, t.symbol, t.direction, t.qty, t.entry, t.entry_time, f"{SOURCE} ({path.name})",
             "UNKNOWN (not in the Performance export)", json.dumps({"fills": t.fills, "report_tz": tz_name}),
             "CLOSED", t.exit, t.exit_time, f"{SOURCE} ({path.name})", net, TRADE_MONITOR_VERSION, t.key,
             t.pnl_gross))
        new.append(cur.lastrowid)
    conn.commit()
    return new


def point_value(conn: sqlite3.Connection, root: str) -> float | None:
    """$ per point for a contract root, from TradingView symbolInfo stored with chart snapshots (never memory)."""
    row = conn.execute("SELECT point_value FROM chart_snapshots WHERE (feed_symbol LIKE ? OR feed_symbol LIKE ?)"
                       " AND point_value IS NOT NULL ORDER BY fetched_at DESC LIMIT 1",
                       (f"%:{root}1!", f"%:{root}")).fetchone()
    return float(row[0]) if row else None


def explain_gap(conn: sqlite3.Connection, t0: int, t1: int, b0: float, b1: float) -> tuple[bool, float | None, str]:
    """Do closed trades exiting inside (t0, t1] explain the balance change b0 -> b1?
    Returns (reconciled, conservative_equity_peak, reason). The peak uses 1m bar extremes during each trade (MFE),
    which can only OVERstate the true unrealized peak -> the drawdown buffer is understated (safe side)."""
    if not conn.execute("SELECT name FROM sqlite_master WHERE name='trades'").fetchone():
        return False, None, "no trades recorded"
    trades = conn.execute("SELECT symbol, direction, qty, entry, entry_time, exit_time, pnl_usd FROM trades"
                          " WHERE status='CLOSED' AND exit_time > ? AND exit_time <= ? ORDER BY exit_time",
                          (t0, t1)).fetchall()
    if not trades:
        return False, None, "no recorded trades in the unobserved window"
    if any(t[6] is None for t in trades):
        return False, None, "net P&L unknown for some trades (commission UNKNOWN)"
    total = sum(t[6] for t in trades)
    if abs(total - (b1 - b0)) > RECONCILE_TOL:
        return False, None, f"recorded trades net {total:.2f} but balance changed {b1 - b0:.2f} -> trades missing"
    running, peak = b0, b0
    for sym, d, qty, entry, et, xt, pnl in trades:
        root = root_of(sym)
        pv = point_value(conn, root)
        bars = conn.execute("SELECT high, low FROM bars WHERE resolution='1' AND (feed_symbol LIKE ? OR feed_symbol LIKE ?)"
                            " AND time >= ? AND time <= ?", (f"%:{root}1!", f"%:{root}", et - et % 60, xt)).fetchall()
        if pv is None or not bars:
            return True, None, f"reconciled, but no 1m bars / point value for {sym} -> peak UNKNOWN"
        mfe = max((h - entry) if d == "LONG" else (entry - l) for h, l in bars)
        peak = max(peak, running + max(mfe, 0) * pv * qty)
        running += pnl
    return True, peak, f"reconciled {len(trades)} trades (net {total:.2f}); peak from 1m bar MFE (conservative)"


IMPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS csv_imports (path TEXT NOT NULL, mtime REAL NOT NULL, imported_at INTEGER NOT NULL,
                                        new_trades INTEGER NOT NULL, PRIMARY KEY (path, mtime));
"""


def auto_import(conn: sqlite3.Connection, folders: list[str], tz_name: str, account: str | None,
                commission_rt: float | None, now: int) -> list[tuple[str, list[int]]]:
    """Import any Performance*.csv in the given folders that is new or changed since last seen."""
    conn.executescript(IMPORTS_SCHEMA)
    done = []
    for folder in folders:
        for p in sorted(Path(folder).glob("Performance*.csv")):
            mt = p.stat().st_mtime
            if conn.execute("SELECT 1 FROM csv_imports WHERE path=? AND mtime=?", (str(p), mt)).fetchone():
                continue
            try:
                ids = import_file(conn, p, tz_name, account, commission_rt)
            except (ValueError, OSError, KeyError) as e:
                ids = []
                done.append((f"{p.name}: NOT imported ({e})", []))
            conn.execute("INSERT OR REPLACE INTO csv_imports VALUES (?,?,?,?)", (str(p), mt, now, len(ids)))
            conn.commit()
            if ids:
                done.append((p.name, ids))
    return done
