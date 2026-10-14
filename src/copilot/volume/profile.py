"""Volume profile and PoC nodes, computed AS OF a bar (non-repainting), to replace the user's
PoC Migration Map [BackQuant] in the setup logic.

Two methods, both recorded:
  "close" : replica of the indicator -- each bar's whole volume goes to the bin of its close
            (indicator lines 74-82). Matches what the user sees as closely as possible.
  "range" : each bar's volume is spread evenly over the bins its high-low range covers (a real profile).
Parameters default to the user's indicator settings read from its chart legend:
lookback 300, bins 30, segment 10 bars, 20 segments. The indicator also counts the still-forming bar;
here only closed bars are used, so the newest segment can differ slightly while a bar is forming.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..market_data.models import Bar

HVN_MIN_PCT = 0.5     # UNCALIBRATED: a "prominent" node is a local max with >= 50% of the POC bin's volume
VOID_MAX_PCT = 0.25   # UNCALIBRATED: zone whose bins average < 25% of POC volume is a volume void


@dataclass(frozen=True)
class ProfileParams:
    lookback: int = 300
    bins: int = 30
    segment_bars: int = 10
    segment_count: int = 20
    method: str = "close"     # close | range


@dataclass
class Node:
    low: float
    high: float
    volume: float
    pct_of_poc: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


@dataclass
class Profile:
    method: str
    as_of_time: int
    lo: float
    hi: float
    bin_size: float
    volumes: list[float]
    poc: Node
    hvns: list[Node]                   # prominent nodes (local maxima), POC included
    segment_pocs: list[tuple[int, Node]] = field(default_factory=list)   # (segment mid-bar time, node), old->new
    # Same idea from the user's fixed indicator (clock-anchored, non-repainting). Filled by the caller via
    # fixed_segment_pocs(); recorded separately so the two definitions can be compared on real outcomes.
    fixed_segment_pocs: list[tuple[int, Node]] = field(default_factory=list)

    def node(self, i: int) -> Node:
        v = self.volumes[i]
        top = max(self.volumes) or 1.0
        return Node(self.lo + i * self.bin_size, self.lo + (i + 1) * self.bin_size, v, v / top)

    def zone_volume_pct(self, low: float, high: float) -> float | None:
        """Average bin volume over [low, high] as a fraction of the POC bin. None if outside the profile."""
        idx = [i for i in range(len(self.volumes))
               if self.lo + (i + 1) * self.bin_size > low and self.lo + i * self.bin_size < high]
        if not idx:
            return None
        top = max(self.volumes) or 1.0
        return sum(self.volumes[i] for i in idx) / len(idx) / top


def _bin(price: float, lo: float, size: float, n: int) -> int:
    return max(0, min(n - 1, int((price - lo) // size)))


def _accumulate(bars: list[Bar], lo: float, size: float, n: int, method: str) -> list[float]:
    vols = [0.0] * n
    for b in bars:
        if method == "close":
            vols[_bin(b.close, lo, size, n)] += b.volume
        else:
            a, z = _bin(b.low, lo, size, n), _bin(b.high, lo, size, n)
            share = b.volume / (z - a + 1)
            for i in range(a, z + 1):
                vols[i] += share
    return vols


def _round_to_tick(x: float, tick: float) -> float:
    return round(x / tick) * tick


def fixed_segment_pocs(closed: list[Bar], bar_seconds: int, tick: float, atr: list[float | None],
                       segment_bars: int = 10, count: int = 20, method: str = "close",
                       row_size: float = 0.0) -> list[tuple[int, Node]]:
    """Replica of "PoC Migration Map [BackQuant] - Fixed" (tradingview/pine/poc_migration_map_fixed.pine),
    Segment Anchoring = Fixed. Segments are clock-time windows of segment_bars * bar_seconds; a segment is
    computed only if one of its bars closes on the boundary (as the Pine `time_close` test), on a fixed price grid
    of row_size (0 = round_to_tick(ATR14 * 0.25) at that bar). Returns the newest `count` (mid-bar time, Node).
    `atr` must be aligned with `closed`. Small ATR differences vs. TradingView (different history start) can
    move a PoC by one row in rare cases.
    """
    span = segment_bars * bar_seconds
    out: list[tuple[int, Node]] = []
    for i, b in enumerate(closed):
        seg = b.time // span
        if (b.time + bar_seconds) // span <= seg:
            continue                      # this bar does not close the segment
        members = [x for x in closed[max(0, i - segment_bars + 1): i + 1] if x.time // span == seg]
        rs = row_size if row_size > 0 else (max(tick, _round_to_tick(atr[i] * 0.25, tick)) if atr[i] else None)
        if not rs or not members:
            continue
        # whole-tick integer grid, identical to the Pine version (no float row-edge ambiguity)
        t = lambda p: int(round(p / tick))
        rs_t = max(1, int(round(rs / tick)))
        lo_t = min(t(x.low) for x in members)
        hi_t = max(t(x.high) for x in members)
        base_t = (lo_t // rs_t) * rs_t
        rows = min((hi_t - base_t) // rs_t + 1, 500)
        vols = [0.0] * rows
        for x in members:
            if method == "close":
                vols[max(0, min(rows - 1, (t(x.close) - base_t) // rs_t))] += x.volume
            else:
                a = max(0, min(rows - 1, (t(x.low) - base_t) // rs_t))
                z = max(0, min(rows - 1, (t(x.high) - base_t) // rs_t))
                for k in range(a, z + 1):
                    vols[k] += x.volume / (z - a + 1)
        best = vols.index(max(vols))
        node = Node((base_t + best * rs_t) * tick, (base_t + (best + 1) * rs_t) * tick, vols[best], 1.0)
        out.append((members[len(members) - 1 - len(members) // 2].time, node))   # Pine: bar_index - int(n/2)
    return out[-count:]


def build_profile(closed: list[Bar], p: ProfileParams = ProfileParams()) -> Profile | None:
    """Profile of the last `lookback` closed bars. None (UNKNOWN) if there is not enough history or no range."""
    if len(closed) < p.lookback:
        return None
    window = closed[-p.lookback:]
    lo, hi = min(b.low for b in window), max(b.high for b in window)
    if hi <= lo:
        return None
    size = (hi - lo) / p.bins
    vols = _accumulate(window, lo, size, p.bins, p.method)
    top = max(vols)
    if top <= 0:
        return None
    prof = Profile(p.method, window[-1].time, lo, hi, size, vols, None, [])  # type: ignore[arg-type]
    prof.poc = prof.node(vols.index(top))
    prof.hvns = [prof.node(i) for i in range(p.bins)
                 if vols[i] >= HVN_MIN_PCT * top
                 and vols[i] >= (vols[i - 1] if i > 0 else 0) and vols[i] >= (vols[i + 1] if i < p.bins - 1 else 0)]
    # segment PoCs: segments counted back from the newest closed bar, bins on the SAME global grid (as indicator)
    segs = []
    for s in range(p.segment_count - 1, -1, -1):
        end = len(window) - s * p.segment_bars
        start = end - p.segment_bars
        if start < 0:
            continue
        seg = window[start:end]
        sv = _accumulate(seg, lo, size, p.bins, p.method)
        m = max(sv)
        if m <= 0:
            continue
        i = sv.index(m)
        node = Node(lo + i * size, lo + (i + 1) * size, m, vols[i] / top)
        segs.append((seg[len(seg) // 2].time, node))
    prof.segment_pocs = segs
    return prof
