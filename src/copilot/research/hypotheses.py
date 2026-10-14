"""Hypothesis registry (spec section 18 DB4 / section 23). Every proposed improvement is recorded with the evidence
that motivated it and every test result. Status flow:
  RESEARCH -> (walk-forward verdict CANDIDATE) -> CANDIDATE -> (user approval) -> APPROVED -> production release
A hypothesis can never change production by itself.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from ..setup_engine.poc_fvg import RuleSet
from .backtest import BACKTEST_VERSION, Variant

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY, statement TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
    variant TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
    last_tested_at INTEGER, sample TEXT, results TEXT, verdict TEXT, version TEXT
);
"""

# Seeded from the Phase 12 replay  and the first graded live trade. Evidence is descriptive only.
SEED = [
    ("H1", "The volume-support filter removes setups that were not worse than the ones it keeps.",
     "Replay 9/29-10/2: setups blocked by volume support would have averaged +0.09R (n=89).",
     Variant("H1_no_volume_filter", RuleSet(name="H1", use_volume_support=False))),
    ("H2", "Moving the stop to break-even after a bar closes beyond +1R improves expectancy.",
     "Winning trades often give back open profit before reaching 2R.",
     Variant("H2_breakeven_1R", RuleSet(name="H2"), breakeven_at_r=1.0)),
    ("H3", "Rejection-candle confirmation alone beats rejection-or-hold.",
     "Hold confirmation (3 closes not beyond the zone) is a provisional, uncalibrated rule.",
     Variant("H3_rejection_only", RuleSet(name="H3", confirmations=("REJECTION",)))),
    ("H4", "A 1.5R target (the user's minimum RR) beats the 2R target.",
     "Most user trades so far closed before 2R; 1:1.5 is the user's stated minimum.",
     Variant("H4_target_1_5R", RuleSet(name="H4", target_r=1.5))),
    ("H5", "Reversal setups alone (against the 5m structure) beat production (trend + reversal).",
     "Replay 9/15-10/7 with engine 0.3.0: reversals the system let through averaged +0.20R (n=20), "
     "trend setups -0.33R (n=41).",
     Variant("H5_reversal_only", RuleSet(name="H5", setup_types=("REVERSAL",)))),
    ("H6", "Trend setups only (no reversals) beat production.",
     "Counter-check of H5: does adding reversal setups help or hurt?",
     Variant("H6_trend_only", RuleSet(name="H6", allow_reversal=False))),
    ("H7", "A 3R target beats the 2R target.",
     "Replay: setups the system lets through average -0.15R with a fixed 2R target -- target placement questioned.",
     Variant("H7_target_3R", RuleSet(name="H7", target_r=3.0))),
    ("H8", "Target at the nearest structural level (HVN / opposing FVG edge), traded only if RR >= minimum, beats a "
           "fixed 2R target.",
     "Target the next volume node or resistance instead of a fixed R multiple.",
     Variant("H8_structural_target", RuleSet(name="H8", target_mode="structural"))),
    ("H9", "FVG retest reversals (against the 5m structure: touch the FVG, leave, come back, a 1m bar enters it and "
           "closes back outside; no POC needed) added to production alerts beat production.",
     "Price fails twice at an FVG before the 5m structure flips; the second rejection is the entry.",
     Variant("H9_fvg_retest_reversal", RuleSet(name="H9", retest_reversal=True, reversal_fvg_only=True))),
]


def seed(conn: sqlite3.Connection, now: int) -> None:
    conn.executescript(SCHEMA)
    for hid, stmt, ev, var in SEED:
        conn.execute("INSERT OR IGNORE INTO hypotheses (id, statement, reason, evidence, variant, status, created_at)"
                     " VALUES (?,?,?,?,?,?,?)", (hid, stmt, "observed in Phase 12 / journals", ev,
                                                 json.dumps({"name": var.name, "rules": asdict(var.rules),
                                                             "breakeven_at_r": var.breakeven_at_r}), "RESEARCH", now))
    conn.commit()


def variants() -> dict[str, Variant]:
    return {hid: var for hid, _, _, var in SEED}


def record(conn: sqlite3.Connection, hid: str, verdict, sample: dict, now: int) -> None:
    status = "CANDIDATE" if verdict.verdict == "CANDIDATE" else "RESEARCH"
    res = {k: asdict(getattr(verdict, k)) for k in ("in_sample", "out_sample", "base_in", "base_out")}
    res["p_better"] = verdict.p_better
    res["reasons"] = verdict.reasons
    conn.execute("UPDATE hypotheses SET last_tested_at=?, sample=?, results=?, verdict=?, status=?, version=? WHERE id=?",
                 (now, json.dumps(sample, default=str), json.dumps(res), verdict.verdict, status, BACKTEST_VERSION, hid))
    conn.commit()
