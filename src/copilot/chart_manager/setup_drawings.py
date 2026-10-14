"""Tier-1 drawings on the 1m pane for an ACTIVE setup only (the 1m pane shows only what
matters for the entry). Drawn only for TRADE_SETUP (setup qualified AND risk gate passed); removed when they
expire or when the setup's lifecycle ends.
"""
from __future__ import annotations

from ..setup_engine.poc_fvg import Evaluation
from .drawings import ChartManager, Drawing

SOURCE = "chart_manager.setup_drawings-0.1"
TTL_SECONDS = 2 * 3600       # UNCALIBRATED: a qualified setup's lines stay at most 2 hours
COLORS = {"ENTRY": "#e0e0e0", "STOP": "#ef5350", "TARGET": "#26a69a"}


def draw_setup(cm: ChartManager, ev: Evaluation, key: str, pane_index: int, symbol: str, now: int) -> list[str]:
    ids = []
    for kind, price in (("ENTRY", ev.entry), ("STOP", ev.stop), ("TARGET", ev.target)):
        if price is None:
            continue
        label = f"{ev.direction} {kind} {price:.1f}" + (f"  RR {ev.rr}" if kind == "TARGET" and ev.rr else "")
        ids.append(cm.draw(Drawing(
            pane_index=pane_index, symbol=symbol, environment="PRODUCTION", type=f"SETUP_{kind}", tier=1,
            creation_time=now, source=SOURCE, timeframe="1", price_high=price, price_low=price, strength=None,
            status="ACTIVE", reason=f"setup {key}", confidence="UNKNOWN", expiration=now + TTL_SECONDS,
            validated=True, shape="horizontal_ray", time1=ev.confirmed_at or now, text=label, color=COLORS[kind],
            style={"showLabel": True, "horzLabelsAlign": "right", "vertLabelsAlign": "top", "linewidth": 2})))
    return ids


def cleanup(cm: ChartManager, now: int, ended_keys: set[str] = frozenset()) -> list[str]:
    """Remove setup drawings that expired or whose setup ended."""
    rows = [r for r in cm.owned(environment="PRODUCTION")
            if r["type"].startswith("SETUP_")
            and ((r["expiration"] is not None and r["expiration"] <= now) or r["reason"][6:] in ended_keys)]
    res = cm.remove(rows, now, "setup expired or ended")
    return res["removed"] + res["missing"]
