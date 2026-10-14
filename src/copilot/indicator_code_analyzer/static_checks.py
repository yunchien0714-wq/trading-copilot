"""Automated Pine source scan for repaint / lookahead / data-proxy risks.

These are PATTERN HITS, not verdicts. Each hit points at line numbers so a human (or the manual
review in reviews.py) can confirm it. Evidence class is always "static_pattern".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ANALYZER_VERSION = "static_checks-0.1.0"


@dataclass(frozen=True)
class Hit:
    check: str
    severity: str        # HIGH = can make history look better than realtime; MEDIUM = misleading data; INFO
    lines: tuple[int, ...]
    detail: str
    evidence: str = "static_pattern"


def _lines(src: str, pattern: str, flags=0) -> list[int]:
    rx = re.compile(pattern, flags)
    return [i for i, line in enumerate(src.splitlines(), 1) if rx.search(line) and not line.lstrip().startswith("//")]


def _code_only(src: str) -> str:
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


def scan(src: str) -> list[Hit]:
    hits: list[Hit] = []
    code = _code_only(src)

    ln = _lines(src, r"lookahead\s*=\s*barmerge\.lookahead_on")
    if ln:
        hits.append(Hit("lookahead_on", "HIGH", tuple(ln),
                        "request.security with lookahead_on: historical bars can see higher-timeframe values "
                        "that were not known yet (future leak) unless the series is offset by [1]."))

    for m in re.finditer(r"ta\.pivot(high|low)\s*\(\s*(?:[^,()]+,\s*)?(\w+)\s*,\s*(\w+)\s*\)", code):
        right = m.group(3)
        ln = _lines(src, re.escape(m.group(0)))
        hits.append(Hit("pivot_confirmation_lag", "MEDIUM", tuple(ln),
                        f"pivot{m.group(1)} confirms only after {right} right-side bars; any drawing anchored at the "
                        f"pivot bar is back-dated (looks earlier on history than it was knowable live)."))

    ln = _lines(src, r"request\.security_lower_tf")
    if ln and re.search(r"close\s*[<>]\s*open\s*\?\s*volume", code):
        hits.append(Hit("updown_bar_volume_proxy", "MEDIUM", tuple(ln),
                        "Buy/sell volume = volume of lower-timeframe up-close vs down-close bars. This is NOT "
                        "bid/ask aggressor delta. If the lower timeframe equals the chart timeframe, each bar has "
                        "one sub-bar and the split degenerates to 0% / 100%."))

    ln = _lines(src, r"\(\s*close\s*-\s*low\s*\)\s*/\s*\(\s*high\s*-\s*low\s*\)")
    if ln and "volume" in code:
        hits.append(Hit("candle_shape_volume_split", "MEDIUM", tuple(ln),
                        "Buy/sell volume estimated from where the close sits in the bar range -- a candle-shape "
                        "proxy, not order flow."))

    islast = _lines(src, r"^\s*if\s+barstate\.islast\b")
    if islast and re.search(r"(high|low|close|volume)\s*\[\s*i\s*\]", code):
        hits.append(Hit("last_bar_recompute", "HIGH", tuple(islast),
                        "Main calculation runs only on the last bar and loops back over history. Output is a "
                        "rolling snapshot: there is no record of what it showed at past times, so it cannot be "
                        "backtested from chart history and it changes as the window rolls."))
        news = len(re.findall(r"\b(box|line|label)\.new\s*\(", code))
        deletes = len(re.findall(r"\.delete\s*\(|\b(box|line|label)\.delete\s*\(", code))
        if news and deletes == 0:
            hits.append(Hit("drawings_never_deleted", "MEDIUM", tuple(islast),
                            f"{news} drawing constructors and no delete calls: each recompute adds another full "
                            f"copy until TradingView's object cap evicts old ones (chart clutter, overlapping text)."))

    ln = _lines(src, r"\bvarip\b|\btimenow\b|calc_on_every_tick\s*=\s*true")
    if ln:
        hits.append(Hit("realtime_dependent_state", "HIGH", tuple(ln),
                        "Uses varip/timenow/calc_on_every_tick: realtime results can differ from historical ones."))

    ln = _lines(src, r"offset\s*=\s*-\s*\d")
    if ln:
        hits.append(Hit("negative_plot_offset", "HIGH", tuple(ln),
                        "Plot shifted left (negative offset): values are displayed on bars before they were computable."))

    return hits
