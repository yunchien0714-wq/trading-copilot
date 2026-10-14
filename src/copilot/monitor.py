"""Live monitoring loop (Phase 7): chart -> engines -> tracker -> risk gate -> outbox/Telegram -> chart drawings.

Analysis only. The co-pilot never places orders. Anything it cannot verify fails closed.
"""
from __future__ import annotations

import json
from datetime import timedelta
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .broker.tradingview_broker import (gate_state, high_water, load_anchor, load_reported_trades,
                                        trades_today)
from .broker.tradingview_broker import read as read_broker, record as record_equity
from .chart_manager.drawings import ChartManager
from .chart_manager.indicator_objects import read as read_indicator_objects
from .chart_manager.levels import select, sync
from .chart_manager.setup_drawings import cleanup as cleanup_setups, draw_setup
from .database.store import connect, save_snapshot
from .event_engine.calendar import HIGH, fetch as fetch_events
from .market_data.series import atr as atr_series
from .market_structure.analysis import analyze_timeframe
from .market_data.rolls import FRONT_JS, boundaries as roll_boundaries, observe as observe_front
from .notifications.formatter import render
from .notifications.commands import poll as poll_commands, sizing_table
from .notifications.telegram import Outbox
from .risk_engine.gate import Proposal, evaluate as risk_evaluate, load_toml
from .session_engine.sessions import market_open, to_et, trading_day, trading_day_start
from .setup_engine.poc_fvg import evaluate_all
from .setup_engine.tracker import SetupTracker
from .alert_tracking.outcomes import daily_summary, db_bars, resolve_pending, sync_live_decisions
from .ops.backup import backup
from .ops.releases import check as check_release, code_sha
from .reports.briefing import build as build_briefing, due as briefing_due
from .reports.dashboard import write as write_dashboard
from .research.shadow import ShadowRunner
from .trade_journal.grader import grade_pending, render as render_journal
from .trade_journal.tilt import check as tilt_check, message as tilt_message
from .trade_journal.notion_sync import (REMIND_WINDOW, NotionClient, NotionError, pending as notion_pending,
                                        pull_notes, sync as notion_sync, unfilled as notion_unfilled)
from .trade_journal.tradovate_csv import auto_import, point_value
from .broker.executions import (read as read_fills, reconcile as reconcile_fills, store as store_fills,
                                symbols_to_query as exec_symbols)
from .trade_monitor.monitor import TradeMonitor, root_of
from .tradingview.cdp import CdpClient, TradingViewUnavailable, secondary_targets
from .tradingview.chart import HISTORY_TARGET, check_mtf_layout, ensure_history, read_panes
from .volume.profile import ProfileParams, build_profile, fixed_segment_pocs

ROOT = Path(__file__).resolve().parents[2]
MICRO_ROOTS = {"MGC1!", "MNQ1!"}       # contracts the user trades as micros (config: contracts_unit = micro)
MINI_ROOTS = {"GC1!", "NQ1!"}
REMIND_STAGES = (("60", 60), ("15", 15))   # minutes before an event


def is_micro(root: str) -> bool | None:
    return True if root in MICRO_ROOTS else False if root in MINI_ROOTS else None


def log(msg: str) -> None:
    print(f"{to_et(int(time.time())):%m-%d %H:%M:%S} ET  {msg}", flush=True)


@dataclass
class LoopState:
    last_1m: dict = field(default_factory=dict)     # instrument -> last processed closed 1m bar time
    last_5m: dict = field(default_factory=dict)
    cache5: dict = field(default_factory=dict)      # instrument -> (5m bar time, analysis, profile)
    events: list | None = None
    events_at: int = 0
    cycles: int = 0
    broker_connected: bool | None = None   # last known broker connection state (for health alerts)
    tv_down_since: int | None = None
    tv_down_alerted: bool = False
    last_heartbeat: int = 0
    layout_bad_since: int | None = None
    layout_alerted: bool = False
    code_sha: str | None = None
    last_bar_close: int | None = None
    shadow: object = None
    shadow_refresh_at: int = 0
    research_proc: object = None
    broker_down_since: int | None = None
    broker_alerts: int = 0
    broker_last_alert: int = 0
    notion_retry_at: int = 0
    research_log_dirty: bool = True        # True at start: days recorded before a restart may still need Notion
    research_notion_retry_at: int = 0
    systems_check_at: int = 0
    notion_pull_at: int = 0
    notion_warned: bool = False


HEARTBEAT_SECONDS = 600
BROKER_ALERT_AFTER = 180          # s down before the first alert (short drops reconnect by themselves)
BROKER_REMIND_FIRST = 1800        # second alert after 30 min down
BROKER_REMIND_EVERY = 7200        # then every 2 h until it is back
TV_DOWN_ALERT_SECONDS = 300


STALE_BARS_CLOSED = 1800    # no new 1m bar for 30 min while TradingView answers -> exchange holiday / closed


def quiet(st: LoopState, now: int) -> bool:
    """Market closed (weekend, daily break, or a holiday seen as stale bars): no system alerts."""
    return not market_open(now) or (st.last_bar_close is not None and now - st.last_bar_close > STALE_BARS_CLOSED)


