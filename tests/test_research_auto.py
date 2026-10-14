import json
import sqlite3
from datetime import date

from copilot.alert_tracking.outcomes import SCHEMA as OUT_SCHEMA, record_decision
from copilot.research.auto import FACTORS, _variant_of, ensure_schema, generate, pick_batch, variant_from
from copilot.research.backtest import MIN_TRADES, SimTrade, walk_forward
from copilot.research.shadow import evaluate_forward, net_r
from copilot.setup_engine.poc_fvg import PRODUCTION, RuleSet

NOW = 1791300000


def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def test_one_factor_variants_cover_every_level_and_never_touch_production():
    c = db()
    n = generate(c, NOW)
    assert n == sum(len(v) for v in FACTORS.values())
    assert generate(c, NOW) == 0                                   # idempotent
    v = _variant_of(c.execute("SELECT * FROM hypotheses WHERE id = 'G:target=structural'").fetchone())
    assert v.rules.target_mode == "structural" and v.rules.use_volume_support
    v = _variant_of(c.execute("SELECT * FROM hypotheses WHERE id = 'G:session=NY AM only'").fetchone())
    assert v.rules.sessions == ("NY_AM",)
    assert PRODUCTION == RuleSet()                                 # production object is untouched


def test_pairs_only_from_levels_that_beat_production_in_sample():
    c = db()
    generate(c, NOW)
    c.execute("UPDATE hypotheses SET is_delta = 0.2 WHERE id IN ('G:target=3R', 'G:confirm=rejection only')")
    c.execute("UPDATE hypotheses SET is_delta = -0.1 WHERE id = 'G:filters=no volume filter'")
    assert generate(c, NOW) == 0                     # both parents short of MIN_TRADES -> cannot reach it either
    c.execute("UPDATE hypotheses SET results = ? WHERE id = 'G:target=3R'",
              (json.dumps({"out_sample": {"n": MIN_TRADES}}),))
    assert generate(c, NOW) == 1
    pair = c.execute("SELECT id FROM hypotheses WHERE id LIKE '% + %'").fetchone()[0]
    assert "target=3R" in pair and "confirm=rejection only" in pair
    v = variant_from([("target", "3R", {"target_r": 3.0}), ("exit", "break-even at 1R", {"breakeven_at_r": 1.0})], "x")
    assert v.rules.target_r == 3.0 and v.breakeven_at_r == 1.0


def test_batch_prefers_untested_and_waits_for_new_data():
    c = db()
    generate(c, NOW)
    c.execute("UPDATE hypotheses SET last_tested_at = ?, tested_days = 20", (NOW,))
    c.execute("UPDATE hypotheses SET last_tested_at = NULL WHERE id = 'G:target=3R'")
    assert [r["id"] for r in pick_batch(c, 22, 5)] == ["G:target=3R"]       # others: only 2 new days
    assert len(pick_batch(c, 25, 50)) == sum(len(v) for v in FACTORS.values())


def _trades(day, rs):
    return [SimTrade(day, i, "LONG", 100, 99, 102, "WIN" if r > 0 else "LOSS", r) for i, r in enumerate(rs)]


def test_better_than_a_losing_baseline_is_not_viable():
    days = [date(2026, 9, 1 + d) for d in range(30)]
    k = MIN_TRADES // 5 + 1
    res = {"production": [t for d in days for t in _trades(d, [-1, -1] * k)],
           "slightly_less_bad": [t for d in days for t in _trades(d, [-1, 0.5] * k)],
           "good": [t for d in days for t in _trades(d, [-1, 2] * k)]}
    v = {x.variant: x for x in walk_forward(res, days, min_expectancy=0.10, p_required=0.95)}
    assert v["slightly_less_bad"].verdict == "INCONCLUSIVE" and v["good"].verdict == "CANDIDATE"


