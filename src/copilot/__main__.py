"""python -m copilot snapshot [--db PATH] [--bars N]

Output is plain ASCII on purpose: some Windows consoles use a legacy code page (e.g. cp950).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .database.store import connect, save_snapshot
from .tradingview.cdp import CdpClient, TradingViewUnavailable
from .tradingview.chart import check_mtf_layout, read_panes

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "copilot.sqlite3"


def _utc(ts: int | None) -> str:
    return "UNKNOWN" if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def cmd_snapshot(args) -> int:
    try:
        with CdpClient() as cdp:
            panes = read_panes(cdp.evaluate, args.bars)
    except TradingViewUnavailable as e:
        print(f"[FAIL] {e}")
        return 2
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with connect(args.db) as conn:
        for snap in panes:
            _print_pane(snap, save_snapshot(conn, snap))
    layout = check_mtf_layout(panes)
    print("=" * 60)
    print(f"5m/1m LAYOUT  : {'OK' if layout['ok'] else 'NOT OK'}")
    for inst, tf in layout["pairs"].items():
        print(f"  pair        : {inst}  context=pane {tf['5']}  execution=pane {tf['1']}")
    for issue in layout["issues"]:
        print(f"  - {issue}")
    return 0


def _print_pane(snap, counts) -> None:
    s = snap.symbol
    last = snap.bars[-1] if snap.bars else None
    print("=" * 60)
    print(f"pane          : {snap.pane_index}")
    print(f"source        : {snap.source}")
    print(f"captured      : {_utc(snap.fetched_at)}")
    print(f"symbol        : {s.full_name}  (realtime name: {s.pro_name or 'UNKNOWN'})")
    print(f"resolution    : {snap.resolution}")
    print(f"tick / $pt    : {s.min_tick if s.min_tick is not None else 'UNKNOWN'} / {s.point_value if s.point_value is not None else 'UNKNOWN'}")
    print(f"feed delay    : {'UNKNOWN' if s.delay_seconds is None else str(s.delay_seconds) + 's'}")
    print(f"DATA QUALITY  : {snap.data_quality.value}  (live alerts allowed: {'YES' if snap.data_quality.live_usable else 'NO'})")
    for r in snap.quality_reasons:
        print(f"  - {r}")
    print(f"bars          : {len(snap.bars)} loaded, {counts['bars_inserted']} new closed bars stored, "
          f"{counts['discrepancies']} discrepancies")
    if last:
        print(f"last bar      : open {_utc(last.time)}  close {last.close}  ({'closed' if last.is_closed else 'forming'})")
    print(f"studies       : {len(snap.studies)}")
    for st in snap.studies:
        hidden = "  (hidden)" if st.get("visible") is False else ""
        print(f"  - {st.get('name')}{hidden}")


def cmd_indicators(args) -> int:
    import time

    from .database.store import save_indicator_scan
    from .indicator_code_analyzer.reviews import REVIEW_VERSION, review_for
    from .indicator_code_analyzer.static_checks import ANALYZER_VERSION, scan
    from .tradingview.indicators import discover

    out_dir = Path(args.db).parent / "indicator_sources"
    try:
        with CdpClient(timeout=30) as cdp:
            found = discover(lambda js: cdp.evaluate(js, await_promise=True), out_dir)
    except TradingViewUnavailable as e:
        print(f"[FAIL] {e}")
        return 2
    now = int(time.time())
    with connect(args.db) as conn:
        for ind in found:
            hits = scan(Path(ind.source_path).read_text(encoding="utf-8")) if ind.source_path else []
            review, status = review_for(ind.script_id or "", ind.digest or "")
            save_indicator_scan(conn, now, ind, hits, ANALYZER_VERSION, status,
                                REVIEW_VERSION if status == "CURRENT" else None)
            print("=" * 60)
            print(f"{ind.name}")
            print(f"  source      : {ind.source_status} ({ind.status_reason}), {ind.lines or 0} lines, panes {ind.panes}")
            print(f"  review      : {status}")
            if review and status == "CURRENT":
                print(f"  repaints    : {review.repaints}   lookahead: {review.lookahead}")
                print(f"  useful for  : {', '.join(k for k, v in review.roles.items() if v == 'USEFUL') or 'nothing rated USEFUL'}")
            elif status == "STALE":
                print("  !! script changed since the manual review -- review must be redone before relying on it")
            for h in hits:
                print(f"  [{h.severity:6}] {h.check} (lines {', '.join(map(str, h.lines))})")
    return 0


def _et(ts: int | None) -> str:
    from .session_engine.sessions import to_et
    return "UNKNOWN" if ts is None else to_et(ts).strftime("%m-%d %H:%M ET")


def _print_tf(a) -> None:
    print(f"--- {a.resolution}m  ({a.bars_used} closed bars, {_et(a.first_time)} -> {_et(a.last_closed_time)})")
    print(f"  ATR(14)     : {a.atr:.2f}" if a.atr is not None else "  ATR(14)     : UNKNOWN")
    print(f"  regime      : {a.regime.state}")
    for e in a.regime.evidence:
        print(f"      - {e}")
    for s in a.swings[-4:]:
        print(f"  swing       : {s.kind:4} {s.price:<10} {s.label or '-':3} at {_et(s.time)} (confirmed {_et(s.confirmed_time)})")
    for e in a.bos[-3:]:
        print(f"  BOS         : {e.direction:4} close {e.close} at {_et(e.time)} broke {e.swing.kind} {e.swing.price}"
              + ("  [SHIFT]" if e.shift else ""))
    for p in a.periods:
        if p.price is None:
            print(f"  level       : {p.name:9} UNKNOWN ({p.reason})")
        elif p.name in ("PDH", "PDL", "PWH", "PWL", "CDH", "CDL") or p.name.startswith(("ASIA", "LONDON", "NY_AM", "NY_PM")):
            print(f"  level       : {p.name:9} {p.price:<10} {p.period}")
    for ev in [e for e in a.liquidity_events if e.kind != "BREAK"][-4:]:
        print(f"  liquidity   : {ev.kind:14} {ev.level.name} {ev.level.price} at {_et(ev.pierce_time)}"
              f" (excursion {ev.excursion:.2f}, resolved {_et(ev.resolved_time)})")
    open_fvgs = [g for g in a.fvgs if g.status not in ("FILLED", "INVALIDATED")]
    print(f"  FVGs        : {len(a.fvgs)} total, {len(open_fvgs)} still open/partially mitigated")
    for g in open_fvgs[-3:]:
        sz = f"{g.size_atr:.2f} ATR" if g.size_atr is not None else "ATR UNKNOWN"
        print(f"      {g.direction} {g.bottom}-{g.top} ({sz}) {g.status} formed {_et(g.time)}")
    rv20 = f"{a.rv20:.2f}" if a.rv20 is not None else "UNKNOWN"
    rvt = f"{a.rv_tod:.2f}" if a.rv_tod is not None else "UNKNOWN"
    print(f"  volume      : RV20 {rv20} | RV time-of-day {rvt} -> {a.volume_state}")
    print(f"  killzone    : {a.killzone or 'none'}")


def cmd_analyze(args) -> int:
    from .market_structure.analysis import analyze_timeframe, mtf_context
    from .tradingview.chart import ensure_history

    try:
        with CdpClient(timeout=90) as cdp:
            ensure_history(lambda js: cdp.evaluate(js, await_promise=True))
            panes = read_panes(cdp.evaluate)
    except TradingViewUnavailable as e:
        print(f"[FAIL] {e}")
        return 2
    layout = check_mtf_layout(panes)
    with connect(args.db) as conn:
        for snap in panes:
            save_snapshot(conn, snap)
    if not layout["pairs"]:
        print("[STOP] no 5m/1m pair on the same instrument:", "; ".join(layout["issues"]))
        return 1
    by_index = {p.pane_index: p for p in panes}
    for inst, tf in layout["pairs"].items():
        p5, p1 = by_index[tf["5"]], by_index[tf["1"]]
        print("=" * 70)
        print(f"{inst}   data quality: 5m {p5.data_quality.value} / 1m {p1.data_quality.value}"
              f"  (analysis only -- live alerts need REALTIME)")
        a5, a1 = analyze_timeframe(p5.bars, "5"), analyze_timeframe(p1.bars, "1")
        _print_tf(a5)
        _print_tf(a1)
        ctx = mtf_context(a5, a1)
        print(f"--- MTF: 5m bias {ctx.bias} | 1m {ctx.execution_state} | aligned "
              f"{'N/A' if ctx.aligned is None else ctx.aligned}")
        for n in ctx.notes:
            print(f"  - {n}")
        print("  params: swing retrace 3.0 ATR (user-calibrated); sweep reclaim 3 bars, equal-level 0.10 ATR UNCALIBRATED")
    return 0


def cmd_draw_levels(args) -> int:
    import time

    from .chart_manager.drawings import ChartManager
    from .chart_manager.levels import select, sync
    from .market_structure.analysis import analyze_timeframe
    from .tradingview.chart import ensure_history

    now = int(time.time())
    with CdpClient(timeout=120) as cdp, connect(args.db) as conn:
        ensure_history(lambda js: cdp.evaluate(js, await_promise=True))
        panes = read_panes(cdp.evaluate)
        layout = check_mtf_layout(panes)
        if not layout["pairs"]:
            print("[STOP] no 5m/1m pair on the same instrument:", "; ".join(layout["issues"]))
            return 1
        cm = ChartManager(conn, lambda js: cdp.evaluate(js, await_promise=True))
        by_index = {p.pane_index: p for p in panes}
        for inst, tf in layout["pairs"].items():
            p5 = by_index[tf["5"]]
            a5 = analyze_timeframe(p5.bars, "5")
            from .chart_manager.indicator_objects import read as read_indicator_objects
            price = p5.bars[-1].close
            occ = read_indicator_objects(cdp.evaluate, p5.pane_index)
            print(f"  indicator objects on chart (not redrawn): {len(occ.zones)} FVG boxes, "
                  f"{len(occ.lines)} structure lines from {', '.join(occ.sources) or 'none'}")
            wanted = select(a5, price, args.max, occ)
            print(f"{inst} 5m (pane {p5.pane_index}), price {price}, ATR {a5.atr:.2f}: {len(wanted)} objects selected")
            for c in wanted:
                rng = f"{c.low}-{c.high}" if c.kind == "ZONE" else f"{c.high}"
                print(f"  w{c.weight} {c.kind:4} {' / '.join(c.names):28} {rng:<18} {'; '.join(c.reasons)}")
            res = sync(cm, wanted, p5.pane_index, p5.symbol.full_name, now)
            print(f"  chart: kept {len(res['kept'])}, added {len(res['added'])}, removed {len(res['removed'])}")
    return 0


CAL_COLORS = {1.0: "#9e9e9e", 1.5: "#9e9e9e", 2.0: "#4fc3f7", 3.0: "#ffb300", 4.0: "#ff7043", 6.0: "#e040fb"}


def cmd_calibrate_swings(args) -> int:
    """Temporarily draw today's 5m swings labelled with their strength, or clear them again."""
    import time

    from .chart_manager.drawings import ChartManager, Drawing
    from .market_structure.calibration import counts_by_threshold, swing_strengths
    from .session_engine.sessions import trading_day

    now = int(time.time())
    with CdpClient(timeout=90) as cdp, connect(args.db) as conn:
        cm = ChartManager(conn, lambda js: cdp.evaluate(js, await_promise=True))
        if args.action == "clear":
            res = cm.remove(cm.owned(environment="CALIBRATION"), now, "calibration finished")
            print(f"removed {len(res['removed'])} calibration drawings ({len(res['missing'])} were already gone)")
            return 0
        if cm.owned(environment="CALIBRATION"):
            print("[STOP] calibration drawings already on the chart -- run 'calibrate-swings clear' first")
            return 1
        panes = read_panes(cdp.evaluate)
        p5 = next((p for p in panes if p.resolution == "5"), None)
        if p5 is None:
            print("[STOP] no 5m pane")
            return 1
        closed = [b for b in p5.bars if b.is_closed]
        today = trading_day(closed[-1].time)
        strengths = swing_strengths(closed)
        todays = [(s, k) for s, k in strengths if trading_day(s.time) == today]
        print(f"5m swings in trading day {today}: {len(todays)} at >=1.0 ATR")
        for k, n in counts_by_threshold(todays).items():
            print(f"  threshold {k:>3} ATR -> {n} swings")
        for s, k in todays:
            cm.draw(Drawing(
                pane_index=p5.pane_index, symbol=p5.symbol.full_name, environment="CALIBRATION",
                type=f"SWING_{s.kind}", tier=3, creation_time=now, source="calibration-0.1", timeframe="5",
                price_high=s.price, price_low=s.price, strength=k, status="ACTIVE",
                reason=f"swing strength {k} ATR (calibration only)", confidence="UNKNOWN",
                expiration=now + 6 * 3600, validated=False,
                shape="text", time1=s.time, text=f"{'H' if s.kind == 'HIGH' else 'L'} {k:g}",
                color=CAL_COLORS[k]))
        print(f"drew {len(todays)} temporary labels on pane {p5.pane_index}")
    return 0


