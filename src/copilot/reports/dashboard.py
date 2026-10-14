"""Performance dashboard (spec Phase 10): one self-contained local HTML file, no network, no external assets.

Every number comes from the local store. Anything that cannot be derived is shown as UNKNOWN with the reason,
never estimated. Small samples are labelled as such.
"""
from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..broker.tradingview_broker import high_water, load_anchor
from ..session_engine.sessions import ET

DASHBOARD_VERSION = "dashboard-0.1.0"
SMALL_SAMPLE = 20


def _e(x) -> str:
    return html.escape("UNKNOWN" if x is None else str(x))


def _usd(x) -> str:
    return "UNKNOWN" if x is None else f"${x:,.2f}"


def _et(t: int | None) -> str:
    return "UNKNOWN" if t is None else datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")


def _table(head: list[str], rows: list[list], cls: str = "") -> str:
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, Markup) else _e(c)}</td>" for c in r) + "</tr>"
                 for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


class Markup(str):
    """Already-escaped HTML fragment."""


def _equity_svg(points: list[tuple[int, float]], lines: dict[str, float]) -> str:
    if len(points) < 2:
        return "<p class='muted'>權益資料不足，無法畫曲線。</p>"
    w, h, pad = 760, 220, 34
    ts = [p[0] for p in points]
    vs = [p[1] for p in points] + list(lines.values())
    lo, hi = min(vs), max(vs)
    span = (hi - lo) or 1.0
    x = lambda t: pad + (t - ts[0]) / ((ts[-1] - ts[0]) or 1) * (w - 2 * pad)
    y = lambda v: h - pad - (v - lo) / span * (h - 2 * pad)
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(v):.1f}" for i, (t, v) in enumerate(points))
    guides = "".join(
        f'<line x1="{pad}" x2="{w - pad}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="g {k}"/>'
        f'<text x="{w - pad}" y="{y(v) - 4:.1f}" text-anchor="end" class="gl">{_e(name)} {v:,.0f}</text>'
        for k, (name, v) in ((k, (k, v)) for k, v in lines.items()))
    return (f'<svg viewBox="0 0 {w} {h}" class="eq" role="img" aria-label="權益曲線">{guides}'
            f'<path d="{path}" class="line"/>'
            f'<text x="{pad}" y="{h - 8}" class="gl">{_e(_et(ts[0]))} ET</text>'
            f'<text x="{w - pad}" y="{h - 8}" text-anchor="end" class="gl">{_e(_et(ts[-1]))} ET</text></svg>')


def account_section(conn: sqlite3.Connection, cfg: dict, state_path: Path) -> str:
    acct = cfg.get("account", {})
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (
            acct.get("size_usd"), acct.get("profit_target_usd"), acct.get("drawdown", {}).get("max_drawdown_usd"))):
        return "<section><h2>帳戶</h2><p>config/account.toml 尚未填完（UNKNOWN），帳戶與回撤資訊暫不顯示。</p></section>"
    size = float(cfg["account"]["size_usd"])
    target = float(cfg["account"]["profit_target_usd"])
    max_dd = float(cfg["account"]["drawdown"]["max_drawdown_usd"])
    last = conn.execute("SELECT account, at, balance, equity FROM equity_marks ORDER BY at DESC LIMIT 1").fetchone()
    if last is None:
        return "<section><h2>帳戶</h2><p>尚無券商帳戶資料（UNKNOWN）。</p></section>"
    account, at, bal, eq = last
    hw, why = high_water(conn, account, size, None, max_dd, load_anchor(state_path))
    threshold = None if hw is None else hw - max_dd
    buffer = None if threshold is None or eq is None else eq - threshold
    cap = None if buffer is None else buffer / 4
    progress = None if bal is None else bal - size
    cards = [
        ("餘額", _usd(bal), f"最後更新 {_et(at)} ET"),
        ("距離目標", _usd(None if progress is None else target - progress),
         f"已 {progress:+,.2f} / 目標 +{target:,.0f}" if progress is not None else ""),
        ("回撤緩衝剩餘", _usd(buffer), f"門檻 {_usd(threshold)}" if threshold is not None else why),
        ("單筆最大虧損（緩衝 1/4）", _usd(cap), "個人規則"),
    ]
    card_html = "".join(f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div>'
                        f'<div class="s">{_e(s)}</div></div>' for k, v, s in cards)
    pts, prev = [], None
    for t, e in conn.execute("SELECT at, equity FROM equity_marks WHERE account=? AND equity IS NOT NULL ORDER BY at",
                             (account,)):
        if e != prev:
            pts.append((t, e))
            prev = e
    if pts and pts[-1][0] != at and eq is not None:
        pts.append((at, eq))
    lines = {"起始": size, "目標": size + target}
    if threshold is not None:
        lines["門檻"] = threshold
    return (f"<section><h2>帳戶 <span class='muted'>{_e(account)}</span></h2><div class='cards'>{card_html}</div>"
            f"{_equity_svg(pts, lines)}<p class='muted'>高水位：{_e(why)}</p></section>")


