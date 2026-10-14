"""Release / configuration audit trail (spec Phase 16).

Every distinct combination of {component versions, code hash, config contents} is recorded once in `releases` with
what changed versus the previous one, so every alert and grade can be tied to the rules that produced it, and a
config edit (e.g. a risk rule) is never silent: the monitor reports it over Telegram.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tomllib
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY, at INTEGER NOT NULL, versions TEXT NOT NULL, code_sha TEXT NOT NULL,
    config_sha TEXT NOT NULL, config TEXT NOT NULL, changes TEXT NOT NULL
);
"""
CONFIG_FILES = ("account.toml", "account_state.toml")


def versions() -> dict[str, str]:
    from .. import FEATURE_VERSION
    from ..alert_tracking.outcomes import OUTCOME_VERSION
    from ..market_structure.analysis import ANALYSIS_VERSION
    from ..reports.dashboard import DASHBOARD_VERSION
    from ..risk_engine.gate import RISK_VERSION
    from ..setup_engine.poc_fvg import ENGINE_VERSION
    from ..trade_journal.grader import GRADER_VERSION
    from ..trade_monitor.monitor import TRADE_MONITOR_VERSION
    return {"market_data": FEATURE_VERSION, "analysis": ANALYSIS_VERSION, "setup_engine": ENGINE_VERSION,
            "risk_engine": RISK_VERSION, "trade_monitor": TRADE_MONITOR_VERSION, "grader": GRADER_VERSION,
            "alert_outcomes": OUTCOME_VERSION, "dashboard": DASHBOARD_VERSION}


def code_sha(src: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(src.rglob("*.py")):
        h.update(f.relative_to(src).as_posix().encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def load_config(config_dir: Path) -> dict[str, object]:
    """Flattened values of all config files ('file:key' -> value). A missing file is recorded as such."""
    out: dict[str, object] = {}
    for name in CONFIG_FILES:
        p = config_dir / name
        try:
            with open(p, "rb") as f:
                out.update({f"{name}:{k}": v for k, v in _flatten(tomllib.load(f)).items()})
        except FileNotFoundError:
            out[f"{name}"] = "MISSING"
        except tomllib.TOMLDecodeError as e:
            out[f"{name}"] = f"INVALID TOML: {e}"
    return out


def diff(old: dict, new: dict) -> list[str]:
    ch = []
    for k in sorted(set(old) | set(new)):
        if k not in old:
            ch.append(f"{k}: (new) -> {new[k]!r}")
        elif k not in new:
            ch.append(f"{k}: {old[k]!r} -> (removed)")
        elif old[k] != new[k]:
            ch.append(f"{k}: {old[k]!r} -> {new[k]!r}")
    return ch


def check(conn: sqlite3.Connection, src: Path, config_dir: Path, now: int,
          code: str | None = None) -> tuple[bool, list[str]]:
    """Record a new release row if anything changed. Returns (first_release, human-readable changes)."""
    conn.executescript(SCHEMA)
    v = versions()
    code = code or code_sha(src)
    cfg = json.loads(json.dumps(load_config(config_dir), default=str))   # datetimes -> str, comparable after reload
    cfg_sha = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]
    last = conn.execute("SELECT versions, code_sha, config_sha, config FROM releases ORDER BY id DESC LIMIT 1").fetchone()
    if last is not None and (json.loads(last[0]), last[1], last[2]) == (v, code, cfg_sha):
        return False, []
    if last is None:
        changes = ["baseline recorded"]
    else:
        changes = [f"version {k}: {a} -> {v.get(k)}" for k, a in json.loads(last[0]).items() if a != v.get(k)]
        changes += [f"version {k}: (new) {b}" for k, b in v.items() if k not in json.loads(last[0])]
        if code != last[1]:
            changes.append(f"code changed ({last[1]} -> {code})")
        changes += [f"config {c}" for c in diff(json.loads(last[3]), cfg)]
    conn.execute("INSERT INTO releases (at, versions, code_sha, config_sha, config, changes) VALUES (?,?,?,?,?,?)",
                 (now, json.dumps(v), code, cfg_sha, json.dumps(cfg), json.dumps(changes)))
    conn.commit()
    return last is None, changes