def health(outbox: Outbox, st: LoopState, now: int, broker_connected: bool | None = None,
           tv_ok: bool | None = None, detail: str = "") -> None:
    """Tell the user when monitoring is blind -- once per state change (observed: a silent broker disconnect
    meant two trades were missed without any warning). Silent while the market is closed :
    the timers restart at the open, so a problem that is still there then is reported a few minutes later."""
    if quiet(st, now):
        st.broker_down_since, st.broker_alerts, st.broker_last_alert = None, 0, 0
        st.tv_down_since, st.tv_down_alerted = None, False
        st.layout_bad_since, st.layout_alerted = None, False
        if broker_connected is not None:
            st.broker_connected = broker_connected
        return
    # Broker: short drops (30-90 s, roughly hourly) reconnect by themselves -> alert only after BROKER_ALERT_AFTER,
    # then keep reminding while it stays down (observed: one alert, then 15 h of silent disconnection).
    if broker_connected is True:
        if st.broker_alerts:
            down = (now - st.broker_down_since) // 60 if st.broker_down_since else 0
            outbox.post("HEALTH", f"HEALTH:broker_up:{now}", f"監控：Tradovate 已重新連線（斷了約 {down} 分鐘）。\n"
                        "交易追蹤和風控已恢復；斷線期間的成交會自動從成交紀錄補回。", now)
        st.broker_down_since, st.broker_alerts, st.broker_last_alert = None, 0, 0
    elif broker_connected is False:
        st.broker_down_since = st.broker_down_since or now
        down = now - st.broker_down_since
        due = BROKER_ALERT_AFTER if st.broker_alerts == 0 else (
            BROKER_REMIND_FIRST if st.broker_alerts == 1 else st.broker_last_alert - st.broker_down_since + BROKER_REMIND_EVERY)
        if down >= due:
            hours, mins = divmod(down // 60, 60)
            outbox.post("HEALTH", f"HEALTH:broker_down:{st.broker_down_since}:{st.broker_alerts}",
                        f"監控：TradingView 的 Tradovate 連線已中斷 {f'{hours} 小時 ' if hours else ''}{mins} 分鐘"
                        f"{'（仍未恢復）' if st.broker_alerts else ''}。\n\n"
                        "- 這段期間無法即時追蹤交易，所有交易提醒都被風控擋住（沒有帳戶資料）。\n"
                        "- 需要你手動處理：TradingView 下方「交易面板」→ Tradovate → 按「連接」（必要時登入）。\n"
                        "- 斷線期間的成交，重新連線後會自動補回。", now)
            st.broker_alerts += 1
            st.broker_last_alert = now
    if broker_connected is not None:
        st.broker_connected = broker_connected
    if tv_ok is False:
        st.tv_down_since = st.tv_down_since or now
        if not st.tv_down_alerted and now - st.tv_down_since >= TV_DOWN_ALERT_SECONDS:
            outbox.post("HEALTH", f"HEALTH:tv_down:{now}", f"監控：已經 {(now - st.tv_down_since) // 60} 分鐘連不到 "
                        f"TradingView（{detail[:120]}）。\n恢復之前沒有分析、也沒有任何提醒。\n"
                        "請確認 TradingView 有開著（要用監控的方式開啟）。", now)
            st.tv_down_alerted = True
    elif tv_ok is True:
        if st.tv_down_alerted:
            outbox.post("HEALTH", f"HEALTH:tv_up:{now}", "監控：TradingView 已恢復連線，監控繼續。", now)
        st.tv_down_since, st.tv_down_alerted = None, False


def event_reminders(outbox: Outbox, events: list | None, now: int) -> int:
    """One reminder per event per stage (60 / 15 min before; 15-min stage only for HIGH importance)."""
    if not events:
        return 0
    sent = 0
    for e in events:
        mins = (e.time - now) / 60
        for stage, lim in REMIND_STAGES:
            if stage == "15" and e.importance < HIGH:
                continue
            if 0 <= mins <= lim:
                key = f"EVENT:{e.id}:{stage}"
                if outbox.conn.execute("SELECT 1 FROM outbox WHERE key = ?", (key,)).fetchone():
                    continue
                level = "重大" if e.importance >= HIGH else "中等"
                text = (f"經濟數據提醒\n\n事件（{level}）：{e.title}\n時間：{to_et(e.time):%H:%M} ET（{int(mins)} 分鐘後）\n\n"
                        "交易規則：不做新聞單。"
                        + ("\n公布前後 30 分鐘內不進場。" if e.importance >= HIGH else ""))
                outbox.post("EVENT", key, text, now)
                sent += 1
                break
    return sent


def audit_release(conn, outbox: Outbox, st: LoopState, now: int) -> None:
    """Phase 16: record every change of rules / code / config; a change after the baseline is reported."""
    try:
        if st.code_sha is None:
            st.code_sha = code_sha(ROOT / "src" / "copilot")
        first, changes = check_release(conn, ROOT / "src" / "copilot", ROOT / "config", now, st.code_sha)
    except Exception as e:                                     # auditing must not stop monitoring
        log(f"release audit failed: {e!r}")
        return
    if changes:
        log("release: " + "; ".join(changes))
    # Rule / version / config changes are reported; a code-only update is just recorded (it is announced in chat).
    shown = [c for c in changes if not c.startswith("code changed")]
    if shown and not first:
        outbox.post("HEALTH", f"HEALTH:release:{now}", "規則／設定已變更\n\n" + "\n".join(f"- {c}" for c in shown[:20])
                    + ("\n..." if len(shown) > 20 else "") + "\n\n之後的提醒都會用新的設定。", now)


def exact_fills(conn, outbox: Outbox, ev_async, snap, cfg: dict, pv_by_root: dict, now: int) -> None:
    """Exact fills from the broker connection (any device, also while this monitor was off): store them, correct
    live-tracked trades to the real fill prices/times, and recover trades the monitor never saw."""
    try:
        roots = [r for r in cfg.get("instruments", {}).get("traded", []) if isinstance(r, str)]
        syms = exec_symbols(conn, snap.raw, [], roots, to_et(now).date(), now)
        res = read_fills(ev_async, syms)
        if not res.get("connected"):
            return
        if res.get("errors"):
            log("executions read errors: " + "; ".join(f"{k}: {v}" for k, v in res["errors"].items()))
        new = store_fills(conn, snap.account_name, res.get("fills", {}), now)
        if new:
            log(f"executions: {new} new fill(s) stored")
        comm = cfg["costs"].get("commission_roundtrip_per_contract_usd")
        enriched, recovered = reconcile_fills(
            conn, snap.account_name, lambda r: pv_by_root.get(r) or point_value(conn, r),
            comm if isinstance(comm, (int, float)) else None, now)
    except Exception as e:                                     # fills are an enrichment; never stop monitoring
        log(f"executions failed: {e!r}")
        return
    for tid in enriched:
        log(f"trade #{tid}: corrected to exact fills (re-grade queued)")
    if recovered:
        rows = conn.execute(f"SELECT id, symbol, direction, qty, entry, exit, entry_time, pnl_usd FROM trades WHERE id IN "
                            f"({','.join('?' * len(recovered))}) ORDER BY entry_time", recovered).fetchall()
        side = {"LONG": "多", "SHORT": "空"}
        lines = "\n".join(f"- #{r[0]} {root_of(r[1])} {side.get(r[2], r[2])} {r[3]:g} 口 {r[4]:.2f} → {r[5]:.2f}，"
                          f"{to_et(r[6]):%m-%d %H:%M} ET，淨損益 {f'${r[7]:.2f}' if r[7] is not None else '未知'}"
                          for r in rows)
        outbox.post("IMPORT", f"RECOVERED:{','.join(map(str, recovered))}",
                    "從成交紀錄補回交易\n\n這些是監控沒看到的時候下的單（其他裝置或監控沒開）。價格是實際成交價；"
                    f"止損／目標當時沒看到。\n{lines}", now)
        log(f"executions: recovered {len(recovered)} trade(s) not seen live")


NOTION_RETRY_SECONDS = 600


def notion_journal(conn, outbox: Outbox, st: LoopState, cfg: dict, now: int) -> bool:
    """Write new / re-graded trades to the user's Notion 交易日誌 and read back the user's own fields. Never blocks
    monitoring; failures are retried every 10 min and reported once per day. Returns True if anything changed."""
    db = cfg.get("notion", {}).get("database_id")
    write = bool(notion_pending(conn)) if db else False
    pull = bool(db) and now >= st.notion_pull_at
    if not (write or pull) or now < st.notion_retry_at:
        return False
    try:
        client = NotionClient()
    except NotionError as e:
        if not st.notion_warned:
            log(f"notion journal skipped: {e}")
            st.notion_warned = True
        st.notion_retry_at = now + NOTION_RETRY_SECONDS
        return False
    errors, n, ch = [], 0, 0
    if write:
        n, errors = notion_sync(conn, client, db, now, cfg.get("notion", {}).get("account_labels"))
        if n:
            log(f"notion journal: {n} page(s) written")
    if pull or write:
        try:
            ch = pull_notes(conn, client, db, now)       # read back 進場理由 / 情緒 (read-only)
            st.notion_pull_at = now + NOTION_RETRY_SECONDS
            if ch:
                log(f"notion journal: {ch} note(s) updated from Notion")
        except NotionError as e:
            errors.append(f"reading notes: {e}")
    if errors:
        st.notion_retry_at = now + NOTION_RETRY_SECONDS
        log("notion journal errors: " + " | ".join(errors))
        if not quiet(st, now):
            outbox.post("HEALTH", f"HEALTH:notion:{to_et(now):%Y%m%d}", "Notion 交易日誌同步失敗（每 10 分鐘自動重試）"
                    "\n\n" + "\n".join(errors[:5]), now)
        return bool(n or ch)
    # one reminder per newly journaled trade, and one end-of-day list of what is still empty
    for tid, title, url in notion_unfilled(conn, now - REMIND_WINDOW):
        key = f"NOTE:{tid}"
        if not conn.execute("SELECT 1 FROM outbox WHERE key = ?", (key,)).fetchone():
            outbox.post("JOURNAL", key, f"交易 {title} 已寫入 Notion 交易日誌。\n"
                        f"花 30 秒趁記憶還新，填「進場理由」和「情緒」：\n{url}", now)
    et_now = to_et(now)
    if et_now.hour == 17 and et_now.minute >= 5:
        left = notion_unfilled(conn, trading_day_start(now))
        key = f"NOTE_DAILY:{trading_day(now)}"
        if left and not conn.execute("SELECT 1 FROM outbox WHERE key = ?", (key,)).fetchone():
            outbox.post("JOURNAL", key, "今天還沒填「進場理由／情緒」的交易：\n"
                        + "\n".join(f"- {t}\n  {u}" for _, t, u in left), now)
    return bool(n or ch)


def session_briefing(conn, outbox: Outbox, st: LoopState, cfg: dict, layout: dict, by_index: dict, snap, hw,
                     n_trades, pv_by_root: dict, now: int) -> None:
    """5 min before Asia / London / NY AM : one briefing per session when the market is open."""
    try:
        for inst, tf in layout["pairs"].items():
            p1 = by_index[tf["1"]]
            closed = [b for b in p1.bars if b.is_closed]
            d = briefing_due(now, closed[-1].time + 60 if closed else None)
            if not d or inst not in st.cache5:
                continue
            sid, name, begin = d
            key = f"BRIEF:{begin:%Y-%m-%d}:{sid}:{inst}"
            if conn.execute("SELECT 1 FROM outbox WHERE key = ?", (key,)).fetchone():
                continue
            _, a5, prof = st.cache5[inst]
            root = root_of(p1.symbol.root)
            max_dd = float(cfg["account"]["drawdown"]["max_drawdown_usd"])
            eq = snap.equity if snap.connected else None
            thr = hw - max_dd if isinstance(hw, (int, float)) else None
            buf = eq - thr if eq is not None and thr is not None else None
            cap = float(cfg["personal"]["max_loss_per_trade_frac_of_buffer"]) * buf if buf is not None else None
            comm = cfg["costs"].get("commission_roundtrip_per_contract_usd")
            table = sizing_table(cap, {root: pv_by_root.get(root)} if pv_by_root.get(root) else {}, comm,
                                 (int(cfg["personal"]["contracts_per_trade_min"]),
                                  int(cfg["personal"]["contracts_per_trade_max"])))
            today = conn.execute("SELECT pnl_usd, exit_time FROM trades WHERE account = ? AND entry_time >= ?"
                                 " ORDER BY COALESCE(exit_time, entry_time)",
                                 (snap.account_name, trading_day_start(now))).fetchall()
            streak = 0
            for pnl, xt in today:
                if xt is not None and pnl is not None:
                    streak = streak + 1 if pnl < 0 else 0
            usd = lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) else "未知"
            pnl_known = [r[0] for r in today if r[0] is not None]
            account = {"equity": usd(eq), "threshold": usd(thr), "buffer": usd(buf), "cap": usd(cap),
                       "sizing": table.strip().splitlines()[-1] if table else None,
                       "n": n_trades if n_trades is not None else "未知", "max": cfg["personal"]["max_trades_per_day"],
                       "pnl": usd(sum(pnl_known)) if pnl_known else "$0.00", "streak": streak}
            text = build_briefing(name, begin, a5, prof, closed[-1].close, st.events, HIGH, account)
            outbox.post("BRIEFING", key, text, now)
            log(f"briefing sent: {sid} {inst}")
    except Exception as e:                                     # a briefing must never disturb monitoring
        log(f"briefing failed: {e!r}")


