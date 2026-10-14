from copilot.market_data.models import Bar
from copilot.market_data.series import atr
from copilot.volume.profile import fixed_segment_pocs

SEC = 300
SPAN = 10 * SEC
T0 = 1_790_000_000 // SPAN * SPAN        # aligned to a segment boundary


def bars(n, start=T0, skip=()):
    out = []
    for i in range(n):
        t = start + i * SEC
        if i in skip:
            continue
        c = 4200 + (i % 7) - (i % 3) * 0.5
        out.append(Bar(t, c, c + 1, c - 1, c + 0.3, 100 + (i % 5) * 40, True))
    return out


def test_segments_are_clock_aligned_and_complete_only():
    b = bars(35)                                  # 3 full segments + 5 bars of a 4th
    res = fixed_segment_pocs(b, SEC, 0.1, atr(b), row_size=1.0)
    assert len(res) == 3
    assert all((t - T0) % SEC == 0 for t, _ in res)


def test_history_start_does_not_move_completed_segments():
    full = bars(60)
    late = full[13:]                              # history starts mid-segment
    a = {t: (n.low, n.high) for t, n in fixed_segment_pocs(full, SEC, 0.1, atr(full), row_size=1.0)}
    b = {t: (n.low, n.high) for t, n in fixed_segment_pocs(late, SEC, 0.1, atr(late), row_size=1.0)}
    common = set(a) & set(b)
    assert len(common) >= 4
    # segments fully inside both histories are identical (the first, partial one in `late` may differ)
    for t in sorted(common)[1:]:
        assert a[t] == b[t]


def test_segment_missing_its_closing_bar_is_skipped():
    b = bars(30, skip={19})                       # last bar of segment 2 missing (like the daily break)
    res = fixed_segment_pocs(b, SEC, 0.1, atr(b), row_size=1.0)
    assert len(res) == 2


def test_new_bars_only_append():
    b = bars(50)
    first = fixed_segment_pocs(b[:40], SEC, 0.1, atr(b[:40]), row_size=1.0)
    later = fixed_segment_pocs(b, SEC, 0.1, atr(b), row_size=1.0)
    assert later[:len(first)] == first


FIX = __import__("pathlib").Path(__file__).parent / "fixtures" / "mgc_5m_pocfix_2026-10-02.json"


def test_replica_matches_tradingview_indicator_on_real_data():
    import json
    import pytest
    if not FIX.exists():
        pytest.skip("real-data fixture not present")
    d = json.loads(FIX.read_text(encoding="utf-8"))
    b = [Bar(t, o, h, l, c, v, True) for t, o, h, l, c, v in d["bars"]]
    got = [round(n.mid, 4) for _, n in fixed_segment_pocs(b, SEC, d["tick"], atr(b), count=20)]
    assert got == [round(x, 4) for x in d["expected_segment_pocs"]]


def test_count_limit():
    b = bars(400)
    assert len(fixed_segment_pocs(b, SEC, 0.1, atr(b), count=20, row_size=1.0)) == 20
