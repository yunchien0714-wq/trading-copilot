"""Notion trade journal (spec Phase 9 journal destination): one page per graded trade in the user's 交易日誌 database.

* Written by the monitor after each grade / re-grade, with the Notion API and an internal-integration token stored in
  the user's environment (COPILOT_NOTION_TOKEN, set by scripts/SET_NOTION_TOKEN.cmd -- never in code or chat).
* Only system properties are written. The user's own fields (進場理由, 情緒) are never sent, so they are never
  overwritten. The page body is system-generated and replaced on re-grade (the page says so at the top).
* Read-only towards the broker like everything else; this module only talks to api.notion.com.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime

from ..notifications.telegram import _user_env, tls_context
from ..session_engine.sessions import ET
from ..trade_monitor.monitor import root_of
from . import zh

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TOKEN_ENV = "COPILOT_NOTION_TOKEN"
SYNC_VERSION = "notion_journal-0.1.0"
USER_FIELDS = ("進場理由", "情緒")         # never written by the program

SCHEMA = """
CREATE TABLE IF NOT EXISTS notion_sync (
    trade_id INTEGER PRIMARY KEY, page_id TEXT NOT NULL, graded_at INTEGER NOT NULL, synced_at INTEGER NOT NULL
);
"""


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str | None = None, timeout: int = 20):
        self.token = token or _user_env(TOKEN_ENV)
        if not self.token:
            raise NotionError(f"{TOKEN_ENV} is not set (run scripts\\SET_NOTION_TOKEN.cmd)")
        self.timeout, self.ctx = timeout, tls_context()

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(4):
            req = urllib.request.Request(API + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self.token}",
                                                  "Notion-Version": NOTION_VERSION,
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503) and attempt < 3:
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise NotionError(f"{method} {path}: HTTP {e.code} {detail}") from None
            except urllib.error.URLError as e:
                raise NotionError(f"{method} {path}: {e.reason}") from None
        raise NotionError(f"{method} {path}: retries exhausted")


# ---------------------------------------------------------------- page content
def _text(s: str) -> list[dict]:
    return [{"type": "text", "text": {"content": s[:2000]}}]


def _num(x) -> dict:
    return {"number": None if x is None else round(float(x), 4)}


def _sel(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def properties(t: sqlite3.Row, g: sqlite3.Row, labels: dict | None = None) -> dict:
    m = json.loads(g["metrics"])
    root = root_of(t["symbol"]) or t["symbol"]
    et = datetime.fromtimestamp(t["entry_time"], ET)
    side = zh.DIRECTION.get(t["direction"], t["direction"])
    r = t["r_multiple"] if t["r_multiple"] is not None else m.get("r")
    return {
        "交易": {"title": _text(f"#{t['id']} {root} {side} {et:%m-%d %H:%M} ET")},
        "交易編號": _num(t["id"]),
        "進場時間": {"date": {"start": et.isoformat()}},
        "商品": _sel(root),
        "方向": _sel(side),
        "口數": _num(t["qty"]),
        "建議口數": _num(m.get("recommended_contracts")),
        "進場": _num(t["entry"]),
        "出場": _num(t["exit"]),
        "止損": _num(t["initial_stop"]),
        "目標": _num(t["initial_target"]),
        "損益": _num(t["pnl_usd"]),
        "R": _num(r),
        "計畫RR": _num(m.get("planned_rr")),
        "評級": _sel(g["grade"]),
        "暫定": {"checkbox": bool(g["provisional"])},
        "品質": _sel(zh.QUALITY.get(g["quality"])),
        "結果": _sel(zh.OUTCOME.get(g["outcome"], "未知")),
        "時段": _sel(zh.KILLZONES.get(m.get("killzone") or "none", m.get("killzone"))),
        "5m結構": _sel(zh.SMC.get(m.get("smc_5m"), "未知")),
        "主要教訓": {"rich_text": _text(zh.sentence(g["lesson"]))},
        "評級版本": {"rich_text": _text(f"{g['version']} / {SYNC_VERSION}")},
        "帳戶": _sel((labels or {}).get(t["account"], t["account"]) if t["account"] else None),
    }


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _text(text)}}


def blocks(t: sqlite3.Row, g: sqlite3.Row) -> list[dict]:
    m = json.loads(g["metrics"])
    out = [{"object": "block", "type": "callout",
            "callout": {"rich_text": _text("以下內容由系統產生，重新評級時會整段覆蓋。你的筆記請寫在「進場理由」「情緒」欄位，"
                                           "那兩欄程式不會動。"), "icon": {"type": "emoji", "emoji": "🤖"}}},
           _block("heading_2", "檢查項目")]
    for cat, name, status, detail in json.loads(g["checks"]):
        out.append(_block("bulleted_list_item", f"{zh.STATUS.get(status, status)}｜{zh.CATEGORIES.get(cat, cat)}｜"
                                                f"{zh.check_name(name)}：{detail}"))
    went = json.loads(g["went_well"])
    if went:
        out.append(_block("heading_2", "做得好"))
        out += [_block("bulleted_list_item", x) for x in went]
    imp = json.loads(g["improve"])
    out.append(_block("heading_2", "可改進"))
    out += [_block("bulleted_list_item", zh.sentence(x)) for x in imp] or [_block("paragraph", "沒有。")]
    out.append(_block("heading_2", "數據"))
    facts = [("分類", zh.CLASSIFICATION.get(g["classification"], g["classification"])), ("出場方式", zh.EXIT.get(m.get("exit_type"), m.get("exit_type"))),
             ("風險金額", None if m.get("risk_usd") is None else f"${m['risk_usd']:.2f}"),
             ("結果點數", m.get("result_pts")), ("最大有利 MFE", m.get("mfe_pts") and f"{m['mfe_pts']} 點"
                                                  + (f"（{m['mfe_r']}R）" if m.get("mfe_r") is not None else "")),
             ("最大不利 MAE", m.get("mae_pts") and f"{m['mae_pts']} 點"),
             ("出場依據", t["exit_source"] if "exit_source" in t.keys() else None)]
    out += [_block("bulleted_list_item", f"{k}：{'UNKNOWN' if v is None else v}") for k, v in facts]
    return out


# ---------------------------------------------------------------- sync
def _find_page(client: NotionClient, db: str, trade_id: int) -> str | None:
    res = client.call("POST", f"/databases/{db}/query",
                      {"filter": {"property": "交易編號", "number": {"equals": trade_id}}, "page_size": 1})
    return res["results"][0]["id"] if res.get("results") else None


def _replace_body(client: NotionClient, page_id: str, children: list[dict]) -> None:
    cursor = None
    while True:
        res = client.call("GET", f"/blocks/{page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
        for b in res.get("results", []):
            client.call("DELETE", f"/blocks/{b['id']}")
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    client.call("PATCH", f"/blocks/{page_id}/children", {"children": children})


def pending(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, sqlite3.Row, str | None]]:
    conn.executescript(SCHEMA)
    prev, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        rows = conn.execute("""SELECT g.*, s.page_id FROM trade_grades g LEFT JOIN notion_sync s ON s.trade_id = g.trade_id
                               WHERE s.trade_id IS NULL OR g.graded_at > s.graded_at ORDER BY g.trade_id""").fetchall()
        return [(conn.execute("SELECT * FROM trades WHERE id = ?", (g["trade_id"],)).fetchone(), g, g["page_id"])
                for g in rows]
    finally:
        conn.row_factory = prev


def sync(conn: sqlite3.Connection, client: NotionClient, database_id: str, now: int,
         labels: dict | None = None) -> tuple[int, list[str]]:
    """Create/update a page for every new or re-graded trade. Returns (pages written, errors)."""
    done, errors = 0, []
    for t, g, page_id in pending(conn):
        if t is None:
            continue
        try:
            props = properties(t, g, labels)
            assert not set(props) & set(USER_FIELDS)
            page_id = page_id or _find_page(client, database_id, t["id"])
            if page_id:
                client.call("PATCH", f"/pages/{page_id}", {"properties": props})
                _replace_body(client, page_id, blocks(t, g))
            else:
                page_id = client.call("POST", "/pages", {"parent": {"database_id": database_id},
                                                         "properties": props, "children": blocks(t, g)})["id"]
            conn.execute("INSERT OR REPLACE INTO notion_sync VALUES (?,?,?,?)", (t["id"], page_id, g["graded_at"], now))
            conn.commit()
            done += 1
        except NotionError as e:
            errors.append(f"trade #{t['id']}: {e}")
    return done, errors


# ---------------------------------------------------------------- user fields back (read-only)
NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_notes (
    trade_id INTEGER PRIMARY KEY, entry_reason TEXT, emotion TEXT, page_id TEXT NOT NULL,
    edited_at TEXT, pulled_at INTEGER NOT NULL
);
"""
REMIND_WINDOW = 86400            # only remind about trades that closed within the last 24 h


