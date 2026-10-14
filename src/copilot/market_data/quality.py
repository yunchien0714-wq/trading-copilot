"""Data-quality assessment. Rules are conservative: anything we cannot verify is not REALTIME."""
from __future__ import annotations

from .models import RESOLUTION_SECONDS, Bar, DataQuality

# Allowed slack beyond one bar length before a "realtime" feed is considered stale.
# Futures trade ~23h/day; outside trading hours staleness is expected and reported as STALE (not an error).
STALE_GRACE_SECONDS = 90


def mark_closed(raw_bars: list[dict], resolution: str, now: int, delay_seconds: int | None) -> list[Bar]:
    """Convert raw bars and flag which are closed.

    A bar is closed only if its end time has passed in *feed time* (now minus feed delay).
    The last bar TradingView returns is always treated as forming, because TradingView keeps
    updating it until the next bar appears -- even if the clock says it should be complete.
    """
    length = RESOLUTION_SECONDS.get(resolution)
    if length is None:
        raise ValueError(f"Unsupported resolution {resolution!r}; refusing to guess bar length")
    feed_now = now - (delay_seconds or 0)
    bars = []
    for i, b in enumerate(raw_bars):
        is_last = i == len(raw_bars) - 1
        closed = (not is_last) and (int(b["time"]) + length <= feed_now)
        bars.append(Bar(int(b["time"]), float(b["open"]), float(b["high"]), float(b["low"]),
                        float(b["close"]), float(b.get("volume") or 0), closed))
    return bars


INFER_MAX_AGE = 90   # newest 1m bar must have opened <= 90 s ago to infer a realtime feed


def assess(bars: list[Bar], resolution: str, now: int, delay_seconds: int | None,
           full_name: str = "") -> tuple[DataQuality, list[str]]:
    reasons: list[str] = []
    if delay_seconds is None:
        # Broker-supplied feeds (e.g. Tradovate via TradingView) report no delay field. Realtime is
        # then INFERRED only from measured freshness on a 1m chart: the bar for the current minute exists,
        # so the feed lags by less than a minute. 5m bars cannot prove that (they only bound lag to 5 min).
        if (resolution == "1" and bars and "_DL:" not in full_name
                and 0 <= now - bars[-1].time <= INFER_MAX_AGE):
            return DataQuality.REALTIME, [f"delay not reported; realtime INFERRED: newest 1m bar opened "
                                          f"{now - bars[-1].time}s ago (lag < 1 bar)"]
        return DataQuality.UNKNOWN, ["feed delay not reported by TradingView"]
    if not bars:
        return DataQuality.UNKNOWN, ["no bars loaded on chart"]
    length = RESOLUTION_SECONDS.get(resolution)
    if length is None:
        return DataQuality.UNKNOWN, [f"unsupported resolution {resolution!r}"]

    if delay_seconds > 0:
        reasons.append(f"feed is delayed by {delay_seconds}s (TradingView symbolInfo.delay)")
        return DataQuality.DELAYED, reasons

    age = now - bars[-1].time  # time since the forming bar OPENED
    if age > length + STALE_GRACE_SECONDS:
        reasons.append(f"latest bar opened {age}s ago on a {length}s timeframe (market closed or feed stalled)")
        return DataQuality.STALE, reasons
    if age < -STALE_GRACE_SECONDS:
        reasons.append(f"latest bar is {-age}s in the future -- clock mismatch")
        return DataQuality.UNKNOWN, reasons
    return DataQuality.REALTIME, reasons


def find_gaps(bars: list[Bar], resolution: str) -> list[tuple[int, int]]:
    """Return (prev_time, next_time) pairs where consecutive bars are not one bar-length apart.

    Gaps are expected across the daily maintenance break and weekends; callers decide what is abnormal.
    """
    length = RESOLUTION_SECONDS[resolution]
    return [(a.time, b.time) for a, b in zip(bars, bars[1:]) if b.time - a.time != length]