def cmd_events(args) -> int:
    import time

    from .event_engine.calendar import HIGH, SOURCE, fetch
    from .session_engine.sessions import to_et

    now = int(time.time())
    try:
        with CdpClient(timeout=60) as cdp:
            ev = fetch(lambda js: cdp.evaluate(js, await_promise=True), now - 3600, now + args.days * 86400)
    except TradingViewUnavailable as e:
        print(f"[FAIL] {e}")
        return 2
    if ev is None:
        print("[UNKNOWN] economic calendar unavailable -> risk gate will block (news window UNKNOWN)")
        return 1
    print(f"source: {SOURCE}; next {args.days} days, US; blackout = HIGH importance -30/+30 min")
    for e in ev:
        if e.importance < args.min_importance:
            continue
        tag = "HIGH  " if e.importance >= HIGH else "medium"
        w = (f"  no entries {to_et(e.time - 1800):%H:%M}-{to_et(e.time + 1800):%H:%M} ET" if e.importance >= HIGH else "")
        print(f"  {to_et(e.time):%a %m-%d %H:%M} ET  {tag}  {e.title}{w}")
    return 0


def cmd_account(args) -> int:
    """Read-only: account state from the broker connection, recorded for the trailing high-water mark."""
    import time

    from .broker.tradingview_broker import gate_state, high_water, load_anchor, read, record
    from .risk_engine.gate import load_toml
    from .session_engine.sessions import to_et

    now = int(time.time())
    cfg = load_toml(Path(__file__).resolve().parents[2] / "config" / "account.toml")
    try:
        with CdpClient(timeout=60) as cdp:
            snap = read(lambda js: cdp.evaluate(js, await_promise=True), now)
    except TradingViewUnavailable as e:
        print(f"[FAIL] {e}")
        return 2
    with connect(args.db) as conn:
        record(conn, snap)
        size, max_dd = cfg["account"].get("size_usd"), cfg["account"].get("drawdown", {}).get("max_drawdown_usd")
        known = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (size, max_dd))
        hw, why = (high_water(conn, snap.account_name or "?", float(size), snap, float(max_dd),
                              load_anchor(Path(__file__).resolve().parents[2] / "config" / "account_state.toml"))
                   if snap.connected and known else
                   (None, "not connected" if not snap.connected else "account size / drawdown not set in config"))
    name = snap.account_name or "UNKNOWN"
    masked = name[:4] + "..." + name[-4:] if len(name) > 8 else name
    print(f"source        : {snap.source}  connected={snap.connected}")
    print(f"account       : {masked} ({snap.account_type})")
    print(f"balance/equity: {snap.balance} / {snap.equity}   open positions {snap.open_positions}, "
          f"working orders {snap.working_orders}, executions {len(snap.executions)}")
    print(f"high-water    : {hw if hw is not None else 'UNKNOWN'}  ({why})")
    for prob in snap.problems:
        print(f"  ! {prob}")
    from .broker.tradingview_broker import load_reported_trades, trades_today
    from .session_engine.sessions import trading_day, trading_day_start
    with connect(args.db) as conn:
        n, nwhy = trades_today(conn, snap.account_name or "?", trading_day_start(now),
                               load_reported_trades(Path(__file__).resolve().parents[2] / "config" / "account_state.toml"),
                               trading_day(now)) if snap.connected else (None, "not connected")
    print(f"trades today  : {n if n is not None else 'UNKNOWN'}  ({nwhy})")
    print("gate state    :", gate_state(snap, hw, to_et(now).date(), n))
    return 0