def test_forward_test_promotes_or_rejects_but_never_changes_production():
    c = db()
    c.executescript(OUT_SCHEMA)
    generate(c, NOW)
    for hid in ("G:target=3R", "G:target=1.5R"):
        c.execute("UPDATE hypotheses SET status='SHADOW', shadow_since=? WHERE id=?", (NOW, hid))
    for i in range(MIN_TRADES):
        for hid, r in (("G:target=3R", 3.0 if i % 2 else -1.0), ("G:target=1.5R", -1.0)):
            record_decision(c, f"k{i}", f"shadow:{hid}", "COMEX_MINI:MGC1!", "LONG", "QUALIFIED_UNGATED", None,
                            NOW + i, 100.0, 99.0, 103.0, 3.0, {}, [])
            c.execute("UPDATE alert_outcomes SET outcome=?, r=? WHERE key=? AND source=?",
                      ("WIN" if r > 0 else "LOSS", r, f"k{i}", f"shadow:{hid}"))
    out = {h: s for h, s, _ in evaluate_forward(c, lambda *a: [], NOW + 10**6, 1.34, 10.0)}
    assert out == {"G:target=3R": "PROPOSED", "G:target=1.5R": "REJECTED_FORWARD"}
    assert PRODUCTION == RuleSet()


def test_net_r_subtracts_commission():
    row = {"direction": "LONG", "entry": 100.0, "stop": 99.0, "target": 102.0, "decided_at": 0, "symbol": "X", "r": 2.0}
    assert net_r(row, lambda *a: [], 0, None, 1.34, 10.0) == 2.0 - 1.34 / 10.0


def test_hypothesis_labels_are_readable_chinese():
    from copilot.research.auto import label
    assert label("G:target=3R + exit=break-even at 1R") == "目標：3R＋出場：到 1R 移保本"
    assert label("G:filters=no volume filter") == "過濾：拿掉成交量過濾"
    assert label("H5") == "H5 只做轉折"


def test_triples_from_a_winning_pair_plus_another_winner():
    c = db()
    generate(c, NOW)
    c.execute("UPDATE hypotheses SET is_delta = 0.2, results = ? WHERE id IN ('G:target=1.75R', 'G:confirm=wick 33%', "
              "'G:fvg=min 0.3 ATR')", (json.dumps({"out_sample": {"n": 40}}),))
    generate(c, NOW)
    c.execute("UPDATE hypotheses SET is_delta = 0.3 WHERE id = 'G:confirm=wick 33% + target=1.75R'"
              " OR id = 'G:target=1.75R + confirm=wick 33%'")
    generate(c, NOW)
    triples = [r[0] for r in c.execute("SELECT id FROM hypotheses WHERE id LIKE '% + % + %'")]
    assert len(triples) == 1 and "fvg=min 0.3 ATR" in triples[0]
    v = _variant_of(c.execute("SELECT * FROM hypotheses WHERE id = ?", (triples[0],)).fetchone())
    assert (v.rules.target_r, v.rules.rejection_wick_frac, v.rules.min_fvg_atr) == (1.75, 0.33, 0.3)


def test_research_digest_lists_best_first():
    import time
    from copilot.monitor import research_digest
    c = db()
    generate(c, NOW)
    for hid, exp in (("G:target=1.75R", 0.12), ("G:target=3R", -0.5)):
        c.execute("UPDATE hypotheses SET last_tested_at=?, verdict='INSUFFICIENT_DATA', results=? WHERE id=?",
                  (NOW, json.dumps({"out_sample": {"n": 20, "expectancy": exp}}), hid))
    txt = research_digest(c, NOW - 10)
    assert "測了 2 個假設" in txt and txt.index("目標：1.75R") < txt.index("目標：3R") and "樣本不足" in txt


def test_full_report_when_everything_is_tested():
    from copilot.research.auto import full_report
    c = db()
    generate(c, NOW)
    c.executescript("CREATE TABLE IF NOT EXISTS research_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);")
    c.execute("INSERT INTO research_state VALUES ('baseline', ?)", (json.dumps(
        {"days": 20, "in_n": 50, "in_exp": -0.18, "out_n": 20, "out_exp": -0.07, "at": NOW}),))
    for hid, i, o, v in (("G:target=1.75R", 0.15, 0.12, "INSUFFICIENT_DATA"), ("G:target=3R", -0.3, -0.5, "REJECTED")):
        c.execute("UPDATE hypotheses SET last_tested_at=?, verdict=?, results=? WHERE id=?",
                  (NOW, v, json.dumps({"in_sample": {"n": 60, "expectancy": i}, "out_sample": {"n": 40, "expectancy": o},
                              "reasons": ["drawdown worse than production"]}), hid))
    c.execute("UPDATE hypotheses SET last_tested_at=?, verdict='INSUFFICIENT_DATA', results=? WHERE id='G:target=2.5R'",
              (NOW, json.dumps({"in_sample": {"n": 3, "expectancy": 2.0}, "out_sample": {"n": 1, "expectancy": 2.5}}),))
    txt = full_report(c)
    assert "足夠（≥30 筆）的 2 個" in txt and "目標：2.5R" not in txt.split("足夠")[1].split("樣本內、")[0]
    assert "最大回撤比目前策略大" in txt and "其中 1 個樣本外不到 30 筆" in txt
    assert "共測了 3 個假設" in txt and "目前策略：樣本內 50 筆 -0.18R" in txt
    assert txt.index("目標：1.75R") < txt.index("目標：3R") and "都 > +0.1R 的：2 個" in txt


