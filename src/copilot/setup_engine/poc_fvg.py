"""Setup "POC_FVG_REACTION" v0.1 -- the user's own description , turned into checkable rules.

User, in their words: enter when the PoC indicator lines up with an FVG or a liquidity sweep (FVG + structure
decide long/short); want to see price react (bounce / rejection) once it is in the POC or FVG; stop below the
FVG; target the next volume node on the right-side profile or the next resistance (opposing FVG, previous high,
sweep level), most often 1:2; skip when the signal is unclear, an opposing FVG is close, or volume does not
support.

Clarified answers :
  * POC = both the 10-bar segment PoC boxes and the profile's highest bin; POC and FVG come from 5m.
  * reaction = 1m rejection candle AND/OR 1m closes holding the zone -- recorded separately
    (hold-bar count to be calibrated from the user's trades; HOLD_BARS is a provisional value)
  * entry = market at the close of the confirming 1m candle
  * stop = beyond the reaction candle's wick (and never inside the FVG)
  * opposing 5m FVG between entry and the 2R target -> no trade
  * no volume support = price closed straight through the high-volume node, OR the zone is a volume void,
    OR counter-direction volume outweighs with-direction volume during the reaction
Long is described; short is the mirror image.
This module evaluates ONE moment from closed bars only -- the caller passes bars up to that moment.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from ..fvg.fvg import FVG
from ..market_data.models import Bar
from ..market_structure.analysis import TimeframeAnalysis
from ..session_engine.sessions import killzone
from ..volume.profile import VOID_MAX_PCT, Profile

SETUP_NAME = "POC_FVG_REACTION"
ENGINE_VERSION = "setup_engine-0.4.0"   # 0.4: FVG retest reversal + FVG-only zones (off in production; PLAN for grading)

HOLD_BARS = 3              # PROVISIONAL (user: "record first, calibrate later")
REJECTION_WICK_FRAC = 0.5  # UNCALIBRATED: rejection wick must be >= 50% of the 1m bar range
REVERSAL_LEVEL_TOL_ATR = 0.6   # swept level must lie in the zone +- this (user-calibrated zone tolerance)
WATCH_DIST_ATR = 1.5       # UNCALIBRATED: start WATCHING when price is within this * 5m ATR of the zone
EXPIRE_BARS = 15           # UNCALIBRATED: 1m bars after first touch without confirmation -> EXPIRED
STOP_BUFFER_TICKS = 1      # UNCALIBRATED: "outside the wick"
TARGET_R = 2.0             # user: "most of the time RR 1:2"

PASS, FAIL, PENDING, UNKNOWN = "PASS", "FAIL", "PENDING", "UNKNOWN"


@dataclass(frozen=True)
class RuleSet:
    """Setup filters as data, so research can test variants WITHOUT touching production (spec 24).
    The defaults ARE production; a variant only ever runs inside backtests."""
    name: str = "production"
    use_structure: bool = True
    use_volume_support: bool = True
    use_opposing_fvg: bool = True
    confirmations: tuple = ("REJECTION", "HOLD")
    hold_bars: int = HOLD_BARS
    target_r: float = TARGET_R
    allow_reversal: bool = True       # counter-structure = zone retrace + 1m rejection
    setup_types: tuple = ("TREND", "REVERSAL")   # research: H5 reversal only / H6 trend only
    target_mode: str = "fixed"        # fixed (target_r) | structural (nearest HVN / opposing FVG, research H8)
    sessions: tuple | None = None     # research: only confirmations inside these killzones (None = any time)
    retest_reversal: bool = False     # FVG retest reversal (the PLAN rule set; alerts only after research H9)
    reversal_fvg_only: bool = False   # reversals may use an FVG without POC (same)
    rejection_wick_frac: float = REJECTION_WICK_FRAC   # research: rejection wick as a fraction of the bar
    min_fvg_atr: float = 0.0          # research: ignore FVGs smaller than this many 5m ATR (0 = any size)
    require_poc: bool = True          # False: any 5m FVG is a zone (ICT-style), no volume-profile POC overlap needed
    max_fvg_atr: float = 0.0          # research : ignore FVGs LARGER than this many 5m ATR (0 = no cap)


def _strategy_overrides() -> tuple[dict, dict]:
    """[rules] and [plan] of config/strategy.toml (the trader's own strategy, written during onboarding -- see
    AGENTS.md). COPILOT_STRATEGY=builtin ignores the file (the test suite uses this). Unknown keys fail loudly."""
    import os
    import tomllib
    from dataclasses import fields
    if os.environ.get("COPILOT_STRATEGY") == "builtin":
        return {}, {}
    path = Path(os.environ.get("COPILOT_STRATEGY_FILE") or Path(__file__).resolve().parents[3] / "config" / "strategy.toml")
    if not path.exists():
        return {}, {}
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(RuleSet)}
    out = []
    for section in ("rules", "plan"):
        d = dict(cfg.get(section, {}))
        bad = set(d) - known
        if bad:
            raise ValueError(f"config/strategy.toml [{section}]: unknown setting(s) {sorted(bad)}")
        out.append({k: tuple(v) if isinstance(v, list) else v for k, v in d.items()})
    return out[0], out[1]


_RULES, _PLAN = _strategy_overrides()
PRODUCTION = RuleSet(**{**_RULES, "name": "production"})
# The user's own trading plan, used to GRADE their trades: production + the FVG retest reversal they defined on
# the plan. Live alerts keep using PRODUCTION until research (H9) passes and the user approves.
PLAN = (replace(PRODUCTION, **{**_PLAN, "name": "plan"}) if (_RULES or _PLAN)
        else RuleSet(name="plan", retest_reversal=True, reversal_fvg_only=True))


@dataclass
class Zone:
    direction: str            # LONG | SHORT
    fvg: FVG
    poc_sources: list[str]    # e.g. ["profile POC", "segment PoC 4212.1-4213.4"]
    nodes: list               # the POC Nodes in confluence with the FVG
    low: float
    high: float

    @property
    def near_edge(self) -> float:      # edge price reaches first
        return self.high if self.direction == "LONG" else self.low

    @property
    def far_edge(self) -> float:       # invalidation side
        return self.low if self.direction == "LONG" else self.high


@dataclass
class Evaluation:
    setup: str
    state: str                # NO_SETUP | WATCHING | POTENTIAL_SETUP | CONFIRMING | QUALIFIED | INVALIDATED | EXPIRED
    direction: str | None
    zone: Zone | None
    conditions: dict = field(default_factory=dict)   # name -> (status, evidence)
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    rr: float | None = None
    structural_targets: list = field(default_factory=list)   # (name, price, rr)
    invalidation: str | None = None
    confirmed_at: int | None = None     # open time of the 1m bar whose close confirmed (entry bar)
    confirmation: str | None = None     # REJECTION | HOLD
    notes: list[str] = field(default_factory=list)
    as_of: int | None = None
    setup_type: str = "TREND"           # TREND (with the 5m structure) | REVERSAL (against it)
    version: str = ENGINE_VERSION

    @property
    def satisfied(self) -> int:
        return sum(1 for s, _ in self.conditions.values() if s == PASS)


def _reversal_sweep(a5: TimeframeAnalysis, zone: "Zone", bars1: list[Bar], until: Bar | None):
    """First 1m bar (after the FVG formed, up to `until`) that traded through a known 5m liquidity level lying in
    the zone (+- tolerance) on the side being reversed, and closed back on the trade side of it. -> (level, bar)."""
    if until is None or not a5.atr:
        return None
    long = zone.direction == "LONG"
    side = "SSL" if long else "BSL"                  # a LONG reversal sweeps sell stops below, a SHORT buy stops above
    tol = REVERSAL_LEVEL_TOL_ATR * a5.atr
    levels = [l for l in a5.levels if l.side == side and zone.low - tol <= l.price <= zone.high + tol]
    for b in bars1:
        if b.time > until.time:
            break
        for l in levels:
            if b.time < l.known_time:
                continue
            if (b.low < l.price and b.close > l.price) if long else (b.high > l.price and b.close < l.price):
                return l, b
    return None


def _overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    return a_hi >= b_lo and b_hi >= a_lo


def _retest_rejection(zone: "Zone", live: list[Bar]) -> Bar | None:
    """First 1m bar that, AFTER price had already left the FVG (a close back outside on the trade side), entered it
    again and closed back outside -- the second test that 'still could not break through' ."""
    long = zone.direction == "LONG"
    outside = (lambda b: b.close > zone.high) if long else (lambda b: b.close < zone.low)
    entered = (lambda b: b.low <= zone.high) if long else (lambda b: b.high >= zone.low)
    left = False
    for b in live:
        if left and entered(b) and outside(b):
            return b
        if outside(b):
            left = True
    return None


def find_zones(a5: TimeframeAnalysis, prof: Profile, include_fvg_only: bool = False) -> list[Zone]:
    """Active 5m FVGs that overlap a POC (segment PoC or profile POC), direction = FVG direction.
    include_fvg_only: also FVGs without a POC (usable only as FVG reversals; )."""
    nodes = ([("profile POC", prof.poc)]
             + [(f"segment PoC {n.low:.1f}-{n.high:.1f}", n) for _, n in prof.segment_pocs]
             + [(f"fixed segment PoC {n.low:.1f}-{n.high:.1f}", n) for _, n in prof.fixed_segment_pocs])
    zones = []
    for g in a5.fvgs:
        if g.status in ("FILLED", "INVALIDATED", "ROLL_GAP"):
            continue
        hits = [(name, n) for name, n in nodes if _overlap(g.bottom, g.top, n.low, n.high)]
        if hits:
            zones.append(Zone("LONG" if g.direction == "BULL" else "SHORT", g, sorted({h[0] for h in hits}),
                              [h[1] for h in hits], g.bottom, g.top))
        elif include_fvg_only:
            zones.append(Zone("LONG" if g.direction == "BULL" else "SHORT", g, [], [], g.bottom, g.top))
    return zones


def evaluate(a5: TimeframeAnalysis, prof: Profile | None, bars1: list[Bar], tick: float,
             min_rr: float) -> Evaluation:
    closed1 = [b for b in bars1 if b.is_closed]
    as_of = closed1[-1].time if closed1 else None
    if prof is None or a5.atr is None or not closed1:
        return Evaluation(SETUP_NAME, "NO_SETUP", None, None, as_of=as_of,
                          notes=["UNKNOWN inputs: volume profile, 5m ATR or 1m bars unavailable"])
    price = closed1[-1].close
    zones = find_zones(a5, prof)
    if not zones:
        return Evaluation(SETUP_NAME, "NO_SETUP", None, None, as_of=as_of,
                          notes=["no active 5m FVG overlapping a POC"])

    zone = min(zones, key=lambda z: zone_distance(z, price))
    return evaluate_zone(a5, prof, closed1, zone, tick, min_rr)


def zone_distance(z: Zone, price: float) -> float:
    return 0.0 if z.low <= price <= z.high else min(abs(price - z.low), abs(price - z.high))


def zone_key(z: Zone) -> str:
    """Stable identity of a zone across evaluations (its 5m FVG)."""
    return f"{z.direction}:{z.fvg.time}:{z.low:.4f}:{z.high:.4f}"


def evaluate_all(a5: TimeframeAnalysis, prof: Profile | None, bars1: list[Bar], tick: float,
                 min_rr: float, rules: RuleSet = PRODUCTION) -> list[Evaluation]:
    """Every active zone evaluated on its own (used by the tracker, so zones never steal each other's state)."""
    closed1 = [b for b in bars1 if b.is_closed]
    if prof is None or a5.atr is None or not closed1:
        return []
    return [evaluate_zone(a5, prof, closed1, z, tick, min_rr, rules)
            for z in find_zones(a5, prof, rules.reversal_fvg_only or not rules.require_poc)]


def evaluate_zone(a5: TimeframeAnalysis, prof: Profile, closed1: list[Bar], zone: Zone, tick: float,
                  min_rr: float, rules: RuleSet = PRODUCTION) -> Evaluation:
    as_of = closed1[-1].time
    price = closed1[-1].close
    dist = lambda z: zone_distance(z, price)
    ev = Evaluation(SETUP_NAME, "NO_SETUP", zone.direction, zone, as_of=as_of)
    long = zone.direction == "LONG"
    c = ev.conditions

    # 1. structure agrees with the FVG direction. Structure = the SMC indicator's
    #    direction, which stays in force until a BOS/CHoCH changes it (no timeout).
    want = "BULLISH" if long else "BEARISH"
    smc_dir = a5.smc.direction if a5.smc else "UNKNOWN"
    if a5.smc is None or smc_dir == "UNDEFINED":
        c["5m structure"] = (UNKNOWN, f"5m SMC structure {smc_dir}")
    else:
        last = a5.smc.events[-1] if a5.smc.events else None
        c["5m structure"] = (PASS if smc_dir == want else FAIL,
                             f"5m SMC {smc_dir}, need {want}"
                             + (f" (last {last.kind} {last.direction} @ {last.level})" if last else ""))
    if not rules.use_structure and c["5m structure"][0] != PASS:
        c["5m structure"] = (PASS, f"ignored by rule set '{rules.name}' ({c['5m structure'][1]})")
    c["5m FVG"] = (PASS, f"{zone.fvg.direction} FVG {zone.low}-{zone.high} ({zone.fvg.status})")
    c["POC confluence"] = ((PASS, ", ".join(zone.poc_sources)) if zone.nodes
                           else (PASS, f"POC not required by rule set '{rules.name}'") if not rules.require_poc
                           else (FAIL, "FVG without a POC -- only valid as an FVG reversal"))

    # Where is price relative to the zone? Walk 1m bars formed after the FVG.
    after = [b for b in closed1 if b.time >= zone.fvg.time + 300]
    touch_i = next((i for i, b in enumerate(after) if (b.low <= zone.high if long else b.high >= zone.low)), None)
    if touch_i is None:
        c["price in zone"] = (PENDING, f"price {price}, zone near edge {zone.near_edge}")
        far = dist(zone)
        ev.state = ("WATCHING" if far <= WATCH_DIST_ATR * a5.atr and c["5m structure"][0] == PASS and zone.nodes
                    else "NO_SETUP")
        if ev.state == "NO_SETUP":
            ev.notes.append("zone too far or structure not aligned")
        return ev
    touch = after[touch_i]
    c["price in zone"] = (PASS, f"first touch at bar {touch.time}")
    since = after[touch_i:]

    # Time order decides: the first close beyond the far edge invalidates -- unless a confirmation came first.
    inv_i = next((i for i, b in enumerate(since)
                  if ((b.close < zone.far_edge) if long else (b.close > zone.far_edge))), None)
    live = since if inv_i is None else since[:inv_i]

    # 2. reaction on 1m: rejection candle and/or N closes holding (recorded separately)
    rej = None
    for b in live:
        rng = b.high - b.low
        if rng <= 0:
            continue
        wick = (min(b.open, b.close) - b.low) if long else (b.high - max(b.open, b.close))
        back_out = (b.close > zone.high) if long else (b.close < zone.low)
        pierced = (b.low <= zone.high) if long else (b.high >= zone.low)
        if pierced and back_out and wick >= rules.rejection_wick_frac * rng:
            rej = b
            break
    holds = len(live)
    hold_bar = live[rules.hold_bars - 1] if holds >= rules.hold_bars else None
    c["1m rejection candle"] = (PASS if rej else PENDING,
                                f"rejection at {rej.time} close {rej.close}" if rej else "none yet")
    c["1m closes hold"] = (PASS if hold_bar else PENDING,
                           f"{holds} 1m closes not beyond the zone since the touch (provisional need {HOLD_BARS})")
    candidates = [(b, kind) for b, kind in ((rej, "REJECTION"), (hold_bar, "HOLD"))
                  if b is not None and kind in rules.confirmations]
    # FVG retest reversal : against the structure, price touched the FVG, left it, came
    # back, and a 1m bar entered the FVG and CLOSED back outside it. Holding closes can never make a reversal, so
    # for a counter-structure zone they are not a confirmation when this rule is on.
    if rules.retest_reversal and rules.allow_reversal and c["5m structure"][0] == FAIL:
        retest = _retest_rejection(zone, live)
        candidates = [x for x in candidates if x[1] != "HOLD"] + ([(retest, "RETEST_REJECTION")] if retest else [])
    confirm, ev.confirmation = min(candidates, key=lambda t: t[0].time) if candidates else (None, None)
    if confirm is None and inv_i is not None:
        bad = since[inv_i]
        ev.state, ev.invalidation = "INVALIDATED", f"1m close {bad.close} beyond zone edge {zone.far_edge} before any confirmation"
        return ev
    upto = live[: live.index(confirm) + 1] if confirm else live

    # 1b. REVERSAL : against the 5m structure when price retraces INTO the zone (an FVG that formed
    #     against the structure, with POC) and the 1m reaction is a REJECTION candle -- a hold of closes is not
    #     enough. A liquidity sweep of a known 5m level in the zone is optional and recorded as the stronger variant.
    #     1m bars are used for the sweep so it is known when it happens, not at the 5m close.
    if c["5m structure"][0] == FAIL and rules.allow_reversal:
        sweep = _reversal_sweep(a5, zone, after, confirm or (live[-1] if live else None))
        if ev.confirmation in ("REJECTION", "RETEST_REJECTION"):
            ev.setup_type = "REVERSAL"
            if ev.confirmation == "RETEST_REJECTION":
                how = (f"price touched the {zone.fvg.direction} FVG, left it, came back and the 1m bar {confirm.time} "
                       f"entered it and closed back outside")
            else:
                how = (f"1m bar {sweep[1].time} swept {sweep[0].name} {sweep[0].price} and closed back inside"
                       if sweep else f"price retraced into the {zone.fvg.direction} FVG (no liquidity sweep)")
                how += ", then 1m rejection"
            c["5m structure"] = (PASS, f"REVERSAL against 5m {smc_dir}: {how}")
            if not zone.nodes:
                c["POC confluence"] = (PASS, "not required for an FVG reversal")
        else:
            c["5m structure"] = (FAIL, f"{c['5m structure'][1]}; reversal needs a 1m rejection candle in the zone")

    # 3. volume support (user's three "no support" cases), judged up to the confirming bar
    problems = []
    node_far = ((min(n.low for n in zone.nodes) if long else max(n.high for n in zone.nodes)) if zone.nodes
                else None)
    broke = next((b for b in upto if ((b.close < node_far) if long else (b.close > node_far))), None) \
        if node_far is not None else None
    if broke:
        problems.append(f"1m close {broke.close} straight through the POC node ({node_far:.1f})")
    vpct = prof.zone_volume_pct(zone.low, zone.high)
    if vpct is not None and vpct < VOID_MAX_PCT:
        problems.append(f"zone is a volume void ({vpct:.0%} of POC volume)")
    with_v = sum(b.volume for b in upto if ((b.close > b.open) if long else (b.close < b.open)))
    against_v = sum(b.volume for b in upto if ((b.close < b.open) if long else (b.close > b.open)))
    if against_v > with_v:
        problems.append(f"counter-direction volume {against_v:.0f} > with-direction {with_v:.0f}")
    if problems:
        c["volume support"] = (FAIL, "; ".join(problems))
    elif vpct is None:
        c["volume support"] = (UNKNOWN, "zone outside the volume profile window")
    else:
        c["volume support"] = (PASS, f"zone at {vpct:.0%} of POC volume; with {with_v:.0f} vs against {against_v:.0f}")
    if not rules.use_volume_support and c["volume support"][0] != PASS:
        c["volume support"] = (PASS, f"ignored by rule set '{rules.name}' ({c['volume support'][1]})")

    if confirm is None:
        ev.state = "POTENTIAL_SETUP" if holds <= 1 else "CONFIRMING"
        if holds > EXPIRE_BARS:
            ev.state, ev.invalidation = "EXPIRED", f"no confirmation within {EXPIRE_BARS} 1m bars of the touch"
        return ev
    if live.index(confirm) + 1 > EXPIRE_BARS:
        ev.state, ev.invalidation = "EXPIRED", f"no confirmation within {EXPIRE_BARS} 1m bars of the touch"
        return ev
    ev.confirmed_at = confirm.time

    # 4. prices: market entry at the confirming close; stop beyond the reaction wick and the FVG edge
    ev.entry = confirm.close
    if long:
        ev.stop = min(zone.low, min(b.low for b in upto)) - STOP_BUFFER_TICKS * tick
    else:
        ev.stop = max(zone.high, max(b.high for b in upto)) + STOP_BUFFER_TICKS * tick
    risk = abs(ev.entry - ev.stop)
    ev.target = ev.entry + rules.target_r * risk if long else ev.entry - rules.target_r * risk
    ev.rr = rules.target_r if risk > 0 else None
    target_label = f"{rules.target_r:g}R"

    # structural targets (information): next HVN, opposing FVG, unswept opposite liquidity
    def rr_to(p: float) -> float:
        return round(((p - ev.entry) if long else (ev.entry - p)) / risk, 2) if risk > 0 else 0.0
    beyond = (lambda p: p > ev.entry) if long else (lambda p: p < ev.entry)
    for n in prof.hvns:
        if beyond(n.mid):
            ev.structural_targets.append(("HVN", n.mid, rr_to(n.mid)))
    opp = [g for g in a5.fvgs if g.status not in ("FILLED", "INVALIDATED")
           and g.direction == ("BEAR" if long else "BULL")]
    for g in opp:
        edge = g.bottom if long else g.top
        if beyond(edge):
            ev.structural_targets.append(("opposing FVG", edge, rr_to(edge)))
    ev.structural_targets.sort(key=lambda t: abs(t[2]))

    # research variant (H8): target = the NEAREST structural level in the trade direction instead of a fixed R
    if rules.target_mode == "structural":
        ahead = [t for t in ev.structural_targets if t[2] > 0]
        if ahead:
            name, price, rr = ahead[0]
            ev.target, ev.rr, target_label = price, rr, f"{name} {price:.1f}"
        c["structural target"] = ((PASS, f"nearest {target_label} at {ev.rr}R") if ahead
                                  else (FAIL, "no structural level ahead"))

    # hard strategy filters
    lo, hi = sorted((ev.entry, ev.target))
    blocking = [g for g in opp if _overlap(g.bottom, g.top, lo, hi)
                and (g.bottom if long else g.top) != ev.target]       # the target FVG itself is not in the way
    c["no opposing FVG before 2R"] = (FAIL if blocking else PASS,
                                      f"blocked by {blocking[0].direction} FVG {blocking[0].bottom}-{blocking[0].top}"
                                      if blocking else "path to 2R clear of opposing 5m FVGs")
    if not rules.use_opposing_fvg and c["no opposing FVG before 2R"][0] != PASS:
        c["no opposing FVG before 2R"] = (PASS, f"ignored by rule set '{rules.name}'")
    c["RR >= minimum"] = (PASS if ev.rr and ev.rr >= min_rr else FAIL, f"RR {ev.rr} vs min {min_rr} ({target_label})")
    if rules.sessions is not None:                                     # research variants only
        kz = killzone(confirm.time) or "none"
        c["session"] = ((PASS if kz in rules.sessions else FAIL), f"confirmed in {kz}, allowed {', '.join(rules.sessions)}")
    if rules.min_fvg_atr > 0:                                          # research variants only
        sz = zone.fvg.size_atr
        c["FVG size"] = ((PASS if sz is not None and sz >= rules.min_fvg_atr else FAIL),
                         f"FVG {sz if sz is None else round(sz, 2)} ATR, need >= {rules.min_fvg_atr}")
    if rules.max_fvg_atr > 0:                                          # research variants only
        sz = zone.fvg.size_atr
        c["FVG max size"] = ((PASS if sz is not None and sz <= rules.max_fvg_atr else FAIL),
                             f"FVG {sz if sz is None else round(sz, 2)} ATR, need <= {rules.max_fvg_atr}")
    if ev.setup_type not in rules.setup_types:                         # research variants H5 / H6 only
        c["setup type"] = (FAIL, f"{ev.setup_type} setups excluded by rule set '{rules.name}'")

    failed = [k for k, (st, _) in c.items() if st in (FAIL, UNKNOWN)]
    if failed:
        # Confirmed, but a strategy filter failed: no trade. Prices are kept so the would-be outcome can be
        # tracked later (spec section 26: learn from setups that were filtered out too).
        ev.state, ev.invalidation = "INVALIDATED", "confirmed but failed: " + ", ".join(failed)
        return ev
    ev.state = "QUALIFIED"
    ev.invalidation = f"1m close {'below' if long else 'above'} {zone.far_edge}"
    return ev
