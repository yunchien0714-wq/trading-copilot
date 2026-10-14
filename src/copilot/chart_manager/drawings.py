"""Chart drawings owned by the co-pilot.

Safety rules:
  * Only shapes whose IDs this module created (and recorded in the chart_drawings table) are ever removed.
    removeAllShapes is never called -- the user's own drawings (e.g. their position tools) are untouchable.
  * Every drawing carries the spec section-5 metadata.
  * environment: PRODUCTION | CALIBRATION (temporary, user-requested). EXPERIMENTAL/RESEARCH objects are
    rejected outright -- spec section 24/37: they must never appear on the live chart.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Callable

ALLOWED_ENVIRONMENTS = {"PRODUCTION", "CALIBRATION"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_drawings (
    entity_id     TEXT PRIMARY KEY,
    pane_index    INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    environment   TEXT    NOT NULL,
    type          TEXT    NOT NULL,
    tier          INTEGER NOT NULL,
    creation_time INTEGER NOT NULL,
    source        TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL,
    price_high    REAL    NOT NULL,
    price_low     REAL    NOT NULL,
    strength      REAL,
    status        TEXT    NOT NULL,
    reason        TEXT    NOT NULL,
    confidence    TEXT    NOT NULL,
    expiration    INTEGER,
    validated     INTEGER NOT NULL,
    removed_at    INTEGER,
    removed_why   TEXT
);
"""


@dataclass
class Drawing:
    pane_index: int
    symbol: str
    environment: str
    type: str                 # e.g. SWING_HIGH, PDH, FVG_BULL
    tier: int                 # 1 critical, 2 context, 3 temporary
    creation_time: int
    source: str               # module + version that produced it
    timeframe: str
    price_high: float
    price_low: float
    strength: float | None
    status: str               # ACTIVE | ...
    reason: str
    confidence: str           # HIGH | MEDIUM | LOW | UNKNOWN
    expiration: int | None
    validated: bool
    # rendering
    shape: str = "text"       # TradingView shape name
    time1: int = 0            # anchor bar time (unix s)
    time2: int | None = None  # second point for multipoint shapes
    text: str = ""
    color: str = "#9e9e9e"
    style: dict | None = None  # extra TradingView overrides (rendering only, not stored)


def build_create_js(d: Drawing) -> str:
    overrides = {"color": d.color, "textcolor": d.color, "linecolor": d.color, "fontsize": 11, **(d.style or {})}
    common = json.dumps({"shape": d.shape, "text": d.text, "overrides": overrides,
                         "lock": True, "disableSelection": False, "disableSave": False})
    if d.time2 is not None:
        pts = json.dumps([{"time": d.time1, "price": d.price_high}, {"time": d.time2, "price": d.price_low}])
        call = f"c.createMultipointShape({pts}, {common})"
    else:
        price = d.price_high
        call = f"c.createShape({json.dumps({'time': d.time1, 'price': price})}, {common})"
    return f"""
(async function() {{
  var c = window.TradingViewApi.chart({int(d.pane_index)});
  var id = {call};
  if (id && typeof id.then === 'function') id = await id;
  return id || null;
}})()
"""


def build_remove_js(pane_index: int, entity_ids: list[str]) -> str:
    return f"""
(function() {{
  var c = window.TradingViewApi.chart({int(pane_index)});
  var want = {json.dumps(entity_ids)}, present = {{}}, removed = [], missing = [];
  c.getAllShapes().forEach(function(s) {{ present[s.id] = true; }});
  want.forEach(function(id) {{
    if (present[id]) {{ c.removeEntity(id); removed.push(id); }} else {{ missing.push(id); }}
  }});
  return {{removed: removed, missing: missing}};
}})()
"""


class ChartManager:
    def __init__(self, conn: sqlite3.Connection, evaluate_async: Callable[[str], object]):
        self.conn, self.eval = conn, evaluate_async
        conn.executescript(SCHEMA)

    def draw(self, d: Drawing) -> str:
        if d.environment not in ALLOWED_ENVIRONMENTS:
            raise ValueError(f"environment {d.environment!r} may not be drawn on the live chart")
        entity_id = self.eval(build_create_js(d))
        if not entity_id:
            raise RuntimeError(f"TradingView did not return an id for {d.type} @ {d.price_high}")
        row = {k: v for k, v in asdict(d).items() if k not in ("shape", "time1", "time2", "text", "color", "style")}
        row["validated"] = int(row["validated"])
        cols = ", ".join(["entity_id", *row])
        self.conn.execute(f"INSERT INTO chart_drawings ({cols}) VALUES ({', '.join('?' * (len(row) + 1))})",
                          (entity_id, *row.values()))
        self.conn.commit()
        return entity_id

    def owned(self, pane_index: int | None = None, environment: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM chart_drawings WHERE removed_at IS NULL", []
        if pane_index is not None:
            q += " AND pane_index = ?"
            args.append(pane_index)
        if environment is not None:
            q += " AND environment = ?"
            args.append(environment)
        return self.conn.execute(q, args).fetchall()

    def remove(self, rows: list[sqlite3.Row], now: int, why: str) -> dict:
        result = {"removed": [], "missing": []}
        by_pane: dict[int, list[str]] = {}
        for r in rows:
            by_pane.setdefault(r["pane_index"], []).append(r["entity_id"])
        for pane, ids in by_pane.items():
            res = self.eval(build_remove_js(pane, ids)) or {}
            for k in result:
                result[k] += res.get(k, [])
        # 'missing' = already gone from the chart (e.g. user deleted it) -> also mark as removed
        for eid in result["removed"] + result["missing"]:
            self.conn.execute("UPDATE chart_drawings SET removed_at = ?, removed_why = ? WHERE entity_id = ?",
                              (now, why, eid))
        self.conn.commit()
        return result
