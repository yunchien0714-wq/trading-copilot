"""Manual trade detection + active trade monitoring (spec sections 14-16). Read-only: never touches orders.

Detection works from the broker's POSITIONS (Tradovate via TradingView), so trades placed on any device are
seen. Field names below follow TradingView's broker API (Position: symbol, qty, side, avgPrice; Order: symbol,
type, side, qty, stopPrice, limitPrice, status). They are NOT yet verified against a real Tradovate fill
: the first raw object of each kind is stored in broker_raw for checking, and any missing field
becomes UNKNOWN rather than a guess.

Exit price: derived from the realized balance change until executions are verified (source recorded).
High-water mark: the trade's MFE gives the unrealized equity peak that polling between reads can miss.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..market_data.models import Bar

TRADE_MONITOR_VERSION = "trade_monitor-0.2.0"   # 0.2: no-stop alert, late stops adopted, Chinese notes
SIDE_ZH = {"LONG": "多", "SHORT": "空"}
NO_STOP_SECONDS = 60      # an open position without a visible stop order this long -> urgent alert
SMC_ZH = {"BULLISH": "多頭", "BEARISH": "空頭", "UNDEFINED": "未定"}
SIDE_LONG = {1, "1", "buy", "Buy", "BUY", "long"}
SIDE_SHORT = {-1, "-1", "sell", "Sell", "SELL", "short"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, account TEXT, symbol TEXT NOT NULL, direction TEXT, qty REAL,
    entry REAL, entry_time INTEGER NOT NULL, entry_source TEXT NOT NULL,
    initial_stop REAL, initial_target REAL, brackets_source TEXT,
    balance_before REAL, context TEXT NOT NULL,
    mfe_points REAL DEFAULT 0, mae_points REAL DEFAULT 0, last_price REAL,
    status TEXT NOT NULL,                     -- ACTIVE | CLOSED
    exit REAL, exit_time INTEGER, exit_source TEXT, pnl_usd REAL, r_multiple REAL,
    notified TEXT NOT NULL DEFAULT '[]', version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_raw (kind TEXT PRIMARY KEY, seen_at INTEGER NOT NULL, raw TEXT NOT NULL);
"""


@dataclass
class TradeNote:
    kind: str          # TRADE_STARTED | TRADE_UPDATE | THESIS_INVALIDATED | TRADE_CLOSED
    trade_id: int
    text: str


def direction_of(pos: dict) -> str | None:
    side = pos.get("side")
    if side in SIDE_LONG:
        return "LONG"
    if side in SIDE_SHORT:
        return "SHORT"
    q = pos.get("qty")
    if isinstance(q, (int, float)) and q != 0:
        return "LONG" if q > 0 else "SHORT"
    return None


def root_of(symbol: str | None) -> str:
    """'COMEX_MINI:MGC1!' / 'MGCZ2026' / 'MGC' -> 'MGC' (letters before the contract month)."""
    s = (symbol or "").split(":")[-1].upper()
    out = ""
    for ch in s:
        if ch.isalpha():
            out += ch
        else:
            break
    for month_code in ("F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"):
        if len(out) > 2 and out.endswith(month_code) and out[:-1] in ("MGC", "GC", "MNQ", "NQ"):
            return out[:-1]
    return out


def brackets(orders: list[dict], symbol: str, direction: str) -> tuple[float | None, float | None]:
    """Stop / target from working orders on the same contract, on the closing side."""
    stop = target = None
    close_side = SIDE_SHORT if direction == "LONG" else SIDE_LONG
    for o in orders or []:
        if root_of(o.get("symbol")) != root_of(symbol) or o.get("side") not in close_side:
            continue
        if isinstance(o.get("stopPrice"), (int, float)) and o.get("stopPrice"):
            stop = float(o["stopPrice"])
        elif isinstance(o.get("limitPrice"), (int, float)) and o.get("limitPrice"):
            target = float(o["limitPrice"])
    return stop, target


