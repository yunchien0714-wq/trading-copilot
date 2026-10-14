"""Trade grading + improvement notes (spec sections 17, 20, 21). Deterministic and evidence-based.

Every check is judged with data available AT ENTRY (bars closed before the entry), so hindsight cannot make a
trade look better or worse. Missing data -> UNKNOWN, never guessed; a grade resting on UNKNOWN critical inputs is
marked provisional.

Grades (the configured plan's rules):
  A  structure aligned + setup zone (5m FVG overlapping a POC) + 1m confirmation + risk rules + execution OK
  B  setup valid, minor execution deviation (size outside the 5-10 plan, chasing, ...)
  C  setup incomplete (no POC confluence / no confirmation) but no rule broken
  D  a strategy or risk rule broken (against 5m structure, no stop, RR < min, over the loss cap, news window,
     too many trades, more than one open position)
Quality (good/bad trade) and outcome (win/loss) are judged separately -- spec section 17.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from ..event_engine.calendar import blackout
from ..risk_engine.gate import size_for
from ..market_data.models import Bar
from ..market_data.series import atr as atr_series
from ..market_data.rolls import boundaries as roll_boundaries, front_at
from ..market_structure.analysis import analyze_timeframe
from ..session_engine.sessions import killzone, to_et
from ..setup_engine.poc_fvg import PLAN, evaluate_all, zone_key   # grading uses the user's plan
from ..trade_monitor.monitor import root_of
from ..volume.profile import ProfileParams, build_profile, fixed_segment_pocs

GRADER_VERSION = "grader-0.4.0"   # 0.4: graded against PLAN (FVG retest reversal)
PASS, FAIL, WARN, UNKNOWN = "PASS", "FAIL", "WARN", "UNKNOWN"
# USER-CALIBRATED: an entry 0.5 ATR (1.8 pts) above a long zone was judged "fine" (at the zone).
# 0.6 keeps that case inside with a small margin; beyond it the entry counts as chasing.
ZONE_TOL_ATR = 0.6
CHASE_ATR = 0.6

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_grades (
    trade_id INTEGER PRIMARY KEY, graded_at INTEGER NOT NULL, grade TEXT NOT NULL, provisional INTEGER NOT NULL,
    quality TEXT NOT NULL, outcome TEXT NOT NULL, classification TEXT NOT NULL,
    checks TEXT NOT NULL, went_well TEXT NOT NULL, improve TEXT NOT NULL, lesson TEXT NOT NULL,
    metrics TEXT NOT NULL, version TEXT NOT NULL
);
"""


@dataclass
class Grade:
    trade_id: int
    grade: str
    provisional: bool
    quality: str            # GOOD | BAD
    outcome: str            # WIN | LOSS | FLAT | UNKNOWN
    classification: str     # e.g. "Good trade / Bad outcome"
    checks: list = field(default_factory=list)      # (category, name, status, detail)
    went_well: list = field(default_factory=list)
    improve: list = field(default_factory=list)
    lesson: str = ""
    metrics: dict = field(default_factory=dict)


def _bars(conn: sqlite3.Connection, root: str, res: str, t_from: int, t_to: int) -> list[Bar]:
    """Closed bars for a contract root from the local store, preferring the realtime feed over the delayed one."""
    rows = conn.execute("SELECT feed_symbol, time, open, high, low, close, volume FROM bars WHERE resolution=? "
                        "AND (feed_symbol LIKE ? OR feed_symbol LIKE ?) AND time >= ? AND time <= ? ORDER BY time",
                        (res, f"%:{root}1!", f"%:{root}", t_from, t_to)).fetchall()
    by_t: dict[int, tuple] = {}
    for r in rows:
        if r[1] not in by_t or "_DL:" in by_t[r[1]][0]:
            by_t[r[1]] = r
    return [Bar(r[1], r[2], r[3], r[4], r[5], r[6], True) for _, r in sorted(by_t.items())]


