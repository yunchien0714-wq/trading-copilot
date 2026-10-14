from datetime import datetime
from zoneinfo import ZoneInfo

from copilot.event_engine.calendar import blackout, parse, reminders
from copilot.risk_engine.gate import evaluate

from test_risk_gate import CFG, STATE, prop

ET = ZoneInfo("America/New_York")
RAW = [  # shape of TradingView's calendar result (2026-10 sample)
    {"id": "1", "title": "Non Farm Payrolls", "country": "US", "importance": 1, "date": "2026-10-02T12:30:00.000Z"},
    {"id": "2", "title": "Fed Barkin Speech", "country": "US", "importance": 0, "date": "2026-10-02T15:00:00.000Z"},
    {"id": "3", "title": "bad date", "country": "US", "importance": 1, "date": "not-a-date"},
]


def t(h, m):
    return int(datetime(2026, 10, 2, h, m, tzinfo=ET).timestamp())


def test_parse_drops_unparseable_times():
    ev = parse(RAW)
    assert [e.title for e in ev] == ["Non Farm Payrolls", "Fed Barkin Speech"]
    assert ev[0].time == t(8, 30)


def test_blackout_window_edges():
    ev = parse(RAW)
    assert blackout(ev, t(8, 0), 30, 30)[0] == "BLACKOUT"
    assert blackout(ev, t(9, 0), 30, 30)[0] == "BLACKOUT"
    assert blackout(ev, t(7, 59), 30, 30)[0] == "CLEAR"
    assert blackout(ev, t(9, 1), 30, 30)[0] == "CLEAR"


def test_medium_importance_does_not_block_but_is_reminded():
    ev = parse(RAW)
    assert blackout(ev, t(11, 0), 30, 30)[0] == "CLEAR"          # Fed speech, importance 0
    rem = reminders(ev, t(10, 25), horizon_min=60)
    assert len(rem) == 1 and "Fed Barkin Speech" in rem[0] and "in 35 min" in rem[0]


def test_calendar_unavailable_is_unknown():
    assert blackout(None, t(8, 0), 30, 30)[0] == "UNKNOWN"


def test_gate_blocks_inside_blackout_and_when_calendar_missing():
    ev = parse(RAW)
    st = {k: v for k, v in STATE.items() if k != "event_check"}
    st["as_of"] = datetime(2026, 10, 2).date()
    inside = evaluate(prop(time=t(8, 40)), CFG | {"personal": CFG["personal"] | {
        "news_blackout_minutes_before": 30, "news_blackout_minutes_after": 30}}, st, events=ev)
    assert any(r.rule == "no high-impact news window" and r.status == "FAIL" and "Non Farm" in r.detail
               for r in inside.rules)
    missing = evaluate(prop(time=t(11, 0)), CFG | {"personal": CFG["personal"] | {
        "news_blackout_minutes_before": 30, "news_blackout_minutes_after": 30}}, st, events=None)
    assert "no high-impact news window" in {r.rule for r in missing.failures}
    clear = evaluate(prop(time=t(11, 0)), CFG | {"personal": CFG["personal"] | {
        "news_blackout_minutes_before": 30, "news_blackout_minutes_after": 30}}, st, events=ev)
    assert "no high-impact news window" not in {r.rule for r in clear.failures}
