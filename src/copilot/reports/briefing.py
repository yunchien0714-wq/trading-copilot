"""Pre-session briefing : 5 minutes before the Asia (20:00), London (02:00) and NY AM (09:30 ET)
sessions, one Telegram message with the picture before trading. Read-only facts from the same analysis the alerts
use; nothing is predicted and no trade is suggested.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from ..session_engine.sessions import ET, to_et
from ..setup_engine.poc_fvg import find_zones

SESSIONS = (("ASIA", "亞洲盤", time(20, 0)), ("LONDON", "倫敦盤", time(2, 0)), ("NY_AM", "紐約上午", time(9, 30)))
LEAD = timedelta(minutes=5)
MAX_BAR_AGE = 600                  # market counts as open if the last 1m bar closed within 10 min
SMC_ZH = {"BULLISH": "多頭", "BEARISH": "空頭", "UNDEFINED": "未定"}
LEVEL_ZH = {"PDH": "前日高", "PDL": "前日低", "CDH": "今日高", "CDL": "今日低", "PWH": "前週高", "PWL": "前週低",
            "ASIA_H": "亞洲盤高", "ASIA_L": "亞洲盤低", "LONDON_H": "倫敦盤高", "LONDON_L": "倫敦盤低",
            "NY_AM_H": "紐約上午高", "NY_AM_L": "紐約上午低", "NY_PM_H": "紐約下午高", "NY_PM_L": "紐約下午低"}


def due(now: int, last_bar_close: int | None) -> tuple[str, str, datetime] | None:
    """(session id, Chinese name, session start) if a briefing is due now and the market is open."""
    if last_bar_close is None or now - last_bar_close > MAX_BAR_AGE:
        return None                                   # closed (Friday night, Saturday, holiday) or no data
    et = to_et(now)
    for sid, name, start in SESSIONS:
        begin = datetime.combine(et.date(), start, tzinfo=ET)
        if begin - LEAD <= et < begin:
            return sid, name, begin
    return None


def _zones(a5, prof, price: float) -> list[str]:
    smc = a5.smc.direction if a5.smc else "UNDEFINED"
    rows = []
    for z in find_zones(a5, prof, include_fvg_only=True):
        aligned = (z.direction == "LONG") == (smc == "BULLISH") and smc in ("BULLISH", "BEARISH")
        if z.nodes and aligned:
            kind = "順勢區"
        elif z.nodes:
            kind = "轉折區（逆結構，要拒絕K線）"
        else:
            kind = "只有 FVG（只能做轉折）"
        desc = f"{z.low:.1f}–{z.high:.1f} {'多' if z.direction == 'LONG' else '空'}方 FVG{'＋POC' if z.nodes else ''}"
        if z.low <= price <= z.high:
            rows.append(("在區域內", 0.0, f"價格在區域內 {desc}｜{kind}"))
            continue
        side = "上方" if z.low > price else "下方"
        dist = (z.low - price) if side == "上方" else (price - z.high)
        rows.append((side, dist, f"{side} {desc}｜距離 {dist:.1f} 點｜{kind}"))
    out = []
    for side in ("在區域內", "上方", "下方"):
        out += [r[2] for r in sorted((r for r in rows if r[0] == side), key=lambda r: r[1])[:3]]
    return out


def _levels(a5, price: float) -> list[str]:
    lv = [(LEVEL_ZH.get(p.name, p.name), p.price) for p in a5.periods
          if p.price is not None and p.complete and p.name in LEVEL_ZH]
    above = sorted((x for x in lv if x[1] > price), key=lambda x: x[1])[:3]
    below = sorted((x for x in lv if x[1] <= price), key=lambda x: -x[1])[:3]
    return ([f"上方 {n} {p:.1f}（+{p - price:.1f}）" for n, p in above]
            + [f"下方 {n} {p:.1f}（−{price - p:.1f}）" for n, p in below])


def _events(events, now: int, high: int) -> list[str]:
    if events is None:
        return ["經濟日曆讀不到（未知）"]
    up = [e for e in events if 0 <= e.time - now <= 8 * 3600 and e.importance >= 0]
    if not up:
        return ["接下來 8 小時沒有中等以上的數據"]
    return [f"{to_et(e.time):%H:%M} {'重大' if e.importance >= high else '中等'} {e.title}"
            + ("（前後 30 分鐘不進場）" if e.importance >= high else "") for e in up[:6]]


def build(session_name: str, begin: datetime, a5, prof, price: float, events, high: int, account: dict) -> str:
    smc = a5.smc.direction if a5.smc else "UNDEFINED"
    last = a5.smc.events[-1] if a5.smc and a5.smc.events else None
    lines = [f"盤前簡報｜{session_name}（{begin:%H:%M} ET 開始）", "",
             f"現價 {price:.1f}",
             f"5m 結構：{SMC_ZH.get(smc, smc)}"
             + (f"（最近：{last.kind} {'向上' if last.direction == 'UP' else '向下'} @ {last.level}，"
                f"{to_et(last.time):%m-%d %H:%M}）" if last else ""), ""]
    zones = _zones(a5, prof, price) if prof is not None else []
    lines += ["附近的區域："] + ([f"- {z}" for z in zones] or ["- 附近沒有有效的 FVG 區域"]) + [""]
    lv = _levels(a5, price)
    if lv:
        lines += ["關鍵價位："] + [f"- {x}" for x in lv] + [""]
    lines += ["經濟數據（接下來 8 小時）："] + [f"- {x}" for x in _events(events, int(begin.timestamp()) - 300, high)] + [""]
    a = account
    lines += ["帳戶：",
              f"- 權益 {a['equity']}，回撤門檻 {a['threshold']}，緩衝 {a['buffer']}",
              f"- 單筆最多虧 {a['cap']}"]
    if a.get("sizing"):
        lines += [f"- {a['sizing']}"]
    lines += [f"- 今天已交易 {a['n']}/{a['max']} 筆，損益 {a['pnl']}"]
    if a.get("streak", 0) >= 2:
        lines += [f"- ⚠️ 目前連續虧損 {a['streak']} 筆：下一筆前先確認是不是你的 setup"]
    lines += ["", "提醒：進場後 60 秒內要有止損單；16:00 ET 之後不進場。"]
    return "\n".join(lines)
