from datetime import datetime

from copilot.reports.briefing import build, due
from copilot.session_engine.sessions import ET

from test_setup_engine import a5, profile


def ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def test_due_five_minutes_before_each_session_only_when_open():
    t = ts(2026, 10, 8, 9, 26)
    assert due(t, t - 30)[0] == "NY_AM"
    assert due(ts(2026, 10, 8, 1, 55), ts(2026, 10, 8, 1, 55))[0] == "LONDON"
    assert due(ts(2026, 10, 7, 19, 59), ts(2026, 10, 7, 19, 58))[0] == "ASIA"
    assert due(ts(2026, 10, 8, 9, 30), ts(2026, 10, 8, 9, 30)) is None        # session already started
    assert due(ts(2026, 10, 8, 9, 20), ts(2026, 10, 8, 9, 20)) is None        # too early
    assert due(ts(2026, 10, 8, 13, 26), ts(2026, 10, 8, 13, 26)) is None      # NY PM not requested
    assert due(ts(2026, 10, 9, 19, 56), ts(2026, 10, 9, 16, 59)) is None      # Friday evening: market closed


def test_briefing_content():
    acct = {"equity": "$50,061.74", "threshold": "$47,731.65", "buffer": "$2,330.09", "cap": "$582.52",
            "sizing": "MGC（每點 $10）：1 口 ≤ 58.1 點", "n": 2, "max": 5, "pnl": "-$120.00", "streak": 2}
    begin = datetime(2026, 10, 8, 9, 30, tzinfo=ET)
    txt = build("紐約上午", begin, a5("BEARISH"), profile(), 4220.0, [], 1, acct)
    assert "盤前簡報｜紐約上午（09:30 ET 開始）" in txt and "現價 4220.0" in txt and "5m 結構：空頭" in txt
    assert "下方 4209.0–4213.0 多方 FVG＋POC｜距離 7.0 點｜轉折區" in txt          # LONG zone against BEARISH
    assert "單筆最多虧 $582.52" in txt and "連續虧損 2 筆" in txt
    assert "接下來 8 小時沒有中等以上的數據" in txt
    txt = build("紐約上午", begin, a5("BULLISH"), profile(), 4220.0, None, 1, dict(acct, streak=0))
    assert "順勢區" in txt and "連續虧損" not in txt and "經濟日曆讀不到" in txt


def test_price_inside_a_zone_is_said_plainly():
    acct = {"equity": "-", "threshold": "-", "buffer": "-", "cap": "-", "n": 0, "max": 5, "pnl": "$0.00", "streak": 0}
    begin = datetime(2026, 10, 8, 9, 30, tzinfo=ET)
    txt = build("紐約上午", begin, a5("BULLISH"), profile(), 4211.0, [], 1, acct)
    assert "價格在區域內 4209.0–4213.0 多方 FVG＋POC｜順勢區" in txt
