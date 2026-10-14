from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import FEATURE_VERSION
from ..market_data.models import ChartSnapshot

SCHEMA_VERSION = 3  # v3 adds indicator_scans (created by schema.sql; no column migration needed)
_FIELDS = ("open", "high", "low", "close", "volume")


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    if current < 2:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chart_snapshots)")}
        if "pane_index" not in cols:
            conn.execute("ALTER TABLE chart_snapshots ADD COLUMN pane_index INTEGER")


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":                       # fresh clone: data/ (and its reports / logs) may not exist yet
        for d in (Path(path).parent, Path(path).parent / "reports", Path(path).parent / "logs"):
            d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8"))
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] < SCHEMA_VERSION:
        _migrate(conn, row[0])
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def save_snapshot(conn: sqlite3.Connection, snap: ChartSnapshot) -> dict:
    """Persist a snapshot and its closed bars. Returns counts for reporting."""
    last = snap.bars[-1] if snap.bars else None
    conn.execute(
        """INSERT INTO chart_snapshots (source, fetched_at, feed_symbol, pro_symbol, resolution, delay_seconds,
               data_quality, quality_reasons, min_tick, point_value, studies, bar_count, last_bar_time,
               last_close, last_bar_closed, feature_version, pane_index)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (snap.source, snap.fetched_at, snap.symbol.full_name, snap.symbol.pro_name, snap.resolution,
         snap.symbol.delay_seconds, snap.data_quality.value, json.dumps(snap.quality_reasons),
         snap.symbol.min_tick, snap.symbol.point_value, json.dumps(snap.studies), len(snap.bars),
         last.time if last else None, last.close if last else None,
         int(last.is_closed) if last else None, FEATURE_VERSION, snap.pane_index),
    )
    inserted = discrepancies = 0
    for b in snap.bars:
        if not b.is_closed:
            continue
        row = conn.execute(
            "SELECT open, high, low, close, volume FROM bars WHERE feed_symbol=? AND resolution=? AND time=?",
            (snap.symbol.full_name, snap.resolution, b.time),
        ).fetchone()
        observed = {f: getattr(b, f) for f in _FIELDS}
        if row is None:
            conn.execute(
                "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (snap.symbol.full_name, snap.resolution, b.time, b.open, b.high, b.low, b.close, b.volume,
                 snap.source, snap.fetched_at, snap.data_quality.value),
            )
            inserted += 1
        elif any(abs(row[f] - observed[f]) > 1e-9 for f in _FIELDS) and conn.execute(
                # the stored bar is kept as first seen; log each distinct revision once, not on every poll
                "SELECT 1 FROM data_discrepancies WHERE feed_symbol=? AND resolution=? AND time=? AND observed=?",
                (snap.symbol.full_name, snap.resolution, b.time, json.dumps(observed))).fetchone() is None:
            conn.execute(
                "INSERT INTO data_discrepancies (detected_at, feed_symbol, resolution, time, stored, observed, source)"
                " VALUES (?,?,?,?,?,?,?)",
                (snap.fetched_at, snap.symbol.full_name, snap.resolution, b.time,
                 json.dumps({f: row[f] for f in _FIELDS}), json.dumps(observed), snap.source),
            )
            discrepancies += 1
    conn.commit()
    return {"bars_inserted": inserted, "discrepancies": discrepancies}


def save_indicator_scan(conn: sqlite3.Connection, scanned_at: int, ind, hits: list, analyzer_version: str,
                        review_status: str, review_version: str | None) -> None:
    conn.execute(
        """INSERT INTO indicator_scans (scanned_at, name, script_id, digest, panes, source_status, status_reason,
               source_sha256, static_hits, analyzer_version, review_status, review_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (scanned_at, ind.name, ind.script_id, ind.digest, json.dumps(ind.panes), ind.source_status,
         ind.status_reason, ind.source_sha256, json.dumps([h.__dict__ for h in hits]), analyzer_version,
         review_status, review_version),
    )
    conn.commit()
