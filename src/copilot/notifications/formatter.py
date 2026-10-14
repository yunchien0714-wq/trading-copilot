"""Concise Telegram texts (spec section 39), in Chinese . Plain text, no markdown -- prices must
never be mangled.

Every message states the data quality, so a DELAYED feed can never be mistaken for a live signal, and
never claims an outcome ("this will win"): it lists conditions, evidence and the invalidation.
Condition / rule names are translated with fixed tables; the evidence keeps the engine's own numbers.
"""
from __future__ import annotations

from ..setup_engine.poc_fvg import Evaluation

COND_ZH = {
    "5m structure": "5m 結構", "5m FVG": "5m FVG", "POC confluence": "POC 重疊", "price in zone": "價格進入區域",
    "1m rejection candle": "1m 拒絕 K 線", "1m closes hold": "1m 收盤守住", "volume support": "成交量支撐",
    "no opposing FVG before 2R": "2R 前沒有反向 FVG", "RR >= minimum": "RR 達最低要求",
    "structural target": "結構目標", "session": "交易時段", "setup type": "setup 類型",
}
RULE_ZH = {
    "account state current": "帳戶資料是最新的", "consistency rule": "一致性規則", "contract unit": "合約種類",
    "data quality": "即時資料", "drawdown buffer": "回撤緩衝", "max trades per day": "每日交易筆數上限",
    "minimum RR": "最低 RR", "no high-impact news window": "避開重大新聞時段", "no overnight risk": "不留倉過夜",
    "open positions": "同時持倉數", "per-trade loss cap": "單筆虧損上限", "stop placement": "止損位置",
}
TARGET_ZH = {"HVN": "高量節點", "opposing FVG": "反向 FVG"}


def _side(d: str | None) -> str:
    return "做多" if d == "LONG" else "做空" if d == "SHORT" else ""


def _header(sym: str, ev: Evaluation) -> str:
    return f"{sym} 5m {_side(ev.direction)}"


def _quality(q: str) -> str:
    return "" if q == "REALTIME" else f"\n[資料{q}：只是分析，不是即時訊號]"


def potential(sym: str, ev: Evaluation, quality: str) -> str:
    c = ev.conditions
    done = [k for k, (s, _) in c.items() if s == "PASS"]
    waiting = [COND_ZH.get(k, k) for k, (s, _) in c.items() if s == "PENDING"]
    return (f"潛在 setup\n\n{_header(sym, ev)}\n\n狀態：觀察中\n"
            f"已符合：{len(done)}/{len(c)}\n"
            f"還在等：{'、'.join(waiting) or '-'}\n\n"
            f"區域：{ev.zone.low}-{ev.zone.high}（{', '.join(ev.zone.poc_sources)}）\n"
            f"失效條件：1m 收盤{'低於' if ev.direction == 'LONG' else '高於'} {ev.zone.far_edge}"
            f"{_quality(quality)}")


def trade_setup(sym: str, ev: Evaluation, gate, quality: str) -> str:
    reasons = "\n".join(f"- {COND_ZH.get(k, k)}：{d}" for k, (s, d) in ev.conditions.items() if s == "PASS")
    targets = "\n".join(f"- {TARGET_ZH.get(n, n)} {p:.1f}（{r}R）"
                        for n, p, r in ev.structural_targets[:3]) or "- 沒有"
    size = (f"{gate.max_contracts} 口 → 打到止損約虧 ${gate.risk_usd_at_max:.0f}（含手續費，上限 "
            f"${gate.loss_cap_usd:.0f}）" if gate.max_contracts else "未知")
    kind = "（轉折單：逆 5m 結構）" if getattr(ev, "setup_type", "TREND") == "REVERSAL" else ""
    return (f"交易 setup{kind}\n\n{_header(sym, ev)}\n\n"
            f"進場：{ev.entry}\n止損：{ev.stop:.1f}\n目標：{ev.target:.1f}\nRR：{ev.rr}\n\n"
            f"把握度：未知（還沒有足夠的歷史交易）\n\n"
            f"符合的條件：\n{reasons}\n\n其他目標：\n{targets}\n\n"
            f"口數：\n{size}\n\n"
            f"失效條件：{ev.invalidation}{_quality(quality)}")


def risk_blocked(sym: str, ev: Evaluation, gate, quality: str) -> str:
    fails = "\n".join(f"- {RULE_ZH.get(r.rule, r.rule)}：{r.detail}" for r in gate.failures)
    return (f"setup 被風控擋下\n\n{_header(sym, ev)}\n"
            f"原本的進場 {ev.entry}／止損 {ev.stop:.1f}／目標 {ev.target:.1f}\n\n"
            f"沒通過的規則：\n{fails}\n\n不做這筆。{_quality(quality)}")


def invalidated(sym: str, ev: Evaluation | None, reason: str) -> str:
    side = _side(ev.direction) if ev else ""
    return f"setup 失效\n\n{sym} {side}\n\n原因：{reason}\n\n不做這筆。"


def event_reminder(text: str) -> str:
    return text


def render(n, sym: str, quality: str) -> str:
    if n.kind == "POTENTIAL":
        return potential(sym, n.evaluation, quality)
    if n.kind == "TRADE_SETUP":
        return trade_setup(sym, n.evaluation, n.risk, quality)
    if n.kind == "RISK_BLOCKED":
        return risk_blocked(sym, n.evaluation, n.risk, quality)
    if n.kind == "INVALIDATED":
        return invalidated(sym, n.evaluation, n.reason)
    raise ValueError(n.kind)