def config_ready() -> bool:
    """The monitor needs a complete config/account.toml (onboarding, AGENTS.md step 3). Prints what is missing."""
    from .risk_engine.gate import config_problems, load_toml
    problems = config_problems(load_toml(Path(__file__).resolve().parents[2] / "config" / "account.toml"))
    if problems:
        print("config/account.toml is not complete -- the monitor does not start until these are filled in "
              "(values from you or your firm's written rules, never guessed):")
        print(chr(10).join("  " + p for p in problems))
    return not problems


def cmd_run(args) -> int:
    from .monitor import run
    from .notifications.telegram import TelegramSender

    if not config_ready():
        return 0                                       # 0: the watchdog must not restart / alert in a loop

    sender = None
    if not args.dry_run:
        try:
            sender = TelegramSender()
        except RuntimeError as e:
            print(f"[INFO] {e} -> running in DRY_RUN mode (messages are recorded in the outbox, not sent)")
    run(args.db, args.interval, sender, draw=not args.no_draw)
    return 0


def cmd_telegram_setup(args) -> int:
    from .notifications.telegram import TelegramSender
    try:
        chats = TelegramSender().recent_chats()
    except RuntimeError as e:
        print(f"[FAIL] {e}")
        return 1
    if not chats:
        print("No chats found. Send any message to your bot in Telegram first, then run this again.")
        return 1
    for c in chats:
        print(f"chat id {c['id']}  ({c['type']}, {c['name']})")
    print('Set it with:  [Environment]::SetEnvironmentVariable("COPILOT_TELEGRAM_CHAT_ID", "<id>", "User")')
    return 0


