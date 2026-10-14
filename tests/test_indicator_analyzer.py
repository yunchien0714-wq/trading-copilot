from pathlib import Path

import pytest

from copilot.indicator_code_analyzer.reviews import REVIEWS, review_for
from copilot.indicator_code_analyzer.static_checks import scan
from copilot.tradingview.indicators import classify


def checks(src):
    return {h.check for h in scan(src)}


# --- static checks on synthetic sources --------------------------------------

def test_lookahead_detected():
    src = '//@version=5\nindicator("x")\nh = request.security(syminfo.tickerid, "D", high, lookahead = barmerge.lookahead_on)\n'
    assert "lookahead_on" in checks(src)


def test_lookahead_in_comment_ignored():
    src = '//@version=5\n// request.security(x, "D", high, lookahead = barmerge.lookahead_on)\n'
    assert "lookahead_on" not in checks(src)


def test_pivot_lag_reports_right_bars():
    hits = [h for h in scan('p = ta.pivothigh(14, 14)\n') if h.check == "pivot_confirmation_lag"]
    assert hits and "14 right-side bars" in hits[0].detail and hits[0].lines == (1,)


def test_pivot_with_source_argument():
    assert "pivot_confirmation_lag" in checks('p = ta.pivotlow(low, 5, 3)\n')


def test_updown_bar_volume_proxy():
    src = 'a = request.security_lower_tf(syminfo.tickerid, "1", close > open ? volume : 0.0)\n'
    assert "updown_bar_volume_proxy" in checks(src)


def test_candle_shape_volume_split():
    src = 'b = volume * (close-low)/(high-low)\n'
    assert "candle_shape_volume_split" in checks(src)


def test_last_bar_recompute_and_leak():
    src = 'if barstate.islast\n    for i = 0 to 10\n        v = volume[i]\n    box.new(0, 1, 2, 0)\n'
    c = checks(src)
    assert {"last_bar_recompute", "drawings_never_deleted"} <= c


def test_last_bar_with_delete_is_not_a_leak():
    src = 'if barstate.islast\n    for i = 0 to 10\n        v = close[i]\n    b = box.new(0, 1, 2, 0)\n    box.delete(b)\n'
    c = checks(src)
    assert "last_bar_recompute" in c and "drawings_never_deleted" not in c


def test_crlf_source_keeps_line_numbers(tmp_path):
    from copilot.tradingview.indicators import normalize_newlines
    src = normalize_newlines("a = 1\r\nb = 2\r\np = ta.pivothigh(3, 3)\r\n")
    f = tmp_path / "x.pine"
    f.write_text(src, encoding="utf-8", newline="\n")
    hit = [h for h in scan(f.read_text(encoding="utf-8")) if h.check == "pivot_confirmation_lag"][0]
    assert hit.lines == (3,)


def test_clean_source_has_no_hits():
    assert scan('//@version=5\nindicator("ema")\nplot(ta.ema(close, 20))\n') == []


# --- source availability ------------------------------------------------------

@pytest.mark.parametrize("raw,status", [
    ({"http": 200, "source": "//@version=5\nindicator('x')"}, "AVAILABLE"),
    ({"http": 200, "source": None}, "UNAVAILABLE"),
    ({"http": 200, "source": "a8f3c9e1b2..."}, "UNAVAILABLE"),
    ({"http": 403, "source": None}, "UNAVAILABLE"),
    ({"fetch_error": "boom"}, "UNAVAILABLE"),
])
def test_classify(raw, status):
    assert classify(raw)[0] == status


# --- review binding -----------------------------------------------------------

def test_review_is_stale_when_digest_changes():
    r = REVIEWS[0]
    assert review_for(r.script_id, r.digest)[1] == "CURRENT"
    assert review_for(r.script_id, "different")[1] == "STALE"
    assert review_for("PUB;nope", "x") == (None, "MISSING")


def test_every_review_states_trade_correlation_unknown():
    for r in REVIEWS:
        assert any(cls == "unknown" for cls, _ in r.findings), r.name
        assert {cls for cls, _ in r.findings} <= {"verified", "empirical", "hypothesis", "unknown"}


# --- scanner vs manual review on fetched indicator sources (skipped if not fetched) ----

SRC_DIR = Path(__file__).resolve().parents[1] / "data" / "indicator_sources"
EXPECTED = {
    "PoC_Migration_Map": {"last_bar_recompute", "drawings_never_deleted"},
    "Support_and_Resistance_Logistic_Regression": {"pivot_confirmation_lag"},
    "Breakout_Volume_Delta": {"updown_bar_volume_proxy"},
    "Time_Relative_Volume_Oscillator": {"candle_shape_volume_split"},
    "ICT_Killzones": set(),
}


@pytest.mark.parametrize("prefix,expected", EXPECTED.items())
def test_scanner_agrees_with_manual_review(prefix, expected):
    files = list(SRC_DIR.glob(f"{prefix}*.pine"))
    if not files:
        pytest.skip("indicator sources not fetched on this machine")
    src = files[0].read_text(encoding="utf-8")
    assert "\r" not in src, "stored source must be LF-normalised (else line numbers double)"
    c = checks(src)
    assert expected <= c
    assert "lookahead_on" not in c and "negative_plot_offset" not in c
