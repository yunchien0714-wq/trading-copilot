"""Exact fills from the TradingView broker connection (read-only) -> trades, from ANY device.

Found: `broker.executions(<contract symbol>)` returns every fill of the account for that contract
(id, price, qty, side, time ms), including fills placed on another computer and fills that happened while this
monitor was not running (it returned the 10-04 fills the next morning). `allExecutions()` is always empty, which is
why fills looked unavailable before. Fill ids are the same ids as in Tradovate's Performance CSV, so trades built here
and trades imported from a CSV share the same identity key and never double count.

How far back TradingView keeps executions is NOT known yet -- fills are therefore stored locally the moment they are
seen, and the CSV import stays as the fallback.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from ..trade_monitor.monitor import TRADE_MONITOR_VERSION, root_of

SOURCE = "broker executions (TradingView)"
EXEC_VERSION = "executions-0.1.0"
LIVE_MATCH_SECONDS = 180          # slack after the last fill (poll interval + clock skew between TV and this PC)

# Query hints for instruments that are not on the chart. TradingView exchange prefix per root (MGC verified from the
# chart; the others follow TradingView's naming and are unverified until a fill is seen) and the
# exchange's listed contract months. A wrong hint only returns no fills -- it can never create a trade.
PREFIX = {"MGC": "COMEX_MINI", "GC": "COMEX", "MNQ": "CME_MINI", "NQ": "CME_MINI"}
MONTHS = {"MGC": "GJMQVZ", "GC": "GJMQVZ", "MNQ": "HMUZ", "NQ": "HMUZ"}
MONTH_NUM = {c: i for i, c in enumerate("FGHJKMNQUVXZ", start=1)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY, account TEXT, symbol TEXT NOT NULL, side INTEGER NOT NULL, qty REAL NOT NULL,
    price REAL NOT NULL, time INTEGER NOT NULL, first_seen_at INTEGER NOT NULL, raw TEXT NOT NULL
);
"""

READ_JS = """
(async function(symbols) {
  var ab = window.TradingViewApi.trading()._activeBroker;
  var b = (ab && typeof ab.value === 'function') ? ab.value() : ab;
  if (!b) return {connected: false};
  var out = {connected: true, fills: {}, errors: {}, front: []};
  try {   // the chart's continuous symbols -> their current front contract, e.g. COMEX_MINI:MGC1! -> COMEX_MINI:MGCZ2026
    window.TradingViewApi._chartWidgetCollection.getAll().forEach(function(w) {
      var si = w.model().mainSeries().symbolInfo();
      if (si && si.front_contract && si.full_name) {
        var f = si.full_name.split(':')[0] + ':' + si.front_contract;
        if (out.front.indexOf(f) < 0) out.front.push(f);
      }
    });
  } catch (e) { out.errors.front = String(e && e.message || e); }
  out.front.forEach(function(f) { if (symbols.indexOf(f) < 0) symbols.push(f); });
  for (var i = 0; i < symbols.length; i++) {
    try { var r = b.executions(symbols[i]); if (r && typeof r.then === 'function') r = await r; out.fills[symbols[i]] = r || []; }
    catch (e) { out.errors[symbols[i]] = String(e && e.message || e); }
  }
  return JSON.parse(JSON.stringify(out));
})(%s)
"""


def candidate_contracts(roots: list[str], today: date, n: int = 2) -> list[str]:
    """Next `n` listed contracts per root, e.g. MGC -> COMEX_MINI:MGCZ2026, COMEX_MINI:MGCG2027."""
    out = []
    for root in roots:
        if root not in PREFIX:
            continue
        y, m, got = today.year, today.month, 0
        while got < n:
            for c in MONTHS[root]:
                if MONTH_NUM[c] >= m and got < n:
                    out.append(f"{PREFIX[root]}:{root}{c}{y}")
                    got += 1
            y, m = y + 1, 1
    return out


def symbols_to_query(conn: sqlite3.Connection, snap_raw: dict, front: list[str], roots: list[str], today: date,
                     now: int) -> list[str]:
    """Contracts to ask for fills: open positions / orders (always exact), the chart's front contracts, contracts
    traded in the last 30 days, and listed-month hints for the user's other instruments."""
    s = set(front)
    for k in ("positions", "orders"):
        s |= {x.get("symbol") for x in (snap_raw.get(k) or []) if isinstance(x, dict) and x.get("symbol")}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='trades'").fetchone():
        s |= {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM trades WHERE entry_time >= ?", (now - 30 * 86400,))
              if r[0] and not r[0].endswith("!")}
    s |= set(candidate_contracts(roots, today))
    return sorted(x for x in s if x)


def store(conn: sqlite3.Connection, account: str | None, fills_by_symbol: dict, now: int) -> int:
    conn.executescript(SCHEMA)
    new = 0
    for sym, fills in fills_by_symbol.items():
        for f in fills:
            try:
                fid, side, qty, price, t = str(f["id"]), int(f["side"]), float(f["qty"]), float(f["price"]), int(f["time"]) // 1000
            except (KeyError, TypeError, ValueError):
                continue                                   # unexpected shape -> ignored, never guessed
            if side not in (1, -1) or qty <= 0:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
                               (fid, account, f.get("symbol") or sym, side, qty, price, t, now, json.dumps(f)))
            new += cur.rowcount
    conn.commit()
    return new