def cmd_telegram_test(args) -> int:
    from .notifications.telegram import TelegramSender
    try:
        TelegramSender().send("Trading co-pilot: test message. Analysis only -- this system never places orders.")
    except RuntimeError as e:
        print(f"[FAIL] {e}")
        return 1
    print("sent")
    return 0


def cmd_grade(args) -> int:
    """Grade closed trades (all ungraded, or --regrade everything) and print the journals."""
    import time

    from .event_engine.calendar import fetch
    from .risk_engine.gate import load_toml
    from .trade_journal.grader import grade_pending, render

    cfg = load_toml(Path(__file__).resolve().parents[2] / "config" / "account.toml")
    now = int(time.time())
    with connect(args.db) as conn:
        if args.regrade:
            conn.execute("DELETE FROM trade_grades") if conn.execute(
                "SELECT name FROM sqlite_master WHERE name='trade_grades'").fetchone() else None
        try:
            with CdpClient(timeout=60) as cdp:
                done = grade_pending(conn, cfg, lambda a, b: fetch(lambda js: cdp.evaluate(js, await_promise=True), a, b), now)
        except TradingViewUnavailable:
            print("[INFO] TradingView unavailable -> news-window check will be UNKNOWN")
            done = grade_pending(conn, cfg, None, now)
    for t, g in done:
        print("=" * 70)
        print(render(g, t))
    if not done:
        print("no closed trades waiting for a grade")
    return 0


