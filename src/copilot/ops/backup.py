"""Daily database backup (spec Phase 16): online copy with SQLite's backup API (safe while the monitor writes),
verified with PRAGMA integrity_check on the copy, oldest copies pruned. A failed check is reported, never hidden."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..session_engine.sessions import ET

KEEP = 14
PREFIX = "copilot-"


@dataclass
class BackupResult:
    path: Path | None
    ok: bool
    detail: str
    size_mb: float = 0.0
    pruned: int = 0


def backup(conn: sqlite3.Connection, dest_dir: Path, now: int, keep: int = KEEP) -> BackupResult:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{PREFIX}{datetime.fromtimestamp(now, ET):%Y%m%d-%H%M}.sqlite3"
    tmp = out.with_suffix(".partial")
    try:
        dst = sqlite3.connect(tmp)
        try:
            conn.backup(dst)
            res = dst.execute("PRAGMA integrity_check").fetchall()
        finally:
            dst.close()
        if res != [("ok",)]:
            return BackupResult(None, False, "integrity_check on the copy failed: "
                                + "; ".join(r[0] for r in res[:5]) + f" (kept at {tmp.name})")
        tmp.replace(out)
    except (sqlite3.Error, OSError) as e:
        return BackupResult(None, False, f"{type(e).__name__}: {e}")
    copies = sorted(dest_dir.glob(f"{PREFIX}*.sqlite3"))
    pruned = 0
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink()
        pruned += 1
    return BackupResult(out, True, "integrity_check ok", round(out.stat().st_size / 1e6, 1), pruned)
