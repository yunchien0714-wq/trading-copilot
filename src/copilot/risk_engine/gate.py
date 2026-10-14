"""Hard risk gate (spec section 12). Absolute priority: nothing -- AI confidence, history, user style -- overrides it.

Fail-closed: a rule whose inputs are missing or "UNKNOWN" FAILS. A TRADE SETUP alert is only allowed when every
rule PASSes; every failure is explained.

Inputs:
  config/account.toml        firm + personal limits (user-supplied, sourced)
  config/account_state.toml  balance / high-water mark / trades today, as of a date (user-supplied until the
                             Tradovate/journal phases can derive it); stale state FAILS
  instrument point value / tick from TradingView symbolInfo (never from memory)
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from ..event_engine.calendar import blackout
from ..session_engine.sessions import to_et

RISK_VERSION = "risk_engine-0.2.0"   # 0.2: size 1-10 from stop + loss cap (was min 5)
UNKNOWN = "UNKNOWN"


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _get(d: dict, dotted: str):
    cur = d
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return UNKNOWN
        cur = cur[k]
    return cur


def known(v) -> bool:
    return v is not None and v != UNKNOWN


@dataclass(frozen=True)
class Proposal:
    symbol: str             # e.g. COMEX_MINI:MGC1!
    direction: str          # LONG | SHORT
    entry: float
    stop: float
    target: float
    point_value: float | None   # $ per 1.0 price move per contract (TradingView symbolInfo.pointvalue)
    tick: float | None
    is_micro: bool | None       # from the instrument root (MGC/MNQ = micro); None = unknown
    time: int                   # unix s of the decision
    data_live: bool             # REALTIME data quality


@dataclass
class RuleResult:
    rule: str
    status: str             # PASS | FAIL | N/A
    detail: str


@dataclass
class GateResult:
    passed: bool
    rules: list[RuleResult] = field(default_factory=list)
    max_contracts: int | None = None
    risk_usd_at_max: float | None = None
    loss_cap_usd: float | None = None
    version: str = RISK_VERSION

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.rules if r.status == "FAIL"]


def size_for(stop_pts: float, point_value: float, commission: float, cap: float, lo: int, hi: int,
             firm_max: int) -> tuple[int, float] | None:
    """Largest contract count in [lo, min(hi, firm_max)] whose full-stop loss incl. round-trip fees fits the cap.
    RR does not change size ; it only has to clear min_rr. None = not even `lo` fits."""
    per_contract = stop_pts * point_value + commission
    fits = [n for n in range(lo, min(hi, firm_max) + 1) if n * per_contract <= cap]
    return (max(fits), max(fits) * per_contract) if fits else None


_NO_EVENTS = object()


def evaluate(p: Proposal, cfg: dict, state: dict, today: date | None = None, events=_NO_EVENTS) -> GateResult:
    """events: list[Event] from the economic calendar, or None if it could not be fetched (UNKNOWN).
    Omitted -> falls back to state["event_check"] (tests / manual override)."""
    R: list[RuleResult] = []

    def add(rule, ok, detail, na=False):
        R.append(RuleResult(rule, "N/A" if na else ("PASS" if ok else "FAIL"), detail))

    # 0. live data (spec: never alert on data we cannot trust in real time)
    add("data quality", p.data_live, "realtime data" if p.data_live else "data is DELAYED/STALE/UNKNOWN -> no live alerts")

    # 1. account state freshness
    as_of = state.get("as_of")
    today = today or to_et(p.time).date()
    fresh = isinstance(as_of, date) and as_of >= today
    add("account state current", fresh, f"account_state as_of {as_of}, need {today}")

    # 2. stop / target geometry
    sign = 1 if p.direction == "LONG" else -1
    stop_pts = (p.entry - p.stop) * sign
    tgt_pts = (p.target - p.entry) * sign
    tick_ok = known(p.tick) and p.tick
    add("stop placement", tick_ok and stop_pts >= p.tick,
        f"stop {stop_pts:.2f} pts on the {'correct' if stop_pts > 0 else 'WRONG'} side of entry"
        + ("" if tick_ok else " (tick size UNKNOWN)"))
    min_rr = _get(cfg, "personal.min_rr")
    rr = tgt_pts / stop_pts if stop_pts > 0 else None
    add("minimum RR", known(min_rr) and rr is not None and rr >= float(min_rr), f"RR {rr if rr is None else round(rr, 2)} vs min {min_rr}")

    # 3. instrument / contract unit
    unit = _get(cfg, "personal.contracts_unit")
    unit_ok = known(unit) and p.is_micro is not None and ((unit == "micro") == p.is_micro)
    add("contract unit", unit_ok, f"user trades {unit} contracts; {p.symbol} is {'micro' if p.is_micro else 'mini' if p.is_micro is False else 'UNKNOWN'}")

    # 4. drawdown buffer and per-trade loss cap (user: one trade <= 1/4 of the REMAINING buffer)
    max_dd = _get(cfg, "account.drawdown.max_drawdown_usd")
    dd_type = _get(cfg, "account.drawdown.type")
    frac = _get(cfg, "personal.max_loss_per_trade_frac_of_buffer")
    bal, peak = state.get("balance_usd", UNKNOWN), state.get("high_water_usd", UNKNOWN)
    cap = buffer = None
    if all(known(x) for x in (max_dd, dd_type, frac, bal, peak)) and dd_type == "trailing_intraday":
        threshold = float(peak) - float(max_dd)
        buffer = float(bal) - threshold
        cap = float(frac) * buffer
        add("drawdown buffer", buffer > 0, f"balance {bal} - trailing threshold {threshold} = buffer {buffer:.2f}")
    else:
        add("drawdown buffer", False, f"UNKNOWN inputs: max_dd={max_dd} type={dd_type} balance={bal} high_water={peak}")

    # 5. size: largest contract count within personal range, firm max, and the loss cap
    comm = _get(cfg, "costs.commission_roundtrip_per_contract_usd")
    lo, hi = _get(cfg, "personal.contracts_per_trade_min"), _get(cfg, "personal.contracts_per_trade_max")
    firm_max = _get(cfg, "account.contracts.max_contracts_micro" if p.is_micro else "account.contracts.max_contracts_mini")
    max_n = risk_at = None
    if all(known(x) for x in (comm, lo, hi, firm_max)) and known(p.point_value) and cap is not None and stop_pts > 0:
        per_contract = stop_pts * float(p.point_value) + float(comm)
        fit = size_for(stop_pts, float(p.point_value), float(comm), cap, int(lo), int(hi), int(firm_max))
        if fit:
            max_n, risk_at = fit
        add("per-trade loss cap", fit is not None,
            f"loss per contract ${per_contract:.2f} (stop {stop_pts:.2f} x ${p.point_value}/pt + ${comm} fees); "
            f"cap ${cap:.2f} = {frac} x buffer; "
            + (f"size {max_n} contracts -> ${risk_at:.2f}" if fit else f"even {lo} contract(s) exceeds the cap"))
    else:
        add("per-trade loss cap", False,
            f"UNKNOWN inputs: commission={comm} size={lo}-{hi} firm_max={firm_max} point_value={p.point_value}")

    # 6. trade count / open positions
    mx = _get(cfg, "personal.max_trades_per_day")
    done = state.get("trades_today", UNKNOWN)
    add("max trades per day", known(mx) and known(done) and int(done) < int(mx), f"{done} trades today, max {mx}")
    max_open = _get(cfg, "personal.max_open_positions")
    open_now = state.get("open_positions", UNKNOWN)
    add("open positions", known(max_open) and known(open_now) and int(open_now) < int(max_open),
        f"{open_now} open, max {max_open}")

    # 7. no overnight: no new entries after the user's last entry time (flat before the 17:00 ET close)
    last_entry = _get(cfg, "personal.last_entry_time_et")
    if known(last_entry):
        hh, mm = map(int, str(last_entry).split(":"))
        now_t = to_et(p.time).time()
        late = time(hh, mm) <= now_t < time(18, 0)
        add("no overnight risk", not late, f"now {now_t:%H:%M} ET, last entry {last_entry} ET")
    else:
        add("no overnight risk", False, "last entry time UNKNOWN")

    # 8. news: no entries inside the blackout around HIGH-importance events (calendar missing -> UNKNOWN -> FAIL)
    if events is _NO_EVENTS:
        ev = state.get("event_check")
        add("no high-impact news window", ev == "CLEAR",
            "event check CLEAR (manual)" if ev == "CLEAR" else f"event check: {ev or 'UNKNOWN (no event calendar)'}")
    else:
        b, a = _get(cfg, "personal.news_blackout_minutes_before"), _get(cfg, "personal.news_blackout_minutes_after")
        if not (known(b) and known(a)):
            add("no high-impact news window", False, f"blackout minutes UNKNOWN (before={b}, after={a})")
        else:
            status, e = blackout(events, p.time, int(b), int(a))
            detail = {"CLEAR": f"no high-importance event within -{b}/+{a} min",
                      "UNKNOWN": "economic calendar unavailable -> UNKNOWN"}.get(status)
            if status == "BLACKOUT":
                detail = f"{e.title} at {to_et(e.time):%H:%M} ET (blackout -{b}/+{a} min)"
            add("no high-impact news window", status == "CLEAR", detail)

    # 9. consistency rule: funded accounts only
    phase = _get(cfg, "account.phase")
    applies = _get(cfg, "account.other_rules.consistency_rule_applies_to")
    add("consistency rule", True, f"applies to {applies}, account is {phase}", na=(phase != applies))

    passed = all(r.status != "FAIL" for r in R)
    return GateResult(passed, R, max_n, risk_at, cap)


# Numeric settings the monitor, grader and research compute with. While any is missing / UNKNOWN the monitor does not
# start (it would otherwise fail on the first trade); the risk gate itself already fails closed on UNKNOWN.
REQUIRED_NUMBERS = (
    ("account", "size_usd"), ("account", "profit_target_usd"), ("account.drawdown", "max_drawdown_usd"),
    ("account.contracts", "max_contracts_mini"), ("account.contracts", "max_contracts_micro"),
    ("personal", "contracts_per_trade_min"), ("personal", "contracts_per_trade_max"),
    ("personal", "max_trades_per_day"), ("personal", "min_rr"), ("personal", "max_loss_per_trade_frac_of_buffer"),
    ("personal", "news_blackout_minutes_before"), ("personal", "news_blackout_minutes_after"),
    ("costs", "commission_roundtrip_per_contract_usd"),
)


def config_problems(cfg: dict) -> list[str]:
    """'[section] key' for every required number that is missing, UNKNOWN or not a number."""
    out = []
    for section, key in REQUIRED_NUMBERS:
        d = cfg
        for part in section.split("."):
            d = d.get(part, {}) if isinstance(d, dict) else {}
        v = d.get(key) if isinstance(d, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            out.append(f"[{section}] {key} = {v!r}")
    return out
