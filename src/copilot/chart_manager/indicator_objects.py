"""What the user's indicators already draw, so the co-pilot does not draw it again .

Read from the live chart (Pine graphics of VISIBLE studies), per study, by role:
  SMC Structures and FVG : boxes = FVG zones; horizontal lines = structure (BOS/CHoCH + current structure),
                           excluding its Fibonacci lines (identified by the prices printed in its fib labels).
Other studies (e.g. the PoC profile, whose boxes are volume rows, not levels) are not used for de-duplication.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

ROLES = {"SMC Structures and FVG": {"boxes": "fvg", "lines": "structure"}}

READ_JS = """
(function(pane, names) {
  var w = window.TradingViewApi._chartWidgetCollection.getAll()[pane], out = {};
  w.model().model().dataSources().forEach(function(s) {
    if (!s.metaInfo) return;
    var n = s.metaInfo().description;
    if (names.indexOf(n) < 0) return;
    try { if (!s.properties().childs().visible.value()) return; } catch (e) {}
    var pc = s._graphics && s._graphics._primitivesCollection;
    function coll(o, k) { try { var c = pc[o].get(k).get(false), a = []; c._primitivesDataById.forEach(function(v){ a.push(v); }); return a; } catch (e) { return []; } }
    out[n] = {
      boxes: coll('dwgboxes', 'boxes').map(function(b){ return [b.y1, b.y2]; }),
      lines: coll('dwglines', 'lines').filter(function(l){ return l.y1 === l.y2; }).map(function(l){ return l.y1; }),
      labels: coll('dwglabels', 'labels').map(function(l){ return [l.t || '', l.y]; })
    };
  });
  return out;
})(%d, %s)
"""

_FIB = re.compile(r"^0?\.\d+\(([\d.]+)\)$")


@dataclass
class Occupied:
    zones: list[tuple[float, float]] = field(default_factory=list)   # (low, high) of indicator FVG boxes
    lines: list[float] = field(default_factory=list)                 # indicator structure line prices
    sources: list[str] = field(default_factory=list)


def parse(raw: dict) -> Occupied:
    occ = Occupied()
    for name, objs in (raw or {}).items():
        roles = ROLES.get(name)
        if not roles:
            continue
        occ.sources.append(name)
        fib_prices = set()
        for text, y in objs.get("labels", []):
            m = _FIB.match(text or "")
            if m:
                fib_prices.add(round(float(y), 4))
        if roles.get("boxes") == "fvg":
            occ.zones += [(min(a, b), max(a, b)) for a, b in objs.get("boxes", [])]
        if roles.get("lines") == "structure":
            occ.lines += [y for y in objs.get("lines", []) if round(float(y), 4) not in fib_prices]
    return occ


def read(evaluate: Callable[[str], dict], pane_index: int) -> Occupied:
    import json
    return parse(evaluate(READ_JS % (int(pane_index), json.dumps(list(ROLES)))))
