import pytest

from copilot.fvg.fvg import FVG
from copilot.market_data.models import Bar
from copilot.market_structure.analysis import TimeframeAnalysis
from copilot.market_structure.smc import SmcState
from copilot.market_structure.structure import Regime
from copilot.setup_engine.poc_fvg import evaluate, find_zones
from copilot.volume.profile import Node, Profile, ProfileParams, build_profile

T5 = 1_790_900_000          # 5m FVG formation bar open
TICK = 0.1


def b(i, o, h, l, c, v=100.0, t0=T5 + 300):
    return Bar(t0 + i * 60, o, h, l, c, v, True)


def profile(poc_lo=4210.0, poc_hi=4212.0, vols=None, segs=()):
    lo, size = 4200.0, 2.0
    vols = vols or [10.0] * 20
    p = Profile("close", T5, lo, lo + size * len(vols), size, vols, None, [])
    i = int((poc_lo - lo) // size)
    p.volumes[i] = max(p.volumes) * 2
    p.poc = p.node(i)
    p.hvns = [p.poc, p.node(15)]       # a second node at 4230-4232 as a structural target
    p.segment_pocs = list(segs)
    return p


def a5(regime="BULLISH", fvgs=None):
    fvgs = fvgs if fvgs is not None else [FVG("BULL", 4213.0, 4209.0, 10, T5, 4.0, 0.8, 1.2, "OPEN")]
    smc = SmcState(regime, 4230.0, 4205.0, 0, 0, []) if regime in ("BULLISH", "BEARISH", "UNDEFINED") else None
    return TimeframeAnalysis("5", 500, T5 - 86400, T5, 5.0, [], [], Regime(regime), [], [], [], fvgs,
                             None, None, "UNKNOWN", None, smc=smc)


def approach_and_reject():
    return [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4215, 4215.5),
            b(2, 4213.5, 4216, 4210.5, 4214.5, v=300)]   # green: wicks to 4210.5, closes back above 4213


# --- volume profile ---------------------------------------------------------------

def bars_for_profile():
    out = []
    for i in range(300):
        c = 4210.5 if i % 3 == 0 else 4200.5 + (i % 20)
        out.append(Bar(T5 + i * 300, c, c + 0.5, c - 0.5, c, 1000 if i % 3 == 0 else 100, True))
    return out


def test_profile_close_method_finds_heaviest_close_bin():
    p = build_profile(bars_for_profile(), ProfileParams())
    assert p.poc.low <= 4210.5 <= p.poc.high
    assert len(p.segment_pocs) == 20 and p.hvns[0].pct_of_poc <= 1.0


def test_profile_unknown_without_enough_history():
    assert build_profile(bars_for_profile()[:100]) is None


def test_range_method_spreads_volume():
    bars = [Bar(T5 + i * 300, 4200, 4210, 4200, 4205, 100, True) for i in range(300)]
    bars[-1] = Bar(bars[-1].time, 4200, 4220, 4200, 4205, 100, True)
    pr = build_profile(bars, ProfileParams(method="range"))
    pc = build_profile(bars, ProfileParams(method="close"))
    assert sum(1 for v in pr.volumes if v > 0) > sum(1 for v in pc.volumes if v > 0)


# --- zones -------------------------------------------------------------------------

def test_zone_requires_poc_overlap():
    assert find_zones(a5(), profile())                                   # POC 4210-4212 inside FVG 4209-4213
    assert not find_zones(a5(), profile(poc_lo=4230.0, poc_hi=4232.0))


def test_segment_poc_counts_as_confluence():
    seg = (T5, Node(4211.0, 4212.0, 50, 0.5))
    z = find_zones(a5(), profile(poc_lo=4230.0, segs=[seg]))
    assert z and z[0].poc_sources == ["segment PoC 4211.0-4212.0"]


# --- states ------------------------------------------------------------------------

def test_watching_before_touch():
    ev = evaluate(a5(), profile(), [b(0, 4220, 4221, 4218, 4219)], TICK, 1.5)
    assert ev.state == "WATCHING" and ev.direction == "LONG"


def test_rejection_qualifies_with_prices():
    ev = evaluate(a5(), profile(), approach_and_reject(), TICK, 1.5)
    assert ev.state == "QUALIFIED", ev.conditions
    assert ev.confirmation == "REJECTION"
    assert ev.entry == 4214.5
    assert ev.stop == pytest.approx(4209.0 - TICK)            # FVG low is below the wick (4210.5)
    assert ev.target == pytest.approx(4214.5 + 2 * (4214.5 - 4208.9))
    assert ev.rr == 2.0


