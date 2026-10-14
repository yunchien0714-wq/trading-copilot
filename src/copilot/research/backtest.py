"""Backtest / walk-forward / bootstrap for setup rule variants (spec sections 23-25, 35).

Environment separation (spec 24): variants run ONLY here, on stored history, bar by bar with data known at each
close. Nothing in this module touches live alerts, the chart, risk limits or setup classification.

A variant = RuleSet (setup filters) + optional exit management (break-even at +X R).
Verdict rules (all must hold to become a CANDIDATE -- which still needs the user's approval for production):
  * >= MIN_TRADES resolved trades for both production and the variant in the out-of-sample segment
  * expectancy (avg R) better than production in-sample AND out-of-sample
  * max drawdown (in R) not worse than production by more than DD_TOLERANCE
  * paired day-bootstrap: P(variant total R > production total R) >= P_REQUIRED
"""
from __future__ import annotations

import bisect
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

from ..alert_tracking.outcomes import resolve
from ..market_data.models import Bar
from ..market_data.series import atr as atr_series
from ..market_data.rolls import boundaries as roll_boundaries, spans_roll
from ..market_structure.analysis import analyze_timeframe
from ..session_engine.sessions import ET, trading_day, trading_day_start
from ..setup_engine.poc_fvg import PRODUCTION, RuleSet, evaluate_all
from ..setup_engine.tracker import SetupTracker
from ..trade_journal.grader import _bars
from ..volume.profile import ProfileParams, build_profile, fixed_segment_pocs

BACKTEST_VERSION = "backtest-0.1.0"
MIN_TRADES = 30
DD_TOLERANCE = 1.2
P_REQUIRED = 0.9


@dataclass(frozen=True)
class Variant:
    name: str
    rules: RuleSet = PRODUCTION
    breakeven_at_r: float | None = None     # move stop to entry after a bar CLOSES with MFE >= this


@dataclass
class SimTrade:
    day: object
    decided_at: int
    direction: str
    entry: float
    stop: float
    target: float
    outcome: str
    r: float                       # NET of round-trip commission when it is known (see simulate)
    gross_r: float | None = None
    fee_r: float | None = None     # commission for one contract expressed in R (None = unknown -> r is gross)


def resolve_managed(direction, entry, stop, target, decided_at, bars: list[Bar], now: int, be_at: float | None):
    """Like alerts.resolve, plus break-even management. The stop moves to entry only AFTER the bar that reached
    +be_at R has closed (you cannot act inside a bar you have not seen finish). Same-bar ambiguity stays a loss."""
    if be_at is None:
        return resolve(direction, entry, stop, target, decided_at, bars, now)
    long = direction == "LONG"
    risk = abs(entry - stop)
    day = trading_day(decided_at)
    cur_stop, armed = stop, False
    last = None
    for b in bars:
        if b.time < decided_at:
            continue
        if trading_day(b.time) != day:
            break
        last = b
        hit_s = b.low <= cur_stop if long else b.high >= cur_stop
        hit_t = b.high >= target if long else b.low <= target
        if hit_s:
            return SimpleNamespace(outcome="LOSS" if not armed else "BREAKEVEN", r=-1.0 if not armed else 0.0)
        if hit_t:
            return SimpleNamespace(outcome="WIN", r=round(abs(target - entry) / risk, 2))
        fav = ((b.high - entry) if long else (entry - b.low)) / risk
        if not armed and fav >= be_at:
            armed, cur_stop = True, entry
    if last is not None and trading_day(now) != day:
        r = ((last.close - entry) if long else (entry - last.close)) / risk
        return SimpleNamespace(outcome="TIMEOUT", r=round(r, 2))
    return SimpleNamespace(outcome="PENDING", r=None)


def simulate(conn: sqlite3.Connection, root: str, tick: float, min_rr: float, days: list,
             variants: list[Variant], now: int, commission_rt: float | None = None) -> dict[str, list[SimTrade]]:
    """One pass over history; 5m analysis computed once per 5m bar and shared by all variants.
    commission_rt ($ per contract round trip) -> every trade's R is reduced by commission / (stop distance x $/pt),
    the cost for any contract count (size scales risk and fees together)."""
    from ..trade_journal.tradovate_csv import point_value
    pv = point_value(conn, root)
    out: dict[str, list[SimTrade]] = {v.name: [] for v in variants}
    gate = lambda ev: SimpleNamespace(passed=True, rules=[], failures=[], version="backtest")
    rolls_ = roll_boundaries(conn, root)
    for day in days:
        start = trading_day_start(int(datetime(day.year, day.month, day.day, 12, tzinfo=ET).timestamp()))
        if spans_roll(start - 3600, start + 23 * 3600, rolls_):
            continue                              # the continuous series jumps between contracts on a roll day
        b1 = _bars(conn, root, "1", start - 600 * 60, start + 23 * 3600)
        b5 = _bars(conn, root, "5", start - 2600 * 300, start + 23 * 3600)
        if not any(b.time >= start for b in b1):
            continue
        mems = {}
        for v in variants:
            m = sqlite3.connect(":memory:")
            m.row_factory = sqlite3.Row
            mems[v.name] = (m, SetupTracker(m, root))
        k5_cached, a5, prof = None, None, None
        closes5 = [x.time + 300 for x in b5]      # sorted -> number of 5m bars closed by t = bisect
        for i, b in enumerate(b1):
            if b.time < start:
                continue
            t_close = b.time + 60
            k5 = bisect.bisect_right(closes5, t_close)
            if k5 != k5_cached:
                hist = b5[max(0, k5 - 2500):k5]
                prof = build_profile(hist, ProfileParams())
                if prof is not None:
                    prof.fixed_segment_pocs = fixed_segment_pocs(hist, 300, tick, atr_series(hist))
                a5, k5_cached = analyze_timeframe(hist, "5", roll_times=rolls_), k5
            window = b1[max(0, i - 600):i + 1]
            for v in variants:
                mems[v.name][1].update(evaluate_all(a5, prof, window, tick, min_rr, v.rules), t_close, gate)
        for v in variants:
            m = mems[v.name][0]
            for s in m.execute("SELECT * FROM setups WHERE state='QUALIFIED' AND entry IS NOT NULL"):
                res = resolve_managed(s["direction"], s["entry"], s["stop"], s["target"], s["confirmed_at"] + 60,
                                      b1, now, v.breakeven_at_r)
                if res.outcome != "PENDING":
                    risk_pts = abs(s["entry"] - s["stop"])
                    fee = (commission_rt / (risk_pts * pv)) if commission_rt and pv and risk_pts > 0 else None
                    out[v.name].append(SimTrade(day, s["confirmed_at"] + 60, s["direction"], s["entry"], s["stop"],
                                                s["target"], res.outcome, round(res.r - (fee or 0.0), 4), res.r,
                                                None if fee is None else round(fee, 4)))
    return out