def shadow_step(conn, st: LoopState, a5, prof, bars1, p1, cfg: dict, now: int) -> None:
    """Forward test of strategies that passed the backtest: same bars as production, own tracker, no alerts."""
    try:
        if st.shadow is None:
            st.shadow = ShadowRunner(ROOT / "data" / "shadow")
        if now >= st.shadow_refresh_at:
            st.shadow.refresh(conn, root_of(p1.symbol.root))
            st.shadow_refresh_at = now + 600
        if st.shadow.runs:
            n = st.shadow.update(conn, a5, prof, bars1, p1.symbol.min_tick or 0.1, float(cfg["personal"]["min_rr"]),
                                 now, p1.symbol.full_name)
            if n:
                log(f"shadow: {n} forward decision(s) recorded")
    except Exception as e:                                     # research must never disturb monitoring
        log(f"shadow step failed: {e!r}")


RESEARCH_MARKER = ROOT / "data" / "research_last_run.txt"


def research_daily(conn, outbox: Outbox, st: LoopState, cfg: dict, now: int) -> None:
    """Once per trading day after the close: judge forward tests and send ONE research digest (the runs themselves
    happen all the time, see research_tick). Production rules are never changed here."""
    day = f"{to_et(now):%Y-%m-%d}"
    try:
        if to_et(now).weekday() >= 5:
            return                                         # no trading day ends on Saturday / Sunday
        if RESEARCH_MARKER.exists() and RESEARCH_MARKER.read_text().strip() == day:
            return
        RESEARCH_MARKER.write_text(day)
        prev = conn.execute("SELECT MAX(created_at) FROM research_log WHERE day < ?", (day,)).fetchone()[0]             if conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_log'").fetchone() else None
        digest = research_digest(conn, prev or now - 86400)    # since the last journal entry: weekend runs too
        if digest:
            outbox.post("RESEARCH", f"RESEARCH_DIGEST:{day}", digest, now)
        try:                                               # other systems : ORB
            from .research.orb import daily as orb_daily
            orb_text, orb_passed = orb_daily(conn, now, cfg["costs"].get("commission_roundtrip_by_root", {}),
                                              cfg.get("research", {}).get("roots", []))
            (ROOT / "data" / "reports" / "orb_report.txt").write_text(orb_text, encoding="utf-8")
            if orb_passed:
                outbox.post("RESEARCH", f"ORB_PASSED:{day}", "開盤區間突破（另一個系統）有版本通過回測：\n"
                            + "\n".join(f"- {x}" for x in orb_passed) + "\n\n只是研究，不發交易提醒、不改策略。"
                            "完整結果在 data/reports/orb_report.txt，也可以在對話裡問我。", now)
        except Exception as e:
            log(f"ORB research failed: {e!r}")
        # forward-test decisions moved to the research process (replay of stored MGC + MNQ days)
    except Exception as e:
        log(f"research daily failed: {e!r}")