def test_run_cycle_pools_instruments_and_counts_instrument_days(monkeypatch):
    import copilot.research.auto as auto
    from copilot.research.auto import Market, run_cycle
    seen = []

    def fake_sim(conn, root, tick, min_rr, days, variants, now, commission):
        seen.append((root, tick, len(days), commission))
        return {v.name: [SimTrade(d, 0, "LONG", 1, 0, 2, "WIN", 1.0) for d in days] for v in variants}
    monkeypatch.setattr(auto, "simulate", fake_sim)
    c = db()
    d = [date(2026, 9, k) for k in range(14, 19)]
    s = run_cycle(c, [Market("MGC", 0.1, tuple(d), 1.34), Market("MNQ", 0.25, tuple(d[2:]), 1.34)], 1.5, NOW, 3)
    assert seen == [("MGC", 0.1, 5, 1.34), ("MNQ", 0.25, 3, 1.34)]
    assert s["days"] == 8 and s["markets"] == "MGC 5 天＋MNQ 3 天"
    assert s["baseline"].in_sample.n + s["baseline"].out_sample.n == 8          # trades from both instruments
    assert {r["tested_days"] for r in c.execute("SELECT tested_days FROM hypotheses WHERE last_tested_at")} == {8}


def test_sample_milestone_sent_once_when_production_reaches_min_trades():
    from types import SimpleNamespace as NS
    from copilot.research.auto import sample_milestone
    c = db()
    s = lambda n: {"days": 29, "markets": "MGC 20 天＋MNQ 9 天",
                   "baseline": NS(out_sample=NS(n=n, expectancy=0.12))}
    assert sample_milestone(c, s(26)) is None
    msg = sample_milestone(c, s(31))
    assert "樣本累積夠了" in msg and "MGC 20 天＋MNQ 9 天" in msg and "31 筆" in msg
    assert sample_milestone(c, s(35)) is None


def test_forward_replay_only_uses_days_after_selection_and_compares_same_days(monkeypatch):
    import copilot.research.shadow as sh
    from copilot.research.auto import Market
    c = db()
    generate(c, NOW)
    since = int(__import__("datetime").datetime(2026, 10, 12, 6, 0, tzinfo=__import__("copilot.session_engine.sessions",
                                                                                    fromlist=["ET"]).ET).timestamp())
    c.execute("UPDATE hypotheses SET status='SHADOW', shadow_since=? WHERE id='G:target=3R'", (since,))
    calls = []

    def fake_sim(conn, root, tick, min_rr, days, variants, now, commission):
        calls.append((root, days[0], [v.name for v in variants]))
        return {v.name: [SimTrade(days[0], 0, "LONG", 1, 0, 2, "WIN", 1.0 if v.name != "production" else -1.0)] * 8
                for v in variants}
    monkeypatch.setattr("copilot.research.backtest.simulate", fake_sim)
    ds = tuple(date(2026, 10, d) for d in (9, 12, 13))
    assert sh.forward_replay(c, [Market("MGC", 0.1, ds, 1.34), Market("MNQ", 0.25, ds[1:], 1.34)], 1.5, NOW) == 8
    assert [(r, str(d)) for r, d, _ in calls] == [("MGC", "2026-10-12"), ("MGC", "2026-10-13"),
                                                   ("MNQ", "2026-10-12"), ("MNQ", "2026-10-13")]   # 10-09: before
    assert sh.forward_replay(c, [Market("MGC", 0.1, ds, 1.34)], 1.5, NOW) == 0          # nothing recomputed
    assert sh.evaluate_forward_replay(c) == []                    # 32 trades but only 2 days: not yet
    monkeypatch.setattr(sh, "FORWARD_MIN_DAYS", 2)
    out = sh.evaluate_forward_replay(c)
    assert out[0][1] == "PROPOSED" and "32 筆" in out[0][2] and "MGC 16 筆" in out[0][2] and "-1.00R" in out[0][2]
