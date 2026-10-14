"""Trading-day, killzone and session/period levels in America/New_York time.

Sources:
  * Trading day boundary 18:00 ET: TradingView symbolInfo.session "1800-1700" for MGC1! .
    Bars at/after 18:00 ET belong to the NEXT trading day; 17:00-18:00 is the daily break.
  * Killzone windows: the user's ICT Killzones & Pivots [TFO] settings on the chart .
A level is only reported when the whole period is inside the loaded history -- otherwise UNKNOWN.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..market_data.models import Bar

ET = ZoneInfo("America/New_York")
DAY_START = time(18, 0)

KILLZONES: dict[str, tuple[time, time]] = {
    "ASIA": (time(20, 0), time(0, 0)),     # crosses midnight
    "LONDON": (time(2, 0), time(5, 0)),
    "NY_AM": (time(9, 30), time(11, 0)),
    "NY_PM": (time(13, 30), time(16, 0)),
}
# NY lunch (12:00-13:00) is deliberately absent: not one of the user's sessions  and disabled in
# their ICT Killzones indicator.


def to_et(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(ET)


def trading_day(ts: int) -> date:
    et = to_et(ts)
    return et.date() + timedelta(days=1) if et.time() >= DAY_START else et.date()


def trading_day_start(ts: int) -> int:
    """Unix time of 18:00 ET on the evening that opened ts's trading day."""
    d = trading_day(ts) - timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, DAY_START.hour, DAY_START.minute, tzinfo=ET).timestamp())


def trading_week(ts: int) -> tuple[int, int]:
    """ISO (year, week) of the trading day -- Sunday-evening bars belong to Monday's week."""
    y, w, _ = trading_day(ts).isocalendar()
    return y, w


def market_open(ts: int) -> bool:
    """CME Globex hours for the user's futures (metals / equity index): Sunday 18:00 ET to Friday 17:00 ET, with the
    daily 17:00-18:00 ET maintenance break. Exchange holidays are not in this calendar -- the monitor also treats
    'no new 1m bar for a long time' as closed."""
    et = to_et(ts)
    wd, t = et.weekday(), et.time()                    # Monday = 0
    if wd == 5:                                        # Saturday
        return False
    if wd == 6:                                        # Sunday: opens 18:00
        return t >= DAY_START
    if wd == 4 and t >= time(17, 0):                   # Friday close
        return False
    return not (time(17, 0) <= t < DAY_START)          # daily break


def killzone(ts: int) -> str | None:
    t = to_et(ts).time()
    for name, (start, end) in KILLZONES.items():
        inside = (t >= start or t < end) if end <= start else (start <= t < end)
        if inside:
            return name
    return None


@dataclass(frozen=True)
class PeriodLevel:
    name: str            # PDH, PDL, PWH, PWL, ASIA_H, ..., CDH (current day high), ...
    price: float | None  # None = UNKNOWN
    period: str          # e.g. trading day 2026-01-02
    known_at: int | None # unix time of the close of the last bar of the period (when the level became final)
    complete: bool
    reason: str = ""


def _hl(bars: list[Bar]) -> tuple[float, float]:
    return max(b.high for b in bars), min(b.low for b in bars)


def _bar_close_time(b: Bar, bar_seconds: int) -> int:
    return b.time + bar_seconds


def period_levels(bars: list[Bar], bar_seconds: int) -> list[PeriodLevel]:
    """Previous/current trading day, previous week, and the most recent COMPLETED instance of each killzone.

    Only closed bars should be passed. A period counts as complete only if the loaded history contains bars
    both before it starts and after it ends -- otherwise its high/low could come from a partial period.
    """
    out: list[PeriodLevel] = []
    if not bars:
        return out

    days: dict[date, list[Bar]] = {}
    for b in bars:
        days.setdefault(trading_day(b.time), []).append(b)
    ordered = sorted(days)
    current = ordered[-1]

    h, l = _hl(days[current])
    out += [PeriodLevel("CDH", h, f"trading day {current}", None, False, "current day, still forming"),
            PeriodLevel("CDL", l, f"trading day {current}", None, False, "current day, still forming")]

    if len(ordered) >= 3:   # need a day before the previous day to know the previous day started inside history
        prev = ordered[-2]
        h, l = _hl(days[prev])
        known = _bar_close_time(days[prev][-1], bar_seconds)
        out += [PeriodLevel("PDH", h, f"trading day {prev}", known, True),
                PeriodLevel("PDL", l, f"trading day {prev}", known, True)]
    else:
        out += [PeriodLevel(n, None, "previous trading day", None, False,
                            "history does not cover the whole previous trading day") for n in ("PDH", "PDL")]

    weeks: dict[tuple[int, int], list[Bar]] = {}
    for b in bars:
        weeks.setdefault(trading_week(b.time), []).append(b)
    wk = sorted(weeks)
    if len(wk) >= 3:
        pw = wk[-2]
        h, l = _hl(weeks[pw])
        known = _bar_close_time(weeks[pw][-1], bar_seconds)
        out += [PeriodLevel("PWH", h, f"week {pw[0]}-W{pw[1]:02d}", known, True),
                PeriodLevel("PWL", l, f"week {pw[0]}-W{pw[1]:02d}", known, True)]
    else:
        out += [PeriodLevel(n, None, "previous week", None, False,
                            "history does not cover the whole previous week") for n in ("PWH", "PWL")]

    # Killzones: group consecutive bars of the same zone into instances; the last instance is complete only
    # if a later bar outside it exists, and it must not be the very first instance in history (could be cut).
    instances: list[tuple[str, list[Bar]]] = []
    for b in bars:
        z = killzone(b.time)
        if z is None:
            continue
        if instances and instances[-1][0] == z and b.time - instances[-1][1][-1].time <= 3 * 3600:
            instances[-1][1].append(b)
        else:
            instances.append((z, [b]))
    last_bar_time = bars[-1].time
    first_bar_time = bars[0].time
    for name in KILLZONES:
        done = [
            (z, bs) for z, bs in instances
            if z == name and bs[-1].time < last_bar_time and killzone(bs[0].time - bar_seconds) != name
            and bs[0].time > first_bar_time
        ]
        if not done:
            out += [PeriodLevel(f"{name}_{s}", None, name, None, False, "no completed session in history")
                    for s in ("H", "L")]
            continue
        z, bs = done[-1]
        h, l = _hl(bs)
        known = _bar_close_time(bs[-1], bar_seconds)
        label = f"{name} {to_et(bs[0].time):%Y-%m-%d}"
        out += [PeriodLevel(f"{name}_H", h, label, known, True), PeriodLevel(f"{name}_L", l, label, known, True)]
    return out