def page_url(page_id: str) -> str:
    return "https://www.notion.so/" + page_id.replace("-", "")


def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    return "".join(x.get("plain_text", "") for x in prop.get("rich_text", [])).strip()


def pull_notes(conn: sqlite3.Connection, client: NotionClient, database_id: str, now: int) -> int:
    """Copy the user's own fields (進場理由, 情緒) from Notion into trade_notes. Reads only; returns rows changed."""
    conn.executescript(NOTES_SCHEMA)
    changed, cursor = 0, None
    while True:
        res = client.call("POST", f"/databases/{database_id}/query",
                          {"page_size": 100, **({"start_cursor": cursor} if cursor else {})})
        for page in res.get("results", []):
            p = page.get("properties", {})
            tid = (p.get("交易編號") or {}).get("number")
            if tid is None:
                continue
            reason = _plain(p.get("進場理由")) or None
            emo = ((p.get("情緒") or {}).get("select") or {}).get("name")
            old = conn.execute("SELECT entry_reason, emotion FROM trade_notes WHERE trade_id = ?", (int(tid),)).fetchone()
            if old is None or tuple(old) != (reason, emo):
                changed += 1
            conn.execute("INSERT OR REPLACE INTO trade_notes VALUES (?,?,?,?,?,?)",
                         (int(tid), reason, emo, page["id"], page.get("last_edited_time"), now))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    conn.commit()
    return changed