def trades_section(conn: sqlite3.Connection, account: str | None = None) -> tuple[str, str]:
    rows = conn.execute("""SELECT t.*, g.grade, g.quality, g.outcome AS g_outcome, g.classification, g.checks,
                                  g.provisional FROM trades t LEFT JOIN trade_grades g ON g.trade_id = t.id
                           WHERE ? IS NULL OR t.account = ? ORDER BY t.entry_time""", (account, account)).fetchall()
    closed = [r for r in rows if r["status"] == "CLOSED"]
    pnl = [r["pnl_usd"] for r in closed if r["pnl_usd"] is not None]
    rs = [r["r_multiple"] for r in closed if r["r_multiple"] is not None]
    wins = sum(1 for p in pnl if p > 0)
    stats = [
        ("已平倉", len(closed), ""),
        ("勝率", f"{wins / len(pnl):.0%}" if pnl else None, f"{wins}/{len(pnl)}"),
        ("淨損益（含手續費）", _usd(sum(pnl)) if pnl else None, ""),
        ("平均 R", f"{sum(rs) / len(rs):+.2f}" if rs else None, f"{len(rs)} 筆有 R"),
    ]
    cards = "".join(f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div>'
                    f'<div class="s">{_e(s)}</div></div>' for k, v, s in stats)
    grades = Counter(r["grade"] for r in rows if r["grade"])
    cls = Counter(r["classification"] for r in rows if r["classification"])
    dist = " ".join(f"<span class='pill g{g}'>{g} × {grades[g]}</span>" for g in "ABCD" if grades[g])
    dist += "".join(f"<span class='pill'>{_e(c)} × {n}</span>" for c, n in cls.most_common())
    table = _table(["#", "進場 ET", "方向", "口數", "進場", "出場", "損益", "R", "評級", "分類"],
                   [[r["id"], _et(r["entry_time"]), r["direction"], int(r["qty"]) if r["qty"] else None,
                     r["entry"], None if r["exit"] is None else round(r["exit"], 2),
                     _usd(r["pnl_usd"]) if r["pnl_usd"] is not None else None,
                     None if r["r_multiple"] is None else f"{r['r_multiple']:+.2f}",
                     Markup(f"<span class='pill g{r['grade']}'>{r['grade']}</span>" +
                            (" (暫定)" if r["provisional"] else "")) if r["grade"] else "未評",
                     r["classification"]] for r in reversed(rows)])
    note = (f"<p class='warn'>只有 {len(closed)} 筆交易，統計僅供參考，還不能下結論（至少 {SMALL_SAMPLE} 筆後才有意義）。</p>"
            if len(closed) < SMALL_SAMPLE else "")
    trades_html = (f"<section><h2>手動交易</h2><div class='cards'>{cards}</div><p>{dist}</p>{note}{table}</section>")

    fails: Counter = Counter()
    graded = [r for r in rows if r["checks"]]
    for r in graded:
        for cat, name, status, *_ in json.loads(r["checks"]):
            if status in ("FAIL", "WARN"):
                fails[(cat, name, status)] += 1
    if fails:
        mist = _table(["類別", "檢查項目", "狀態", "次數", "佔已評交易"],
                      [[c, n, s, k, f"{k / len(graded):.0%}"] for (c, n, s), k in fails.most_common()])
    else:
        mist = "<p class='muted'>目前沒有失敗的檢查項目。</p>"
    mistakes_html = (f"<section><h2>重複出現的錯誤</h2><p class='muted'>來自 {len(graded)} 筆已評級交易。"
                     f"{'樣本太少，只是觀察，不是結論。' if len(graded) < SMALL_SAMPLE else ''}</p>{mist}</section>")
    return trades_html, mistakes_html


