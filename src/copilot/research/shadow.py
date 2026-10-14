"""Gate 2 of the research loop: forward ("shadow") testing on live data.

A hypothesis that passed the backtest runs next to production on every new 1m bar with its own setup tracker
(separate database file per hypothesis, so production's tables are never touched). Its decisions are stored in
alert_outcomes as source 'shadow:<id>' and resolved like every other decision. It sends no trade alerts.
Once it has MIN_TRADES resolved trades it is either PROPOSED to the user or sent back to research.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from ..alert_tracking.outcomes import decisions_from, record_decision
from ..setup_engine.poc_fvg import evaluate_all
from ..setup_engine.tracker import SetupTracker
from .auto import VIABLE_EXPECTANCY, _variant_of
from .backtest import MIN_TRADES, resolve_managed

UNGATED = lambda ev: SimpleNamespace(passed=True, rules=[], failures=[], version="shadow-ungated")


class ShadowRunner:
    def __init__(self, folder: Path):
        self.folder = folder
        self.runs: dict[str, tuple] = {}            # hid -> (conn, tracker, variant)

    def refresh(self, conn: sqlite3.Connection, root: str) -> None:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='hypotheses'").fetchone():
            return
        prev, conn.row_factory = conn.row_factory, sqlite3.Row
        try:
            rows = {r["id"]: r for r in conn.execute("SELECT * FROM hypotheses WHERE status = 'SHADOW'")}
        finally:
            conn.row_factory = prev
        for hid in [h for h in self.runs if h not in rows]:
            self.runs.pop(hid)[0].close()
        for hid, r in rows.items():
            if hid not in self.runs:
                self.folder.mkdir(parents=True, exist_ok=True)
                sc = sqlite3.connect(self.folder / (re.sub(r"[^A-Za-z0-9_.-]+", "_", hid) + ".sqlite3"))
                sc.row_factory = sqlite3.Row
                self.runs[hid] = (sc, SetupTracker(sc, root), _variant_of(r))

    def update(self, conn: sqlite3.Connection, a5, prof, bars1: list, tick: float, min_rr: float, now: int,
               symbol: str) -> int:
        n = 0
        for hid, (sc, tracker, v) in self.runs.items():
            tracker.update(evaluate_all(a5, prof, bars1, tick, min_rr, v.rules), now, UNGATED)
            for s, dec, why, at, entry, stop, target, rr, cond in decisions_from(sc, risk_known=False):
                before = conn.total_changes
                record_decision(conn, s["key"], f"shadow:{hid}", symbol, s["direction"], dec, why, at, entry, stop,
                                target, rr, cond, s["poc_sources"])
                n += conn.total_changes - before
        conn.commit()
        return n


def net_r(row, bars_for, now: int, be_at, commission: float | None, pv: float | None) -> float | None:
    """Net R of one resolved decision: re-resolved with the variant's exit management, minus commission."""
    if be_at is not None:
        res = resolve_managed(row["direction"], row["entry"], row["stop"], row["target"], row["decided_at"],
                              bars_for(row["symbol"], row["decided_at"], row["decided_at"] + 86400), now, be_at)
        r = res.r
    else:
        r = row["r"]
    if r is None:
        return None
    risk = abs(row["entry"] - row["stop"])
    fee = commission / (risk * pv) if commission and pv and risk > 0 else 0.0
    return r - fee


def evaluate_forward(conn: sqlite3.Connection, bars_for, now: int, commission: float | None,
                     pv: float | None) -> list[tuple[str, str, str]]:
    """SHADOW hypotheses with enough forward trades -> PROPOSED or REJECTED_FORWARD (final). Returns (id, status, text)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='hypotheses'").fetchone():
        return []
    prev, conn.row_factory = conn.row_factory, sqlite3.Row
    out = []
    try:
        for h in conn.execute("SELECT * FROM hypotheses WHERE status = 'SHADOW'").fetchall():
            since = h["shadow_since"] or 0
            be_at = json.loads(h["variant"]).get("breakeven_at_r")
            mine = [net_r(r, bars_for, now, be_at, commission, pv) for r in conn.execute(
                "SELECT * FROM alert_outcomes WHERE source = ? AND decision = 'QUALIFIED_UNGATED' AND decided_at >= ?"
                " AND outcome IS NOT NULL AND outcome != 'PENDING'", (f"shadow:{h['id']}", since))]
            mine = [x for x in mine if x is not None]
            if len(mine) < MIN_TRADES:
                continue
            prod = [net_r(r, bars_for, now, None, commission, pv) for r in conn.execute(
                "SELECT * FROM alert_outcomes WHERE source = 'live' AND decision IN ('ALERTED', 'BLOCKED')"
                " AND decided_at >= ? AND outcome IS NOT NULL AND outcome != 'PENDING'", (since,))]
            prod = [x for x in prod if x is not None]
            exp = sum(mine) / len(mine)
            pexp = sum(prod) / len(prod) if prod else None
            ok = exp > VIABLE_EXPECTANCY and (pexp is None or exp > pexp)
            status = "PROPOSED" if ok else "REJECTED_FORWARD"
            detail = (f"前瞻測試 {len(mine)} 筆，平均 {exp:+.2f}R（已扣手續費）；同期目前策略 "
                      + (f"{len(prod)} 筆 {pexp:+.2f}R" if pexp is not None else "沒有交易"))
            conn.execute("UPDATE hypotheses SET status = ?, verdict = ? WHERE id = ?",
                         (status, ("FORWARD_PASSED: " if ok else "FORWARD_FAILED: ") + detail, h["id"]))
            out.append((h["id"], status, detail))
        conn.commit()
    finally:
        conn.row_factory = prev
    return out


# ------------------------------------------------------------------------------------------------------------------
# Forward test by replay . Days that START on/after the day a hypothesis entered SHADOW were not in the
# backtest that selected it, so simulating them from stored bars is an honest forward test -- and unlike the live
# runner it covers every instrument in research (MGC + MNQ) and the days the computer was asleep.
# 30+ forward trades can sit on just 2 days. Trades on the same
# day share one market regime, so a decision also needs this many distinct trading days.
FORWARD_MIN_DAYS = 10
REPLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_replay (
    root TEXT NOT NULL, day TEXT NOT NULL, variant TEXT NOT NULL, rs TEXT NOT NULL, computed_at INTEGER NOT NULL,
    PRIMARY KEY (root, day, variant)
);
"""