def cmd_alerts(args) -> int:
    """Alert outcome report; --backfill-days N replays stored bars to build a sample (source=replay)."""
    import time
    from collections import defaultdict
    from datetime import timedelta

    from .alert_tracking.outcomes import db_bars, replay_backfill, resolve_pending, sync_live_decisions
    from .risk_engine.gate import load_toml
    from .session_engine.sessions import trading_day

    cfg = load_toml(Path(__file__).resolve().parents[2] / "config" / "account.toml")
    now = int(time.time())
    with connect(args.db) as conn:
        if args.backfill_days:
            last = conn.execute("SELECT MAX(time) FROM bars WHERE resolution='1' AND feed_symbol LIKE ?",
                                (f"%:{args.root}1!",)).fetchone()[0]
            first = conn.execute("SELECT MIN(time) FROM bars WHERE resolution='1' AND feed_symbol LIKE ?",
                                 (f"%:{args.root}1!",)).fetchone()[0]
            today = trading_day(now)
            days = sorted({trading_day(t) for t in range(first + 86400, last, 3600)} - {today})[-args.backfill_days:]
            print(f"replaying {len(days)} complete trading days: {', '.join(map(str, days))}")
            n = replay_backfill(conn, args.root, args.tick, float(cfg["personal"]["min_rr"]), days)
            print(f"recorded {n} replay decisions")
        sync_live_decisions(conn)
        resolve_pending(conn, db_bars(conn), now)
        rows = conn.execute("SELECT source, decision, filtered_by, outcome, r, classification, user_traded"
                            " FROM alert_outcomes").fetchall()
    if not rows:
        print("no confirmed setups recorded yet")
        return 0
    groups = defaultdict(list)
    for r in rows:
        groups[(r[0], r[1])].append(r)
    print(f"\n{'source':7} {'decision':18} {'n':>3} {'win':>4} {'loss':>4} {'open':>4} {'win%':>5} {'avg R':>6} {'total R':>8}")
    for (src, dec), rs in sorted(groups.items()):
        done = [x for x in rs if x[3] in ("WIN", "LOSS", "TIMEOUT")]
        wins = sum(1 for x in done if x[3] == "WIN" or (x[3] == "TIMEOUT" and (x[4] or 0) > 0))
        tot = sum(x[4] for x in done if x[4] is not None)
        print(f"{src:7} {dec:18} {len(rs):>3} {wins:>4} {len(done) - wins:>4} {len(rs) - len(done):>4} "
              f"{(100 * wins / len(done)) if done else 0:>5.0f} {(tot / len(done)) if done else 0:>6.2f} {tot:>8.2f}")
    print("\nfiltered setups by the filter that blocked them (would-be result):")
    by_f = defaultdict(list)
    for r in rows:
        if r[1] == "FILTERED" and r[4] is not None:
            for f in (r[2] or "?").split(", "):
                by_f[f].append(r[4])
    for f, rs in sorted(by_f.items(), key=lambda kv: -len(kv[1])):
        print(f"  {f:28} n={len(rs):>3}  avg {sum(rs) / len(rs):+.2f}R  total {sum(rs):+.2f}R")
    n_done = sum(1 for r in rows if r[4] is not None)
    if n_done < 30:
        print(f"\nNOTE: only {n_done} resolved setups -- far too few for conclusions (descriptive only).")
    return 0


def research_days(conn, root: str, now: int, context_after: int = 0) -> list:
    """Complete stored 1m days of root. context_after: 5m bars before this time do not count as context (e.g. a
    contract roll jump inside the look-back that no roll record covers)."""
    from collections import Counter

    from .session_engine.sessions import trading_day, trading_day_start
    times = [r[0] for r in conn.execute(
        "SELECT DISTINCT time FROM bars WHERE resolution='1' AND (feed_symbol LIKE ? OR feed_symbol LIKE ?)",
        (f"%:{root}1!", f"%:{root}"))]
    if not times:
        return []
    counts = Counter(trading_day(t) for t in times)
    first = min(times)
    # the first stored day is usually partial (history started mid-session) -- unless it starts at the open
    partial_first = trading_day(first) if first > trading_day_start(first) else None
    days = sorted(d for d, n in counts.items() if n >= 600 and d != partial_first and d != trading_day(now))
    # the 5m context needs the same look-back the backtest uses (2,500 bars), else early days of a newly added
    # instrument would be judged on a thinner profile / structure than the others
    t5 = sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT time FROM bars WHERE resolution='5' AND (feed_symbol LIKE ? OR feed_symbol LIKE ?)"
        " AND time >= ?", (f"%:{root}1!", f"%:{root}", context_after)))
    import bisect
    return [d for d in days if bisect.bisect_left(t5, _day_start(d)) >= CONTEXT_BARS_5M]


CONTEXT_BARS_5M = 2500


def _day_start(d) -> int:
    from datetime import datetime

    from .session_engine.sessions import ET, trading_day_start
    return trading_day_start(int(datetime(d.year, d.month, d.day, 12, tzinfo=ET).timestamp()))


