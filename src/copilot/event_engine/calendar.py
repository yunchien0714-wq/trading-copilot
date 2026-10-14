"""Economic events from TradingView's economic calendar (fetched inside the TradingView page, read-only).

Informational only (spec section 13): events never create a directional signal. They feed
  * reminders  ("EVENT REMINDER ... upcoming in 35 minutes")
  * the risk gate's news blackout: no entries from `before` minutes ahead of to `after` minutes after a
    HIGH-importance event (30 / 30).
Importance is TradingView's own field (1 = high, 0 = medium, -1 = low). If the calendar cannot be fetched the
blackout check returns UNKNOWN, which the gate treats as a failure.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..session_engine.sessions import to_et

SOURCE = "TradingView economic calendar (economic-calendar.tradingview.com)"
HIGH = 1

FETCH_JS = """
(async function(fromIso, toIso, countries) {
  var url = 'https://economic-calendar.tradingview.com/events?from=' + fromIso + '&to=' + toIso + '&countries=' + countries;
  try {
    var r = await fetch(url);
    if (r.status !== 200) return {error: 'HTTP ' + r.status};
    var d = await r.json();
    return {events: (d.result || []).map(function(e) {
      return {id: e.id, title: e.title, country: e.country, importance: e.importance, date: e.date,
              source: e.source, actual: e.actual, forecast: e.forecast, previous: e.previous};
    })};
  } catch (e) { return {error: String(e)}; }
})(%s, %s, %s)
"""


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    country: str
    importance: int
    time: int          # unix s UTC
    publisher: str | None


def fetch(evaluate_async: Callable[[str], dict], start: int, end: int, countries: str = "US") -> list[Event] | None:
    """None = calendar unavailable (UNKNOWN)."""
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    res = evaluate_async(FETCH_JS % (json.dumps(iso(start)), json.dumps(iso(end)), json.dumps(countries)))
    if not res or "error" in res:
        return None
    return parse(res["events"])


def parse(raw: list[dict]) -> list[Event]:
    out = []
    for e in raw:
        try:
            t = int(datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp())
        except (KeyError, ValueError, TypeError):
            continue          # an event without a parseable time is dropped, never guessed
        out.append(Event(str(e.get("id")), e.get("title") or "?", e.get("country") or "?",
                         int(e.get("importance") if e.get("importance") is not None else -1), t, e.get("source")))
    return sorted(out, key=lambda x: x.time)


def blackout(events: list[Event] | None, t: int, before_min: int, after_min: int,
             min_importance: int = HIGH) -> tuple[str, Event | None]:
    """("CLEAR", None) | ("BLACKOUT", event) | ("UNKNOWN", None) for a decision at time t."""
    if events is None:
        return "UNKNOWN", None
    for e in events:
        if e.importance >= min_importance and e.time - before_min * 60 <= t <= e.time + after_min * 60:
            return "BLACKOUT", e
    return "CLEAR", None


def reminders(events: list[Event], now: int, horizon_min: int = 120, min_importance: int = 0) -> list[str]:
    out = []
    for e in events:
        mins = (e.time - now) // 60
        if 0 <= mins <= horizon_min and e.importance >= min_importance:
            level = "HIGH" if e.importance >= HIGH else "medium"
            out.append(f"EVENT REMINDER ({level}): {e.title} at {to_et(e.time):%H:%M} ET -- in {mins} min. "
                       f"Trading rule: do not initiate news-driven trades.")
    return out