def test_close_beyond_far_edge_before_confirmation_invalidates():
    bars = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219, 4208, 4208.5)]
    ev = evaluate(a5(), profile(), bars, TICK, 1.5)
    assert ev.state == "INVALIDATED"


def test_stop_out_after_entry_does_not_rewrite_the_setup():
    bars = approach_and_reject() + [b(3, 4214.5, 4214.6, 4205, 4205.5)]
    ev = evaluate(a5(), profile(), bars, TICK, 1.5)
    assert ev.state == "QUALIFIED" and ev.confirmed_at == bars[2].time


def test_hold_confirmation_after_n_bars():
    bars = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4212.5, 4212.8),
            b(2, 4212.8, 4213.2, 4212.0, 4212.6), b(3, 4212.6, 4213.4, 4212.2, 4213.0, v=200)]
    ev = evaluate(a5(), profile(), bars, TICK, 1.5)
    assert ev.confirmation == "HOLD" and ev.confirmed_at == bars[3].time


def test_structure_not_aligned_blocks_qualification_without_a_rejection():
    bars = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4212.5, 4212.8),
            b(2, 4212.8, 4213.2, 4212.0, 4212.6), b(3, 4212.6, 4213.4, 4212.2, 4213.0, v=200)]   # HOLD only
    ev = evaluate(a5("BEARISH"), profile(), bars, TICK, 1.5)
    assert ev.state == "INVALIDATED" and "5m structure" in ev.invalidation
    assert ev.entry is not None          # would-be prices kept for outcome tracking
    assert "reversal needs a 1m rejection candle" in ev.conditions["5m structure"][1]


# --- reversal setups : against the 5m structure = sweep in the zone + 1m rejection ------------

def a5_with_levels(regime, levels):
    a = a5(regime)
    a.levels = levels
    return a


def ssl(price, known=T5):
    from copilot.liquidity.liquidity import Level
    return Level("SWING_LOW", "SSL", price, known, "structure")


def test_reversal_qualifies_with_sweep_and_rejection():
    # 5m BEARISH, LONG zone 4209-4213; the rejection bar wicks to 4210.5 through a swing low at 4211 and closes 4214.5
    ev = evaluate(a5_with_levels("BEARISH", [ssl(4211.0)]), profile(), approach_and_reject(), TICK, 1.5)
    assert ev.state == "QUALIFIED", ev.conditions
    assert ev.setup_type == "REVERSAL" and "swept SWING_LOW 4211.0" in ev.conditions["5m structure"][1]


def test_reversal_needs_a_rejection_not_just_holding_closes():
    bars = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4210.8, 4212.8),     # sweeps 4211 but closes inside
            b(2, 4212.8, 4213.2, 4212.0, 4212.6), b(3, 4212.6, 4213.4, 4212.2, 4213.0, v=200)]
    ev = evaluate(a5_with_levels("BEARISH", [ssl(4211.0)]), profile(), bars, TICK, 1.5)
    assert ev.confirmation == "HOLD" and ev.setup_type == "TREND" and ev.state == "INVALIDATED"
    assert "reversal needs a 1m rejection candle" in ev.conditions["5m structure"][1]


def test_reversal_without_sweep_is_a_zone_retrace_with_rejection():
    # price bounces into the FVG and rejects -- no sweep needed (a level known only later is no sweep)
    late = approach_and_reject()[-1].time + 60
    ev = evaluate(a5_with_levels("BEARISH", [ssl(4211.0, known=late)]), profile(), approach_and_reject(), TICK, 1.5)
    assert ev.state == "QUALIFIED" and ev.setup_type == "REVERSAL"
    assert "retraced into the BULL FVG (no liquidity sweep)" in ev.conditions["5m structure"][1]


def test_trend_setup_is_unchanged():
    ev = evaluate(a5_with_levels("BULLISH", [ssl(4211.0)]), profile(), approach_and_reject(), TICK, 1.5)
    assert ev.state == "QUALIFIED" and ev.setup_type == "TREND"