def forward_replay(conn: sqlite3.Connection, markets: list, min_rr: float, now: int) -> int:
    """Simulate production + every SHADOW hypothesis on each completed day after it entered SHADOW (only days not
    simulated yet). markets: research.auto.Market list. Returns the number of (day, variant) results stored."""
    from ..session_engine.sessions import trading_day
    from .backtest import Variant, simulate
    conn.executescript(REPLAY_SCHEMA)
    prev, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        shadows = conn.execute("SELECT * FROM hypotheses WHERE status = 'SHADOW'").fetchall()
    finally:
        conn.row_factory = prev
    if not shadows:
        return 0
    start = {h["id"]: trading_day(h["shadow_since"]) for h in shadows if h["shadow_since"]}
    variants = {h["id"]: _variant_of(h) for h in shadows if h["id"] in start}
    stored = 0
    for m in markets:
        for d in m.days:
            want = [hid for hid, s in start.items() if d >= s]
            if not want:
                continue
            have = {r[0] for r in conn.execute("SELECT variant FROM forward_replay WHERE root=? AND day=?",
                                               (m.root, str(d)))}
            todo = [hid for hid in want if hid not in have]
            if not todo and "production" in have:
                continue
            vs = [Variant("production")] + [variants[h] for h in todo]
            res = simulate(conn, m.root, m.tick, min_rr, [d], vs, now, m.commission)
            for name, trades in res.items():
                key = "production" if name == "production" else next(h for h in todo if variants[h].name == name)
                conn.execute("INSERT OR REPLACE INTO forward_replay VALUES (?,?,?,?,?)",
                             (m.root, str(d), key, json.dumps([t.r for t in trades]), now))
                stored += 1
            conn.commit()
    return stored


def evaluate_forward_replay(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """SHADOW hypotheses with MIN_TRADES replayed forward trades -> PROPOSED / REJECTED_FORWARD (final). Production is
    compared on exactly the same instrument-days. Returns (id, status, text)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='forward_replay'").fetchone():
        return []
    out = []
    for (hid,) in conn.execute("SELECT id FROM hypotheses WHERE status = 'SHADOW'").fetchall():
        rows = conn.execute("SELECT root, day, rs FROM forward_replay WHERE variant = ?", (hid,)).fetchall()
        mine = [r for _, _, rs in rows for r in json.loads(rs)]
        if len(mine) < MIN_TRADES or len({d for _, d, _ in rows}) < FORWARD_MIN_DAYS:
            continue
        prod = [r for root, day, _ in rows for row in conn.execute(
            "SELECT rs FROM forward_replay WHERE root=? AND day=? AND variant='production'", (root, day))
                for r in json.loads(row[0])]
        per_root: dict = {}
        for root, _, rs in rows:
            per_root[root] = per_root.get(root, 0) + len(json.loads(rs))
        exp = sum(mine) / len(mine)
        pexp = sum(prod) / len(prod) if prod else None
        ok = exp > VIABLE_EXPECTANCY and (pexp is None or exp > pexp)
        status = "PROPOSED" if ok else "REJECTED_FORWARD"
        days = sorted({d for _, d, _ in rows})
        detail = (f"前瞻測試 {len(mine)} 筆（" + "、".join(f"{k} {v} 筆" for k, v in per_root.items())
                  + f"，{days[0]}～{days[-1]}），平均 {exp:+.2f}R（已扣手續費）；同期目前策略 "
                  + (f"{len(prod)} 筆 {pexp:+.2f}R" if pexp is not None else "沒有交易"))
        conn.execute("UPDATE hypotheses SET status = ?, verdict = ? WHERE id = ?",
                     (status, ("FORWARD_PASSED: " if ok else "FORWARD_FAILED: ") + detail, hid))
        out.append((hid, status, detail))
    conn.commit()
    return out


def forward_progress(conn: sqlite3.Connection) -> list[tuple[str, int, float | None, int]]:
    """(hypothesis id, forward trades so far, average net R, distinct days) for every SHADOW hypothesis."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='forward_replay'").fetchone():
        return []
    out = []
    for (hid,) in conn.execute("SELECT id FROM hypotheses WHERE status = 'SHADOW'").fetchall():
        got = conn.execute("SELECT day, rs FROM forward_replay WHERE variant=?", (hid,)).fetchall()
        rs = [r for _, x in got for r in json.loads(x)]
        out.append((hid, len(rs), sum(rs) / len(rs) if rs else None, len({d for d, _ in got})))
    return out