def research_markets(conn, cfg: dict, now: int) -> list:
    """Instruments pooled in research (config [research].roots). Tick from TradingView symbol info stored with the
    chart snapshots; commission per instrument from [costs] -- an instrument without a known tick is skipped."""
    from datetime import datetime

    from .research.auto import Market
    costs = cfg["costs"]
    by_root = costs.get("commission_roundtrip_by_root", {})
    out = []
    for root in cfg.get("research", {}).get("roots", []):
        row = conn.execute("SELECT min_tick FROM chart_snapshots WHERE (feed_symbol LIKE ? OR feed_symbol LIKE ?)"
                           " AND min_tick IS NOT NULL ORDER BY fetched_at DESC LIMIT 1",
                           (f"%:{root}1!", f"%:{root}")).fetchone()
        if not row:
            print(f"research: {root} skipped -- tick size unknown (no chart snapshot)")
            continue
        comm = by_root.get(root, costs.get("commission_roundtrip_per_contract_usd"))
        after = cfg.get("research", {}).get("context_after", {}).get(root)
        after = int(datetime.fromisoformat(after).timestamp()) if after else 0
        out.append(Market(root, float(row[0]), tuple(research_days(conn, root, now, after)),
                          comm if isinstance(comm, (int, float)) else None))
    return out


def forward_step(conn, markets: list, min_rr: float, now: int) -> list:
    """Replay the newest completed days for every SHADOW hypothesis, then decide those with enough forward trades."""
    from .research.shadow import evaluate_forward_replay, forward_replay
    try:
        n = forward_replay(conn, markets, min_rr, now)
        if n:
            print(f"forward replay: {n} day result(s) stored")
        return evaluate_forward_replay(conn)
    except Exception as e:                                 # a forward-test problem must not stop the research
        print(f"forward replay failed: {e!r}")
        return []


