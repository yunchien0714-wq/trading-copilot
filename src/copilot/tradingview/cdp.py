"""Minimal Chrome DevTools Protocol client for TradingView Desktop (Electron).

Same mechanism the TradingView MCP uses: TradingView must be started with
--remote-debugging-port=9222. Only talks to localhost. Read-only use in Phase 1.
"""
from __future__ import annotations

import itertools
import json
import re
import urllib.request

from websockets.sync.client import connect


class TradingViewUnavailable(RuntimeError):
    pass


class EvaluationError(RuntimeError):
    pass


def _primary_layout() -> str | None:
    """The chart layout the monitor watches (config [tradingview].primary_layout). With several TradingView tabs
    open, /json/list order follows the user's tab activity -- without this the monitor could latch onto another
    instrument's tab (observed: an MNQ tab was opened for research data)."""
    import tomllib
    from pathlib import Path
    try:
        cfg = tomllib.loads((Path(__file__).resolve().parents[3] / "config" / "account.toml").read_text("utf-8"))
        return cfg.get("tradingview", {}).get("primary_layout") or None
    except (OSError, ValueError):
        return None


def chart_targets(host: str = "localhost", port: int = 9222, timeout: float = 3.0) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/list", timeout=timeout) as r:
            targets = json.load(r)
    except OSError as e:
        raise TradingViewUnavailable(
            f"No CDP endpoint on {host}:{port}. Start TradingView Desktop with --remote-debugging-port={port}."
        ) from e
    return [t for t in targets if t.get("type") == "page" and re.search(r"tradingview\.com/chart", t.get("url", ""), re.I)]


def find_chart_target(host: str = "localhost", port: int = 9222, timeout: float = 3.0,
                      layout: str | None = None) -> dict:
    pages = chart_targets(host, port, timeout)
    layout = layout or _primary_layout()
    if layout:
        for t in pages:
            if f"/chart/{layout}" in t.get("url", ""):
                return t
        raise TradingViewUnavailable(f"the main chart layout {layout} is not open in TradingView")
    if pages:
        return pages[0]
    raise TradingViewUnavailable("CDP is up but no TradingView chart page was found")


def secondary_targets(host: str = "localhost", port: int = 9222) -> list[dict]:
    """Chart tabs other than the main one: read for data only (no alerts, no drawings)."""
    layout = _primary_layout()
    if not layout:
        return []
    return [t for t in chart_targets(host, port) if f"/chart/{layout}" not in t.get("url", "")]


class CdpClient:
    def __init__(self, host: str = "localhost", port: int = 9222, timeout: float = 10.0, target: dict | None = None):
        self.host, self.port, self.timeout, self.target = host, port, timeout, target
        self._ws = None
        self._ids = itertools.count(1)

    def __enter__(self):
        target = self.target or find_chart_target(self.host, self.port)
        # TradingView pages can return large payloads (bars, pine graphics) -> raise the 1MB default.
        self._ws = connect(target["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024, open_timeout=self.timeout)
        return self

    def __exit__(self, *exc):
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def evaluate(self, expression: str, await_promise: bool = False):
        msg_id = next(self._ids)
        self._ws.send(json.dumps({
            "id": msg_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        }))
        while True:
            reply = json.loads(self._ws.recv(timeout=self.timeout))
            if reply.get("id") == msg_id:
                break
        if "error" in reply:
            raise EvaluationError(reply["error"].get("message", str(reply["error"])))
        result = reply["result"]
        if "exceptionDetails" in result:
            d = result["exceptionDetails"]
            raise EvaluationError((d.get("exception") or {}).get("description") or d.get("text") or "JS error")
        return result["result"].get("value")
