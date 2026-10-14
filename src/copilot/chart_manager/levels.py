"""Pick the few 5m context objects worth drawing, and keep the chart in sync with that choice.

User choices : on the 5m pane draw previous day/week levels, session levels, unswept liquidity and
unfilled FVGs, at most 12 objects. The 1m pane only ever shows an active setup (Phase 5), never these.

Selection rules (display filters only -- the engines still record everything):
  * period/session levels: kept while within MAX_DIST_ATR of price, even if already traded through
  * swing / equal-level liquidity: only levels with no sweep/break yet
  * FVGs: only OPEN / TOUCHED / MITIGATED_50, size >= FVG_MIN_ATR, formed in the current or previous trading day
  * levels closer than MERGE_ATR are merged into one line with a combined label
  * ranked by type weight, then distance from price; capped at max_objects
Cleanup (spec section 6): every sync removes our drawings that are no longer selected (superseded, swept,
filled, too far, older than two trading days) and records why. Matching drawings are left untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..market_structure.analysis import TimeframeAnalysis
from .indicator_objects import Occupied
from ..session_engine.sessions import trading_day
from .drawings import ChartManager, Drawing

MAX_DIST_ATR = 6.0     # UNCALIBRATED display radius
MERGE_ATR = 0.25       # UNCALIBRATED
FVG_MIN_ATR = 0.5      # UNCALIBRATED display filter (detection itself keeps every size, per user)
SOURCE = "chart_manager.levels-0.1"

WEIGHT = {"PWH": 6, "PWL": 6, "PDH": 5, "PDL": 5, "EQH": 4, "EQL": 4,
          "LONDON_H": 3, "LONDON_L": 3, "NY_AM_H": 3, "NY_AM_L": 3, "NY_PM_H": 3, "NY_PM_L": 3,
          "ASIA_H": 3, "ASIA_L": 3, "SWING_HIGH": 2, "SWING_LOW": 2, "FVG": 2}
COLOR = {"period": "#e0e0e0", "session": "#90caf9", "liquidity": "#ffb300", "fvg_bull": "#26a69a",
         "fvg_bear": "#ef5350"}


@dataclass
class Candidate:
    kind: str              # LINE | ZONE
    names: list[str]
    category: str          # period | session | liquidity | fvg_bull | fvg_bear
    high: float
    low: float
    anchor_time: int
    weight: int
    reasons: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def key(self) -> str:
        return f"{self.kind}:{'/'.join(sorted(self.names))}:{self.high:.4f}:{self.low:.4f}"


def candidates(a: TimeframeAnalysis) -> list[Candidate]:
    if a.atr is None or a.last_closed_time is None:
        return []
    out: list[Candidate] = []
    for p in a.periods:
        if p.price is None or not p.complete or p.name.startswith("CD"):
            continue
        cat = "period" if p.name[:2] in ("PD", "PW") else "session"
        out.append(Candidate("LINE", [p.name], cat, p.price, p.price, (p.known_at or 0) - 1,
                             WEIGHT.get(p.name, 1), [f"{p.name} ({p.period})"]))
    touched = {(e.level.name, e.level.price, e.level.known_time) for e in a.liquidity_events}
    for lv in a.levels:
        if lv.source == "session" or (lv.name, lv.price, lv.known_time) in touched:
            continue
        out.append(Candidate("LINE", [lv.name], "liquidity", lv.price, lv.price, lv.known_time,
                             WEIGHT.get(lv.name, 1), [f"unswept {lv.side} {lv.name}"]))
    today = trading_day(a.last_closed_time)
    for g in a.fvgs:
        if g.status in ("FILLED", "INVALIDATED", "ROLL_GAP") or g.size_atr is None or g.size_atr < FVG_MIN_ATR:
            continue
        if (today - trading_day(g.time)).days > 1:
            continue
        out.append(Candidate("ZONE", ["FVG"], f"fvg_{g.direction.lower()}", g.top, g.bottom, g.time,
                             WEIGHT["FVG"], [f"{g.direction} FVG {g.size_atr:.2f} ATR, {g.status}"]))
    return out


def _duplicated(c: Candidate, occ: Occupied, tol: float) -> bool:
    """Already on the chart from the user's indicators (don't draw it twice)."""
    if c.kind == "ZONE":
        return any(c.low <= hi and lo <= c.high for lo, hi in occ.zones)
    if c.category == "liquidity":
        return any(abs(c.mid - y) <= tol for y in occ.lines)
    return False          # previous day/week and session levels: no indicator draws them


def select(a: TimeframeAnalysis, price: float, max_objects: int = 12,
           occupied: Occupied | None = None) -> list[Candidate]:
    if a.atr is None:
        return []
    near = [c for c in candidates(a) if abs(c.mid - price) <= MAX_DIST_ATR * a.atr]
    if occupied is not None:
        near = [c for c in near if not _duplicated(c, occupied, MERGE_ATR * a.atr)]
    # merge lines that sit on top of each other (keep the heavier one's category/anchor)
    lines = sorted([c for c in near if c.kind == "LINE"], key=lambda c: -c.weight)
    merged: list[Candidate] = []
    for c in lines:
        twin = next((m for m in merged if abs(m.mid - c.mid) <= MERGE_ATR * a.atr), None)
        if twin:
            twin.names += [n for n in c.names if n not in twin.names]
            twin.reasons += c.reasons
            twin.weight += 1          # confluence counts a little
        else:
            merged.append(c)
    pool = merged + [c for c in near if c.kind == "ZONE"]
    pool.sort(key=lambda c: (-c.weight, abs(c.mid - price)))
    return pool[:max_objects]


def to_drawing(c: Candidate, pane_index: int, symbol: str, now: int, horizon: int) -> Drawing:
    label = " / ".join(c.names)
    common = dict(pane_index=pane_index, symbol=symbol, environment="PRODUCTION",
                  type=label if c.kind == "LINE" else f"FVG_{c.category[4:].upper()}",
                  tier=2 if c.category in ("period", "session") else 3, creation_time=now, source=SOURCE,
                  timeframe="5", price_high=c.high, price_low=c.low, strength=float(c.weight), status="ACTIVE",
                  reason="; ".join(c.reasons), confidence="HIGH" if c.category in ("period", "session") else "MEDIUM",
                  expiration=now + horizon, validated=True, color=COLOR[c.category], text=label)
    if c.kind == "LINE":
        return Drawing(**common, shape="horizontal_ray", time1=c.anchor_time,
                       style={"showLabel": True, "horzLabelsAlign": "right", "vertLabelsAlign": "top",
                              "linestyle": 2, "linewidth": 1})
    return Drawing(**common, shape="rectangle", time1=c.anchor_time, time2=now,
                   style={"backgroundColor": COLOR[c.category], "fillBackground": True, "transparency": 85,
                          "extendRight": True, "showLabel": True, "horzLabelsAlign": "right"})


def sync(cm: ChartManager, wanted: list[Candidate], pane_index: int, symbol: str, now: int,
         horizon: int = 2 * 86400) -> dict:
    """Make our PRODUCTION drawings on this pane equal to `wanted`. Returns what was kept/added/removed."""
    existing = {r["reason"] + "|" + f"{r['price_high']:.4f}|{r['price_low']:.4f}": r
                for r in cm.owned(pane_index=pane_index, environment="PRODUCTION")}
    keep, add = [], []
    for c in wanted:
        d = to_drawing(c, pane_index, symbol, now, horizon)
        k = d.reason + "|" + f"{d.price_high:.4f}|{d.price_low:.4f}"
        if k in existing:
            keep.append(existing.pop(k)["entity_id"])
        else:
            add.append(d)
    removed = cm.remove(list(existing.values()), now, "no longer selected (swept/filled/superseded/too far/expired)")
    added = [cm.draw(d) for d in add]
    return {"kept": keep, "added": added, "removed": removed["removed"] + removed["missing"]}
