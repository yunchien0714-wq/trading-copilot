"""Telegram query commands (read-only). Only the configured chat id is answered; anything else is ignored.

/status  /account  /events  /today  /setups  /mute <min>  /unmute  /help
Nothing here can place orders or change settings; /mute only silences non-critical notifications
(HEALTH and TRADE_SETUP are never muted).
"""
from __future__ import annotations

import json
import sqlite3
import time

from ..session_engine.sessions import to_et, trading_day, trading_day_start

NEVER_MUTED = {"HEALTH", "TRADE_SETUP", "TRADE_CLOSED", "TILT", "PROPOSAL", "NO_STOP"}
SCHEMA = "CREATE TABLE IF NOT EXISTS telegram_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);"

HELP = ("指令（只讀，不會下單）：\n/status 監控狀態、資料、結構\n/account 帳戶、回撤緩衝、單筆上限\n"
        "/events 接下來的經濟數據\n/today 今天的交易和評級\n/setups 正在追蹤的區域\n"
        "/rules 哪些規則有幫助、哪些讓你虧（每個潛在 setup）\n"
        "/mute 30 靜音非重要通知 30 分鐘\n/unmute 取消靜音")


def _get(conn, k, default=None):
    conn.executescript(SCHEMA)
    r = conn.execute("SELECT v FROM telegram_state WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def _set(conn, k, v):
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO telegram_state VALUES (?,?)", (k, json.dumps(v)))
    conn.commit()


def muted_until(conn) -> int:
    return int(_get(conn, "muted_until", 0) or 0)


def sizing_table(cap, point_values: dict, commission, size_range) -> str:
    """Widest stop (points) that still fits the loss cap at each contract count (incl. round-trip fees)."""
    if not isinstance(cap, (int, float)) or commission is None or not size_range or not point_values:
        return ""
    lo, hi = size_range
    out = ["", "各口數最多可放的止損（含手續費）："]
    for root, pv in sorted(point_values.items()):
        cells = []
        for n in sorted({lo, 2, 3, 5, 10, hi}):
            if lo <= n <= hi:
                pts = (cap / n - commission) / pv
                cells.append(f"{n} 口 ≤ {pts:.1f} 點" if pts > 0 else f"{n} 口 不行")
        out.append(f"{root}（每點 ${pv:g}）：" + "、".join(cells))
    return "\n".join(out)


SMC_ZH = {"BULLISH": "多頭", "BEARISH": "空頭", "UNDEFINED": "未定"}
SIDE_ZH = {"LONG": "多", "SHORT": "空"}
STATE_ZH = {"WATCHING": "觀察中", "POTENTIAL_SETUP": "潛在", "CONFIRMING": "等確認"}


def _v(x, fmt=None):
    if not isinstance(x, (int, float)):
        return "未知"
    return fmt.format(x) if fmt else str(x)


def answer(cmd: str, arg: str, conn: sqlite3.Connection, ctx: dict, now: int) -> str:
    """ctx: live context from the monitor cycle (snap, gate state, panes, events, smc)."""
    if cmd == "/status":
        lines = [f"監控 {to_et(now):%H:%M} ET", f"券商連線：{'正常' if ctx.get('broker_connected') else '中斷'}"]
        lines += [f"{p}：{'即時' if q == 'REALTIME' else q}" for p, q in ctx.get("data", {}).items()]
        lines.append(f"5m 結構：{SMC_ZH.get(ctx.get('smc_5m'), '未知')}"
                     + (f"（{ctx['smc_last']}）" if ctx.get("smc_last") else ""))
        lines.append(f"5m／1m 圖表：{'正常' if ctx.get('pairing') == 'OK' else ctx.get('pairing', '未知')}")
        m = muted_until(conn)
        if m > now:
            lines.append(f"靜音到 {to_et(m):%H:%M} ET")
        return "\n".join(lines)
    if cmd == "/account":
        st = ctx.get("gate_state", {})
        cap = ctx.get("loss_cap")
        return (f"帳戶\n權益：{_v(st.get('balance_usd'), '${:,.2f}')}\n最高權益：{_v(st.get('high_water_usd'), '${:,.2f}')}\n"
                f"今天交易：{_v(st.get('trades_today'))} 筆（上限 {ctx.get('max_trades', '?')}）\n"
                f"持倉數：{_v(st.get('open_positions'))}\n"
                f"目前單筆虧損上限：{_v(cap, '${:.0f}')}"
                + sizing_table(cap, ctx.get("point_values", {}), ctx.get("commission"), ctx.get("size_range")))
    if cmd == "/events":
        ev = ctx.get("events")
        if ev is None:
            return "經濟日曆暫時讀不到（未知）。"
        up = [e for e in ev if 0 <= e.time - now <= 2 * 86400 and e.importance >= 0]
        if not up:
            return "接下來 48 小時沒有中等以上的美國經濟數據。"
        wd = "一二三四五六日"
        return "經濟數據（ET）\n" + "\n".join(
            f"週{wd[to_et(e.time).weekday()]} {to_et(e.time):%H:%M} {'重大' if e.importance >= 1 else '中等'} {e.title}"
            for e in up[:15])
    if cmd == "/rules":
        from ..alert_tracking.rules_report import build as rules_build, text as rules_text
        return rules_text(rules_build(conn, "live"))
    if cmd == "/today":
        start = trading_day_start(now)
        has = lambda n: conn.execute("SELECT name FROM sqlite_master WHERE name=?", (n,)).fetchone()
        if not has("trades"):
            return "還沒有交易紀錄。"
        q = ("SELECT t.id, t.direction, t.qty, t.entry, t.exit, t.pnl_usd, t.status, "
             + ("g.grade, g.classification FROM trades t LEFT JOIN trade_grades g ON g.trade_id = t.id" if has("trade_grades")
                else "NULL, NULL FROM trades t") + " WHERE t.entry_time >= ? ORDER BY t.entry_time")
        rows = conn.execute(q, (start,)).fetchall()
        if not rows:
            return f"交易日 {trading_day(now)} 還沒有交易。"
        net = sum(r[5] for r in rows if r[5] is not None)
        return f"今天 {trading_day(now)}：{len(rows)} 筆，淨損益 ${net:.2f}\n" + "\n".join(
            f"#{r[0]} {SIDE_ZH.get(r[1], r[1])} {r[2]:g} 口 {r[3]:.1f}→{f'{r[4]:.1f}' if r[4] else '持倉中'} "
            f"{f'${r[5]:.2f}' if r[5] is not None else ''} {('評級 ' + r[7]) if r[7] else ''}".rstrip() for r in rows)
    if cmd == "/setups":
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='setups'").fetchone():
            return "還沒有追蹤中的 setup。"
        rows = conn.execute("SELECT direction, zone_low, zone_high, state FROM setups WHERE state IN "
                            "('WATCHING','POTENTIAL_SETUP','CONFIRMING') ORDER BY updated_at DESC LIMIT 8").fetchall()
        return ("追蹤中的區域\n" + "\n".join(f"{SIDE_ZH.get(r[0], r[0])} {r[1]}-{r[2]} {STATE_ZH.get(r[3], r[3])}"
                                          for r in rows)) if rows else "目前沒有追蹤中的區域。"
    if cmd == "/mute":
        mins = int(arg) if arg.isdigit() and 0 < int(arg) <= 480 else 30
        _set(conn, "muted_until", now + mins * 60)
        return f"已靜音 {mins} 分鐘（健康警報、交易 setup、平倉、情緒警報、策略提案仍會照常送）。"
    if cmd == "/unmute":
        _set(conn, "muted_until", 0)
        return "已取消靜音。"
    return HELP


def poll(sender, conn: sqlite3.Connection, ctx: dict, now: int | None = None) -> int:
    """Answer new messages from the configured chat. Returns number of commands handled."""
    now = now or int(time.time())
    conn.executescript(SCHEMA)
    offset = _get(conn, "update_offset", 0)
    updates = sender._call("getUpdates", {"offset": offset, "timeout": 0})
    handled = 0
    for u in updates:
        _set(conn, "update_offset", u["update_id"] + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(sender.chat_id):
            continue                                     # not the user's chat: ignore silently
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        sender.send(answer(cmd, parts[1].strip() if len(parts) > 1 else "", conn, ctx, now))
        handled += 1
    return handled