def research_journal(conn, outbox: Outbox, st: LoopState, cfg: dict, now: int) -> bool:
    """Every ET day (weekends too -- research runs then) after 17:10: record the day's research in research_log,
    then write it to the Notion 研究日誌 (retried every 10 min until it succeeds).
    Returns True when a new day was recorded (the dashboard shows the journal)."""
    from .research.journal import snapshot, sync as research_sync, sync_systems
    new = False
    try:
        et = to_et(now)
        # the day to record: today after 17:10 ET, else yesterday -- so a day the computer slept through the close
        #  is still recorded when it wakes up, covering everything since the previous record
        day = f"{et.date() if (et.hour, et.minute) >= (17, 10) else et.date() - timedelta(days=1)}"
        has_log = conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_log'").fetchone()
        last = conn.execute("SELECT MAX(day) FROM research_log").fetchone()[0] if has_log else None
        due = (et.hour, et.minute) >= (17, 10) or (last is not None and last < day)
        if due and (last is None or last < day):
            if snapshot(conn, day, now, missing_days(conn)):
                log(f"research journal: {day} recorded")
                st.research_log_dirty = new = True
        db = cfg.get("notion", {}).get("research_database_id")
        if not db or now < st.research_notion_retry_at:
            return new
        if now >= st.systems_check_at:                      # other systems' pages: as soon as results exist
            st.systems_check_at = now + 600
            ns, serr = sync_systems(conn, NotionClient(), db, now)
            if ns:
                log(f"research journal: {ns} strategy page(s) written to Notion")
            if serr:
                st.research_notion_retry_at = now + NOTION_RETRY_SECONDS
                log("research journal Notion errors: " + " | ".join(serr[:3]))
        if not st.research_log_dirty:
            return new
        n, errors = research_sync(conn, NotionClient(), db, now)
        if n:
            log(f"research journal: {n} day(s) written to Notion")
        if errors:
            st.research_notion_retry_at = now + NOTION_RETRY_SECONDS
            log("research journal Notion errors: " + " | ".join(errors[:3]))
        else:
            st.research_log_dirty = False
    except Exception as e:                                     # bookkeeping must never disturb monitoring
        st.research_notion_retry_at = now + NOTION_RETRY_SECONDS
        log(f"research journal failed: {e!r}")
    return new


RESEARCH_STATUS = ROOT / "data" / "research_status.json"
RESEARCH_GAP_OPEN = 1800       # market open: one research batch every 30 min (low priority)
RESEARCH_GAP_CLOSED = 60       # market closed: back to back
RESEARCH_IDLE = 7200           # nothing left to test: look again in 2 h