@dataclass
class FillTrade:
    symbol: str
    direction: str
    qty: float = 0.0
    entry_time: int = 0
    exit_time: int = 0
    opens: list = field(default_factory=list)       # (id, side, qty, price, time)
    closes: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return "|".join(sorted({("B" if s == 1 else "S") + i for i, s, *_ in self.opens + self.closes}))

    @property
    def entry(self) -> float:
        return sum(q * p for _, _, q, p, _ in self.opens) / sum(q for _, _, q, _, _ in self.opens)

    @property
    def exit(self) -> float:
        return sum(q * p for _, _, q, p, _ in self.closes) / sum(q for _, _, q, _, _ in self.closes)

    def gross(self, pv: float) -> float:
        sign = 1 if self.direction == "LONG" else -1
        return (self.exit - self.entry) * sign * self.qty * pv


def build_trades(fills: list[tuple]) -> tuple[list[FillTrade], FillTrade | None]:
    """fills: (id, symbol, side, qty, price, time) for ONE symbol. Flat -> position -> flat = one trade.
    A fill that flips the position closes the trade and opens the next one with the remainder.
    Returns (closed trades, still-open trade or None)."""
    done, cur, pos = [], None, 0.0
    for fid, sym, side, qty, price, t in sorted(fills, key=lambda f: (f[5], f[0])):
        left = qty
        while left > 1e-9:
            if cur is None:
                cur = FillTrade(sym, "LONG" if side == 1 else "SHORT", entry_time=t)
            same = (side == 1) == (cur.direction == "LONG")
            if same:
                cur.opens.append((fid, side, left, price, t))
                cur.qty += left
                pos += left
                left = 0
            else:
                q = min(left, pos)
                cur.closes.append((fid, side, q, price, t))
                pos -= q
                left -= q
                if pos <= 1e-9:
                    cur.exit_time, pos = t, 0.0
                    done.append(cur)
                    cur = None
    return done, cur


def reconcile(conn: sqlite3.Connection, account: str | None, pv_of, commission_rt: float | None,
              now: int) -> tuple[list[int], list[int]]:
    """Turn stored fills into trades. Returns (enriched live-tracked trade ids, newly recovered trade ids).
    pv_of(root) -> $/point or None (unknown -> P&L UNKNOWN, never assumed)."""
    conn.executescript(SCHEMA)
    enriched, recovered = [], []
    syms = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM fills")]
    for sym in syms:
        fills = conn.execute("SELECT id, symbol, side, qty, price, time FROM fills WHERE symbol = ?", (sym,)).fetchall()
        closed, _ = build_trades(fills)
        pv = pv_of(root_of(sym))
        for ft in closed:
            if conn.execute("SELECT 1 FROM trades WHERE fill_key = ?", (ft.key,)).fetchone():
                continue
            gross = ft.gross(pv) if pv else None
            net = gross - commission_rt * ft.qty if gross is not None and commission_rt is not None else None
            fills_json = json.dumps({"fills": [dict(zip(("id", "side", "qty", "price", "time"), f))
                                               for f in ft.opens + ft.closes], "source": SOURCE})
            # a trade the live monitor saw: first seen while this position was open (between first and last fill)
            seen = conn.execute("SELECT id, initial_stop, status FROM trades WHERE fill_key IS NULL AND symbol = ?"
                                " AND direction = ? AND entry_time BETWEEN ? AND ? ORDER BY entry_time LIMIT 1",
                                (sym, ft.direction, ft.entry_time, ft.exit_time + LIVE_MATCH_SECONDS)).fetchone()
            if seen and seen[2] != "CLOSED":
                continue                                   # live monitor has not closed it yet -> next cycle
            live = seen
            if live:
                tid, stop, _ = live
                sign = 1 if ft.direction == "LONG" else -1
                r = ((ft.exit - ft.entry) * sign / ((ft.entry - stop) * sign)
                     if stop is not None and (ft.entry - stop) * sign > 0 else None)
                # net P&L: fills + round-trip commission win over the live balance change, because half of the
                # commission is charged at the entry fill -- before the first poll records balance_before
                # (observed: the cash balance drops by half the commission at the entry fill). The live value is only kept when the fills' P&L cannot be computed.
                conn.execute("UPDATE trades SET entry = ?, entry_time = ?, exit = ?, exit_time = ?, qty = ?,"
                             " fill_key = ?, pnl_gross = ?, pnl_usd = COALESCE(?, pnl_usd), r_multiple = ?,"
                             " entry_source = ?, exit_source = ? WHERE id = ?",
                             (round(ft.entry, 6), ft.entry_time, round(ft.exit, 6), ft.exit_time, ft.qty, ft.key,
                              gross, net, r, SOURCE + " (exact fills; first seen live)", SOURCE, tid))
                conn.execute("DELETE FROM trade_grades WHERE trade_id = ?", (tid,)) if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='trade_grades'").fetchone() else None
                enriched.append(tid)
            else:
                cur = conn.execute(
                    "INSERT INTO trades (account, symbol, direction, qty, entry, entry_time, entry_source, initial_stop,"
                    " initial_target, brackets_source, balance_before, context, status, exit, exit_time, exit_source,"
                    " pnl_usd, r_multiple, notified, version, fill_key, pnl_gross) VALUES"
                    " (?,?,?,?,?,?,?,NULL,NULL,?,NULL,?,?,?,?,?,?,NULL,'[]',?,?,?)",
                    (account, sym, ft.direction, ft.qty, round(ft.entry, 6), ft.entry_time,
                     SOURCE + " (not seen live)", "UNKNOWN (brackets were not observed)", fills_json, "CLOSED",
                     round(ft.exit, 6), ft.exit_time, SOURCE, net, f"{TRADE_MONITOR_VERSION}+{EXEC_VERSION}",
                     ft.key, gross))
                recovered.append(cur.lastrowid)
    conn.commit()
    return enriched, recovered


def read(evaluate_async, symbols: list[str]) -> dict:
    return evaluate_async(READ_JS % json.dumps(symbols))