def test_opposing_fvg_before_2r_blocks():
    fvgs = [FVG("BULL", 4213.0, 4209.0, 10, T5, 4.0, 0.8, 1.2, "OPEN"),
            FVG("BEAR", 4222.0, 4219.0, 11, T5, 3.0, 0.6, 1.0, "OPEN")]
    ev = evaluate(a5(fvgs=fvgs), profile(), approach_and_reject(), TICK, 1.5)
    assert ev.state == "INVALIDATED" and "no opposing FVG before 2R" in ev.invalidation


def test_counter_volume_fails_volume_support():
    bars = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4212.0, 4212.5, v=900),   # heavy red touch
            b(2, 4213.5, 4215, 4210.5, 4214.5, v=100)]                                  # light rejection
    ev = evaluate(a5(), profile(), bars, TICK, 1.5)
    assert ev.conditions["volume support"][0] == "FAIL" and ev.state == "INVALIDATED"


def test_close_through_poc_node_fails_volume_support():
    bars = [b(0, 4220, 4221, 4218, 4219), b(1, 4219, 4219.5, 4209.4, 4209.6, v=50),
            b(2, 4209.6, 4214, 4209.5, 4213.8, v=400)]
    ev = evaluate(a5(), profile(), bars, TICK, 1.5)
    assert "POC node" in ev.conditions["volume support"][1]


def test_volume_void_fails():
    p = profile()
    p.volumes = [100.0] * 20
    p.volumes[4] = p.volumes[5] = p.volumes[6] = 1.0   # zone 4209-4213 sits in almost no volume
    p.volumes[15] = 5000.0
    p.poc = p.node(5)          # keep a node overlapping the zone so the zone still exists
    ev = evaluate(a5(), p, approach_and_reject(), TICK, 1.5)
    assert "volume void" in ev.conditions["volume support"][1]


def test_undefined_structure_is_unknown_not_pass():
    ev = evaluate(a5("UNDEFINED"), profile(), approach_and_reject(), TICK, 1.5)
    assert ev.conditions["5m structure"][0] == "UNKNOWN" and ev.state != "QUALIFIED"


def test_unknown_inputs_no_setup():
    assert evaluate(a5(), None, approach_and_reject(), TICK, 1.5).state == "NO_SETUP"


# --- research variants: they never change production (RuleSet defaults) -------------------------------------

def test_reversal_only_variant_excludes_trend_setups():
    from copilot.setup_engine.poc_fvg import RuleSet
    from copilot.setup_engine.poc_fvg import evaluate_all
    ev = evaluate_all(a5("BULLISH"), profile(), approach_and_reject(), TICK, 1.5,
                      RuleSet(name="H5", setup_types=("REVERSAL",)))[0]
    assert ev.state == "INVALIDATED" and "setup type" in ev.invalidation


def test_structural_target_uses_the_nearest_level_ahead():
    from copilot.setup_engine.poc_fvg import RuleSet
    from copilot.setup_engine.poc_fvg import evaluate_all
    ev = evaluate_all(a5("BULLISH"), profile(), approach_and_reject(), TICK, 1.5,
                      RuleSet(name="H8", target_mode="structural"))[0]
    # the only HVN ahead is the 4230-4232 node (mid 4231) -> RR (4231-4214.5)/(4214.5-4208.9)
    assert ev.target == 4231.0 and ev.rr == round((4231.0 - 4214.5) / (4214.5 - 4208.9), 2)
    assert ev.conditions["structural target"][0] == "PASS"


# --- FVG retest reversal : touch, leave, come back, enter and close back outside -------

def retest_bars():
    return [b(0, 4220, 4221, 4218, 4219),
            b(1, 4213.0, 4216, 4212.5, 4214),      # first touch, closes back above the FVG (left it); small wick
            b(2, 4214, 4215, 4213.5, 4214.5),      # outside
            b(3, 4213.2, 4215, 4212.8, 4213.6)]    # back into the FVG and closes above it again -> the retest rejection


def test_retest_reversal_counts_in_the_plan_but_not_in_production():
    from copilot.setup_engine.poc_fvg import PLAN, evaluate_all
    prod = evaluate(a5("BEARISH"), profile(), retest_bars(), TICK, 1.5)
    assert prod.state == "INVALIDATED" and prod.confirmation == "HOLD"          # alerts unchanged
    ev = evaluate_all(a5("BEARISH"), profile(), retest_bars(), TICK, 1.5, PLAN)[0]
    assert ev.state == "QUALIFIED", ev.conditions
    assert ev.setup_type == "REVERSAL" and ev.confirmation == "RETEST_REJECTION" and ev.entry == 4213.6
    assert "left it, came back" in ev.conditions["5m structure"][1]