def unfilled(conn: sqlite3.Connection, since: int) -> list[tuple[int, str, str]]:
    """(trade_id, title, url) of journal pages whose 進場理由 and 情緒 are both still empty, for trades closed
    since `since`."""
    conn.executescript(SCHEMA + NOTES_SCHEMA)
    rows = conn.execute("""SELECT t.id, t.symbol, t.direction, t.entry_time, s.page_id FROM notion_sync s
                           JOIN trades t ON t.id = s.trade_id LEFT JOIN trade_notes n ON n.trade_id = t.id
                           WHERE COALESCE(t.exit_time, t.entry_time) >= ?
                             AND n.entry_reason IS NULL AND n.emotion IS NULL ORDER BY t.id""", (since,)).fetchall()
    return [(r[0], f"#{r[0]} {root_of(r[1]) or r[1]} {zh.DIRECTION.get(r[2], r[2])} "
                   f"{datetime.fromtimestamp(r[3], ET):%m-%d %H:%M} ET", page_url(r[4])) for r in rows]


# ---------------------------------------------------------------- one-time setup
JOURNAL_TITLE = "交易日誌"
JOURNAL_PROPERTIES = {
    "交易": {"title": {}}, "交易編號": {"number": {}}, "進場時間": {"date": {}}, "商品": {"select": {}},
    "方向": {"select": {}}, "口數": {"number": {}}, "建議口數": {"number": {}}, "進場": {"number": {}},
    "出場": {"number": {}}, "止損": {"number": {}}, "目標": {"number": {}}, "損益": {"number": {"format": "dollar"}},
    "R": {"number": {}}, "計畫RR": {"number": {}}, "評級": {"select": {}}, "暫定": {"checkbox": {}},
    "品質": {"select": {}}, "結果": {"select": {}}, "時段": {"select": {}}, "5m結構": {"select": {}},
    "主要教訓": {"rich_text": {}}, "評級版本": {"rich_text": {}}, "帳戶": {"select": {}},
    # the trader's own fields -- read back, never written by the system
    "進場理由": {"rich_text": {}}, "情緒": {"select": {}},
}


def create_journal(client: NotionClient, parent_page: str) -> str:
    """Create the 交易日誌 database under a page the integration can access; returns its id."""
    return client.call("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page},
        "title": [{"type": "text", "text": {"content": JOURNAL_TITLE}}],
        "properties": JOURNAL_PROPERTIES})["id"]