def grade_trade(conn: sqlite3.Connection, t: sqlite3.Row, cfg: dict, events, point_value: float | None,
                trades_before_today: int | None, loss_cap: float | None, tick: float | None = None) -> Grade:
    C: list = []

    def chk(cat, name, status, detail):
        C.append((cat, name, status, detail))

    long = t["direction"] == "LONG"
    sgn = 1 if long else -1
    root = root_of(t["symbol"])
    et = t["entry_time"]
    entry, stop, target, qty = t["entry"], t["initial_stop"], t["initial_target"], t["qty"]
    m: dict = {"entry_et": to_et(et).strftime("%m-%d %H:%M ET"), "killzone": killzone(et) or "none"}

    # ---------------- context as of entry (bars closed before the entry minute)
    b5 = _bars(conn, root, "5", et - 2600 * 300, et - 300)
    b1 = _bars(conn, root, "1", et - 600 * 60, et - 60)
    # the stored bars are the chart's continuous symbol: only comparable with fills in the chart's front contract
    front = front_at(conn, root, et)
    mismatch = front is not None and not t["symbol"].endswith("!") and t["symbol"] != front
    why_no_a5 = (f"traded {t['symbol']} while the chart showed {front} (contract roll) -- prices not comparable"
                 if mismatch else "not enough stored 5m history before the entry")
    a5 = (analyze_timeframe(b5, "5", roll_times=roll_boundaries(conn, root))
          if len(b5) > 50 and not mismatch else None)
    if a5 and a5.smc and a5.smc.direction in ("BULLISH", "BEARISH"):
        want = "BULLISH" if long else "BEARISH"
        last = a5.smc.events[-1] if a5.smc.events else None
        chk("strategy", "5m structure aligned", PASS if a5.smc.direction == want else FAIL,
            f"5m SMC {a5.smc.direction} at entry" + (f" (last {last.kind} {last.direction} @ {last.level})" if last else ""))
        m["smc_5m"] = a5.smc.direction
    else:
        chk("strategy", "5m structure aligned", UNKNOWN, why_no_a5)

    zone_ev = None
    if a5 and a5.atr:
        prof = build_profile(b5[-300:], ProfileParams())
        if prof:
            prof.fixed_segment_pocs = fixed_segment_pocs(b5, 300, tick or 0.1, atr_series(b5))
        evs = [e for e in evaluate_all(a5, prof, b1, tick or 0.1, float(cfg["personal"]["min_rr"]), PLAN)
               if e.direction == t["direction"]] if prof and b1 else []
        tol = ZONE_TOL_ATR * a5.atr
        at_zone = [e for e in evs if e.zone.low - tol <= entry <= e.zone.high + tol]
        if at_zone:
            zone_ev = min(at_zone, key=lambda e: abs((e.zone.low + e.zone.high) / 2 - entry))
            # Entered INSIDE the confirming 1m bar (before it closed)? Then the setup is judged with that bar, and the
            # early entry is an execution note -- not a second strategy violation .
            anticipated = None
            if zone_ev.confirmed_at is None:
                bar0 = et - et % 60
                nxt = _bars(conn, root, "1", bar0, bar0)
                if nxt:
                    later = [e for e in evaluate_all(a5, prof, b1 + nxt, tick or 0.1, float(cfg["personal"]["min_rr"]), PLAN)
                             if e.direction == t["direction"] and zone_key(e.zone) == zone_key(zone_ev.zone)]
                    if later and later[0].confirmed_at == bar0:
                        anticipated = later[0]
            eff = anticipated or zone_ev
            if eff.setup_type == "REVERSAL":
                # against the 5m structure, but a valid reversal (zone retrace + 1m rejection, )
                C[:] = [(cat, n, PASS, eff.conditions["5m structure"][1])
                        if n == "5m structure aligned" and s == FAIL else (cat, n, s, d) for cat, n, s, d in C]
                m["setup_type"] = "REVERSAL"
            chk("strategy", "setup zone (5m FVG + POC)", PASS,
                f"{zone_ev.zone.direction} zone {zone_ev.zone.low}-{zone_ev.zone.high} ({', '.join(zone_ev.zone.poc_sources)})")
        elif evs:
            near = min(evs, key=lambda e: min(abs(entry - e.zone.low), abs(entry - e.zone.high)))
            d = min(abs(entry - near.zone.low), abs(entry - near.zone.high)) / a5.atr
            chk("strategy", "setup zone (5m FVG + POC)", FAIL,
                f"entry {d:.1f} ATR away from the nearest {t['direction']} zone {near.zone.low}-{near.zone.high}")
        else:
            chk("strategy", "setup zone (5m FVG + POC)", FAIL, f"no active 5m FVG overlapping a POC in the {t['direction']} direction")
        if zone_ev and anticipated:
            chk("execution", "1m reaction confirmation", WARN,
                f"entered {et - (et - et % 60)}s into the {anticipated.confirmation} candle of "
                f"{to_et(anticipated.confirmed_at):%H:%M} ET, before it closed (your rule: enter on the confirming close)")
        elif zone_ev:
            confirmed = zone_ev.confirmed_at is not None or zone_ev.state in ("QUALIFIED",)
            chk("strategy", "1m reaction confirmation",
                PASS if confirmed else FAIL,
                f"{zone_ev.confirmation} at {to_et(zone_ev.confirmed_at):%H:%M} ET" if zone_ev.confirmed_at
                else f"zone state {zone_ev.state} -- entered before a rejection candle / holding closes")
        if zone_ev:
            beyond = (entry - zone_ev.zone.high) if long else (zone_ev.zone.low - entry)
            if beyond > CHASE_ATR * a5.atr:
                chk("execution", "entry location", WARN, f"entry {beyond / a5.atr:.1f} ATR beyond the zone edge (chasing)")
            else:
                chk("execution", "entry location", PASS, "entered at/inside the zone")
    else:
        chk("strategy", "setup zone (5m FVG + POC)", UNKNOWN, why_no_a5 if mismatch else "no 5m analysis available at entry")

    # ---------------- risk
    if stop is None:
        unseen = "csv" in (t["entry_source"] or "") or "not seen live" in (t["entry_source"] or "")
        chk("risk", "stop defined", UNKNOWN if unseen else FAIL,
            "stop not observed (trade placed while the monitor could not see the account) -- bracket orders unknown"
            if unseen else "no stop order seen")
    else:
        chk("risk", "stop defined", PASS, f"stop {stop}")
        risk_pts = (entry - stop) * sgn
        if target is not None and risk_pts > 0:
            rr = (target - entry) * sgn / risk_pts
            chk("risk", "planned RR", PASS if rr >= float(cfg["personal"]["min_rr"]) else FAIL,
                f"RR {rr:.2f} vs min {cfg['personal']['min_rr']}")
            m["planned_rr"] = round(rr, 2)
        if point_value and qty:
            risk_usd = risk_pts * point_value * qty + float(cfg["costs"]["commission_roundtrip_per_contract_usd"]) * qty
            m["risk_usd"] = round(risk_usd, 2)
            if loss_cap is not None:
                cons, obs = loss_cap if isinstance(loss_cap, tuple) else (loss_cap, loss_cap)
                if risk_usd <= cons:
                    chk("risk", "loss cap (1/4 of buffer)", PASS, f"risk ${risk_usd:.0f} vs cap ${cons:.0f}")
                elif risk_usd > obs:
                    chk("risk", "loss cap (1/4 of buffer)", FAIL, f"risk ${risk_usd:.0f} vs cap ${obs:.0f}")
                else:
                    chk("execution", "loss cap (1/4 of buffer)", WARN,
                        f"risk ${risk_usd:.0f}: within the cap from observed equity (${obs:.0f}) but over the "
                        f"conservative cap (${cons:.0f}); the true cap is in between")
            else:
                chk("risk", "loss cap (1/4 of buffer)", UNKNOWN, "drawdown buffer at entry unknown")
    # size vs the recommended size (largest count whose full stop fits the conservative loss cap; :
    # above it = too much risk, below it = recorded only)
    lo, hi = int(cfg["personal"]["contracts_per_trade_min"]), int(cfg["personal"]["contracts_per_trade_max"])
    firm_max = int(cfg["account"]["contracts"]["max_contracts_micro" if root.startswith("M") else "max_contracts_mini"])
    rec = None
    if stop is not None and point_value and loss_cap is not None and (entry - stop) * sgn > 0:
        cons = loss_cap[0] if isinstance(loss_cap, tuple) else loss_cap
        fit = size_for((entry - stop) * sgn, point_value, float(cfg["costs"]["commission_roundtrip_per_contract_usd"]),
                       cons, lo, hi, firm_max)
        rec = fit[0] if fit else 0
        m["recommended_contracts"] = rec
    if rec is None:
        chk("execution", "size vs recommended", WARN if not lo <= qty <= min(hi, firm_max) else UNKNOWN,
            f"{qty:g} contracts; recommended size unknown (stop or drawdown buffer unknown), allowed {lo}-{hi}")
    elif qty > rec:
        chk("execution", "size vs recommended", WARN,
            f"{qty:g} contracts > recommended {rec} for this stop" if rec else
            f"{qty:g} contracts, but even 1 contract exceeded the loss cap with this stop")
    else:
        chk("execution", "size vs recommended", PASS,
            f"{qty:g} contracts, recommended {rec}" + (" (below the recommendation -- recorded only)" if qty < rec else ""))
    if trades_before_today is not None:
        mx = cfg["personal"]["max_trades_per_day"]
        chk("risk", "trades per day", PASS if trades_before_today < mx else FAIL, f"trade #{trades_before_today + 1} of max {mx}")
    st, ev = blackout(events, et, int(cfg["personal"]["news_blackout_minutes_before"]),
                      int(cfg["personal"]["news_blackout_minutes_after"]))
    chk("risk", "outside news window", {"CLEAR": PASS, "BLACKOUT": FAIL}.get(st, UNKNOWN),
        f"{ev.title} at {to_et(ev.time):%H:%M} ET" if ev else ("no high-importance event nearby" if st == "CLEAR" else "calendar unavailable"))

    # ---------------- exit / excursion (hindsight is fine here: this is what happened)
    xt, exit_px = t["exit_time"], t["exit"]
    path = _bars(conn, root, "1", et - et % 60, xt) if xt and not mismatch else []   # chart prices != other contract
    if path and entry is not None:
        mfe = max((b.high - entry) if long else (entry - b.low) for b in path)
        mae = max((entry - b.low) if long else (b.high - entry) for b in path)
        m["mfe_pts"], m["mae_pts"] = round(max(mfe, 0), 2), round(max(mae, 0), 2)
    if exit_px is not None and entry is not None:
        m["result_pts"] = round((exit_px - entry) * sgn, 2)
        if stop is not None and (entry - stop) * sgn > 0:
            risk_pts = (entry - stop) * sgn
            m["r"] = round(m["result_pts"] / risk_pts, 2)
            if "mfe_pts" in m:
                m["mfe_r"] = round(m["mfe_pts"] / risk_pts, 2)
            how = ("stop" if abs(exit_px - stop) <= 0.3 else "target" if target and abs(exit_px - target) <= 0.3
                   else "manual")
            m["exit_type"] = how
    pnl = t["pnl_usd"] if t["pnl_usd"] is not None else t["pnl_gross"] if "pnl_gross" in t.keys() else None
    outcome = "UNKNOWN" if pnl is None else "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"

    # ---------------- grade
    status = {(c[0], c[1]): c[2] for c in C}
    rule_fail = any(c[2] == FAIL and (c[0] == "risk" or c[1] == "5m structure aligned") for c in C)
    setup_fail = any(c[2] == FAIL and c[1] in ("setup zone (5m FVG + POC)", "1m reaction confirmation") for c in C)
    warn = any(c[2] == WARN for c in C)
    grade = "D" if rule_fail else "C" if setup_fail else "B" if warn else "A"
    critical_unknown = [c[1] for c in C if c[2] == UNKNOWN and c[1] in
                        ("5m structure aligned", "setup zone (5m FVG + POC)", "stop defined", "outside news window")]
    quality = "GOOD" if grade in ("A", "B") else "BAD"
    cls = f"{'Good' if quality == 'GOOD' else 'Bad'} trade / " + {"WIN": "Good outcome", "LOSS": "Bad outcome",
                                                                    "FLAT": "Flat outcome"}.get(outcome, "Unknown outcome")

    # ---------------- notes
    good = [d for cat, n, s, d in C if s == PASS and n in ("5m structure aligned", "setup zone (5m FVG + POC)",
                                                            "1m reaction confirmation", "planned RR", "entry location")]
    imp = []
    if status.get(("strategy", "5m structure aligned")) == FAIL:
        imp.append("Traded against the 5m structure -- your rule is to take only the side the 5m SMC structure gives.")
    if status.get(("strategy", "setup zone (5m FVG + POC)")) == FAIL:
        imp.append("No 5m FVG + POC zone at the entry -- this was not your setup. Wait for price to reach a zone.")
    if status.get(("strategy", "1m reaction confirmation")) == FAIL:
        imp.append("Entered before the 1m reaction (rejection candle or holding closes) -- wait for the confirmation close.")
    if status.get(("execution", "1m reaction confirmation")) == WARN:
        imp.append("Entered inside the confirming candle, before it closed -- the setup was right, but wait for the close "
                   "(a candle can still turn before it closes).")
    if status.get(("execution", "entry location")) == WARN:
        imp.append("Entry chased away from the zone -- the stop gets wider and RR worse. Let price come back.")
    if status.get(("execution", "size vs recommended")) == WARN:
        imp.append(f"Size {qty:g} was above the recommended {rec} for this stop -- size from the stop distance and "
                   "the loss cap before the entry." if rec else
                   f"Size {qty:g} with this stop: even 1 contract was over the loss cap -- the stop was too wide to take "
                   "the trade." if rec == 0 else
                   f"Size {qty:g} is outside {lo}-{hi} contracts.")
    if status.get(("risk", "planned RR")) == FAIL:
        imp.append("Planned RR below your 1:1.5 minimum.")
    if status.get(("risk", "loss cap (1/4 of buffer)")) == FAIL:
        imp.append("Risk exceeded 1/4 of the remaining drawdown buffer -- reduce contracts or tighten to a valid stop.")
    if status.get(("risk", "outside news window")) == FAIL:
        imp.append("Entered inside a high-importance news window, which you do not trade.")
    if status.get(("execution", "loss cap (1/4 of buffer)")) == WARN:
        imp.append("Risk may have exceeded 1/4 of the remaining buffer -- size from the conservative cap.")
    if m.get("exit_type") == "manual" and m.get("mfe_r", 0) >= 1.5 and m.get("r", 0) < 1:
        imp.append(f"Price went {m['mfe_r']}R in your favour but the trade closed at {m['r']}R -- review the exit plan.")
    if m.get("exit_type") == "stop" and m.get("mfe_r", 0) >= 1:
        imp.append(f"Observation (not a rule): price reached +{m['mfe_r']}R before the full stop. Whether a break-even or "
                   "partial-exit rule would help must be tested on many trades first (hypothesis, spec 23).")
    if stop is None and ("csv" in (t["entry_source"] or "") or "not seen live" in (t["entry_source"] or "")):
        imp.append("Stop unknown for this imported trade -- R-based review not possible.")
    lesson = imp[0] if imp else ("Process followed; the outcome is variance, not a mistake." if outcome == "LOSS"
                                 else "Repeat this: it matched your plan.")
    return Grade(t["id"], grade, bool(critical_unknown), quality, outcome, cls, C, good, imp, lesson, m)


