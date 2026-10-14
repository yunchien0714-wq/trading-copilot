"""Fixed English -> Chinese translations for the grader's output (Notion journal).

Deterministic lookup tables and regex patterns only -- no free rewriting. Text that matches no pattern is kept in
English unchanged, so nothing is ever invented or lost.
"""
from __future__ import annotations

import re

CHECKS = {
    "5m structure aligned": "5m 結構方向一致",
    "setup zone (5m FVG + POC)": "進場區（5m FVG + POC）",
    "1m reaction confirmation": "1m 反應確認",
    "entry location": "進場位置",
    "stop defined": "有設止損",
    "planned RR": "計畫 RR",
    "loss cap (1/4 of buffer)": "單筆虧損上限（緩衝 1/4）",
    "size vs recommended": "口數 vs 建議口數",
    "trades per day": "每日交易筆數",
    "outside news window": "避開重大新聞時段",
}
CATEGORIES = {"strategy": "策略", "execution": "執行", "risk": "風控"}
STATUS = {"PASS": "✅ 通過", "FAIL": "❌ 違規", "WARN": "⚠️ 注意", "UNKNOWN": "❔ 無法判斷"}
KILLZONES = {"ASIA": "亞洲", "LONDON": "倫敦", "NY_AM": "紐約上午", "NY_PM": "紐約下午", "none": "非時段"}
SMC = {"BULLISH": "多頭", "BEARISH": "空頭"}
OUTCOME = {"WIN": "獲利", "LOSS": "虧損", "FLAT": "平手", "UNKNOWN": "未知"}
QUALITY = {"GOOD": "好交易", "BAD": "壞交易"}
DIRECTION = {"LONG": "多", "SHORT": "空"}
EXIT = {"stop": "止損出場", "target": "目標出場", "manual": "手動出場"}
CLASSIFICATION = {"Good trade / Good outcome": "好交易／好結果", "Good trade / Bad outcome": "好交易／壞結果",
                  "Good trade / Flat outcome": "好交易／平手", "Good trade / Unknown outcome": "好交易／結果未知",
                  "Bad trade / Good outcome": "壞交易／好結果（運氣）", "Bad trade / Bad outcome": "壞交易／壞結果",
                  "Bad trade / Flat outcome": "壞交易／平手", "Bad trade / Unknown outcome": "壞交易／結果未知"}

_N = r"([-+]?\d+(?:\.\d+)?)"
SENTENCES: list[tuple[str, str]] = [
    (r"Traded against the 5m structure -- your rule is to take only the side the 5m SMC structure gives\.",
     "逆 5m 結構進場——你的規則是只做 5m SMC 結構給的方向。"),
    (r"No 5m FVG \+ POC zone at the entry -- this was not your setup\. Wait for price to reach a zone\.",
     "進場位置沒有 5m FVG + POC 區——這不是你的 setup。等價格到區域再說。"),
    (r"Entered before the 1m reaction \(rejection candle or holding closes\) -- wait for the confirmation close\.",
     "在 1m 反應出現前就進場（拒絕 K 線或收盤守住）——等確認 K 收盤。"),
    (r"Entered inside the confirming candle, before it closed -- the setup was right, but wait for the close "
     r"\(a candle can still turn before it closes\)\.",
     "在確認 K 線收盤前就進場——setup 是對的，但要等收盤（K 線收盤前還可能翻轉）。"),
    (r"Entry chased away from the zone -- the stop gets wider and RR worse\. Let price come back\.",
     "追價、離開區域進場——止損變寬、RR 變差。讓價格回來。"),
    (rf"Size {_N} was above the recommended {_N} for this stop -- size from the stop distance and the loss cap before "
     r"the entry\.", r"口數 \1 超過這個止損距離的建議口數 \2——進場前先用止損距離和虧損上限算口數。"),
    (rf"Size {_N} with this stop: even 1 contract was over the loss cap -- the stop was too wide to take the trade\.",
     r"\1 口、這個止損：連 1 口都超過虧損上限——止損太寬，這筆不該做。"),
    (rf"Size {_N} is outside {_N}-{_N} contracts\.", r"口數 \1 不在 \2–\3 口範圍內。"),
    (r"Planned RR below your 1:1\.5 minimum\.", "計畫 RR 低於你的最低 1:1.5。"),
    (r"Risk exceeded 1/4 of the remaining drawdown buffer -- reduce contracts or tighten to a valid stop\.",
     "風險超過剩餘回撤緩衝的 1/4——減少口數，或改用合理的較近止損。"),
    (r"Entered inside a high-importance news window, which you do not trade\.",
     "在重大新聞時段內進場，這是你不做的時段。"),
    (r"Risk may have exceeded 1/4 of the remaining buffer -- size from the conservative cap\.",
     "風險可能超過剩餘緩衝的 1/4——用保守的上限來算口數。"),
    (rf"Price went {_N}R in your favour but the trade closed at {_N}R -- review the exit plan\.",
     r"價格曾往有利方向走到 \1R，但最後在 \2R 平倉——檢討出場計畫。"),
    (rf"Observation \(not a rule\): price reached \+{_N}R before the full stop\. Whether a break-even or partial-exit "
     r"rule would help must be tested on many trades first \(hypothesis, spec 23\)\.",
     r"觀察（不是規則）：打到止損前價格曾到 +\1R。保本或分批出場是否有幫助，要先用大量交易驗證（假設）。"),
    (r"Stop unknown for this imported trade -- R-based review not possible\.",
     "這筆匯入交易沒有止損資料——無法用 R 檢討。"),
    (r"Process followed; the outcome is variance, not a mistake\.", "照計畫執行；虧損是正常波動，不是錯誤。"),
    (r"Repeat this: it matched your plan\.", "照這樣重複：完全符合你的計畫。"),
]
_COMPILED = [(re.compile("^" + p + "$"), r) for p, r in SENTENCES]


def sentence(text: str) -> str:
    """Translate one grader sentence; unknown text is returned unchanged."""
    for rx, rep in _COMPILED:
        if rx.match(text):
            return rx.sub(rep, text)
    return text


def check_name(name: str) -> str:
    return CHECKS.get(name, name)
