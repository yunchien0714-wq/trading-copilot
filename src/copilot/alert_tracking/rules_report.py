"""Which rules help and which ones cost money? (record every potential setup, entered or not.)

Built only from alert_outcomes (every confirmed setup with its real prices, plus every unconfirmed zone touch with a
hypothetical touch entry). For each rule: setups it blocked ALONE (the clean signal) and together with other rules,
what they would have made, and whether the user traded them. Descriptive only: a rule is changed only after a
research test (hypotheses, >= 30 trades) and the user's approval.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

MIN_HINT = 10          # fewer resolved setups than this -> "too few to say"

RULE_ZH = {
    "5m structure": "5m 結構方向",
    "volume support": "成交量支撐",
    "no opposing FVG before 2R": "2R 前沒有反向 FVG",
    "RR >= minimum": "RR 達最低要求",
    "risk gate": "風控（帳戶規則）",
    "expired: no 1m confirmation": "等不到 1m 確認（過期）",
    "closed beyond the zone before any confirmation": "確認前就收盤穿過區域",
}
DECISION_ZH = {"ALERTED": "系統發出提醒", "BLOCKED": "風控擋下", "QUALIFIED_UNGATED": "合格（重播，未過風控）",
               "FILTERED": "被規則過濾", "UNCONFIRMED": "碰到區域但未確認"}


@dataclass
class Bucket:
    n: int = 0
    wins: int = 0
    total_r: float = 0.0
    traded: int = 0

    def add(self, r: float, win: bool, traded) -> None:
        self.n += 1
        self.wins += int(win)
        self.total_r += r
        self.traded += int(bool(traded))

    @property
    def avg(self) -> float:
        return self.total_r / self.n if self.n else 0.0

    def hint(self, blocked: bool = True) -> str:
        if self.n < MIN_HINT:
            return f"樣本不足（{self.n}/{MIN_HINT}）"
        if not blocked:
            return "有正期望" if self.avg > 0 else "負期望"
        return "可能太嚴（被擋下的平均是正的）" if self.avg > 0 else "有在保護你（被擋下的平均是負的）"


@dataclass
class Report:
    decisions: dict = field(default_factory=lambda: defaultdict(Bucket))
    only: dict = field(default_factory=lambda: defaultdict(Bucket))      # blocked by this rule alone
    any_: dict = field(default_factory=lambda: defaultdict(Bucket))      # this rule among the failures
    by_type: dict = field(default_factory=lambda: defaultdict(Bucket))   # (setup type, taken|blocked)
    unresolved: int = 0


def _setup_type(conditions: str | None) -> str:
    try:
        st = json.loads(conditions or "{}").get("5m structure")
        return "REVERSAL" if st and str(st[1]).startswith("REVERSAL") else "TREND"
    except (ValueError, TypeError, IndexError):
        return "TREND"


def build(conn: sqlite3.Connection, source: str = "live", since: int | None = None) -> Report:
    rep = Report()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='alert_outcomes'").fetchone():
        return rep
    rows = conn.execute("SELECT decision, filtered_by, outcome, r, user_traded, conditions FROM alert_outcomes"
                        " WHERE source = ? AND decided_at >= ?", (source, since or 0)).fetchall()
    for dec, by, outcome, r, traded, cond in rows:
        if outcome in (None, "PENDING") or r is None:
            rep.unresolved += 1
            continue
        win = outcome == "WIN" or (outcome == "TIMEOUT" and r > 0)
        rep.decisions[dec].add(r, win, traded)
        taken = dec in ("ALERTED", "BLOCKED", "QUALIFIED_UNGATED")
        rep.by_type[(_setup_type(cond), "taken" if taken else "blocked")].add(r, win, traded)
        if dec in ("FILTERED", "UNCONFIRMED", "BLOCKED"):
            rules = [x.strip() for x in (by or "").split(",") if x.strip()]
            for rule in rules:
                rep.any_[rule].add(r, win, traded)
            if len(rules) == 1:
                rep.only[rules[0]].add(r, win, traded)
    return rep


def text(rep: Report) -> str:
    """Telegram / CLI version."""
    if not rep.decisions:
        return "規則檢查：還沒有已結算的 setup。" + (f"（{rep.unresolved} 筆還在等結果）" if rep.unresolved else "")
    out = ["規則檢查（每個潛在 setup，不論有沒有進場）", "R 未扣手續費；只是描述統計，改規則要先經過研究驗證和你同意。", ""]
    for dec, b in sorted(rep.decisions.items(), key=lambda kv: -kv[1].n):
        out.append(f"{DECISION_ZH.get(dec, dec)}：{b.n} 筆，勝 {b.wins}，平均 {b.avg:+.2f}R，你有進場 {b.traded}")
    out += ["", "各規則「單獨」擋下的 setup（最能看出規則好壞）："]
    for rule, b in sorted(rep.only.items(), key=lambda kv: -kv[1].n):
        out.append(f"- {RULE_ZH.get(rule, rule)}：{b.n} 筆，平均 {b.avg:+.2f}R → {b.hint()}")
    types = {"TREND": "順勢", "REVERSAL": "轉折"}
    lines = [f"- {types[t]}／{'系統放行' if k == 'taken' else '被擋'}：{b.n} 筆，平均 {b.avg:+.2f}R"
             for (t, k), b in sorted(rep.by_type.items())]
    if lines:
        out += ["", "順勢 vs 轉折："] + lines
    if rep.unresolved:
        out += ["", f"另有 {rep.unresolved} 筆還在等結果。"]
    return "\n".join(out)


def html_tables(rep: Report, table) -> str:
    """Dashboard version; `table(head, rows)` is the dashboard's table renderer."""
    if not rep.decisions:
        return "<p class='muted'>還沒有已結算的 setup。</p>"
    dec = table(["決策", "筆數", "勝", "平均 R", "你有進場"],
                [[DECISION_ZH.get(d, d), b.n, b.wins, f"{b.avg:+.2f}", b.traded]
                 for d, b in sorted(rep.decisions.items(), key=lambda kv: -kv[1].n)])
    rules = table(["規則", "單獨擋下", "平均 R（單獨）", "判讀", "一起擋下", "平均 R（一起）"],
                  [[RULE_ZH.get(r, r), rep.only[r].n if r in rep.only else 0,
                    f"{rep.only[r].avg:+.2f}" if r in rep.only else "-",
                    rep.only[r].hint() if r in rep.only else "沒有單獨擋過",
                    b.n, f"{b.avg:+.2f}"] for r, b in sorted(rep.any_.items(), key=lambda kv: -kv[1].n)])
    types = {"TREND": "順勢", "REVERSAL": "轉折"}
    ty = table(["類型", "系統放行／被擋", "筆數", "平均 R"],
               [[types[t], "放行" if k == "taken" else "被擋", b.n, f"{b.avg:+.2f}"]
                for (t, k), b in sorted(rep.by_type.items())])
    return dec + rules + ty