def save(conn: sqlite3.Connection, g: Grade, now: int) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO trade_grades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (g.trade_id, now, g.grade, int(g.provisional), g.quality, g.outcome, g.classification,
                  json.dumps(g.checks), json.dumps(g.went_well), json.dumps(g.improve), g.lesson,
                  json.dumps(g.metrics), GRADER_VERSION))
    conn.commit()


def render(g: Grade, t: sqlite3.Row) -> str:
    """Telegram journal, in Chinese  via the same fixed tables as the Notion journal."""
    from . import zh
    m = g.metrics
    pnl = f"${t['pnl_usd']:.2f}" if t["pnl_usd"] is not None else None
    res = "、".join(x for x in (f"{m['r']}R" if "r" in m else None, pnl) if x) or "未知"
    exit_src = t["exit_source"] if "exit_source" in t.keys() else ""
    exit_txt = "未平倉" if t["exit"] is None else f"{t['exit']:.1f}" + ("（推算）" if "derived" in (exit_src or "") else "")
    kind = "（轉折單）" if m.get("setup_type") == "REVERSAL" else ""
    lines = [f"交易日誌 #{t['id']}{kind}", "",
             f"{root_of(t['symbol'])} {zh.DIRECTION.get(t['direction'], t['direction'])} {t['qty']:g} 口  {m.get('entry_et', '')}",
             f"進場 {t['entry']:.1f} → 出場 {exit_txt}　結果：{res}",
             f"最大有利／不利：{m.get('mfe_pts', '?')} / {m.get('mae_pts', '?')} 點"
             + (f"（{zh.EXIT.get(m['exit_type'], m['exit_type'])}）" if "exit_type" in m else ""),
             "", f"評級：{g.grade}{'（暫定，缺資料）' if g.provisional else ''}",
             f"分類：{zh.CLASSIFICATION.get(g.classification, g.classification)}", "", "檢查項目："]
    sym = {PASS: "✅", FAIL: "❌", WARN: "⚠️", UNKNOWN: "❔"}
    lines += [f"{sym[s]} {zh.check_name(n)}：{d}" for _, n, s, d in g.checks]
    if g.went_well:
        lines += ["", "做得好："] + [f"- {x}" for x in g.went_well]
    if g.improve:
        lines += ["", "可改進："] + [f"- {zh.sentence(x)}" for x in g.improve]
    lines += ["", f"主要教訓：\n{zh.sentence(g.lesson)}"]
    return "\n".join(lines)