def notes_section(conn: sqlite3.Connection, account: str | None = None) -> str:
    """Emotion (filled in by the user in Notion) vs. grade and result. Descriptive only."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='trade_notes'").fetchone() is None:
        return ("<section><h2>情緒與結果</h2><p class='muted'>還沒從 Notion 讀到任何「進場理由／情緒」。"
                "在 Notion 交易日誌填寫後，這裡會自動統計。</p></section>")
    rows = conn.execute("""SELECT COALESCE(n.emotion, '未填') emo, COUNT(*) n, SUM(t.pnl_usd > 0) w,
                                  SUM(t.pnl_usd) pnl, AVG(t.r_multiple) avg_r, COUNT(t.r_multiple) nr,
                                  GROUP_CONCAT(g.grade, '') grades,
                                  SUM(n.entry_reason IS NOT NULL) reasons
                           FROM trades t LEFT JOIN trade_notes n ON n.trade_id = t.id
                           LEFT JOIN trade_grades g ON g.trade_id = t.id
                           WHERE t.status = 'CLOSED' AND (? IS NULL OR t.account = ?)
                           GROUP BY emo ORDER BY n DESC""", (account, account)).fetchall()
    total = sum(r["n"] for r in rows)
    filled = sum(r["n"] for r in rows if r["emo"] != "未填")
    tbl = _table(["情緒", "筆數", "勝率", "淨損益", "平均 R", "評級", "有寫進場理由"],
                 [[r["emo"], r["n"], f"{(r['w'] or 0) / r['n']:.0%}", _usd(r["pnl"]) if r["pnl"] is not None else None,
                   f"{r['avg_r']:+.2f}（{r['nr']} 筆）" if r["avg_r"] is not None else None,
                   " ".join(sorted(r["grades"] or "")) or "-", r["reasons"]] for r in rows])
    note = (f"<p class='warn'>只有 {filled} 筆有填情緒，還看不出規律（至少每種情緒各 5～10 筆後再看）。</p>"
            if filled < SMALL_SAMPLE else "")
    return (f"<section><h2>情緒與結果</h2><p class='muted'>情緒是你在 Notion 自己填的；{filled}/{total} 筆已填。"
            f"只是描述統計，不是因果。</p>{note}{tbl}</section>")


def alerts_section(conn: sqlite3.Connection) -> str:
    """Every potential setup -- alerted, blocked, filtered or never confirmed -- and what it would have made."""
    from ..alert_tracking.rules_report import build as rules_build, html_tables
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='alert_outcomes'").fetchone() is None:
        return "<section><h2>規則檢查</h2><p class='muted'>尚無資料。</p></section>"
    live, replay = rules_build(conn, "live"), rules_build(conn, "replay")
    note = ("<p class='muted'>每個潛在 setup 都有記錄，不論有沒有進場。「碰到區域但未確認」是假設在第一次碰到區域邊緣就進場"
            "（止損在區域另一側、目標 2R），用來檢查「等 1m 確認」這條規則。R 未扣手續費；判讀只是描述統計，"
            "改規則前要先經過研究驗證和你的同意。</p>")
    return (f"<section><h2>規則檢查（即時）</h2>{note}{html_tables(live, _table)}"
            f"<h2>規則檢查（歷史重播）</h2>{html_tables(replay, _table)}</section>")


def research_log_section(conn: sqlite3.Connection) -> str:
    """研究紀錄: one row per day from research_log (same records as the Notion 研究日誌)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_log'").fetchone() is None:
        return ""
    rows = []
    for day, data in conn.execute("SELECT day, data FROM research_log ORDER BY day DESC LIMIT 60"):
        d = json.loads(data)
        b = d.get("baseline") or {}
        top = d["top"][0] if d.get("top") else None
        rows.append([day, d.get("data"), d.get("tested"), d.get("new"),
                     f"{b['out_n']} 筆 / {b['out_exp']:+.2f}R" if b.get("out_n") is not None else None,
                     f"{d.get('enough')} / {d.get('total_tested')}",
                     f"{top['label']}：{top['n']} 筆 {top['exp']:+.2f}R（{top['verdict']}）" if top else None,
                     "、".join(d.get("shadow") or []) or "無"])
    if not rows:
        return ""
    return ("<section><h2>研究紀錄</h2><p class='muted'>每天收盤後記錄一次（週末也記），內容和 Notion 研究日誌相同。"
            "R 已扣手續費；樣本外至少 30 筆才能判斷。研究不會改動正式策略。</p>"
            + _table(["日期", "資料", "測試", "新增", "目前策略樣本外", "達 30 筆／已測", "當天最佳", "前瞻測試中"], rows)
            + "</section>")