def research_tick(st: LoopState, now: int) -> None:
    """Keep strategy research running : start the next batch when the previous one has finished
    and its pause is over. Runs in its own below-normal-priority process; never touches production."""
    try:
        if st.research_proc is not None and st.research_proc.poll() is None:
            return                                         # still running
        status = json.loads(RESEARCH_STATUS.read_text()) if RESEARCH_STATUS.exists() else {}
        if status.get("running_pid") and pid_alive(status["running_pid"]):
            return                                         # a run started before this monitor (re)started
        ended = status.get("ended_at", 0)
        gap = RESEARCH_IDLE if status.get("tested") == 0 else (RESEARCH_GAP_CLOSED if quiet(st, now)
                                                               else RESEARCH_GAP_OPEN)
        if now - ended < gap:
            return
        import subprocess
        import sys
        batch = "20" if quiet(st, now) else "8"
        logf = open(ROOT / "data" / "logs" / "research_auto.log", "a", encoding="utf-8")
        st.research_proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "copilot", "research", "--auto", "--batch", batch], cwd=ROOT / "src",
            stdout=logf, stderr=subprocess.STDOUT, creationflags=0x00004000 | 0x08000000)   # below normal, no window
        log(f"research run started (pid {st.research_proc.pid}, {batch} hypotheses)")
    except Exception as e:
        log(f"research tick failed: {e!r}")


def pid_alive(pid: int) -> bool:
    """Windows-safe liveness check (os.kill would TERMINATE the process on Windows)."""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x00100000, False, int(pid))         # SYNCHRONIZE
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == 0x102        # WAIT_TIMEOUT -> still running
    finally:
        k.CloseHandle(h)


def research_digest(conn, since: int) -> str | None:
    """One line per hypothesis tested in the last day, best first."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='hypotheses'").fetchone():
        return None
    rows = conn.execute("SELECT id, verdict, results, status FROM hypotheses WHERE last_tested_at >= ?", (since,)).fetchall()
    if not rows:
        return None
    from .research.auto import VERDICT_ZH, label, sample_progress
    items = []
    for hid, verdict, results, status in rows:
        o = json.loads(results or "{}").get("out_sample", {})
        items.append((o.get("expectancy", -9) if o.get("n") else -9, hid, verdict, o, status))
    items.sort(reverse=True)
    shadow = [label(h) for _, h, _, _, s in items if s == "SHADOW"]
    lines = [f"策略研究日報：上次日報後測了 {len(items)} 個假設（已扣手續費）", ""]
    lines += [f"- {label(h)}：樣本外 {o.get('n', 0)} 筆 {o.get('expectancy', 0):+.2f}R → {VERDICT_ZH.get(v, v)}"
              for _, h, v, o, _ in items[:8]]
    if len(items) > 8:
        lines.append(f"- …其餘 {len(items) - 8} 個較差")
    lines += ["", "前瞻測試中：" + ("、".join(shadow) if shadow else "無"), ""]
    lines += sample_progress(conn, missing_days(conn)) + ["", "目前策略不變。"]
    return "\n".join(lines)


HISTORY_FILL_MARKER = ROOT / "data" / "history_fill_last.txt"
# TradingView serves at most ~20,700 1m bars (~3 weeks) for MGC1! (probed: requestMoreDataAvailable
# turned false at 20,700) -- older 1m history can only come from what this monitor stored over time.
HISTORY_FILL_TARGET = {"1": 25000, "5": 6000}
FULL_DAY_1M = 1300                 # a full CME day has 1,380 1m bars; fewer = something is missing


def missing_days(conn, root: str | None = None, lookback: int = 15) -> list[tuple[str, int]]:
    """Trading days (Mon-Fri, recent first excluded) whose stored 1m bars look incomplete. Holidays show up here
    too -- this is a hint for the digest, never a reason to make data up."""
    from collections import Counter
    from datetime import timedelta
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bars'").fetchone():
        return []
    if root is None:                                       # the instrument with the most stored 1m bars
        row = conn.execute("SELECT feed_symbol FROM bars WHERE resolution='1' GROUP BY feed_symbol"
                           " ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if row is None:
            return []
        root = row[0].split(":")[-1].removesuffix("1!")
    times = [r[0] for r in conn.execute(
        "SELECT DISTINCT time FROM bars WHERE resolution='1' AND (feed_symbol LIKE ? OR feed_symbol LIKE ?)",
        (f"%:{root}1!", f"%:{root}"))]
    if not times:
        return []
    counts = Counter(trading_day(t) for t in times)
    first, last = min(counts), max(counts)
    out, d = [], last - timedelta(days=1)                  # the last stored day may still be forming
    while d > first and d >= last - timedelta(days=lookback):
        if d.weekday() < 5 and counts.get(d, 0) < FULL_DAY_1M:
            out.append((f"{d}", counts.get(d, 0)))
        d -= timedelta(days=1)
    return out


def collect_secondary(conn, targets: dict | None = None, max_bars: int = 600) -> int:
    """Other TradingView chart tabs (e.g. the MNQ tab kept for research data): bars are stored, nothing else --
    no setup alerts, no drawings, no broker reads. targets = ensure_history targets to load older bars first."""
    added = 0
    try:
        for t in secondary_targets():
            with CdpClient(timeout=120, target=t) as c:
                if targets:
                    ensure_history(lambda js: c.evaluate(js, await_promise=True), targets)
                added += sum(save_snapshot(conn, s)["bars_inserted"] for s in read_panes(c.evaluate, max_bars))
    except Exception as e:                                     # must never disturb monitoring of the main chart
        log(f"secondary chart read failed: {e!r}")
    return added


def history_fill(cdp: CdpClient, conn, st: LoopState, now: int) -> None:
    """Once per ET day while the market is closed: load all the history TradingView will give and store it, so
    days the monitor missed (computer off) are recovered while they are still inside TradingView's window."""
    day = f"{to_et(now):%Y-%m-%d}"
    try:
        if not quiet(st, now) or (HISTORY_FILL_MARKER.exists() and HISTORY_FILL_MARKER.read_text().strip() == day):
            return
        HISTORY_FILL_MARKER.write_text(day)
        loaded = ensure_history(lambda js: cdp.evaluate(js, await_promise=True), HISTORY_FILL_TARGET)
        added = sum(save_snapshot(conn, s)["bars_inserted"]
                    for s in read_panes(cdp.evaluate, max(HISTORY_FILL_TARGET.values())))
        added += collect_secondary(conn, HISTORY_FILL_TARGET, max(HISTORY_FILL_TARGET.values()))
        gaps = missing_days(conn)
        log(f"history fill: {[(x['resolution'], x['bars']) for x in loaded]}, {added} new bars stored"
            + (f"; incomplete days {gaps}" if gaps else ""))
    except Exception as e:                                     # must never disturb monitoring
        log(f"history fill failed: {e!r}")


