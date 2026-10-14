"""Live smoke test against the running TradingView Desktop. Run with: pytest -m live"""
import pytest

from copilot.tradingview.cdp import CdpClient
from copilot.tradingview.chart import read_panes, read_snapshot


@pytest.mark.live
def test_live_reads_every_pane():
    with CdpClient() as cdp:
        panes = read_panes(cdp.evaluate, 200)
    assert panes and [p.pane_index for p in panes] == list(range(len(panes)))
    for p in panes:
        assert p.bars and p.symbol.full_name and p.resolution


@pytest.mark.live
def test_live_snapshot_reads_real_chart():
    with CdpClient() as cdp:
        snap = read_snapshot(cdp.evaluate, 500)
    assert snap.symbol.full_name
    assert snap.bars, "chart returned no bars"
    assert snap.bars[-1].is_closed is False
    times = [b.time for b in snap.bars]
    assert times == sorted(times) and len(set(times)) == len(times)
    for b in snap.bars:
        assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
