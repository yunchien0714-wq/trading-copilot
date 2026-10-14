"""Research journal (記得每天都要記錄新研究 -> Notion 研究日誌 + dashboard section).

One record per ET trading day, taken after the close from what the research loop stored: hypotheses tested and
created in the last 24 h, the current strategy's baseline, sample progress, forward tests. Pure bookkeeping of
backtest results that already exist -- nothing here changes rules, and nothing is estimated or filled in.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from .auto import MIN_TRADES, VERDICT_ZH, _data_text, label

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_log (
    day TEXT PRIMARY KEY, created_at INTEGER NOT NULL, data TEXT NOT NULL,
    notion_page TEXT, notion_synced_at INTEGER
);
"""
TOP = 5
PAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_log_pages (day TEXT NOT NULL, strategy TEXT NOT NULL, page TEXT NOT NULL,
                                               PRIMARY KEY (day, strategy));
"""


def _pages_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PAGES_SCHEMA)
    if "synced_at" not in {r[1] for r in conn.execute("PRAGMA table_info(research_log_pages)")}:
        conn.execute("ALTER TABLE research_log_pages ADD COLUMN synced_at INTEGER")


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def snapshot(conn: sqlite3.Connection, day: str, now: int, gaps: list | None = None) -> dict | None:
    """Record day's research (everything since the previous record). None when research has not started yet."""
    if not _has(conn, "hypotheses"):
        return None
    conn.executescript(SCHEMA)
    prev = conn.execute("SELECT MAX(created_at) FROM research_log WHERE day < ?", (day,)).fetchone()[0]
    since = prev or now - 86400                            # since the previous record: nothing falls between days
    rows = conn.execute("SELECT id, verdict, results, status, created_at, last_tested_at FROM hypotheses").fetchall()
    tested, new, enough, shadow = [], [], 0, []
    for hid, verdict, results, status, created, last in rows:
        o = json.loads(results or "{}").get("out_sample", {})
        if last and o.get("n", 0) >= MIN_TRADES:
            enough += 1
        if last and last >= since:
            tested.append({"id": hid, "label": label(hid), "n": o.get("n", 0), "exp": o.get("expectancy", 0.0),
                           "verdict": VERDICT_ZH.get(verdict, verdict)})
        if created and created >= since:
            new.append(label(hid))
        if status == "SHADOW":
            shadow.append(label(hid))
    tested.sort(key=lambda x: -(x["exp"] if x["n"] else -9))
    st = conn.execute("SELECT v FROM research_state WHERE k = 'baseline'").fetchone() \
        if _has(conn, "research_state") else None
    b = json.loads(st[0]) if st else {}
    data = {
        "day": day, "since": since, "data": _data_text(b) if b else "UNKNOWN",
        "baseline": {k: b.get(k) for k in ("in_n", "in_exp", "out_n", "out_exp")} if b else None,
        "tested": len(tested), "verdicts": dict(Counter(t["verdict"] for t in tested)), "top": tested[:TOP],
        "new": len(new), "new_labels": new[:10], "enough": enough,
        "total_tested": sum(1 for r in rows if r[5]), "total": len(rows), "shadow": shadow, "gaps": gaps,
        "systems": system_pages(conn),
        "forward": _forward(conn),
    }
    conn.execute("INSERT INTO research_log (day, created_at, data) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET"
                 " created_at = excluded.created_at, data = excluded.data",       # keep the Notion page id
                 (day, now, json.dumps(data, ensure_ascii=False)))
    conn.commit()
    return data


MAIN_STRATEGY = "POC/FVG（目前策略）"
SYSTEM_NAMES = {"ORB": "開盤區間突破（5 分鐘K線）", "ORB15": "開盤區間突破（15 分鐘K線，長期）"}
TOP_VERSIONS = 5


def _forward(conn: sqlite3.Connection) -> list[dict]:
    from .shadow import forward_progress
    return [{"label": label(h), "n": n, "exp": e, "days": dd}
            for h, n, e, dd in sorted(forward_progress(conn), key=lambda x: -x[1])]


