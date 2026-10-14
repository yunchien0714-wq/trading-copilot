import copy
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from copilot.risk_engine.gate import Proposal, evaluate, load_toml

ET = ZoneInfo("America/New_York")
T = int(datetime(2026, 10, 5, 10, 0, tzinfo=ET).timestamp())
CFG_DIR = Path(__file__).resolve().parents[1] / "config"

CFG = {
    "account": {"phase": "evaluation", "drawdown": {"max_drawdown_usd": 1000, "type": "trailing_intraday"},
                "contracts": {"max_contracts_mini": 4, "max_contracts_micro": 40},
                "other_rules": {"consistency_rule_applies_to": "funded"}},
    "personal": {"contracts_per_trade_min": 1, "contracts_per_trade_max": 10, "contracts_unit": "micro",
                 "max_trades_per_day": 5, "min_rr": 1.5, "max_loss_per_trade_frac_of_buffer": 0.25,
                 "max_open_positions": 1, "last_entry_time_et": "16:30"},
    "costs": {"commission_roundtrip_per_contract_usd": 1.0},
}
STATE = {"as_of": date(2026, 10, 5), "balance_usd": 25000.0, "high_water_usd": 25000.0,
         "trades_today": 0, "open_positions": 0, "event_check": "CLEAR"}


def prop(**kw):
    base = dict(symbol="COMEX_MINI:MGC1!", direction="LONG", entry=4214.5, stop=4212.5, target=4218.5,
                point_value=10.0, tick=0.1, is_micro=True, time=T, data_live=True)
    base.update(kw)
    return Proposal(**base)


def failing(res):
    return {r.rule for r in res.failures}


def test_all_rules_pass_and_size_respects_loss_cap():
    res = evaluate(prop(), CFG, STATE)
    assert res.passed, [(r.rule, r.detail) for r in res.failures]
    # buffer 1000 -> cap 250; per contract 2 pts * $10 + $1 = $21 -> 10 contracts = $210 <= 250
    assert res.loss_cap_usd == 250 and res.max_contracts == 10 and res.risk_usd_at_max == pytest.approx(210)


def test_cap_shrinks_with_remaining_buffer():
    st = dict(STATE, balance_usd=24500.0, high_water_usd=25000.0)   # buffer 500 -> cap 125
    res = evaluate(prop(), CFG, st)
    assert res.loss_cap_usd == 125 and res.max_contracts == 5          # 5 * 21 = 105, 6 * 21 = 126 > 125


def test_wide_stop_sizes_down_below_five():
    res = evaluate(prop(stop=4209.5, target=4222.0), CFG, STATE)       # 5 pts -> $51/contract, 4 x 51 <= 250
    assert res.passed and res.max_contracts == 4


def test_stop_too_wide_for_one_contract_fails():
    res = evaluate(prop(stop=4189.5, target=4262.0), CFG, STATE)       # 25 pts -> $251/contract > 250
    assert "per-trade loss cap" in failing(res) and not res.passed and res.max_contracts is None


def test_trailing_high_water_reduces_buffer():
    st = dict(STATE, balance_usd=25300.0, high_water_usd=25600.0)    # threshold 24600 -> buffer 700
    assert evaluate(prop(), CFG, st).loss_cap_usd == pytest.approx(175)


@pytest.mark.parametrize("change,rule", [
    (dict(data_live=False), "data quality"),
    (dict(stop=4216.0), "stop placement"),
    (dict(target=4216.0), "minimum RR"),
    (dict(is_micro=False), "contract unit"),
    (dict(point_value=None), "per-trade loss cap"),
    (dict(time=int(datetime(2026, 10, 5, 16, 45, tzinfo=ET).timestamp())), "no overnight risk"),
])
def test_each_rule_can_block(change, rule):
    assert rule in failing(evaluate(prop(**change), CFG, STATE))


@pytest.mark.parametrize("state_change,rule", [
    (dict(as_of=date(2026, 10, 2)), "account state current"),
    (dict(trades_today=5), "max trades per day"),
    (dict(open_positions=1), "open positions"),
    (dict(event_check=None), "no high-impact news window"),
    (dict(balance_usd="UNKNOWN"), "drawdown buffer"),
])
def test_state_rules_block(state_change, rule):
    assert rule in failing(evaluate(prop(), CFG, dict(STATE, **state_change)))


def test_unknown_config_value_fails_closed():
    cfg = copy.deepcopy(CFG)
    cfg["costs"]["commission_roundtrip_per_contract_usd"] = "UNKNOWN"
    assert "per-trade loss cap" in failing(evaluate(prop(), cfg, STATE))


def test_consistency_rule_not_applied_in_evaluation():
    r = next(r for r in evaluate(prop(), CFG, STATE).rules if r.rule == "consistency rule")
    assert r.status == "N/A"


def test_real_config_files_currently_block_everything():
    cfg = load_toml(CFG_DIR / "account.toml")
    st = load_toml(CFG_DIR / "account_state.toml")
    res = evaluate(prop(), cfg, st)
    assert not res.passed            # UNKNOWN values remain -> fail closed until the user fills them in


def test_shipped_template_config_lists_what_is_missing():
    from copilot.risk_engine.gate import config_problems
    probs = config_problems(load_toml(CFG_DIR / "account.toml"))
    assert "[account] size_usd = 'UNKNOWN'" in probs and any("commission" in p for p in probs)
    full = {"account": {"size_usd": 50000, "profit_target_usd": 3000,
                        "drawdown": {"max_drawdown_usd": 2500}, "contracts": {"max_contracts_mini": 10,
                                                                             "max_contracts_micro": 100}},
            "personal": {"contracts_per_trade_min": 1, "contracts_per_trade_max": 10, "max_trades_per_day": 5,
                         "min_rr": 1.5, "max_loss_per_trade_frac_of_buffer": 0.25,
                         "news_blackout_minutes_before": 30, "news_blackout_minutes_after": 30},
            "costs": {"commission_roundtrip_per_contract_usd": 1.5}}
    assert config_problems(full) == []
