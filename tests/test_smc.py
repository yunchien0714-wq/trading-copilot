import json
from pathlib import Path

import pytest

from copilot.market_data.models import Bar
from copilot.market_structure.smc import run

FIX = Path(__file__).parent / "fixtures" / "mgc_5m_smc_2026-10-02.json"


def bar(i, o, h, l, c):
    return Bar(1_790_000_000 + i * 300, o, h, l, c, 100, True)


def seq(points):
    """Bars walking through the given closes, 0.5 wicks."""
    out, prev = [], points[0]
    for i, c in enumerate(points):
        out.append(bar(i, prev, max(prev, c) + 0.5, min(prev, c) - 0.5, c))
        prev = c
    return out


def test_uptrend_then_choch_down():
    up = [10, 11, 12, 13, 14, 15, 14, 13, 14, 15, 16, 17, 18, 19, 20, 19, 18, 17, 18, 19, 20, 21, 22, 23]
    down = [22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12]
    st = run(seq([float(x) for x in up + down]))
    kinds = [(e.kind, e.direction) for e in st.events]
    assert ("CHOCH", "DOWN") in kinds
    assert st.direction == "BEARISH"


def test_structure_persists_without_breaks():
    up = [10, 11, 12, 13, 14, 15, 14, 13, 14, 15, 16, 17, 18, 19, 20]
    flat = [19.5, 19.8, 19.6, 19.7] * 20          # long sideways: no timeout, structure stays
    st = run(seq([float(x) for x in up + flat]))
    assert st.direction == "BULLISH"


def test_wick_through_protected_low_is_not_a_choch():
    up = [10, 11, 12, 13, 14, 15, 14, 13, 14, 15, 16, 17, 18, 19, 20]
    bars = seq([float(x) for x in up])
    st0 = run(bars)
    lo = st0.structure_low
    n = len(bars)
    bars.append(bar(n, 20, 20.2, lo - 3, 19.5))           # deep wick, close back above
    assert run(bars).direction == "BULLISH"


@pytest.mark.skipif(not FIX.exists(), reason="real-data fixture not present")
def test_replica_reproduces_indicator_on_real_data():
    data = json.loads(FIX.read_text(encoding="utf-8"))
    bars = [Bar(t, o, h, l, c, v, True) for t, o, h, l, c, v in data["bars"]]
    expected = [(k, round(y, 4)) for k, y in data["expected_last_events"]]
    st = run(bars)
    got = [(e.kind.replace("CHOCH", "CHoCH"), round(e.level, 4)) for e in st.events]
    # the fixture may hold one more closed bar than when the chart labels were read
    assert got[-len(expected):] == expected or got[-len(expected) - 1:-1] == expected