def system_pages(conn: sqlite3.Connection) -> dict[str, dict]:
    """Other systems (在 Notion 標記是不同策略): per strategy, its best versions by out-of-sample
    average -> one Notion page per strategy per day, tagged with 策略."""
    if not _has(conn, "system_results"):
        return {}
    from .orb import SYSTEMS, label, variants
    out = {}
    for system, name in SYSTEM_NAMES.items():
        rows = conn.execute("SELECT variant, results, data FROM system_results WHERE system=?", (system,)).fetchall()
        if not rows:
            continue
        bm = next((k for k, v in SYSTEMS.items() if v == system), 5)
        names = {v.name: label(v) for v in variants(bm)}
        evs = sorted(((names.get(v, v), json.loads(r), json.loads(dt)) for v, r, dt in rows),
                     key=lambda x: -(x[1]["out"]["expectancy"] if x[1]["out"]["n"] else -9))
        data = evs[0][2]
        per = data.get("markets") or {}
        data_text = ("＋".join(f"{k} {n} 天" for k, n in per.items()) if isinstance(per, dict) else str(per)) \
            + (f"（{data['first']} 起）" if data.get("first") else "")
        top = [{"label": lb, "in_n": e["in"]["n"], "in_exp": e["in"]["expectancy"], "n": e["out"]["n"],
                "exp": e["out"]["expectancy"], "dd": e["out"]["max_dd_r"], "verdict": e["verdict"]}
               for lb, e, _ in evs[:TOP_VERSIONS]]
        out[name] = {"data": data_text, "versions": len(evs), "top": top,
                     "passed": sum(1 for _, e, _ in evs if e["verdict"] == "通過回測")}
    return out


def system_lines_of(name: str, s: dict) -> list[str]:
    out = [f"策略：{name}（另一個系統，只在回測裡跑）", f"資料：{s['data']}，R 已扣手續費",
           f"共 {s['versions']} 個版本，通過回測 {s['passed']} 個。後段表現最好的 {len(s['top'])} 個："]
    out += [f"- {t['label']}：前段 {t['in_n']} 筆 {t['in_exp']:+.2f}R｜後段 {t['n']} 筆 {t['exp']:+.2f}R｜"
            f"最大回撤 {t['dd']:.1f}R → {t['verdict']}" for t in s["top"]]
    out.append("只是研究，不發交易提醒，也不改動目前的策略。")
    return out


def lines(d: dict) -> list[str]:
    """Readable Chinese summary of one day's record (Notion page body)."""
    out = [f"資料：{d['data']}（R 已扣手續費）"]
    b = d.get("baseline")
    if b:
        out.append(f"目前策略：樣本內 {b['in_n']} 筆 {b['in_exp']:+.2f}R，樣本外 {b['out_n']} 筆 {b['out_exp']:+.2f}R"
                   f"（樣本外至少 {MIN_TRADES} 筆才能判斷）")
    out.append(f"上次紀錄後測了 {d['tested']} 個假設" + ("：" + "、".join(f"{k} {v}" for k, v in d["verdicts"].items())
                                                 if d["verdicts"] else ""))
    out += [f"- {t['label']}：樣本外 {t['n']} 筆 {t['exp']:+.2f}R → {t['verdict']}" for t in d["top"]]
    out.append(f"上次紀錄後新增 {d['new']} 個假設" + ("：" + "、".join(d["new_labels"]) if d["new_labels"] else ""))
    out.append(f"累計：{d['total_tested']} / {d['total']} 個假設測過；樣本外已達 {MIN_TRADES} 筆的 {d['enough']} 個")
    fw = d.get("forward") or []
    if fw:
        out.append(f"前瞻測試進度（要 {MIN_TRADES} 筆、橫跨 10 個交易日才判定；MGC＋MNQ）：")
        out += [f"- {f['label']}：{f['n']} 筆、{f['days']} 天" + (f"，平均 {f['exp']:+.2f}R" if f["exp"] is not None else "")
                for f in fw]
    else:
        out.append("前瞻測試中：" + ("、".join(d["shadow"]) if d["shadow"] else "無"))
    if d.get("gaps"):
        out.append("資料缺漏的交易日：" + "、".join(f"{g[0]}（{g[1]} 根）" for g in d["gaps"]) + "（可能是假日）")
    out.append("目前策略不變。要改策略必須：回測通過 → 前瞻測試通過 → 你同意。")
    return out


# ------------------------------------------------------------------------------------------------ Notion
DB_TITLE = "研究日誌"
DB_PROPERTIES = {
    "日期": {"title": {}},
    "資料": {"rich_text": {}},
    "今天測試": {"number": {"format": "number"}},
    "今天新增": {"number": {"format": "number"}},
    "目前策略樣本外筆數": {"number": {"format": "number"}},
    "目前策略樣本外R": {"number": {"format": "number"}},
    "達30筆的假設": {"number": {"format": "number"}},
    "今日最佳": {"rich_text": {}},
    "前瞻測試中": {"number": {"format": "number"}},
    "策略": {"select": {}},
}


def ensure_strategy_property(client, database_id: str) -> None:
    """Add the 策略 select column to a 研究日誌 created by an older version (no-op when it exists)."""
    db = client.call("GET", f"/databases/{database_id}")
    if "策略" not in db.get("properties", {}):
        client.call("PATCH", f"/databases/{database_id}", {"properties": {"策略": {"select": {}}}})


def create_database(client, parent_page: str) -> str:
    res = client.call("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page},
        "title": [{"type": "text", "text": {"content": DB_TITLE}}],
        "properties": DB_PROPERTIES})
    return res["id"]


