import sqlite3
from types import SimpleNamespace

from copilot.chart_manager.drawings import ChartManager
from copilot.chart_manager.setup_drawings import cleanup, draw_setup
from copilot.fvg.fvg import FVG
from copilot.setup_engine.poc_fvg import evaluate_all, zone_key
from copilot.setup_engine.tracker import SetupTracker

from test_chart_manager import FakeChart
from test_setup_engine import T5, TICK, a5, approach_and_reject, b, profile

PASS_GATE = lambda ev: SimpleNamespace(passed=True, rules=[], failures=[], version="risk-test")
FAIL_GATE = lambda ev: SimpleNamespace(passed=False, rules=[SimpleNamespace(rule="data quality", status="FAIL",
                                                                              detail="delayed")],
                                       failures=[1], version="risk-test")


def tracker():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return SetupTracker(conn, "MGC"), conn


def evals(bars, analysis=None):
    return evaluate_all(analysis or a5(), profile(), bars, TICK, 1.5)


def kinds(ns):
    return [n.kind for n in ns]


# touches the 4209-4213 FVG with a small GREEN bar: nothing failed yet, no confirmation yet -> potential
TOUCH = b(1, 4212.7, 4212.95, 4212.6, 4212.9)


def test_watch_potential_trade_setup_each_once():
    tr, _ = tracker()
    seq = approach_and_reject()
    assert kinds(tr.update(evals(seq[:1]), 1, PASS_GATE)) == []                 # WATCHING: quiet
    potential = [seq[0], TOUCH]                                                 # touched, no confirmation yet
    assert kinds(tr.update(evals(potential), 2, PASS_GATE)) == ["POTENTIAL"]
    assert kinds(tr.update(evals(potential), 3, PASS_GATE)) == []               # no repeat
    qualified = potential + [b(2, 4213.6, 4216, 4210.5, 4214.5, v=300)]      # lower wick 56% of range
    assert kinds(tr.update(evals(qualified), 4, PASS_GATE)) == ["TRADE_SETUP"]
    assert kinds(tr.update(evals(qualified), 5, PASS_GATE)) == []               # frozen terminal state


def test_zone_with_a_failed_condition_is_not_potential():
    tr, _ = tracker()
    red_touch = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4212.6, 4213.2)]   # counter volume > with
    assert kinds(tr.update(evals(red_touch), 1, PASS_GATE)) == []


def test_risk_failure_is_reported_not_sent_as_trade():
    tr, conn = tracker()
    ns = tr.update(evals(approach_and_reject()), 1, FAIL_GATE)
    assert kinds(ns) == ["RISK_BLOCKED"]
    row = conn.execute("SELECT risk_passed, risk_detail FROM setups").fetchone()
    assert row["risk_passed"] == 0 and "delayed" in row["risk_detail"]


def test_invalidation_only_notified_if_user_was_told():
    tr, _ = tracker()
    bars = [b(0, 4220, 4221, 4218, 4219), TOUCH]
    assert kinds(tr.update(evals(bars), 1, PASS_GATE)) == ["POTENTIAL"]
    dead = bars + [b(2, 4212.9, 4213.0, 4207, 4207.5)]                         # close below the FVG
    assert kinds(tr.update(evals(dead), 2, PASS_GATE)) == ["INVALIDATED"]
    tr2, _ = tracker()                                                          # never alerted -> silent
    assert kinds(tr2.update(evals([b(0, 4214, 4214.5, 4207, 4207.5)]), 1, PASS_GATE)) == []


def test_vanished_zone_expires():
    tr, conn = tracker()
    bars = [b(0, 4220, 4221, 4218, 4219), TOUCH]
    tr.update(evals(bars), 1, PASS_GATE)
    ns = tr.update([], 2, PASS_GATE)                                            # 5m FVG gone
    assert kinds(ns) == ["INVALIDATED"]
    assert conn.execute("SELECT state FROM setups").fetchone()["state"] == "EXPIRED"


def test_two_zones_keep_separate_state():
    fvgs = [FVG("BULL", 4213.0, 4209.0, 10, T5, 4.0, 0.8, 1.2, "OPEN"),
            FVG("BULL", 4212.5, 4210.5, 11, T5 + 300, 2.0, 0.4, 1.0, "OPEN")]
    tr, conn = tracker()
    bars = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4212.6, 4213.2)]
    for t in range(1, 6):                                                        # repeated updates: no spam
        ns = tr.update(evals(bars, a5(fvgs=fvgs)), t, PASS_GATE)
        assert len(ns) <= 2 and (t == 1 or ns == [])
    assert conn.execute("SELECT COUNT(*) FROM setups").fetchone()[0] == 2


def test_setup_drawings_drawn_and_expired():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    fake = FakeChart()
    cm = ChartManager(conn, fake.evaluate)
    ev = evals(approach_and_reject())[0]
    ids = draw_setup(cm, ev, zone_key(ev.zone), 1, "MGC", now=1000)
    assert len(ids) == 3 and all("chart(1)" in js for js in fake.calls)
    assert cleanup(cm, now=1001) == []                                           # not expired yet
    assert sorted(cleanup(cm, now=1000 + 2 * 3600)) == sorted(ids)
    assert "USER1" in fake.shapes