def daily_backup(conn, outbox: Outbox, now: int) -> None:
    """Phase 16: one verified database copy per ET day (after the CME close); failures are reported. Called every
    cycle: if the computer slept through the close , the copy is made as soon as it is awake again."""
    dest = ROOT / "data" / "backups"
    if any(dest.glob(f"copilot-{to_et(now):%Y%m%d}-*.sqlite3")):
        return
    newest = max((f.stat().st_mtime for f in dest.glob("copilot-*.sqlite3")), default=0)
    if to_et(now).hour < 17 and now - newest < 86400:
        return                                 # the usual time is after the close; earlier only to catch up
    res = backup(conn, dest, now)
    if res.ok:
        log(f"backup -> {res.path.name} ({res.size_mb} MB, {res.detail}, pruned {res.pruned})")
    else:
        log(f"backup FAILED: {res.detail}")
        outbox.post("HEALTH", f"HEALTH:backup:{to_et(now):%Y%m%d}", f"資料庫備份失敗\n\n{res.detail}", now)


def watch_rolls(cdp: CdpClient, conn, outbox: Outbox, st: LoopState, now: int) -> None:
    """Detect a contract roll of the chart's continuous symbols (front_contract changes) and tell the user."""
    try:
        fronts = cdp.evaluate(FRONT_JS) or []
    except Exception as e:                                     # detection must not stop monitoring
        log(f"roll check failed: {e!r}")
        return
    for root, contract in {tuple(x) for x in fronts}:
        ch = observe_front(conn, root, contract, now)
        if not ch:
            continue
        old, new, boundary = ch
        st.cache5.clear()                                      # re-analyse with the roll boundary
        log(f"contract roll {root}: {old} -> {new} (boundary {to_et(boundary):%m-%d %H:%M} ET, inferred)")
        outbox.post("HEALTH", f"HEALTH:roll:{root}:{new}",
                    f"合約換月：{root}1! 已換成 {new.split(':')[-1]}（原本 {old.split(':')[-1]}）。\n\n"
                    "- Tradovate 下單請改用新合約；圖表上的價格現在是新合約的。\n"
                    "- 兩個合約之間的跳空不是真的缺口，跨過它形成的 FVG 會被忽略。\n"
                    "- 換月後約一天內，成交量分布／POC 會混到兩個合約的價格，POC 位置請多保留。\n"
                    "- 如果還在交易舊合約，評級時無法用圖表價格檢查（價格對不上），會標成無法判斷。", now)