def _props(d: dict) -> dict:
    from ..trade_journal.notion_sync import _num, _text
    b = d.get("baseline") or {}
    top = d["top"][0] if d["top"] else None
    return {
        "日期": {"title": _text(f"{d['day']}｜{MAIN_STRATEGY}")},
        "資料": {"rich_text": _text(d["data"])},
        "今天測試": _num(d["tested"]),
        "今天新增": _num(d["new"]),
        "目前策略樣本外筆數": _num(b.get("out_n")),
        "目前策略樣本外R": _num(None if b.get("out_exp") is None else round(b["out_exp"], 2)),
        "達30筆的假設": _num(d["enough"]),
        "今日最佳": {"rich_text": _text(f"{top['label']}：{top['n']} 筆 {top['exp']:+.2f}R → {top['verdict']}"
                                       if top else "（今天沒有測試）")},
        "前瞻測試中": _num(len(d["shadow"])),
        "策略": {"select": {"name": MAIN_STRATEGY}},
    }


def _system_props(day: str, name: str, d: dict, s: dict) -> dict:
    from ..trade_journal.notion_sync import _num, _text
    top = s["top"][0] if s["top"] else None
    return {
        "日期": {"title": _text(f"{day}｜{name}")},
        "資料": {"rich_text": _text(s["data"])},
        "今天測試": _num(s["versions"]),
        "今日最佳": {"rich_text": _text(f"{top['label']}：{top['n']} 筆 {top['exp']:+.2f}R → {top['verdict']}"
                                       if top else "—")},
        "策略": {"select": {"name": name}},
    }


def sync(conn: sqlite3.Connection, client, database_id: str, now: int) -> tuple[int, list[str]]:
    """Write every not-yet-synced day to Notion (one page per day, rewritten if the day is recorded again)."""
    from ..trade_journal.notion_sync import NotionError, _block, _replace_body
    if not _has(conn, "research_log"):
        return 0, []
    conn.executescript(PAGES_SCHEMA)
    n, errors = 0, []
    for day, data, page in conn.execute("SELECT day, data, notion_page FROM research_log"
                                        " WHERE notion_synced_at IS NULL OR notion_synced_at < created_at"
                                        " ORDER BY day").fetchall():
        d = json.loads(data)
        body = [_block("paragraph", s) for s in lines(d)]
        try:
            if page:
                client.call("PATCH", f"/pages/{page}", {"properties": _props(d)})
                _replace_body(client, page, body)
            else:
                page = client.call("POST", "/pages", {"parent": {"database_id": database_id},
                                                      "properties": _props(d), "children": body})["id"]
            conn.execute("UPDATE research_log SET notion_page=?, notion_synced_at=? WHERE day=?", (page, now, day))
            conn.commit()
            n += 1
        except NotionError as e:
            errors.append(f"{day}: {e}")
    return n, errors


def sync_systems(conn: sqlite3.Connection, client, database_id: str, now: int) -> tuple[int, list[str]]:
    """Other systems: one Notion page per strategy per test day ('2026-01-02｜開盤區間突破（5 分鐘K線）', tagged 策略),
    written as soon as new results exist and rewritten when the same day is re-tested ."""
    from datetime import datetime
    from ..session_engine.sessions import ET
    from ..trade_journal.notion_sync import NotionError, _block, _replace_body
    if not _has(conn, "system_results"):
        return 0, []
    _pages_schema(conn)
    pages = system_pages(conn)
    n, errors = 0, []
    for system, name in SYSTEM_NAMES.items():
        if name not in pages:
            continue
        tested = conn.execute("SELECT MAX(tested_at) FROM system_results WHERE system=?", (system,)).fetchone()[0]
        day = f"{datetime.fromtimestamp(tested, ET):%Y-%m-%d}"
        prev = conn.execute("SELECT page, synced_at FROM research_log_pages WHERE day=? AND strategy=?",
                            (day, name)).fetchone()
        if prev and prev[1] and prev[1] >= tested:
            continue                                       # already shows these results
        sp = pages[name]
        body = [_block("paragraph", x) for x in system_lines_of(name, sp)]
        props = _system_props(day, name, {}, sp)
        try:
            if prev:
                client.call("PATCH", f"/pages/{prev[0]}", {"properties": props})
                _replace_body(client, prev[0], body)
                conn.execute("UPDATE research_log_pages SET synced_at=? WHERE day=? AND strategy=?", (now, day, name))
            else:
                pid = client.call("POST", "/pages", {"parent": {"database_id": database_id},
                                                     "properties": props, "children": body})["id"]
                conn.execute("INSERT INTO research_log_pages (day, strategy, page, synced_at) VALUES (?,?,?,?)",
                             (day, name, pid, now))
            conn.commit()
            n += 1
        except NotionError as e:
            errors.append(f"{name}: {e}")
    return n, errors
