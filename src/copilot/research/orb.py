"""Opening-range breakout (ORB) -- a separate system, research only .

Rules (confirmed with the ):
  * Range: high / low of the 5m bars 09:30 ET .. 09:30 + range_minutes (MGC and MNQ both use 09:30).
  * Entry: the first 5m bar that CLOSES beyond the range (above -> long, below -> short), entered at that close; the
    bar must close by entry_until (11:00 ET). One trade per day.
  * Stop: the other side of the range (or its midpoint), one tick beyond. Target: target_r x risk.
  * Exit: stop / target, else at the 16:00 ET close (flat well before Apex's 16:59 rule).
  * Same 5m bar touching stop and target counts as a LOSS (conservative: the order inside the bar is unknown).
  * fade_prev_ny: only trade AGAINST the previous trading day's New York session (16:00 close vs 09:30 open):
    up -> shorts only, down -> longs only; the first breakout in the allowed direction is taken.
R is net of round-trip commission. Uses only stored 5m bars (more history than 1m). Never touches production.
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from ..market_data.models import Bar
from ..session_engine.sessions import ET
from ..trade_journal.grader import _bars
from .backtest import MIN_TRADES, SimTrade, Stats

ORB_VERSION = "orb-0.1.0"
VIABLE_EXPECTANCY = 0.10
VIABLE_P = 0.95
STOP_BUFFER_TICKS = 1


@dataclass(frozen=True)
class OrbRules:
    name: str
    range_minutes: int = 15
    stop: str = "range"               # range (other side) | mid (range midpoint)
    target_r: float = 2.0
    entry_until: tuple = (11, 0)
    exit_at: tuple = (16, 0)
    fade_prev_ny: bool = False
    bar_minutes: int = 5              # 5m (default) or 15m (observed: 15m reaches back ~10 months)


def variants(bar_minutes: int = 5) -> list[OrbRules]:
    out = []
    tag = "" if bar_minutes == 5 else f" [{bar_minutes}m bars]"
    for rm in (15, 30):
        for stop in ("range", "mid"):
            for tr in (1.0, 1.5, 2.0):
                for fade in (False, True):
                    out.append(OrbRules(f"ORB {rm}m stop={stop} {tr}R{' fade-prevNY' if fade else ''}{tag}",
                                        rm, stop, tr, fade_prev_ny=fade, bar_minutes=bar_minutes))
    return out


def _ts(d: date, hm: tuple) -> int:
    return int(datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=ET).timestamp())


def session_days(conn: sqlite3.Connection, root: str, today: date, bar_minutes: int = 5) -> dict[date, list[Bar]]:
    """ET dates (before today) whose stored bars cover the whole 09:30-16:00 session (390 / bar_minutes bars)."""
    sec, need = bar_minutes * 60, 390 // bar_minutes
    out = {}
    first = conn.execute(f"SELECT MIN(time) FROM bars WHERE resolution='{bar_minutes}' AND (feed_symbol LIKE ? OR feed_symbol LIKE ?)",
                         (f"%:{root}1!", f"%:{root}")).fetchone()[0]
    if first is None:
        return out
    d = datetime.fromtimestamp(first, ET).date()
    while d < today:
        if d.weekday() < 5:
            bars = _bars(conn, root, str(bar_minutes), _ts(d, (9, 30)), _ts(d, (16, 0)) - sec)
            if len(bars) != need and bar_minutes > 5 and bar_minutes % 5 == 0:
                bars = aggregate(_bars(conn, root, "5", _ts(d, (9, 30)), _ts(d, (16, 0)) - 300), _ts(d, (9, 30)),
                                 bar_minutes)                    # newer days: 15m built from stored 5m bars
            if len(bars) == need:
                out[d] = bars
        d += timedelta(days=1)
    return out


def aggregate(bars5: list[Bar], start: int, bar_minutes: int) -> list[Bar]:
    """Complete bar_minutes bars from 5m bars (a bucket missing any 5m bar is dropped -> the day is not used)."""
    k, out = bar_minutes // 5, []
    by = {}
    for b in bars5:
        by.setdefault((b.time - start) // (bar_minutes * 60), []).append(b)
    for i in sorted(by):
        g = by[i]
        if len(g) == k:
            out.append(Bar(start + i * bar_minutes * 60, g[0].open, max(x.high for x in g), min(x.low for x in g),
                           g[-1].close, sum(x.volume for x in g), True))
    return out


def simulate_day(bars: list[Bar], d: date, rules: OrbRules, tick: float, pv: float, commission: float | None,
                 allowed: str | None) -> SimTrade | None:
    t0 = _ts(d, (9, 30))
    rng = [b for b in bars if b.time < t0 + rules.range_minutes * 60]
    sec = rules.bar_minutes * 60
    if len(rng) != rules.range_minutes // rules.bar_minutes:
        return None
    hi, lo = max(b.high for b in rng), min(b.low for b in rng)
    if hi <= lo:
        return None
    until = _ts(d, rules.entry_until)
    rest = [b for b in bars if b.time >= t0 + rules.range_minutes * 60]
    for i, b in enumerate(rest):
        if b.time + sec > until:
            return None
        side = "LONG" if b.close > hi else "SHORT" if b.close < lo else None
        if side is None or (allowed and side != allowed):
            continue
        entry = b.close
        level = lo if side == "LONG" else hi
        if rules.stop == "mid":
            level = (hi + lo) / 2
        stop = level - STOP_BUFFER_TICKS * tick if side == "LONG" else level + STOP_BUFFER_TICKS * tick
        risk = abs(entry - stop)
        if risk <= 0 or (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
            continue                                       # e.g. mid stop on the wrong side: no valid trade
        target = entry + rules.target_r * risk if side == "LONG" else entry - rules.target_r * risk
        outcome, r = None, None
        for x in rest[i + 1:]:
            hit_s = x.low <= stop if side == "LONG" else x.high >= stop
            hit_t = x.high >= target if side == "LONG" else x.low <= target
            if hit_s:
                outcome, r = "LOSS", -1.0
                break
            if hit_t:
                outcome, r = "WIN", rules.target_r
                break
        if outcome is None:                               # 16:00 ET close
            last = bars[-1].close
            outcome, r = "TIMEOUT", ((last - entry) if side == "LONG" else (entry - last)) / risk
        fee = commission / (risk * pv) if commission and pv else None
        return SimTrade(d, b.time + sec, side, entry, stop, target, outcome, round(r - (fee or 0.0), 4), round(r, 4),
                        None if fee is None else round(fee, 4))
    return None


def prev_ny_direction(days: dict[date, list[Bar]], d: date) -> str | None:
    """UP / DOWN of the previous stored trading day's 09:30-16:00 session; None when unknown or flat."""
    prev = [x for x in days if x < d]
    if not prev:
        return None
    bars = days[max(prev)]
    if (d - max(prev)).days > 4:                          # a gap in the data, not just a weekend / holiday
        return None
    o, c = bars[0].open, bars[-1].close
    return "UP" if c > o else "DOWN" if c < o else None