@dataclass
class Stats:
    n: int
    wins: int
    win_rate: float
    expectancy: float
    total_r: float
    profit_factor: float | None
    max_dd_r: float

    @staticmethod
    def of(trades: list[SimTrade]) -> "Stats":
        rs = [t.r for t in sorted(trades, key=lambda t: t.decided_at)]
        if not rs:
            return Stats(0, 0, 0.0, 0.0, 0.0, None, 0.0)
        wins = sum(1 for r in rs if r > 0)
        gain, loss = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
        eq = peak = dd = 0.0
        for r in rs:
            eq += r
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        return Stats(len(rs), wins, wins / len(rs), sum(rs) / len(rs), sum(rs), (gain / loss) if loss else None, dd)


def bootstrap_p_better(cand: list[SimTrade], base: list[SimTrade], days: list, n: int = 2000, seed: int = 7) -> float | None:
    """Paired day bootstrap: P(sum R candidate > sum R production) when days are resampled with replacement."""
    if not days:
        return None
    rng = random.Random(seed)
    per_c = {d: sum(t.r for t in cand if t.day == d) for d in days}
    per_b = {d: sum(t.r for t in base if t.day == d) for d in days}
    better = 0
    for _ in range(n):
        sample = [rng.choice(days) for _ in days]
        if sum(per_c[d] for d in sample) > sum(per_b[d] for d in sample):
            better += 1
    return better / n


@dataclass
class Verdict:
    variant: str
    in_sample: Stats
    out_sample: Stats
    base_in: Stats
    base_out: Stats
    p_better: float | None
    verdict: str            # CANDIDATE | REJECTED | INCONCLUSIVE | INSUFFICIENT_DATA | BASELINE
    reasons: list[str] = field(default_factory=list)


def walk_forward(results: dict[str, list[SimTrade]], days: list, split: float = 2 / 3,
                 baseline: str = "production", min_expectancy: float | None = None,
                 p_required: float = P_REQUIRED) -> list[Verdict]:
    """min_expectancy: also require this absolute (net) expectancy in-sample AND out-of-sample -- 'better than a
    losing baseline' is not enough to call a strategy viable."""
    days = sorted(days)
    cut = max(1, int(len(days) * split))
    ins, oos = set(days[:cut]), set(days[cut:])
    base = results[baseline]
    b_in, b_out = Stats.of([t for t in base if t.day in ins]), Stats.of([t for t in base if t.day in oos])
    verdicts = []
    for name, trades in results.items():
        s_in, s_out = Stats.of([t for t in trades if t.day in ins]), Stats.of([t for t in trades if t.day in oos])
        if name == baseline:
            verdicts.append(Verdict(name, s_in, s_out, b_in, b_out, None, "BASELINE"))
            continue
        p = bootstrap_p_better(trades, base, days)
        why = []
        # The VARIANT needs enough out-of-sample trades. Production's own count is not a gate (observed: with 18
        # production trades nothing could ever pass); the comparison is carried by the bootstrap and expectancy,
        # and the absolute min_expectancy keeps "better than a weak baseline" from passing.
        if s_out.n < MIN_TRADES:
            why.append(f"out-of-sample trades {s_out.n} < {MIN_TRADES} (production {b_out.n})")
            v = "INSUFFICIENT_DATA"
        else:
            better_both = s_in.expectancy > b_in.expectancy and s_out.expectancy > b_out.expectancy
            dd_ok = s_out.max_dd_r <= max(b_out.max_dd_r * DD_TOLERANCE, b_out.max_dd_r + 1)
            if not better_both:
                why.append("expectancy not better in both in-sample and out-of-sample")
            if not dd_ok:
                why.append("drawdown worse than production")
            if p is not None and p < p_required:
                why.append(f"bootstrap P(better) {p:.2f} < {p_required}")
            if min_expectancy is not None and min(s_in.expectancy, s_out.expectancy) <= min_expectancy:
                why.append(f"expectancy {s_in.expectancy:+.2f} / {s_out.expectancy:+.2f}R not above +{min_expectancy}R "
                           "in both halves")
            v = "CANDIDATE" if not why else ("REJECTED" if not better_both else "INCONCLUSIVE")
        verdicts.append(Verdict(name, s_in, s_out, b_in, b_out, p, v, why))
    return verdicts