class TradeMonitor:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(SCHEMA)
        conn.row_factory = sqlite3.Row

    def _keep_raw(self, kind: str, obj, now: int) -> None:
        if obj and not self.conn.execute("SELECT 1 FROM broker_raw WHERE kind=?", (kind,)).fetchone():
            self.conn.execute("INSERT INTO broker_raw VALUES (?,?,?)", (kind, now, json.dumps(obj, default=str)[:20000]))

    def active(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trades WHERE status='ACTIVE'").fetchall()

    def update(self, snap, now: int, bars_by_root: dict[str, list[Bar]], point_value: dict[str, float],
               context: dict) -> list[TradeNote]:
        """snap: BrokerSnapshot with .raw (positions/orders/executions). context: market context at this moment."""
        if not snap.connected:
            return []
        raw = getattr(snap, "raw", {}) or {}
        positions = [p for p in raw.get("positions") or [] if (p.get("qty") or 0) != 0]
        orders = raw.get("orders") or []
        if positions:
            self._keep_raw("position", positions[0], now)
        if orders:
            self._keep_raw("order", orders[0], now)
        if raw.get("allExecutions"):
            self._keep_raw("execution", raw["allExecutions"][-1], now)
        notes: list[TradeNote] = []
        open_roots = {}
        for p in positions:
            open_roots[root_of(p.get("symbol"))] = p
        # --- new positions -> TRADE STARTED
        active = {root_of(r["symbol"]): r for r in self.active()}
        for root, p in open_roots.items():
            if root in active:
                continue
            d = direction_of(p)
            entry = p.get("avgPrice") if isinstance(p.get("avgPrice"), (int, float)) else None
            stop, target = brackets(orders, p.get("symbol"), d) if d else (None, None)
            cur = self.conn.execute(
                "INSERT INTO trades (account, symbol, direction, qty, entry, entry_time, entry_source, initial_stop,"
                " initial_target, brackets_source, balance_before, context, status, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snap.account_name, p.get("symbol"), d, abs(p.get("qty") or 0) or None, entry, now,
                 "broker position avgPrice; time = first seen by poll" if entry else "UNKNOWN",
                 stop, target, "working orders" if (stop or target) else "UNKNOWN (no bracket orders seen)",
                 snap.balance, json.dumps(context), "ACTIVE", TRADE_MONITOR_VERSION))
            tid = cur.lastrowid
            risk_pts = abs(entry - stop) if entry and stop else None
            notes.append(TradeNote("TRADE_STARTED", tid, (
                f"交易開始\n\n{root} {SIDE_ZH.get(d, '方向未知')} {abs(p.get('qty') or 0):g} 口\n"
                f"進場：{entry if entry else '未知'}\n止損：{stop or '未知'}\n目標：{target or '未知'}\n"
                f"風險：{f'{risk_pts:.1f} 點' if risk_pts else '未知（沒看到止損單）'}\n\n"
                f"5m 結構：{SMC_ZH.get(context.get('smc_5m'), '未知')}\n"
                f"系統 setup 狀態：{context.get('setup_state', '無')}")))
        # --- active trades: excursions, updates, closes
        for t in self.active():
            root = root_of(t["symbol"])
            bars = [b for b in bars_by_root.get(root, []) if b.time + 60 > t["entry_time"]]
            mfe, mae, last = t["mfe_points"] or 0.0, t["mae_points"] or 0.0, t["last_price"]
            if t["entry"] and bars:
                sgn = 1 if t["direction"] == "LONG" else -1
                for b in bars:
                    fav = (b.high - t["entry"]) if sgn > 0 else (t["entry"] - b.low)
                    adv = (t["entry"] - b.low) if sgn > 0 else (b.high - t["entry"])
                    mfe, mae = max(mfe, fav), max(mae, adv)
                last = bars[-1].close
            notified = set(json.loads(t["notified"]))
            self.conn.execute("UPDATE trades SET mfe_points=?, mae_points=?, last_price=? WHERE id=?",
                              (mfe, mae, last, t["id"]))
            # A stop placed AFTER the entry (bracket added late) is adopted as the trade's stop .
            stop_now, target_now = brackets(orders, t["symbol"], t["direction"]) if root in open_roots else (None, None)
            initial_stop = t["initial_stop"]
            if initial_stop is None and stop_now is not None:
                initial_stop = stop_now
                self.conn.execute("UPDATE trades SET initial_stop=?, initial_target=COALESCE(initial_target, ?),"
                                  " brackets_source=? WHERE id=?",
                                  (stop_now, target_now, f"working orders, stop first seen {now - t['entry_time']}s "
                                   "after entry", t["id"]))
            risk = abs(t["entry"] - initial_stop) if t["entry"] and initial_stop else None
            if root not in open_roots:
                notes.append(self._close(t, snap, now, point_value.get(root), risk, mfe, mae))
                continue
            # No stop order on an open position: urgent, never muted .
            age = now - t["entry_time"]
            if stop_now is None and age >= NO_STOP_SECONDS and "NOSTOP" not in notified:
                notified.add("NOSTOP")
                qty = t["qty"] or 0
                notes.append(TradeNote("NO_STOP", t["id"],
                                       f"⚠️ 持倉沒有止損單\n\n{root} {SIDE_ZH.get(t['direction'], t['direction'])} {qty:g} 口，"
                                       f"已經 {age} 秒看不到止損單。\n價格往反方向走的話，虧損沒有上限。\n\n"
                                       "請在 Tradovate 設好止損。系統不會幫你下單或改單。"))
            elif stop_now is not None and "NOSTOP" in notified and "STOP_OK" not in notified:
                notified.add("STOP_OK")
                notes.append(TradeNote("STOP_OK", t["id"],
                                       f"已看到止損單：{root} 止損 {stop_now}"
                                       + (f"（風險 {abs(t['entry'] - stop_now):.1f} 點）" if t["entry"] else "")))
            if risk and mfe >= risk and "R1" not in notified:
                notified.add("R1")
                notes.append(TradeNote("TRADE_UPDATE", t["id"],
                                       f"交易更新\n\n{root} {SIDE_ZH.get(t['direction'], t['direction'])}\n\n"
                                       f"價格已到 +1R（最大有利 {mfe:.1f} 點）。\n"
                                       f"5m 結構：{SMC_ZH.get(context.get('smc_5m'), '未知')}\n\n系統不會自動做任何動作。"))
            # Only a change AFTER entry invalidates: the structure turned against the trade, or (a reversal entered
            # against the structure) a new 5m break continued against it. Structure that was already against the
            # trade at entry is the reversal itself, not an invalidation .
            against = {"LONG": "BEARISH", "SHORT": "BULLISH"}.get(t["direction"])
            start = json.loads(t["context"] or "{}")
            turned = start.get("smc_5m") != against
            new_break = bool(context.get("smc_last")) and context.get("smc_last") != start.get("smc_last")
            if against and context.get("smc_5m") == against and (turned or new_break) and "THESIS" not in notified:
                notified.add("THESIS")
                why = (f"進場後 5m 結構轉成{SMC_ZH.get(against, against)}" if turned else
                       "進場後 5m 又出現逆向的結構突破（這筆進場時本來就是逆結構）")
                notes.append(TradeNote("THESIS_INVALIDATED", t["id"],
                                       f"進場論點失效\n\n{root} {SIDE_ZH.get(t['direction'], t['direction'])}\n\n{why}"
                                       f"（{context.get('smc_last', '最近突破未知')}）。\n原本的進場理由已經不成立。"
                                       "\n\n系統不會自動做任何動作。"))
            self.conn.execute("UPDATE trades SET notified=? WHERE id=?", (json.dumps(sorted(notified)), t["id"]))
        self.conn.commit()
        return notes

    def _close(self, t, snap, now, pv, risk, mfe, mae) -> TradeNote:
        pnl = (snap.balance - t["balance_before"]) if snap.balance is not None and t["balance_before"] is not None else None
        exit_px = None
        if pnl is not None and pv and t["qty"] and t["entry"]:
            sgn = 1 if t["direction"] == "LONG" else -1
            exit_px = t["entry"] + sgn * pnl / (pv * t["qty"])     # includes fees -> approximate
        r = (pnl / (risk * pv * t["qty"])) if pnl is not None and risk and pv and t["qty"] else None
        self.conn.execute(
            "UPDATE trades SET status='CLOSED', exit=?, exit_time=?, exit_source=?, pnl_usd=?, r_multiple=?,"
            " mfe_points=?, mae_points=? WHERE id=?",
            (exit_px, now, "derived from balance change incl. fees (executions not verified yet)" if exit_px else "UNKNOWN",
             pnl, r, mfe, mae, t["id"]))
        root = root_of(t["symbol"])
        return TradeNote("TRADE_CLOSED", t["id"], (
            f"交易平倉\n\n{root} {SIDE_ZH.get(t['direction'], t['direction'])} {t['qty']:g} 口\n"
            f"進場：{t['entry']}\n出場：{f'約 {exit_px:.1f}（由餘額推算）' if exit_px else '未知'}\n"
            f"損益：{f'${pnl:.2f}' if pnl is not None else '未知'}\nR：{f'{r:.2f}' if r is not None else '未知'}\n"
            f"最大有利／不利：{mfe:.1f} / {mae:.1f} 點\n\n接著會用實際成交價更正，然後評級。"))

    def peak_equity_hint(self, point_value: dict[str, float]) -> float | None:
        """Equity peak implied by the active trade's MFE (balance_before + MFE * $/pt * qty)."""
        best = None
        for t in self.active():
            pv = point_value.get(root_of(t["symbol"]))
            if pv and t["qty"] and t["balance_before"] is not None:
                v = t["balance_before"] + (t["mfe_points"] or 0) * pv * t["qty"]
                best = v if best is None else max(best, v)
        return best