def research_auto(args, cfg: dict, now: int) -> int:
    """One batch of the continuous research loop (started by the monitor whenever the previous batch has finished).
    Never changes production. Telegram only when a strategy passes the backtest; otherwise the monitor sends one
    digest per trading day."""
    import json
    import os
    import time

    from .notifications.telegram import Outbox, TelegramSender
    from .research.auto import full_report, label, run_cycle, sample_milestone, summary_text

    status = Path(__file__).resolve().parents[2] / "data" / "research_status.json"
    if not isinstance(cfg["personal"].get("min_rr"), (int, float)):
        print("research: [personal] min_rr is not set in config/account.toml -- nothing to test")
        status.write_text(json.dumps({"ended_at": now, "tested": 0, "remaining": None}))
        return 0
    status.write_text(json.dumps({"running_pid": os.getpid(), "started_at": now}))
    tested = 0
    try:
        with connect(args.db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")       # long reads here must not block the monitor's writes
            markets = research_markets(conn, cfg, now)
            if not markets:
                print("research: no instruments in config [research].roots -- nothing to test")
                return 0
            forward = forward_step(conn, markets, float(cfg["personal"]["min_rr"]), now)
            t0 = time.time()
            s = run_cycle(conn, markets, float(cfg["personal"]["min_rr"]), now, args.batch)
            tested = len(s["tested"])
            text = summary_text(s) + f"\n\n（耗時 {(time.time() - t0) / 60:.0f} 分鐘）"
            print(f"[{time.strftime('%Y-%m-%d %H:%M')}] {text}")
            done = tested > 0 and s.get("remaining") == 0          # every hypothesis tested on the current data
            if done:
                report = full_report(conn)
                print(report)
                (Path(__file__).resolve().parents[2] / "data" / "reports" / "research_report.txt").write_text(
                    report, encoding="utf-8")
            milestone = sample_milestone(conn, s)
            if milestone:
                print(milestone)
            if s["shadow"] or done or milestone or forward:
                try:
                    sender = TelegramSender()
                except RuntimeError:
                    sender = None
                conn.row_factory = None
                ob = Outbox(conn, sender)
                if s["shadow"]:
                    ob.post("RESEARCH", f"RESEARCH_SHADOW:{now}",
                            "有策略通過回測，開始前瞻影子測試（只記錄，不發交易提醒、不改策略）：\n"
                            + "\n".join(f"- {label(h)}" for h in s["shadow"]) + "\n\n" + text, int(time.time()))
                if done:
                    ob.post("RESEARCH", f"RESEARCH_DONE:{now}", report, int(time.time()))
                if milestone:
                    ob.post("RESEARCH", "RESEARCH_SAMPLE_ENOUGH", milestone, int(time.time()))
                for hid, st_, detail in forward:
                    if st_ == "PROPOSED":
                        ob.post("PROPOSAL", f"PROPOSAL:{hid}",
                                f"找到可能可行的策略（需要你決定）\n\n{label(hid)}\n{detail}\n\n已通過：回測（扣手續費、"
                                "樣本外、統計檢定）＋前瞻測試。目前策略沒有變動。\n要不要改用，請在 Claude 對話裡告訴我，"
                                "我會先給你完整報告。", int(time.time()))
                    else:
                        ob.post("RESEARCH", f"FORWARD:{hid}", f"前瞻測試未通過：{label(hid)}\n{detail}\n"
                                "已排除，目前策略不變。", int(time.time()))
    finally:
        status.write_text(json.dumps({"ended_at": int(time.time()), "tested": tested,
                                      "remaining": None if not tested else s.get("remaining")}))
    return 0


def cmd_research(args) -> int:
    """Backtest + walk-forward every registered hypothesis against production on stored history."""
    import time

    from .research.backtest import Variant, simulate, walk_forward
    from .research.hypotheses import record, seed, variants
    from .risk_engine.gate import load_toml
    from .session_engine.sessions import trading_day

    cfg = load_toml(Path(__file__).resolve().parents[2] / "config" / "account.toml")
    now = int(time.time())
    if args.auto:
        return research_auto(args, cfg, now)
    with connect(args.db) as conn:
        seed(conn, now)
        from collections import Counter
        counts = Counter(trading_day(r[0]) for r in conn.execute(
            "SELECT DISTINCT time FROM bars WHERE resolution='1' AND (feed_symbol LIKE ? OR feed_symbol LIKE ?)",
            (f"%:{args.root}1!", f"%:{args.root}")))
        first_day = min(counts)               # the first stored day is partial (history started mid-session)
        days = sorted(d for d, n in counts.items() if n >= 600 and d != first_day and d != trading_day(now))
        hyp = variants()
        vs = [Variant("production")] + list(hyp.values())
        print(f"backtesting {len(vs)} variants over {len(days)} trading days ({days[0]} .. {days[-1]}) -- research only")
        t0 = time.time()
        res = simulate(conn, args.root, args.tick, float(cfg["personal"]["min_rr"]), days, vs, now)
        print(f"simulated in {time.time() - t0:.0f}s")
        verdicts = walk_forward(res, days)
        by_name = {v.name: hid for hid, v in hyp.items()}
        for vd in verdicts:
            if vd.variant in by_name:
                record(conn, by_name[vd.variant], vd, {"days": [str(d) for d in days], "root": args.root}, now)
    print(f"\n{'variant':22} {'IS n':>5} {'IS exp':>7} {'OOS n':>6} {'OOS exp':>8} {'OOS maxDD':>9} {'P(better)':>9}  verdict")
    for vd in verdicts:
        print(f"{vd.variant:22} {vd.in_sample.n:>5} {vd.in_sample.expectancy:>+7.2f} {vd.out_sample.n:>6} "
              f"{vd.out_sample.expectancy:>+8.2f} {vd.out_sample.max_dd_r:>9.2f} "
              f"{(f'{vd.p_better:.2f}' if vd.p_better is not None else '-'):>9}  {vd.verdict}")
        for r in vd.reasons:
            print(f"{'':24}- {r}")
    print("\nNothing above changes production. A CANDIDATE still needs more data, paper monitoring and your approval.")
    return 0


def cmd_rules(args) -> int:
    from .alert_tracking.rules_report import build, text
    with connect(args.db) as conn:
        print(text(build(conn, args.source)))
    return 0


def cmd_notion_setup(args) -> int:
    """One-time: create both Notion databases and print the ids to put into config/account.toml [notion]."""
    from .research.journal import create_database as create_research
    from .trade_journal.notion_sync import NotionClient, NotionError, create_journal
    try:
        client = NotionClient()
        journal = create_journal(client, args.parent)
        research = create_research(client, args.parent)
    except NotionError as e:
        print(f"Notion setup failed: {e}")
        return 1
    print("Created. Put these into config/account.toml under [notion]:")
    print(f'database_id = "{journal}"')
    print(f'research_database_id = "{research}"')
    return 0


def cmd_notion_sync(args) -> int:
    import time

    from .risk_engine.gate import load_toml
    from .trade_journal.notion_sync import SCHEMA, NotionClient, NotionError, sync

    root = Path(__file__).resolve().parents[2]
    db = load_toml(root / "config" / "account.toml").get("notion", {}).get("database_id")
    try:
        client = NotionClient()
    except NotionError as e:
        print(f"[FAIL] {e}")
        return 1
    with connect(args.db) as conn:
        if args.resync:
            conn.executescript(SCHEMA)
            conn.execute("UPDATE notion_sync SET graded_at = -1")
            conn.commit()
        n, errors = sync(conn, client, db, int(time.time()),
                         load_toml(root / "config" / "account.toml").get("notion", {}).get("account_labels"))
    print(f"[{'OK' if not errors else 'FAIL'}] {n} page(s) written" + "".join(f"\n  {e}" for e in errors))
    return 0 if not errors else 1


def cmd_supervise(args) -> int:
    from .ops.supervisor import supervise, telegram_notifier

    root = Path(__file__).resolve().parents[2]
    if not config_ready():
        return 0
    return supervise(root / "src", root / "data" / "logs" / "monitor.log", telegram_notifier(Path(args.db)))


def cmd_backup(args) -> int:
    import time

    from .ops.backup import backup

    root = Path(__file__).resolve().parents[2]
    with connect(args.db) as conn:
        res = backup(conn, root / "data" / "backups", int(time.time()))
    print(f"[{'OK' if res.ok else 'FAIL'}] {res.path or ''} {res.size_mb} MB -- {res.detail}; pruned {res.pruned}")
    return 0 if res.ok else 1


def cmd_dashboard(args) -> int:
    import os
    import time

    from .reports.dashboard import write
    from .risk_engine.gate import load_toml

    root = Path(__file__).resolve().parents[2]
    with connect(args.db) as conn:
        out = write(conn, load_toml(root / "config" / "account.toml"), root, int(time.time()))
    print(out)
    if args.open:
        os.startfile(out)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="copilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("rules", help="which rules help or cost (every potential setup, entered or not)")
    rp.add_argument("--source", default="live", choices=["live", "replay"])
    rp.add_argument("--db", default=str(DEFAULT_DB))
    rp.set_defaults(func=cmd_rules)
    nsu = sub.add_parser("notion-setup", help="create the 交易日誌 and 研究日誌 databases under a Notion page")
    nsu.add_argument("--parent", required=True, help="id of a Notion page shared with your integration")
    nsu.set_defaults(func=cmd_notion_setup)
    ns = sub.add_parser("notion-sync", help="write graded trades to the Notion 交易日誌 database")
    ns.add_argument("--resync", action="store_true", help="rewrite every page's system fields and body")
    ns.add_argument("--db", default=str(DEFAULT_DB))
    ns.set_defaults(func=cmd_notion_sync)
    sv = sub.add_parser("supervise", help="run the monitor under a watchdog (restart + Telegram alert on crash)")
    sv.add_argument("--db", default=str(DEFAULT_DB))
    sv.set_defaults(func=cmd_supervise)
    bk = sub.add_parser("backup", help="verified copy of the database to data/backups (keeps 14)")
    bk.add_argument("--db", default=str(DEFAULT_DB))
    bk.set_defaults(func=cmd_backup)
    dsp = sub.add_parser("dashboard", help="write the local performance dashboard (data/reports/dashboard.html)")
    dsp.add_argument("--open", action="store_true")
    dsp.add_argument("--db", default=str(DEFAULT_DB))
    dsp.set_defaults(func=cmd_dashboard)
    rsp = sub.add_parser("research", help="backtest + walk-forward the registered hypotheses (research only)")
    rsp.add_argument("--auto", action="store_true", help="one run of the continuous research loop")
    rsp.add_argument("--batch", type=int, default=20, help="hypotheses per --auto run")
    rsp.add_argument("--root", default="MGC")
    rsp.add_argument("--tick", type=float, default=0.1)
    rsp.add_argument("--db", default=str(DEFAULT_DB))
    rsp.set_defaults(func=cmd_research)
    alp = sub.add_parser("alerts", help="alert outcome report (would-be results of every confirmed setup)")
    alp.add_argument("--backfill-days", type=int, default=0)
    alp.add_argument("--root", default="MGC")
    alp.add_argument("--tick", type=float, default=0.1)
    alp.add_argument("--db", default=str(DEFAULT_DB))
    alp.set_defaults(func=cmd_alerts)
    gp = sub.add_parser("grade", help="grade closed trades and print the journal")
    gp.add_argument("--regrade", action="store_true")
    gp.add_argument("--db", default=str(DEFAULT_DB))
    gp.set_defaults(func=cmd_grade)
    rp = sub.add_parser("run", help="live monitoring loop (analysis + alerts; never places orders)")
    rp.add_argument("--interval", type=int, default=30)
    rp.add_argument("--dry-run", action="store_true", help="record messages in the outbox without sending")
    rp.add_argument("--no-draw", action="store_true", help="do not touch chart drawings")
    rp.add_argument("--db", default=str(DEFAULT_DB))
    rp.set_defaults(func=cmd_run)
    sub.add_parser("telegram-setup", help="list chat ids that messaged your bot").set_defaults(func=cmd_telegram_setup)
    sub.add_parser("telegram-test", help="send one test message").set_defaults(func=cmd_telegram_test)
    acp = sub.add_parser("account", help="read-only account state from the TradingView broker connection")
    acp.add_argument("--db", default=str(DEFAULT_DB))
    acp.set_defaults(func=cmd_account)
    ep = sub.add_parser("events", help="upcoming economic events from TradingView's calendar")
    ep.add_argument("--days", type=int, default=7)
    ep.add_argument("--min-importance", type=int, default=0)
    ep.set_defaults(func=cmd_events)
    dp = sub.add_parser("draw-levels", help="Phase 4: sync the 5m context drawings (max 12) with current analysis")
    dp.add_argument("--max", type=int, default=12)
    dp.add_argument("--db", default=str(DEFAULT_DB))
    dp.set_defaults(func=cmd_draw_levels)
    cp = sub.add_parser("calibrate-swings", help="draw/clear temporary swing-strength labels on the 5m pane")
    cp.add_argument("action", choices=["draw", "clear"])
    cp.add_argument("--db", default=str(DEFAULT_DB))
    cp.set_defaults(func=cmd_calibrate_swings)
    ap = sub.add_parser("analyze", help="Phase 3 structure / liquidity / FVG / volume analysis of the 5m+1m panes")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.set_defaults(func=cmd_analyze)
    ip = sub.add_parser("indicators", help="fetch indicator sources, scan them, and check review status")
    ip.add_argument("--db", default=str(DEFAULT_DB))
    ip.set_defaults(func=cmd_indicators)
    sp = sub.add_parser("snapshot", help="capture the active TradingView chart into the local DB")
    sp.add_argument("--db", default=str(DEFAULT_DB))
    sp.add_argument("--bars", type=int, default=5000)
    sp.set_defaults(func=cmd_snapshot)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