def run(conn: sqlite3.Connection, markets: list, today: date, bar_minutes: int = 5) -> dict:
    """markets: (root, tick, point_value, commission). Returns {variant name: [SimTrade]} and the day list."""
    vs = variants(bar_minutes)
    res: dict[str, list[SimTrade]] = {v.name: [] for v in vs}
    all_days: set = set()
    per_root: dict = {}
    for root, tick, pv, comm in markets:
        days = session_days(conn, root, today, bar_minutes)
        all_days |= set(days)
        per_root[root] = len(days)
        for d, bars in days.items():
            direction = prev_ny_direction(days, d)
            for v in vs:
                allowed = None
                if v.fade_prev_ny:
                    if direction is None:
                        continue
                    allowed = "SHORT" if direction == "UP" else "LONG"
                t = simulate_day(bars, d, v, tick, pv, comm, allowed)
                if t:
                    res[v.name].append(t)
    return {"trades": res, "days": sorted(all_days), "per_root": per_root}


def p_positive(trades: list[SimTrade], days: list, n: int = 2000, seed: int = 7) -> float | None:
    """Day bootstrap: P(total R > 0) -- a new system has no production version to beat, it must make money."""
    if not days:
        return None
    rng = random.Random(seed)
    per = {d: sum(t.r for t in trades if t.day == d) for d in days}
    return sum(1 for _ in range(n) if sum(per[rng.choice(days)] for _ in days) > 0) / n


def evaluate(trades: list[SimTrade], days: list, split: float = 2 / 3) -> dict:
    days = sorted(days)
    cut = max(1, int(len(days) * split))
    ins, oos = set(days[:cut]), set(days[cut:])
    s_in, s_out = Stats.of([t for t in trades if t.day in ins]), Stats.of([t for t in trades if t.day in oos])
    p = p_positive(trades, days)
    why = []
    if s_out.n < MIN_TRADES:
        why.append(f"樣本外只有 {s_out.n} 筆")
    if min(s_in.expectancy, s_out.expectancy) <= VIABLE_EXPECTANCY:
        why.append(f"平均 {s_in.expectancy:+.2f} / {s_out.expectancy:+.2f}R，沒有兩段都超過 +{VIABLE_EXPECTANCY}R")
    if p is None or p < VIABLE_P:
        why.append(f"統計檢定 {0 if p is None else p:.2f}，未達 {VIABLE_P}")
    verdict = "通過回測" if not why else ("樣本不足" if s_out.n < MIN_TRADES else
                                          "不明確" if s_out.expectancy > 0 and s_in.expectancy > 0 else "沒有優勢")
    return {"in": asdict(s_in), "out": asdict(s_out), "p_positive": p, "verdict": verdict, "reasons": why,
            "split_day": str(days[cut]) if cut < len(days) else None}


