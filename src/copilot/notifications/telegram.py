"""Telegram delivery with a local outbox (every message is recorded before sending; failures are kept).

Token: environment variable COPILOT_TELEGRAM_TOKEN only (never stored in files or the DB).
Chat id: COPILOT_TELEGRAM_CHAT_ID, or discovered once with `python -m copilot telegram-setup` after the user
has messaged their bot.
TLS: an explicit ssl.create_default_context() (a non-ASCII home path can break path-based CA loading on Windows).
"""
from __future__ import annotations

import json
import os
import sqlite3
import ssl
import urllib.error
import urllib.request
from typing import Protocol

API = "https://api.telegram.org/bot{token}/{method}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, kind TEXT NOT NULL, key TEXT,
    text TEXT NOT NULL, status TEXT NOT NULL, sent_at INTEGER, error TEXT
);
"""


class Sender(Protocol):
    def send(self, text: str) -> None: ...


def tls_context() -> ssl.SSLContext:
    """Verified TLS. Windows installs root CAs lazily, so Python's view of the system store can miss a root
    (observed: api.telegram.org's GoDaddy G2 root -> CERTIFICATE_VERIFY_FAILED). Add Mozilla's roots from
    certifi, passed as DATA (cadata), not as a file path -- OpenSSL file loading can break on a non-ASCII
    home directory. Verification is never disabled."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        with open(certifi.where(), encoding="ascii") as f:
            ctx.load_verify_locations(cadata=f.read())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return ctx


def _user_env(name: str) -> str | None:
    """Process env first; on Windows also the user-scope registry value, so a variable set after this
    process started (scripts/set_telegram_token.ps1) works without restarting anything."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0] or None
    except (ImportError, OSError):
        return None


class TelegramSender:
    def __init__(self, token: str | None = None, chat_id: str | None = None, timeout: float = 10.0):
        self.token = token or _user_env("COPILOT_TELEGRAM_TOKEN")
        self.chat_id = chat_id or _user_env("COPILOT_TELEGRAM_CHAT_ID")
        self.timeout = timeout
        self.ctx = tls_context()
        if not self.token:
            raise RuntimeError("COPILOT_TELEGRAM_TOKEN is not set")

    def _call(self, method: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(API.format(token=self.token, method=method),
                                     data=json.dumps(payload or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
        if not body.get("ok"):
            # never echo the token: errors only carry Telegram's description
            raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
        return body["result"]

    def send(self, text: str) -> None:
        if not self.chat_id:
            raise RuntimeError("COPILOT_TELEGRAM_CHAT_ID is not set (run telegram-setup)")
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True})

    def recent_chats(self) -> list[dict]:
        seen = {}
        for u in self._call("getUpdates"):
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
            if chat:
                seen[chat["id"]] = {"id": chat["id"], "type": chat.get("type"),
                                    "name": chat.get("username") or chat.get("title") or chat.get("first_name")}
        return list(seen.values())


class MockSender:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("mock failure")
        self.sent.append(text)


class Outbox:
    def __init__(self, conn: sqlite3.Connection, sender: Sender | None):
        self.conn, self.sender = conn, sender
        conn.executescript(SCHEMA)

    def post(self, kind: str, key: str | None, text: str, now: int) -> str:
        """Record, then try to send. Returns the final status. No sender -> DRY_RUN (recorded, not sent)."""
        cur = self.conn.execute("INSERT INTO outbox (created_at, kind, key, text, status) VALUES (?,?,?,?,?)",
                                (now, kind, key, text, "PENDING"))
        oid = cur.lastrowid
        from .commands import NEVER_MUTED, muted_until
        if self.sender is None:
            status, err = "DRY_RUN", None
        elif kind not in NEVER_MUTED and muted_until(self.conn) > now:
            status, err = "MUTED", None                  # recorded, not sent (/mute)
        else:
            try:
                self.sender.send(text)
                status, err = "SENT", None
            except Exception as e:                       # delivery problems must never crash monitoring
                status, err = "FAILED", str(e)[:300]
        self.conn.execute("UPDATE outbox SET status=?, sent_at=?, error=? WHERE id=?",
                          (status, now if status == "SENT" else None, err, oid))
        self.conn.commit()
        return status

    def retry_failed(self, now: int, max_age: int = 900) -> int:
        """Re-send FAILED messages younger than max_age seconds (stale alerts are not worth sending late)."""
        if self.sender is None:
            return 0
        n = 0
        for row in self.conn.execute("SELECT id, text FROM outbox WHERE status='FAILED' AND created_at >= ?",
                                     (now - max_age,)).fetchall():
            try:
                self.sender.send(row[1])
                self.conn.execute("UPDATE outbox SET status='SENT', sent_at=?, error=NULL WHERE id=?", (now, row[0]))
                n += 1
            except Exception as e:
                self.conn.execute("UPDATE outbox SET error=? WHERE id=?", (str(e)[:300], row[0]))
        self.conn.commit()
        return n
