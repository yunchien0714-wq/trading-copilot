"""Per-zone setup lifecycle + alert de-duplication (spec sections 8, 9, 10, 36).

Each zone (keyed by its 5m FVG) keeps its own state in the `setups` table, so nearby zones never overwrite
each other (the flip-flopping seen in replays). Terminal states are frozen: QUALIFIED, INVALIDATED,
EXPIRED (TRADE_ACTIVE / COMPLETED come with manual-trade detection, Phase 8).

Notifications (each at most once per zone):
  POTENTIAL      first time a zone reaches POTENTIAL_SETUP or CONFIRMING with no condition failed yet
  TRADE_SETUP    QUALIFIED and the hard risk gate PASSED
  RISK_BLOCKED   QUALIFIED but the risk gate FAILED (states which rules failed -- spec 12)
  INVALIDATED    the zone died (INVALIDATED / EXPIRED / disappeared) after the user was already told about it
WATCHING never notifies: "no setup" is a normal, quiet output.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from .poc_fvg import ENGINE_VERSION, Evaluation, zone_key

TERMINAL = {"QUALIFIED", "INVALIDATED", "EXPIRED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS setups (
    key TEXT PRIMARY KEY, symbol TEXT NOT NULL, setup TEXT NOT NULL, direction TEXT NOT NULL,
    zone_low REAL NOT NULL, zone_high REAL NOT NULL, state TEXT NOT NULL,
    first_seen INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    entry REAL, stop REAL, target REAL, rr REAL, confirmed_at INTEGER, confirmation TEXT, invalidation TEXT,
    conditions TEXT NOT NULL, poc_sources TEXT NOT NULL,
    engine_version TEXT NOT NULL, risk_version TEXT, risk_passed INTEGER, risk_detail TEXT,
    alerted TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS setup_transitions (
    key TEXT NOT NULL, at INTEGER NOT NULL, from_state TEXT, to_state TEXT NOT NULL
);
"""


@dataclass
class Notification:
    kind: str            # POTENTIAL | TRADE_SETUP | RISK_BLOCKED | INVALIDATED
    key: str
    evaluation: Evaluation | None
    risk: object | None = None
    reason: str = ""


class SetupTracker:
    def __init__(self, conn: sqlite3.Connection, symbol: str):
        self.conn, self.symbol = conn, symbol
        conn.executescript(SCHEMA)

    def _row(self, key):
        return self.conn.execute("SELECT * FROM setups WHERE key = ?", (key,)).fetchone()

    def _transition(self, key, now, old, new):
        if old != new:
            self.conn.execute("INSERT INTO setup_transitions VALUES (?,?,?,?)", (key, now, old, new))

    def update(self, evaluations: list[Evaluation], now: int,
               risk_check: Callable[[Evaluation], object]) -> list[Notification]:
        """risk_check(ev) -> GateResult (with .passed, .failures, .version)."""
        out: list[Notification] = []
        seen = set()
        for ev in evaluations:
            if ev.zone is None:
                continue
            key = zone_key(ev.zone)
            seen.add(key)
            row = self._row(key)
            if row is not None and row["state"] in TERMINAL:
                continue                                   # frozen
            if row is None and ev.state == "NO_SETUP":
                continue                                   # not in play yet -- nothing worth recording
            alerted = set(json.loads(row["alerted"])) if row else set()
            gate = risk_check(ev) if ev.state == "QUALIFIED" else None
            if row is None:
                self.conn.execute(
                    "INSERT INTO setups (key, symbol, setup, direction, zone_low, zone_high, state, first_seen,"
                    " updated_at, conditions, poc_sources, engine_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, self.symbol, ev.setup, ev.direction, ev.zone.low, ev.zone.high, ev.state, now, now,
                     json.dumps(ev.conditions), json.dumps(ev.zone.poc_sources), ev.version))
            self._transition(key, now, row["state"] if row else None, ev.state)
            self.conn.execute(
                "UPDATE setups SET state=?, updated_at=?, entry=?, stop=?, target=?, rr=?, confirmed_at=?,"
                " confirmation=?, invalidation=?, conditions=?, engine_version=?, risk_version=?, risk_passed=?,"
                " risk_detail=? WHERE key=?",
                (ev.state, now, ev.entry, ev.stop, ev.target, ev.rr, ev.confirmed_at, ev.confirmation,
                 ev.invalidation, json.dumps(ev.conditions), ev.version,
                 getattr(gate, "version", None), None if gate is None else int(gate.passed),
                 None if gate is None else json.dumps([(r.rule, r.status, r.detail) for r in gate.rules]), key))

            new = []
            # Spec 9: a potential setup = most conditions developing, NONE already failed. A zone whose 5m
            # structure or volume support has already failed is not "potential" -- it stays quiet (observed:
            # without this, 10-02 would have produced 25 potential alerts, most dead within 2 minutes).
            clean = not any(st in ("FAIL", "UNKNOWN") for st, _ in ev.conditions.values())
            if ev.state in ("POTENTIAL_SETUP", "CONFIRMING") and clean and "POTENTIAL" not in alerted:
                new.append(Notification("POTENTIAL", key, ev))
            elif ev.state == "QUALIFIED":
                kind = "TRADE_SETUP" if gate.passed else "RISK_BLOCKED"
                if kind not in alerted:
                    new.append(Notification(kind, key, ev, gate))
            elif ev.state in ("INVALIDATED", "EXPIRED") and alerted and "INVALIDATED" not in alerted:
                new.append(Notification("INVALIDATED", key, ev, reason=ev.invalidation or ev.state))
            if new:
                alerted |= {n.kind for n in new}
                self.conn.execute("UPDATE setups SET alerted=? WHERE key=?", (json.dumps(sorted(alerted)), key))
                out += new

        # zones that vanished (5m FVG filled/invalidated) before reaching a terminal state
        for row in self.conn.execute("SELECT * FROM setups WHERE symbol = ? AND state NOT IN ('QUALIFIED',"
                                     " 'INVALIDATED', 'EXPIRED')", (self.symbol,)).fetchall():
            if row["key"] in seen:
                continue
            self._transition(row["key"], now, row["state"], "EXPIRED")
            self.conn.execute("UPDATE setups SET state='EXPIRED', updated_at=?, invalidation=? WHERE key=?",
                              (now, "zone no longer active (5m FVG filled or invalidated)", row["key"]))
            alerted = set(json.loads(row["alerted"]))
            if alerted and "INVALIDATED" not in alerted:
                self.conn.execute("UPDATE setups SET alerted=? WHERE key=?",
                                  (json.dumps(sorted(alerted | {"INVALIDATED"})), row["key"]))
                out.append(Notification("INVALIDATED", row["key"], None,
                                        reason="zone no longer active (5m FVG filled or invalidated)"))
        self.conn.commit()
        return out