SCHEMA = """
CREATE TABLE IF NOT EXISTS system_results (
    system TEXT NOT NULL, variant TEXT NOT NULL, tested_at INTEGER NOT NULL, data TEXT NOT NULL,
    results TEXT NOT NULL, version TEXT NOT NULL, PRIMARY KEY (system, variant)
);
"""


def record(conn: sqlite3.Connection, system: str, name: str, ev: dict, data: dict, now: int) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO system_results VALUES (?,?,?,?,?,?)",
                 (system, name, now, json.dumps(data), json.dumps(ev), ORB_VERSION))
    conn.commit()


SYSTEMS = {5: "ORB", 15: "ORB15"}      # system id in system_results per bar size


def label(v: OrbRules) -> str:
    return ((f"[{v.bar_minutes} 分鐘K線] " if v.bar_minutes != 5 else "")
            + f"開盤區間 {v.range_minutes} 分鐘｜止損{'區間另一邊' if v.stop == 'range' else '區間中間'}｜"
            f"目標 {v.target_r:g}R" + ("｜只做跟昨天紐約盤相反方向" if v.fade_prev_ny else ""))


def report(results: dict, days: list, data_text: str, bar_minutes: int = 5) -> str:
    rows = []
    for v in variants(bar_minutes):
        ev = evaluate(results[v.name], days)
        rows.append((v, ev))
    rows.sort(key=lambda x: -(x[1]["out"]["expectancy"] if x[1]["out"]["n"] else -9))
    lines = [f"開盤區間突破（ORB，{bar_minutes} 分鐘K線）回測：{data_text}，R 已扣手續費",
             f"前段/後段分界：{rows[0][1]['split_day']}", ""]
    for v, ev in rows:
        i, o = ev["in"], ev["out"]
        lines.append(f"- {label(v)}：前段 {i['n']} 筆 {i['expectancy']:+.2f}R｜後段 {o['n']} 筆 {o['expectancy']:+.2f}R"
                     f"｜最大回撤 {o['max_dd_r']:.1f}R → {ev['verdict']}"
                     + (f"（{'；'.join(ev['reasons'])}）" if ev["reasons"] else ""))
    lines += ["", f"測了 {len(rows)} 個版本：版本越多，越容易有一兩個剛好看起來很好，所以門檻定得比較嚴。",
              "只是研究，不會發交易提醒，也不會改動目前的策略。"]
    return "\n".join(lines)


def markets_of(conn: sqlite3.Connection, commission_by_root: dict, roots: list | None = None) -> list:
    from ..trade_journal.tradovate_csv import point_value
    out = []
    for root in roots if roots is not None else ("MGC", "MNQ"):
        row = conn.execute("SELECT min_tick FROM chart_snapshots WHERE (feed_symbol LIKE ? OR feed_symbol LIKE ?)"
                           " AND min_tick IS NOT NULL ORDER BY fetched_at DESC LIMIT 1",
                           (f"%:{root}1!", f"%:{root}")).fetchone()
        pv = point_value(conn, root)
        if row and pv:
            out.append((root, float(row[0]), pv, commission_by_root.get(root)))
    return out


def daily(conn: sqlite3.Connection, now: int, commission_by_root: dict, roots: list | None = None) -> tuple[str, list[str]]:
    """Re-run every ORB version on all stored days, with 5m bars and with 15m bars (longer history). Returns the
    report and the versions that pass the backtest (the monitor tells the user; nothing is traded or changed)."""
    today = datetime.fromtimestamp(now, ET).date()
    markets = markets_of(conn, commission_by_root, roots)
    texts, passed = [], []
    for bm, system in SYSTEMS.items():
        r = run(conn, markets, today, bm)
        days = r["days"]
        for v in variants(bm):
            ev = evaluate(r["trades"][v.name], days)
            record(conn, system, v.name, ev, {"markets": r["per_root"], "days": len(days),
                                              "first": str(days[0]) if days else None}, now)
            if ev["verdict"] == "通過回測":
                passed.append(label(v))
        per = "＋".join(f"{k} {n} 天" for k, n in r["per_root"].items())
        texts.append(report(r["trades"], days, f"{per}（{days[0]} 起）" if days else per, bm))
    return "\n\n".join(texts), passed


def best_line(conn: sqlite3.Connection, system: str = "ORB") -> str | None:
    """One line for the research journal: the version with the best out-of-sample average."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='system_results'").fetchone():
        return None
    best = None
    for name, res, data in conn.execute("SELECT variant, results, data FROM system_results WHERE system=?", (system,)):
        ev = json.loads(res)
        if ev["out"]["n"] and (best is None or ev["out"]["expectancy"] > best[1]["out"]["expectancy"]):
            best = (name, ev, json.loads(data))
    if best is None:
        return None
    bm = next((k for k, v in SYSTEMS.items() if v == system), 5)
    v = next((x for x in variants(bm) if x.name == best[0]), None)
    o = best[1]["out"]
    return (f"最好的版本：{label(v) if v else best[0]}，後段 {o['n']} 筆 {o['expectancy']:+.2f}R"
            f"（資料 {best[2].get('days')} 個日期）→ {best[1]['verdict']}")