def test_retest_needs_a_return_after_leaving():
    from copilot.setup_engine.poc_fvg import PLAN, evaluate_all
    bars = retest_bars()[:3]                        # touched and left, but never came back
    ev = evaluate_all(a5("BEARISH"), profile(), bars, TICK, 1.5, PLAN)[0]
    assert ev.setup_type == "TREND" and ev.state != "QUALIFIED"


def test_fvg_without_poc_only_as_a_reversal():
    from copilot.setup_engine.poc_fvg import PLAN, evaluate_all
    far_poc = profile(poc_lo=4230.0, poc_hi=4232.0)
    assert evaluate_all(a5("BEARISH"), far_poc, retest_bars(), TICK, 1.5) == []          # production: no zone
    rev = evaluate_all(a5("BEARISH"), far_poc, retest_bars(), TICK, 1.5, PLAN)[0]
    assert rev.state == "QUALIFIED" and rev.conditions["POC confluence"][0] == "PASS"
    trend = evaluate_all(a5("BULLISH"), far_poc, approach_and_reject(), TICK, 1.5, PLAN)[0]
    assert trend.state == "INVALIDATED" and "POC confluence" in trend.invalidation       # trend still needs a POC
    watch = evaluate_all(a5("BULLISH"), far_poc, [b(0, 4220, 4221, 4218, 4219)], TICK, 1.5, PLAN)[0]
    assert watch.state == "NO_SETUP"                                                     # no POTENTIAL noise


def test_max_fvg_size_variant_skips_large_fvgs_only():
    from copilot.setup_engine.poc_fvg import RuleSet, evaluate_all
    big = evaluate_all(a5("BULLISH"), profile(), approach_and_reject(), TICK, 1.5,
                       RuleSet(name="x", max_fvg_atr=0.5))[0]                   # fixture FVG is 0.8 ATR
    assert big.state == "INVALIDATED" and "FVG max size" in big.invalidation
    ok = evaluate_all(a5("BULLISH"), profile(), approach_and_reject(), TICK, 1.5, RuleSet(name="y", max_fvg_atr=1.5))[0]
    assert ok.state == "QUALIFIED"


def test_strategy_file_is_parsed_and_unknown_keys_fail(tmp_path, monkeypatch):
    import copilot.setup_engine.poc_fvg as pf
    f = tmp_path / "strategy.toml"
    f.write_text('[rules]\nrequire_poc = false\nsessions = ["LONDON", "NY_AM"]\n[plan]\nretest_reversal = true\n')
    monkeypatch.delenv("COPILOT_STRATEGY", raising=False)
    monkeypatch.setenv("COPILOT_STRATEGY_FILE", str(f))
    rules, plan = pf._strategy_overrides()
    assert rules == {"require_poc": False, "sessions": ("LONDON", "NY_AM")} and plan == {"retest_reversal": True}
    f.write_text("[rules]\nno_such_rule = 1\n")
    with pytest.raises(ValueError):
        pf._strategy_overrides()


def test_ict_rules_take_an_fvg_without_poc():
    from copilot.setup_engine.poc_fvg import RuleSet, evaluate_all
    far = profile(poc_lo=4232.0, poc_hi=4234.0)                     # POC far from the 4209-4213 FVG
    assert evaluate_all(a5("BULLISH"), far, approach_and_reject(), TICK, 1.5) == []      # built-in rules: no zone
    ict = RuleSet(name="ict", require_poc=False, use_volume_support=False, confirmations=("REJECTION",))
    ev = evaluate_all(a5("BULLISH"), far, approach_and_reject(), TICK, 1.5, ict)[0]
    assert ev.state == "QUALIFIED" and "not required" in ev.conditions["POC confluence"][1]


def test_shipped_default_strategy_file_loads():
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "COPILOT_STRATEGY"}
    out = subprocess.run([sys.executable, "-c", "from copilot.setup_engine.poc_fvg import PRODUCTION as P;"
                          "print(P.require_poc, P.sessions, P.confirmations, P.use_volume_support)"],
                         capture_output=True, text=True, env=env, cwd=os.path.join(os.path.dirname(__file__), "..", "src"))
    assert out.stdout.strip() == "False ('LONDON', 'NY_AM') ('REJECTION',) False", out.stderr