def cycle(cdp: CdpClient, conn, outbox: Outbox, st: LoopState, draw: bool) -> None:
    now = int(time.time())
    ev_async = lambda js: cdp.evaluate(js, await_promise=True)
    cfg = load_toml(ROOT / "config" / "account.toml")
    audit_release(conn, outbox, st, now)
    watch_rolls(cdp, conn, outbox, st, now)
    if st.cycles % 20 == 0:
        ensure_history(ev_async)
    collect_secondary(conn, HISTORY_TARGET if st.cycles % 20 == 0 else None)
    st.cycles += 1
    panes = read_panes(cdp.evaluate)
    closes = [p.bars[-1].time + 60 for p in panes if p.bars]
    if closes:
        st.last_bar_close = max(closes)
    for s in panes:
        save_snapshot(conn, s)
    layout = check_mtf_layout(panes)
    if not layout["pairs"]:
        log("no 5m/1m pair: " + "; ".join(layout["issues"]))
        st.layout_bad_since = st.layout_bad_since or now
        if not st.layout_alerted and now - st.layout_bad_since >= TV_DOWN_ALERT_SECONDS and not quiet(st, now):
            outbox.post("HEALTH", f"HEALTH:layout:{now}", "監控：圖表沒有組成同一商品的 5m／1m 一對，暫停 setup 分析"
                        "（交易追蹤照常）：\n" + "\n".join(f"- {i}" for i in layout["issues"]) +
                        "\n\n請把兩個窗格設成同一個商品（左 5m、右 1m）。", now)
            st.layout_alerted = True
        # no return: setup analysis is skipped (no pairs), but trade detection / grading / imports keep running
    else:
        if st.layout_alerted:
            outbox.post("HEALTH", f"HEALTH:layout_ok:{now}", "監控：5m／1m 圖表已恢復，setup 分析繼續。", now)
        st.layout_bad_since, st.layout_alerted = None, False
    if st.events is None or now - st.events_at > 3600:
        st.events, st.events_at = fetch_events(ev_async, now - 3600, now + 2 * 86400), now
    event_reminders(outbox, st.events, now)

    snap = read_broker(ev_async, now)
    record_equity(conn, snap)
    health(outbox, st, now, broker_connected=snap.connected, tv_ok=True)
    if now - st.last_heartbeat >= HEARTBEAT_SECONDS:
        st.last_heartbeat = now
        log(f"heartbeat: cycle {st.cycles}, broker {'connected' if snap.connected else 'DISCONNECTED'}, "
            f"data {', '.join(f'{p.resolution}m {p.data_quality.value}' for p in panes)}")

    cm = ChartManager(conn, ev_async)
    by_index = {p.pane_index: p for p in panes}

    # --- Phase 8: manual trades (any device) -> TRADE STARTED / UPDATE / CLOSED
    bars_by_root, pv_by_root, ctx = {}, {}, {}
    for p in panes:                        # any 1m pane gives prices for trade excursions, paired or not
        if p.resolution == "1":
            bars_by_root.setdefault(root_of(p.symbol.root), [b for b in p.bars if b.is_closed])
            pv_by_root.setdefault(root_of(p.symbol.root), p.symbol.point_value)
    for inst, tf in layout["pairs"].items():
        p1 = by_index[tf["1"]]
        r = root_of(p1.symbol.root)
        bars_by_root[r] = [b for b in p1.bars if b.is_closed]
        pv_by_root[r] = p1.symbol.point_value
        cached = st.cache5.get(inst)
        if cached and cached[1].smc:
            smc = cached[1].smc
            ctx["smc_5m"] = smc.direction
            if smc.events:
                e = smc.events[-1]
                ctx["smc_last"] = f"{e.kind} {e.direction} @ {e.level}"
    row = conn.execute("SELECT state, direction FROM setups ORDER BY updated_at DESC LIMIT 1").fetchone()         if conn.execute("SELECT name FROM sqlite_master WHERE name='setups'").fetchone() else None
    ctx["setup_state"] = f"{row['state']} {row['direction']}" if row else "none"
    tm = TradeMonitor(conn)
    for note in tm.update(snap, now, bars_by_root, pv_by_root, ctx):
        outbox.post(note.kind, f"TRADE:{note.trade_id}:{note.kind}", note.text, now)
        log(f"{note.kind} trade #{note.trade_id}")
    if snap.connected:
        exact_fills(conn, outbox, ev_async, snap, cfg, pv_by_root, now)
    try:                                                       # emotional-trading warnings (warning only)
        tilt = tilt_check(conn, now, cfg)
        if tilt:
            outbox.post("TILT", "TILT:" + "|".join(e.key for e in tilt), tilt_message(tilt, conn, now), now)
            log("tilt warning: " + ", ".join(e.key for e in tilt))
    except Exception as e:
        log(f"tilt check failed: {e!r}")
    # Phase 9: journal + grade every newly closed / imported trade
    refresh_dashboard = False
    for t, g in grade_pending(conn, cfg, lambda a, b: fetch_events(ev_async, a, b), now):
        outbox.post("JOURNAL", f"JOURNAL:{t['id']}", render_journal(g, t), now)
        log(f"journal trade #{t['id']}: grade {g.grade} ({g.classification})")
        refresh_dashboard = True
    if notion_journal(conn, outbox, st, cfg, now):
        refresh_dashboard = True
    # Phase 12: what would each confirmed setup have done? (resolved quietly; reported in the daily summary)
    sync_live_decisions(conn)
    resolve_pending(conn, db_bars(conn), now)
    et_now = to_et(now)
    if et_now.hour == 17 and et_now.minute >= 5:              # after the CME close, once per trading day
        day = trading_day(now)
        key = f"SUMMARY:{day}"
        if not conn.execute("SELECT 1 FROM outbox WHERE key = ?", (key,)).fetchone():
            outbox.post("SUMMARY", key, daily_summary(conn, day, trading_day_start(now), now), now)
            refresh_dashboard = True
    daily_backup(conn, outbox, now)
    history_fill(cdp, conn, st, now)                          # more research data: recover missed days
    if (et_now.hour, et_now.minute) >= (17, 10):
        research_daily(conn, outbox, st, cfg, now)
    if research_journal(conn, outbox, st, cfg, now):
        refresh_dashboard = True
    research_tick(st, now)
    if refresh_dashboard:                                      # Phase 10: local HTML, never uploaded
        try:
            log(f"dashboard -> {write_dashboard(conn, cfg, ROOT, now)}")
        except Exception as e:                                 # a report failure must not stop monitoring
            log(f"dashboard failed: {e!r}")
    hint = tm.peak_equity_hint(pv_by_root)
    if hint is not None and snap.account_name:
        conn.execute("INSERT INTO equity_marks VALUES (?,?,?,?,?)",
                     (now, snap.account_name, snap.balance, hint, "MFE-implied unrealized peak (trade monitor)"))
        conn.commit()
    hw = high_water(conn, snap.account_name or "?", float(cfg["account"]["size_usd"]), snap,
                    float(cfg["account"]["drawdown"]["max_drawdown_usd"]),
                    load_anchor(ROOT / "config" / "account_state.toml"))[0] if snap.connected else None
    # backfill: Tradovate Performance exports dropped into the configured folders are imported automatically
    bcfg = cfg.get("broker", {})
    comm = cfg["costs"].get("commission_roundtrip_per_contract_usd")
    for name, ids in auto_import(conn, bcfg.get("import_folders", []), bcfg.get("tradovate_report_timezone", "UTC"),
                                 snap.account_name, comm if isinstance(comm, (int, float)) else None, now):
        rows = conn.execute(f"SELECT direction, qty, entry, exit, pnl_usd FROM trades WHERE id IN ({','.join('?' * len(ids))})",
                            ids).fetchall() if ids else []
        side = {"LONG": "多", "SHORT": "空"}
        lines = "\n".join(f"- {side.get(r[0], r[0])} {r[1]:g} 口 {r[2]:.1f} → {r[3]:.1f}，淨損益 "
                          f"{f'${r[4]:.2f}' if r[4] is not None else '未知'}" for r in rows)
        outbox.post("IMPORT", f"IMPORT:{name}:{now}", f"已匯入交易\n\n{name}：{len(ids)} 筆\n{lines}", now)
        log(f"imported {len(ids)} trades from {name}")
    n_trades, n_why = trades_today(conn, snap.account_name or "?", trading_day_start(now),
                                   load_reported_trades(ROOT / "config" / "account_state.toml"), trading_day(now)) \
        if snap.connected else (None, "broker not connected")
    if snap.connected and n_trades is None:
        key = f"MISSED:{trading_day(now)}"
        if not conn.execute("SELECT 1 FROM outbox WHERE key = ?", (key,)).fetchone():
            outbox.post("HEALTH", key, "有沒看到的交易\n\n系統沒在看的時候，帳戶餘額有變動"
                        f"（{n_why}）。今天的交易筆數因此未知，交易提醒會先被擋住。\n\n"
                        "通常重新連線後會從成交紀錄自動補回；如果沒有，請在 Tradovate：Reports → Performance "
                        "匯出 CSV 到「文件」資料夾，系統會自動匯入。", now)
    state = gate_state(snap, hw, to_et(now).date(), n_trades)
    for inst, tf in layout["pairs"].items():
        p5, p1 = by_index[tf["5"]], by_index[tf["1"]]
        c1 = [b for b in p1.bars if b.is_closed]
        c5 = [b for b in p5.bars if b.is_closed]
        if not c1 or not c5 or st.last_1m.get(inst) == c1[-1].time:
            continue
        st.last_1m[inst] = c1[-1].time
        if st.cache5.get(inst, (None,))[0] != c5[-1].time:
            hist = c5[-2500:]
            prof = build_profile(hist, ProfileParams())
            if prof is not None:
                prof.fixed_segment_pocs = fixed_segment_pocs(hist, 300, p5.symbol.min_tick or 0.1, atr_series(hist))
            a5 = analyze_timeframe(hist, "5", roll_times=roll_boundaries(conn, root_of(p5.symbol.root)))
            st.cache5[inst] = (c5[-1].time, a5, prof)
            if draw and a5.atr is not None:
                occ = read_indicator_objects(cdp.evaluate, p5.pane_index)
                res = sync(cm, select(a5, p5.bars[-1].close, 12, occ), p5.pane_index, p5.symbol.full_name, now)
                if res["added"] or res["removed"]:
                    log(f"{inst} 5m levels: +{len(res['added'])} -{len(res['removed'])}")
        _, a5, prof = st.cache5[inst]
        evals = evaluate_all(a5, prof, c1[-600:], p1.symbol.min_tick or 0.1, float(cfg["personal"]["min_rr"]))
        shadow_step(conn, st, a5, prof, c1[-600:], p1, cfg, now)      # research gate 2: record only, never alerts

        def risk_check(ev, p1=p1):
            prop = Proposal(inst, ev.direction, ev.entry, ev.stop, ev.target, p1.symbol.point_value,
                            p1.symbol.min_tick, is_micro(p1.symbol.root), now, p1.data_quality.live_usable)
            return risk_evaluate(prop, cfg, state, events=st.events)

        tracker = SetupTracker(conn, inst)
        quality = p1.data_quality.value
        ended = set()
        for n in tracker.update(evals, now, risk_check):
            outbox.post(n.kind, n.key, render(n, inst.split(":")[-1], quality), now)
            log(f"{inst} {n.kind} {n.key}")
            if n.kind == "TRADE_SETUP" and draw:
                draw_setup(cm, n.evaluation, n.key, p1.pane_index, p1.symbol.full_name, now)
            if n.kind == "INVALIDATED":
                ended.add(n.key)
        if draw:
            cleanup_setups(cm, now, ended)
    session_briefing(conn, outbox, st, cfg, layout, by_index, snap, hw, n_trades, pv_by_root, now)
    outbox.retry_failed(now)

    # Telegram query commands (read-only); never allowed to break monitoring
    if outbox.sender is not None and hasattr(outbox.sender, "_call"):
        cap = None
        if all(isinstance(state.get(k), (int, float)) for k in ("balance_usd", "high_water_usd")):
            cap = float(cfg["personal"]["max_loss_per_trade_frac_of_buffer"]) * (
                state["balance_usd"] - (state["high_water_usd"] - float(cfg["account"]["drawdown"]["max_drawdown_usd"])))
        qctx = {"broker_connected": snap.connected, "data": {f"{p.resolution}m {p.symbol.root}": p.data_quality.value for p in panes},
                "smc_5m": ctx.get("smc_5m"), "smc_last": ctx.get("smc_last"),
                "pairing": "OK" if layout["pairs"] else "; ".join(layout["issues"]),
                "gate_state": state, "loss_cap": cap, "max_trades": cfg["personal"]["max_trades_per_day"],
                "point_values": {r: v for r, v in pv_by_root.items() if v},
                "commission": float(cfg["costs"]["commission_roundtrip_per_contract_usd"]),
                "size_range": (int(cfg["personal"]["contracts_per_trade_min"]), int(cfg["personal"]["contracts_per_trade_max"])),
                "events": st.events}
        try:
            n = poll_commands(outbox.sender, conn, qctx, now)
            if n:
                log(f"answered {n} telegram command(s)")
        except Exception as e:                           # network hiccups etc.
            log(f"telegram command poll failed: {type(e).__name__}: {e}")