def hypotheses_section(conn: sqlite3.Connection) -> str:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hypotheses'").fetchone() is None:
        return ""
    rows = conn.execute("SELECT * FROM hypotheses ORDER BY id").fetchall()
    out = []
    for r in rows:
        res = json.loads(r["results"]) if r["results"] else {}
        o, b = res.get("out_sample", {}), res.get("base_out", {})
        out.append([r["id"], r["statement"], r["verdict"] or "未測試", r["status"],
                    f"{o.get('n', '-')} 筆 / {o.get('expectancy', 0):+.2f}R" if o else None,
                    f"{b.get('n', '-')} 筆 / {b.get('expectancy', 0):+.2f}R" if b else None,
                    None if res.get("p_better") is None else f"{res['p_better']:.2f}",
                    _et(r["last_tested_at"])])
    return ("<section><h2>規則假設研究</h2><p class='muted'>只在回測中執行，不影響正式提醒。"
            "任何假設要改成正式規則，都需要你同意。</p>"
            + _table(["ID", "假設", "結論", "狀態", "樣本外（假設）", "樣本外（正式）", "P(較好)", "測試時間 ET"], out)
            + "</section>")


CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--bd:#e3e2dc;--acc:#2f6f5e;--neg:#b23b3b;--warn:#8a6100}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--mut:#9a9a94;--card:#1f1f1d;--bd:#33332f;
--acc:#6fbfa6;--neg:#e07a7a;--warn:#e0b45a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,"Microsoft JhengHei","PingFang TC",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:32px 0 12px}.muted{color:var(--mut);font-size:13px}.warn{color:var(--warn);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
.k{color:var(--mut);font-size:12px}.v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.s{color:var(--mut);font-size:12px;overflow-wrap:anywhere}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--bd);border-radius:10px;margin-top:10px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;border-bottom:1px solid var(--bd);text-align:left;white-space:nowrap}
th{color:var(--mut);font-weight:500}tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--bd);font-size:12px;margin:0 4px 4px 0}
.gA{color:var(--acc)}.gD{color:var(--neg)}
svg.eq{width:100%;height:auto;background:var(--card);border:1px solid var(--bd);border-radius:10px;margin-top:10px}
.line{fill:none;stroke:var(--acc);stroke-width:2}.g{stroke:var(--bd);stroke-dasharray:4 4}.g.門檻{stroke:var(--neg)}
.gl{fill:var(--mut);font-size:11px}
"""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """A fresh installation has an empty database: create the tables this report reads (all IF NOT EXISTS)."""
    from ..broker.tradingview_broker import SCHEMA as EQUITY
    from ..trade_journal.grader import SCHEMA as GRADES
    from ..trade_journal.notion_sync import NOTES_SCHEMA
    from ..trade_monitor.monitor import SCHEMA as TRADES
    for schema in (EQUITY, TRADES, GRADES, NOTES_SCHEMA):
        conn.executescript(schema)


def build(conn: sqlite3.Connection, cfg: dict, state_path: Path, now: int) -> str:
    _ensure_tables(conn)
    prev, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        last = conn.execute("SELECT account FROM equity_marks ORDER BY at DESC LIMIT 1").fetchone()
        account = last[0] if last else None                  # statistics are per account (accounts get replaced)
        trades_html, mistakes_html = trades_section(conn, account)
        body = (account_section(conn, cfg, state_path) + trades_html + mistakes_html + notes_section(conn, account)
                + alerts_section(conn)
                + research_log_section(conn) + hypotheses_section(conn))
    finally:
        conn.row_factory = prev
    return (f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>交易績效</title>"
            f"<style>{CSS}</style></head><body><main><h1>交易績效</h1>"
            f"<p class='muted'>產生於 {_e(_et(now))} ET · 本機資料 · {DASHBOARD_VERSION}</p>{body}</main></body></html>")


def write(conn: sqlite3.Connection, cfg: dict, root: Path, now: int) -> Path:
    out = root / "data" / "reports" / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(conn, cfg, root / "config" / "account_state.toml", now), encoding="utf-8")
    return out
