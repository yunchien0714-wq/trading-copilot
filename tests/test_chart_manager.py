import json
import re
import sqlite3

import pytest

from copilot.chart_manager.drawings import ChartManager, Drawing, build_create_js, build_remove_js
from copilot.market_structure.calibration import K_GRID, counts_by_threshold, swing_strengths

from test_phase3 import zigzag


class FakeChart:
    """Stands in for TradingView: tracks shapes, including one the user drew themselves."""
    def __init__(self):
        self.shapes = {"USER1": "short_position"}
        self.n = 0
        self.calls = []

    def evaluate(self, js):
        self.calls.append(js)
        assert "removeAllShapes" not in js
        if "createShape" in js or "createMultipointShape" in js:
            self.n += 1
            sid = f"AI{self.n}"
            self.shapes[sid] = "text"
            return sid
        if "removeEntity" in js:
            want = json.loads(re.search(r"var want = (\[.*?\])", js).group(1))
            removed = [w for w in want if w in self.shapes]
            for w in removed:
                del self.shapes[w]
            return {"removed": removed, "missing": [w for w in want if w not in removed]}
        raise AssertionError("unexpected js")


def drawing(env="PRODUCTION", **kw):
    base = dict(pane_index=0, symbol="COMEX_MINI_DL:MGC1!", environment=env, type="PDH", tier=2,
                creation_time=1000, source="test", timeframe="5", price_high=4222.8, price_low=4222.8,
                strength=None, status="ACTIVE", reason="previous day high", confidence="HIGH",
                expiration=None, validated=True, time1=900)
    base.update(kw)
    return Drawing(**base)


@pytest.fixture
def cm():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    fake = FakeChart()
    return ChartManager(conn, fake.evaluate), fake


def test_draw_records_all_spec_metadata(cm):
    m, fake = cm
    eid = m.draw(drawing())
    row = m.owned()[0]
    assert row["entity_id"] == eid
    for col in ("type", "creation_time", "source", "timeframe", "price_high", "price_low", "strength",
                "status", "reason", "confidence", "expiration", "validated"):
        assert col in row.keys()


@pytest.mark.parametrize("env", ["EXPERIMENTAL", "RESEARCH"])
def test_experimental_objects_never_drawn(cm, env):
    m, fake = cm
    with pytest.raises(ValueError):
        m.draw(drawing(env))
    assert fake.calls == []


def test_remove_only_touches_own_drawings(cm):
    m, fake = cm
    m.draw(drawing("CALIBRATION"))
    m.draw(drawing("CALIBRATION"))
    m.draw(drawing("PRODUCTION"))
    res = m.remove(m.owned(environment="CALIBRATION"), 2000, "done")
    assert sorted(res["removed"]) == ["AI1", "AI2"]
    assert "USER1" in fake.shapes and "AI3" in fake.shapes
    assert len(m.owned()) == 1


def test_drawing_deleted_by_user_is_marked_missing(cm):
    m, fake = cm
    m.draw(drawing())
    del fake.shapes["AI1"]
    res = m.remove(m.owned(), 2000, "cleanup")
    assert res["missing"] == ["AI1"] and m.owned() == []


def test_create_js_targets_the_right_pane_and_multipoint():
    js = build_create_js(drawing(pane_index=1, shape="rectangle", time2=1200, price_low=4200.0))
    assert "chart(1)" in js and "createMultipointShape" in js
    assert "chart(0)" in build_create_js(drawing())
    assert "removeAllShapes" not in build_remove_js(0, ["x"])


def test_swing_strengths_monotone_counts():
    strengths = swing_strengths(zigzag())
    counts = counts_by_threshold(strengths)
    vals = [counts[k] for k in K_GRID]
    assert vals == sorted(vals, reverse=True)
    assert all(k in K_GRID for _, k in strengths)
