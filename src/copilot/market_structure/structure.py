"""Swings, break of structure, and regime classification for one timeframe.

User definitions :
  * Swing = a point followed by a MEANINGFUL retracement (not an N-bar fractal). Implemented as an ATR
    zigzag: an extreme becomes a confirmed swing once a LATER bar retraces >= retrace_atr * ATR from it.
  * BOS = a candle CLOSE beyond the last confirmed swing. Wick-only breaks are not BOS (liquidity engine
    handles those as sweeps).
Every object records when it became knowable (confirmed_index / index of the closing bar) so later phases
can replay history without lookahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..market_data.models import Bar
from ..market_data.series import atr as atr_series


@dataclass(frozen=True)
class StructureParams:
    atr_len: int = 14
    # CALIBRATED by the user on the 5m MGC chart: swings labelled with strength >= 3 ("H3") are the
    # ones they consider a meaningful pullback; 1-2x ATR swings are noise to them.
    retrace_atr: float = 3.0
    stale_bars: int = 60          # no BOS for this many bars -> NEUTRAL. UNCALIBRATED
    calibrated: bool = True       # refers to retrace_atr only; stale_bars is still a default


@dataclass(frozen=True)
class Swing:
    kind: str                 # HIGH | LOW
    price: float
    index: int                # bar of the extreme
    time: int
    confirmed_index: int      # bar whose retracement confirmed it (knowable at that bar's close)
    confirmed_time: int
    label: str | None         # HH | LH | HL | LL | None (first of its kind)


@dataclass(frozen=True)
class BosEvent:
    direction: str            # UP | DOWN
    index: int                # bar that closed beyond the swing
    time: int
    close: float
    swing: Swing
    shift: bool               # True if it breaks AGAINST the prevailing swing-label trend (structure shift)


@dataclass
class Regime:
    state: str                # BULLISH | BEARISH | NEUTRAL | RANGE | TRANSITION | UNKNOWN
    evidence: list[str] = field(default_factory=list)


def detect_swings(bars: list[Bar], p: StructureParams = StructureParams()) -> list[Swing]:
    a = atr_series(bars, p.atr_len)
    swings: list[Swing] = []
    direction = 0                    # +1 tracking a high, -1 tracking a low, 0 undecided
    hi_i = lo_i = 0
    for i in range(1, len(bars)):
        b = bars[i]
        if bars[i].high > bars[hi_i].high:
            hi_i = i
        if bars[i].low < bars[lo_i].low:
            lo_i = i
        thr = a[i]
        if thr is None:
            continue
        thr *= p.retrace_atr
        # Retracement must come from a LATER bar than the extreme: intrabar order of high/low is unknown.
        hi_conf = direction >= 0 and i > hi_i and bars[hi_i].high - b.low >= thr
        lo_conf = direction <= 0 and i > lo_i and b.high - bars[lo_i].low >= thr
        if direction == 0 and hi_conf and lo_conf:
            # both legs qualify on the same bar: take the older extreme first
            hi_conf, lo_conf = (True, False) if hi_i < lo_i else (False, True)
        # An extreme on the first loaded bar may not be the real extreme (it could lie before the history
        # starts), so it only sets the direction and is not recorded as a swing.
        if hi_conf:
            if hi_i > 0:
                swings.append(_mk(swings, "HIGH", bars, hi_i, i))
            direction = -1
            lo_i = max(range(hi_i + 1, i + 1), key=lambda k: -bars[k].low)
        elif lo_conf:
            if lo_i > 0:
                swings.append(_mk(swings, "LOW", bars, lo_i, i))
            direction = 1
            hi_i = max(range(lo_i + 1, i + 1), key=lambda k: bars[k].high)
    return swings


def _mk(prev: list[Swing], kind: str, bars: list[Bar], idx: int, conf: int) -> Swing:
    price = bars[idx].high if kind == "HIGH" else bars[idx].low
    last_same = next((s for s in reversed(prev) if s.kind == kind), None)
    label = None
    if last_same is not None:
        if kind == "HIGH":
            label = "HH" if price > last_same.price else "LH"
        else:
            label = "HL" if price > last_same.price else "LL"
    return Swing(kind, price, idx, bars[idx].time, conf, bars[conf].time, label)


def _trend_from_labels(swings: list[Swing], upto_index: int) -> str | None:
    known = [s for s in swings if s.confirmed_index <= upto_index and s.label]
    hi = next((s.label for s in reversed(known) if s.kind == "HIGH"), None)
    lo = next((s.label for s in reversed(known) if s.kind == "LOW"), None)
    if hi == "HH" and lo == "HL":
        return "UP"
    if hi == "LH" and lo == "LL":
        return "DOWN"
    return None


def detect_bos(bars: list[Bar], swings: list[Swing]) -> list[BosEvent]:
    """One BOS per swing: the first close beyond a swing that was already confirmed before that bar."""
    events: list[BosEvent] = []
    broken: set[tuple[str, int]] = set()
    for i, b in enumerate(bars):
        known = [s for s in swings if s.confirmed_index < i]
        last_hi = next((s for s in reversed(known) if s.kind == "HIGH"), None)
        last_lo = next((s for s in reversed(known) if s.kind == "LOW"), None)
        trend = _trend_from_labels(swings, i - 1)
        if last_hi and ("HIGH", last_hi.index) not in broken and b.close > last_hi.price:
            broken.add(("HIGH", last_hi.index))
            events.append(BosEvent("UP", i, b.time, b.close, last_hi, trend == "DOWN"))
        if last_lo and ("LOW", last_lo.index) not in broken and b.close < last_lo.price:
            broken.add(("LOW", last_lo.index))
            events.append(BosEvent("DOWN", i, b.time, b.close, last_lo, trend == "UP"))
    return events


def classify_regime(bars: list[Bar], swings: list[Swing], events: list[BosEvent],
                    p: StructureParams = StructureParams()) -> Regime:
    highs = [s for s in swings if s.kind == "HIGH" and s.label]
    lows = [s for s in swings if s.kind == "LOW" and s.label]
    if not highs or not lows:
        return Regime("UNKNOWN", ["fewer than two confirmed swing highs and lows in loaded history"])
    hi, lo = highs[-1], lows[-1]
    ev = [f"last swing high {hi.label} @ {hi.price}", f"last swing low {lo.label} @ {lo.price}"]
    last = events[-1] if events else None
    if last:
        ev.append(f"last BOS {last.direction} at close {last.close} (broke swing {last.swing.kind} @ {last.swing.price})"
                  + (" -- against prior structure" if last.shift else ""))
    if last is None or len(bars) - 1 - last.index > p.stale_bars:
        ev.append(f"no BOS in the last {p.stale_bars} bars")
        return Regime("NEUTRAL", ev)
    if last.shift:
        # A shift stays a TRANSITION until a swing in the new direction confirms it after the break:
        # a higher low after an UP shift, a lower high after a DOWN shift.
        want_kind, want_label = ("LOW", "HL") if last.direction == "UP" else ("HIGH", "LH")
        confirmed = any(s.kind == want_kind and s.label == want_label and s.confirmed_index > last.index
                        for s in swings)
        if not confirmed:
            ev.append(f"no {want_label} confirmed since the shift yet")
            return Regime("TRANSITION", ev)
        ev.append(f"shift confirmed by a {want_label} after the break")
    if hi.label == "HH" and lo.label == "HL" and last.direction == "UP":
        return Regime("BULLISH", ev)
    if hi.label == "LH" and lo.label == "LL" and last.direction == "DOWN":
        return Regime("BEARISH", ev)
    ev.append("swing labels and last BOS do not agree on a direction")
    return Regime("RANGE", ev)