def _tick(conn: sqlite3.Connection, root: str) -> float | None:
    row = conn.execute("SELECT min_tick FROM chart_snapshots WHERE (feed_symbol LIKE ? OR feed_symbol LIKE ?)"
                       " AND min_tick IS NOT NULL ORDER BY fetched_at DESC LIMIT 1", (f"%:{root}1!", f"%:{root}")).fetchone()
    return float(row[0]) if row else None


def _loss_cap_at(conn: sqlite3.Connection, t: sqlite3.Row, cfg: dict) -> tuple[float, float] | None:
    """1/4 of the drawdown buffer at entry, as (conservative, observed):
      observed     -- high-water = highest equity actually observed before the entry
      conservative -- also counts the bar-MFE peaks of trades reconciled from imports (overstates the peak),
                      the same inputs the live risk gate uses.
    None if the balance before the trade is unknown."""
    from .tradovate_csv import explain_gap
    if t["balance_before"] is None:
        return None
    size = float(cfg["account"]["size_usd"])
    dd = float(cfg["account"]["drawdown"]["max_drawdown_usd"])
    frac = float(cfg["personal"]["max_loss_per_trade_frac_of_buffer"])
    marks = conn.execute("SELECT at, balance, equity FROM equity_marks WHERE account = ? AND at <= ? ORDER BY at",
                         (t["account"], t["entry_time"])).fetchall()
    observed = max([size] + [mk[2] for mk in marks if mk[2] is not None])
    conservative = observed
    for (t0, b0, _), (t1, b1, _) in zip(marks, marks[1:]):
        if t1 - t0 > 180 and b0 is not None and b1 is not None and abs(b1 - b0) > 0.005:
            ok, peak, _ = explain_gap(conn, t0, t1, b0, b1)
            if ok and peak is not None:
                conservative = max(conservative, peak)
    cap = lambda hw: frac * (t["balance_before"] - (hw - dd))
    return cap(conservative), cap(observed)


def grade_pending(conn: sqlite3.Connection, cfg: dict, fetch_events, now: int) -> list[tuple[sqlite3.Row, Grade]]:
    """Grade every CLOSED trade without a grade. fetch_events(start, end) -> list | None (None = UNKNOWN)."""
    from ..session_engine.sessions import trading_day_start
    from .tradovate_csv import point_value
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    todo = conn.execute("SELECT * FROM trades WHERE status='CLOSED' AND id NOT IN (SELECT trade_id FROM trade_grades)"
                        " ORDER BY entry_time").fetchall()
    if not todo:
        return []
    events = fetch_events(todo[0]["entry_time"] - 86400, todo[-1]["entry_time"] + 86400) if fetch_events else None
    out = []
    for t in todo:
        root = root_of(t["symbol"])
        n_before = conn.execute("SELECT COUNT(*) FROM trades WHERE account IS ? AND entry_time >= ? AND entry_time < ?",
                                (t["account"], trading_day_start(t["entry_time"]), t["entry_time"])).fetchone()[0]
        g = grade_trade(conn, t, cfg, events, point_value(conn, root), n_before, _loss_cap_at(conn, t, cfg),
                        _tick(conn, root))
        save(conn, g, now)
        out.append((t, g))
    return out
