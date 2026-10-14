"""Indicator discovery + source retrieval (Phase 2).

Source comes from TradingView's pine-facade, fetched inside the page with the user's own session
(same endpoint the TradingView MCP uses for pine_open). Protected / invite-only scripts return no
readable source -- those are recorded as source_status=UNAVAILABLE, never reconstructed or guessed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DISCOVER_JS = """
(async function() {
  var ws = window.TradingViewApi._chartWidgetCollection.getAll();
  var seen = {}, out = [];
  for (var p = 0; p < ws.length; p++) {
    var srcs = ws[p].model().model().dataSources();
    for (var i = 0; i < srcs.length; i++) {
      var s = srcs[i];
      if (!s.metaInfo || !s.id || String(s.id()).indexOf('ESD$') === 0) continue;
      var mi = s.metaInfo();
      var key = mi.scriptIdPart || mi.id;
      if (seen[key]) { seen[key].panes.push(p); continue; }
      var o = {name: mi.description || mi.shortDescription, script_id: mi.scriptIdPart || null,
               pine_version: mi.pine ? mi.pine.version : null, digest: mi.pine ? mi.pine.digest : null,
               is_price_study: mi.is_price_study, panes: [p], http: null, source: null, fetch_error: null};
      seen[key] = o; out.push(o);
      if (!o.script_id) continue;
      try {
        var r = await fetch('https://pine-facade.tradingview.com/pine-facade/get/' +
                            encodeURIComponent(o.script_id) + '/' + o.pine_version, {credentials: 'include'});
        o.http = r.status;
        var t = await r.text();
        try { var d = JSON.parse(t); o.source = d.source || null; } catch (e) { o.fetch_error = 'non-JSON response'; }
      } catch (e) { o.fetch_error = String(e && e.message || e); }
    }
  }
  return out;
})()
"""

# Protected scripts are served as an encrypted/compiled blob rather than Pine text.
_PINE_MARKERS = ("//@version", "indicator(", "study(", "strategy(", "library(")


@dataclass
class IndicatorSource:
    name: str
    script_id: str | None
    pine_version: str | None
    digest: str | None
    is_price_study: bool | None
    panes: list[int]
    source_status: str          # AVAILABLE | UNAVAILABLE
    status_reason: str
    source_path: str | None
    source_sha256: str | None
    lines: int | None


def normalize_newlines(src: str) -> str:
    return src.replace("\r\n", "\n").replace("\r", "\n")


def classify(raw: dict) -> tuple[str, str]:
    src = raw.get("source")
    if raw.get("fetch_error"):
        return "UNAVAILABLE", f"fetch error: {raw['fetch_error']}"
    if raw.get("http") not in (200, None):
        return "UNAVAILABLE", f"HTTP {raw.get('http')}"
    if not src:
        return "UNAVAILABLE", "no source returned (likely protected or invite-only)"
    if not any(m in src for m in _PINE_MARKERS):
        return "UNAVAILABLE", "response is not readable Pine (likely protected/encrypted)"
    return "AVAILABLE", "readable Pine source"


def discover(evaluate_async: Callable[[str], list], out_dir: Path) -> list[IndicatorSource]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for raw in evaluate_async(DISCOVER_JS) or []:
        status, reason = classify(raw)
        path = sha = lines = None
        if status == "AVAILABLE":
            # TradingView serves CRLF; on Windows a plain write_text would turn that into CR CR LF and
            # double every line number in the analyzer output. Normalise, then write LF verbatim.
            src = normalize_newlines(raw["source"])
            sha = hashlib.sha256(src.encode("utf-8")).hexdigest()
            safe = "".join(c if c.isalnum() else "_" for c in raw["name"])[:60]
            path = out_dir / f"{safe}__{sha[:10]}.pine"
            path.write_text(src, encoding="utf-8", newline="\n")
            lines = src.count("\n") + 1
        results.append(IndicatorSource(raw["name"], raw.get("script_id"), raw.get("pine_version"),
                                       raw.get("digest"), raw.get("is_price_study"), raw.get("panes") or [],
                                       status, reason, str(path) if path else None, sha, lines))
    (out_dir / "index.json").write_text(json.dumps([r.__dict__ for r in results], indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    return results
