"""Continuous strategy research : keep testing new hypotheses, never change production until a
strategy is proven -- and even then only with the user's approval.

Three gates, because testing many variants makes some look good by luck:
  1. BACKTEST  -- walk-forward on stored history, R NET of commission. Pass = >= MIN_TRADES out-of-sample trades,
                  net expectancy > VIABLE_EXPECTANCY in both halves, better than production in both halves,
                  paired day-bootstrap P(better) >= VIABLE_P, drawdown within tolerance. -> status SHADOW
  2. SHADOW    -- the rule set runs on live data next to production, recording what it WOULD have done (no alerts,
                  no orders). Fresh data neutralises the multiple-testing problem. Pass = >= MIN_TRADES resolved,
                  net expectancy > VIABLE_EXPECTANCY and better than production over the same period. -> PROPOSED
  3. USER      -- a PROPOSED strategy is reported; production changes only after the user says yes.

Search: one factor at a time against production first; factor levels that beat production in-sample are then
combined in pairs. Hypotheses are re-tested when enough new days have been stored.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from dataclasses import asdict, dataclass, replace

from ..setup_engine.poc_fvg import PRODUCTION, RuleSet
from .backtest import MIN_TRADES, Variant, simulate, walk_forward
from .hypotheses import SCHEMA, SEED, record

VIABLE_EXPECTANCY = 0.10      # R per trade after commission
VIABLE_P = 0.95
RETEST_AFTER_DAYS = 5         # re-test a hypothesis once this many new trading days are stored

# factor -> list of (level label, change). A change is a dict of RuleSet fields, plus "breakeven_at_r" for exits.
FACTORS: dict[str, list[tuple[str, dict]]] = {
    "target": [("1.5R", {"target_r": 1.5}), ("1.75R", {"target_r": 1.75}), ("2.25R", {"target_r": 2.25}),
               ("2.5R", {"target_r": 2.5}), ("3R", {"target_r": 3.0}), ("structural", {"target_mode": "structural"})],
    "confirm": [("rejection only", {"confirmations": ("REJECTION",)}), ("hold 2 bars", {"hold_bars": 2}),
                ("hold 4 bars", {"hold_bars": 4}), ("hold 5 bars", {"hold_bars": 5}),
                ("wick 33%", {"rejection_wick_frac": 0.33}), ("wick 66%", {"rejection_wick_frac": 0.66})],
    "fvg": [("min 0.3 ATR", {"min_fvg_atr": 0.3}), ("min 0.5 ATR", {"min_fvg_atr": 0.5}),
            ("min 0.8 ATR", {"min_fvg_atr": 0.8}),
            # finer grid around the FVG-size winners (0.3 / 0.5 ATR)
            ("min 0.4 ATR", {"min_fvg_atr": 0.4}), ("min 0.6 ATR", {"min_fvg_atr": 0.6})],
    # do very large FVGs (impulse gaps) behave worse? -- combinable with a minimum size
    "fvgmax": [("max 1.5 ATR", {"max_fvg_atr": 1.5}), ("max 2.5 ATR", {"max_fvg_atr": 2.5})],
    "filters": [("no volume filter", {"use_volume_support": False}),
                ("no opposing-FVG filter", {"use_opposing_fvg": False}),
                ("no structure filter", {"use_structure": False})],
    "type": [("reversal only", {"setup_types": ("REVERSAL",)}), ("trend only", {"allow_reversal": False}),
             ("FVG retest reversal", {"retest_reversal": True, "reversal_fvg_only": True})],
    "exit": [("break-even at 1R", {"breakeven_at_r": 1.0}),
             ("break-even at 0.5R", {"breakeven_at_r": 0.5}), ("break-even at 1.5R", {"breakeven_at_r": 1.5})],
    "session": [("London & NY AM", {"sessions": ("LONDON", "NY_AM")}), ("NY AM only", {"sessions": ("NY_AM",)}),
                ("no Asia", {"sessions": ("LONDON", "NY_AM", "NY_PM")}),
                ("London only", {"sessions": ("LONDON",)}), ("NY AM & PM", {"sessions": ("NY_AM", "NY_PM")}),
                ("killzones only", {"sessions": ("ASIA", "LONDON", "NY_AM", "NY_PM")})],
}

EXTRA_COLUMNS = {"kind": "TEXT", "factors": "TEXT", "shadow_since": "INTEGER", "tested_days": "INTEGER",
                 "is_delta": "REAL"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hypotheses)")}
    for c, t in EXTRA_COLUMNS.items():
        if c not in cols:
            conn.execute(f"ALTER TABLE hypotheses ADD COLUMN {c} {t}")
    conn.commit()


def variant_from(levels: list[tuple[str, str, dict]], hid: str) -> Variant:
    rules, be = {}, None
    for _, _, change in levels:
        for k, v in change.items():
            if k == "breakeven_at_r":
                be = v
            else:
                rules[k] = v
    return Variant(hid, replace(PRODUCTION, name=hid, **rules), be)


def _hid(levels: list[tuple[str, str, dict]]) -> str:
    return "G:" + " + ".join(f"{f}={lab}" for f, lab, _ in levels)


def _insert(conn: sqlite3.Connection, levels: list[tuple[str, str, dict]], reason: str, now: int) -> bool:
    hid = _hid(levels)
    if conn.execute("SELECT 1 FROM hypotheses WHERE id = ?", (hid,)).fetchone():
        return False
    v = variant_from(levels, hid)
    conn.execute("INSERT INTO hypotheses (id, statement, reason, evidence, variant, status, created_at, kind, factors)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (hid, f"Production with {', '.join(f'{f}: {lab}' for f, lab, _ in levels)} beats production.",
                  reason, "generated", json.dumps({"name": v.name, "rules": asdict(v.rules),
                                                    "breakeven_at_r": v.breakeven_at_r}),
                  "RESEARCH", now, "generated", json.dumps([[f, lab] for f, lab, _ in levels])))
    return True


def generate(conn: sqlite3.Connection, now: int, max_new_pairs: int = 12, max_new_triples: int = 8) -> int:
    """One-factor variants always exist; pairs are added from levels that beat production in-sample, and triples
    from pairs that beat production plus another winning level of a different factor."""
    ensure_schema(conn)
    n = sum(_insert(conn, [(f, lab, ch)], "one-factor search", now) for f, lv in FACTORS.items() for lab, ch in lv)
    winners, good_pairs = [], []
    for hid, factors, delta, results in conn.execute("SELECT id, factors, is_delta, results FROM hypotheses"
                                                     " WHERE kind='generated' AND is_delta > 0 ORDER BY is_delta DESC"):
        fl = json.loads(factors)
        levels = [(f, lab, dict(FACTORS[f])[lab]) for f, lab in fl if f in FACTORS and lab in dict(FACTORS[f])]
        n_out = json.loads(results or "{}").get("out_sample", {}).get("n", 0)
        if len(levels) == 1:
            winners.append((levels[0], n_out))
        elif len(levels) == 2:
            good_pairs.append((levels, n_out))
    # a combination is generated only when at least one parent already has MIN_TRADES out-of-sample
    # trades. Stacking conditions on parents that are all short of trades gives combinations that cannot reach
    # MIN_TRADES either (adding filters only removes trades) -- they come back automatically as data grows.
    added = 0
    for (a, na), (b, nb) in itertools.combinations(winners, 2):
        if a[0] != b[0] and added < max_new_pairs and max(na, nb) >= MIN_TRADES:
            added += _insert(conn, [a, b], "pair of one-factor winners", now)
    tri = 0
    for pair, npair in good_pairs:
        for w, nw in winners:
            if tri < max_new_triples and w[0] not in {p[0] for p in pair} and max(npair, nw) >= MIN_TRADES:
                tri += _insert(conn, sorted(pair + [w], key=lambda x: x[0]), "winning pair + another winner", now)
    conn.commit()
    return n + added + tri


def _variant_of(row) -> Variant:
    v = json.loads(row["variant"])
    r = v["rules"]
    for k in ("confirmations", "setup_types", "sessions"):
        if isinstance(r.get(k), list):
            r[k] = tuple(r[k])
    return Variant(v["name"], RuleSet(**r), v.get("breakeven_at_r"))


def pick_batch(conn: sqlite3.Connection, n_days: int, size: int) -> list:
    """Never-tested first, then hypotheses whose last test had RETEST_AFTER_DAYS fewer days than now."""
    rows = conn.execute("SELECT * FROM hypotheses WHERE status IN ('RESEARCH', 'CANDIDATE')"
                        " ORDER BY (last_tested_at IS NOT NULL), last_tested_at").fetchall()
    out = [r for r in rows if r["last_tested_at"] is None
           or (r["tested_days"] or 0) + RETEST_AFTER_DAYS <= n_days]
    return out[:size]


@dataclass(frozen=True)
class Market:
    """One instrument in the research data set (MGC + MNQ pooled for more trades)."""
    root: str
    tick: float
    days: tuple
    commission: float | None          # $ per contract round trip (None -> R stays gross for this instrument)


def markets_text(markets) -> str:
    return "＋".join(f"{m.root} {len(m.days)} 天" for m in markets)


def run_cycle(conn: sqlite3.Connection, markets: list[Market], min_rr: float, now: int, batch_size: int = 8) -> dict:
    """One research run over every instrument's stored days (trades pooled, split and bootstrapped by date).
    Returns a summary; production is never touched."""
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    from .hypotheses import seed
    seed(conn, now)
    ensure_schema(conn)
    generate(conn, now)
    n_days = sum(len(m.days) for m in markets)             # instrument-days: more data -> retest
    days = sorted({d for m in markets for d in m.days})
    batch = pick_batch(conn, n_days, batch_size)
    if not batch:
        return {"tested": [], "shadow": [], "days": n_days, "markets": markets_text(markets)}
    variants = [Variant("production")] + [_variant_of(r) for r in batch]
    res: dict = {v.name: [] for v in variants}
    for m in markets:
        for name, trades in simulate(conn, m.root, m.tick, min_rr, list(m.days), variants, now, m.commission).items():
            res[name] += trades
    verdicts = {v.variant: v for v in walk_forward(res, days, min_expectancy=VIABLE_EXPECTANCY, p_required=VIABLE_P)}
    base = verdicts["production"]
    tested, shadow = [], []
    for r in batch:
        vd = verdicts[r["id"]] if r["id"] in verdicts else verdicts[_variant_of(r).name]
        record(conn, r["id"], vd, {"markets": {m.root: {"days": [str(d) for d in m.days],
                                                        "net_of_commission": m.commission} for m in markets}}, now)
        delta = vd.in_sample.expectancy - base.in_sample.expectancy if vd.in_sample.n else None
        status = "SHADOW" if vd.verdict == "CANDIDATE" else "RESEARCH"
        conn.execute("UPDATE hypotheses SET tested_days = ?, is_delta = ?, status = ?,"
                     " shadow_since = CASE WHEN ? = 'SHADOW' AND shadow_since IS NULL THEN ? ELSE shadow_since END"
                     " WHERE id = ?", (n_days, delta, status, status, now, r["id"]))
        tested.append((r["id"], vd))
        if status == "SHADOW":
            shadow.append(r["id"])
    conn.executescript("CREATE TABLE IF NOT EXISTS research_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);")
    conn.execute("INSERT OR REPLACE INTO research_state VALUES ('baseline', ?)", (json.dumps(
        {"days": n_days, "markets": markets_text(markets), "in_n": base.in_sample.n, "in_exp": base.in_sample.expectancy,
         "out_n": base.out_sample.n, "out_exp": base.out_sample.expectancy, "at": now}),))
    conn.commit()
    generate(conn, now)                                    # pairs / triples from the new winners
    remaining = len(pick_batch(conn, n_days, 10 ** 6))     # 0 -> every hypothesis is tested on the current data
    return {"tested": tested, "shadow": shadow, "days": n_days, "markets": markets_text(markets), "baseline": base,
            "remaining": remaining}


def sample_milestone(conn: sqlite3.Connection, s: dict) -> str | None:
    """Once, when the current strategy first has MIN_TRADES out-of-sample trades --
    from then on the backtest verdicts can actually say something. Returns the message (and marks it sent)."""
    b = s.get("baseline")
    if b is None or b.out_sample.n < MIN_TRADES:
        return None
    conn.executescript("CREATE TABLE IF NOT EXISTS research_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);")
    if conn.execute("SELECT 1 FROM research_state WHERE k = 'sample_enough_sent'").fetchone():
        return None
    conn.execute("INSERT INTO research_state VALUES ('sample_enough_sent', ?)", (str(s.get("days")),))
    conn.commit()
    return (f"樣本累積夠了\n\n資料：{_data_text(s)}\n目前策略樣本外 {b.out_sample.n} 筆（門檻 {MIN_TRADES} 筆），"
            f"平均 {b.out_sample.expectancy:+.2f}R（已扣手續費）。\n\n"
            "從現在起，回測結果開始有判斷力。所有假設會用這份資料重測，測完會發完整報告。\n目前策略不變。")


def _data_text(d: dict) -> str:
    """'MGC 20 天＋MNQ 15 天' (instrument-days), or the older single-instrument form."""
    return d.get("markets") or f"{d['days']} 個交易日"


def sample_progress(conn: sqlite3.Connection, gaps: list | None = None) -> list[str]:
    """How close research is to having enough trades to judge anything (樣本不足 -> 改善方法)."""
    has = lambda t: conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()
    st = conn.execute("SELECT v FROM research_state WHERE k = 'baseline'").fetchone() if has("research_state") else None
    b = json.loads(st[0]) if st else None
    rows = conn.execute("SELECT results FROM hypotheses WHERE last_tested_at IS NOT NULL").fetchall() \
        if has("hypotheses") else []
    enough = sum(1 for (r,) in rows if json.loads(r or "{}").get("out_sample", {}).get("n", 0) >= MIN_TRADES)
    lines = ["樣本進度："]
    if b:
        lines += [f"- 資料 {_data_text(b)}（每天自動加入新的一天；每多 {RETEST_AFTER_DAYS} 天全部重測）",
                  f"- 目前策略樣本外 {b['out_n']} 筆（至少 {MIN_TRADES} 筆才能判斷）"]
    lines.append(f"- 樣本外已達 {MIN_TRADES} 筆的假設：{enough} / {len(rows)} 個")
    if gaps is not None:
        lines.append("- 資料缺漏的交易日：" + ("、".join(f"{d}（{n} 根）" for d, n in gaps) + "（可能是假日）"
                                          if gaps else "無"))
    return lines


def full_report(conn: sqlite3.Connection) -> str:
    """Sent when every hypothesis has been tested on the current data ."""
    from collections import Counter
    rows = conn.execute("SELECT id, verdict, status, results FROM hypotheses WHERE last_tested_at IS NOT NULL").fetchall()
    st = conn.execute("SELECT v FROM research_state WHERE k = 'baseline'").fetchone() \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_state'").fetchone() else None
    b = json.loads(st[0]) if st else None
    items = []
    for hid, verdict, status, results in rows:
        res = json.loads(results or "{}")
        o, i = dict(res.get("out_sample", {}), _reasons=res.get("reasons") or []), res.get("in_sample", {})
        items.append((hid, verdict, status, i, o))
    counts = Counter(VERDICT_ZH.get(v, v) for _, v, _, _, _ in items)
    lines = ["策略研究完整報告：所有假設都已用目前的資料測完", ""]
    if b:
        lines += [f"資料：{_data_text(b)}（R 已扣手續費）",
                  f"目前策略：樣本內 {b['in_n']} 筆 {b['in_exp']:+.2f}R，樣本外 {b['out_n']} 筆 {b['out_exp']:+.2f}R", ""]
    lines += [f"共測了 {len(items)} 個假設：" + "、".join(f"{k} {v}" for k, v in counts.most_common()), ""]
    ranked = sorted((x for x in items if x[4].get("n")), key=lambda x: -x[4].get("expectancy", -9))
    # rank only hypotheses with enough out-of-sample trades -- a +2R average from 1 trade is luck.
    enough = [x for x in ranked if x[4].get("n", 0) >= MIN_TRADES]
    lines.append(f"樣本外筆數足夠（≥{MIN_TRADES} 筆）的 {len(enough)} 個，表現最好的 10 個：")
    for hid, verdict, status, i, o in enough[:10]:
        lines.append(f"- {label(hid)}：樣本內 {i.get('n', 0)} 筆 {i.get('expectancy', 0):+.2f}R｜樣本外 {o.get('n', 0)} 筆 "
                     f"{o.get('expectancy', 0):+.2f}R → {VERDICT_ZH.get(verdict, verdict)}"
                     + (f"（{'；'.join(REASON_ZH(r) for r in o.get('_reasons', []))}）" if o.get("_reasons") else ""))
    if not enough:
        lines.append("- 還沒有")
    positive = [x for x in ranked if x[4].get("expectancy", 0) > VIABLE_EXPECTANCY and x[3].get("expectancy", 0) > VIABLE_EXPECTANCY]
    small = sum(1 for x in positive if x[4].get("n", 0) < MIN_TRADES)
    lines += ["", f"樣本內、樣本外都 > +{VIABLE_EXPECTANCY}R 的：{len(positive)} 個"
              + (f"（其中 {small} 個樣本外不到 {MIN_TRADES} 筆，可能只是運氣）" if small else "")]
    shadow = [label(h) for h, _, s, _, _ in items if s == "SHADOW"]
    lines += ["前瞻測試中：" + ("、".join(shadow) if shadow else "無"), ""]
    lines += sample_progress(conn) + [""]
    lines += ["接下來：等新的交易日資料累積，舊假設每多 5 個交易日會自動重測；有好的組合會自動往下配對。",
              "目前策略不變。要改策略必須：回測通過 → 前瞻測試通過 → 你同意。"]
    return "\n".join(lines)


def REASON_ZH(r: str) -> str:
    """Backtest reasons (English, from walk_forward) in plain Chinese."""
    import re
    if r == "drawdown worse than production":
        return "最大回撤比目前策略大"
    if r == "expectancy not better in both in-sample and out-of-sample":
        return "前後兩段沒有都比目前策略好"
    m = re.match(r"bootstrap P\(better\) ([\d.]+) < ([\d.]+)", r)
    if m:
        return f"統計檢定 {m.group(1)}，未達 {m.group(2)}"
    m = re.match(r"expectancy ([-+\d.]+) / ([-+\d.]+)R not above \+([\d.]+)R", r)
    if m:
        return f"平均 {m.group(1)} / {m.group(2)}R，沒有兩段都超過 +{m.group(3)}R"
    m = re.match(r"out-of-sample trades (\d+) < (\d+)", r)
    if m:
        return f"樣本外只有 {m.group(1)} 筆"
    return r


VERDICT_ZH = {"CANDIDATE": "通過回測", "REJECTED": "比目前差，排除", "INCONCLUSIVE": "不明確",
              "INSUFFICIENT_DATA": "樣本不足", "BASELINE": "基準"}
FACTOR_ZH = {"target": "目標", "confirm": "確認", "filters": "過濾", "type": "類型", "exit": "出場", "session": "時段",
             "fvg": "FVG 大小", "fvgmax": "FVG 上限"}
LEVEL_ZH = {"structural": "結構目標", "rejection only": "只用拒絕K線", "hold 2 bars": "收盤守住 2 根",
            "hold 5 bars": "收盤守住 5 根", "no volume filter": "拿掉成交量過濾", "no opposing-FVG filter": "拿掉反向FVG過濾",
            "no structure filter": "拿掉結構過濾", "reversal only": "只做轉折", "trend only": "只做順勢",
            "break-even at 1R": "到 1R 移保本", "London & NY AM": "只做倫敦＋紐約上午", "NY AM only": "只做紐約上午",
            "no Asia": "不做亞洲盤", "hold 4 bars": "收盤守住 4 根", "wick 33%": "拒絕K線影線≥33%",
            "wick 66%": "拒絕K線影線≥66%", "min 0.3 ATR": "至少 0.3 ATR", "min 0.5 ATR": "至少 0.5 ATR",
            "min 0.8 ATR": "至少 0.8 ATR", "FVG retest reversal": "FVG 二次測試轉折",
            "min 0.4 ATR": "至少 0.4 ATR", "min 0.6 ATR": "至少 0.6 ATR", "max 1.5 ATR": "最多 1.5 ATR",
            "max 2.5 ATR": "最多 2.5 ATR", "break-even at 0.5R": "到 0.5R 移保本", "break-even at 1.5R": "到 1.5R 移保本",
            "London only": "只做倫敦", "NY AM & PM": "只做紐約（上午＋下午）", "killzones only": "只做四個時段內"}
SEED_ZH = {"H1": "拿掉成交量過濾", "H2": "到 1R 移保本", "H3": "只用拒絕K線", "H4": "目標 1.5R", "H5": "只做轉折",
           "H6": "只做順勢", "H7": "目標 3R", "H8": "結構目標", "H9": "FVG 二次測試轉折"}


def label(hid: str) -> str:
    """Readable Chinese name of a hypothesis id, e.g. 'G:target=3R + exit=break-even at 1R'."""
    if not hid.startswith("G:"):
        return f"{hid} {SEED_ZH.get(hid, '')}".strip()
    parts = []
    for p in hid[2:].split(" + "):
        f, _, lv = p.partition("=")
        parts.append(f"{FACTOR_ZH.get(f, f)}：{LEVEL_ZH.get(lv, lv)}")
    return "＋".join(parts)


def summary_text(s: dict) -> str:
    if not s["tested"]:
        return f"策略研究：沒有需要重新測試的假設（{_data_text(s)}）。"
    b = s["baseline"]
    lines = [f"策略研究（{_data_text(s)}，R 已扣手續費）",
             f"目前策略：樣本內 {b.in_sample.n} 筆 {b.in_sample.expectancy:+.2f}R，樣本外 {b.out_sample.n} 筆 "
             f"{b.out_sample.expectancy:+.2f}R", ""]
    for hid, vd in sorted(s["tested"], key=lambda t: -(t[1].out_sample.expectancy if t[1].out_sample.n else -9)):
        lines.append(f"- {label(hid)}：樣本外 {vd.out_sample.n} 筆 {vd.out_sample.expectancy:+.2f}R → "
                     f"{VERDICT_ZH.get(vd.verdict, vd.verdict)}")
    if s["shadow"]:
        lines += ["", "通過回測、開始前瞻影子測試（只記錄，不發提醒、不改策略）：", *[f"- {label(h)}" for h in s["shadow"]]]
    lines += ["", "目前策略不變。要改策略必須：回測通過 → 前瞻測試通過 → 你同意。"]
    return "\n".join(lines)