def keep_awake() -> None:
    """If a computer goes to standby the monitor stops. While the monitor runs, ask Windows not to IDLE-sleep --
    the same request a video player makes. Power settings are not changed; closing the lid, the power button or
    choosing Sleep still sleep. The request ends with the process."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ok = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log("keep-awake requested (idle standby blocked while monitoring)" if ok else "keep-awake request failed")
    except Exception as e:                                     # never a reason to stop monitoring
        log(f"keep-awake unavailable: {e!r}")


def run(db: str, interval: int, sender, draw: bool, max_cycles: int | None = None) -> None:
    conn = connect(db)
    outbox = Outbox(conn, sender)
    st = LoopState()
    log(f"monitoring started (interval {interval}s, sender {'telegram' if sender else 'DRY_RUN'}, "
        f"drawing {'on' if draw else 'off'}) -- analysis only, never places orders")
    if max_cycles is None:
        keep_awake()
    while max_cycles is None or st.cycles < max_cycles:
        try:
            with CdpClient(timeout=120) as cdp:
                while max_cycles is None or st.cycles < max_cycles:
                    t0 = time.time()
                    cycle(cdp, conn, outbox, st, draw)
                    time.sleep(max(1.0, interval - (time.time() - t0)))
        except TradingViewUnavailable as e:
            log(f"TradingView unavailable: {e} -- retrying in 30s")
            health(outbox, st, int(time.time()), tv_ok=False, detail=str(e))
            st.cycles += 0 if max_cycles is None else 1
            time.sleep(30)
        except KeyboardInterrupt:
            log("stopped by user")
            return
        except Exception as e:
            log("cycle error (monitoring continues):\n" + traceback.format_exc(limit=3))
            health(outbox, st, int(time.time()), tv_ok=False, detail=f"{type(e).__name__}: {e}")
            st.cycles += 0 if max_cycles is None else 1
            time.sleep(10)
