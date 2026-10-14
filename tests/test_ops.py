import json
import sqlite3
import sys
from pathlib import Path

import pytest

from copilot.ops.backup import backup
from copilot.ops.releases import check, diff
from copilot.ops.supervisor import RotatingLog, backoff, crash_storm, supervise

NOW = 1791230000


def test_backup_is_verified_and_pruned(tmp_path):
    src = sqlite3.connect(tmp_path / "live.sqlite3")
    src.execute("CREATE TABLE t (x)")
    src.execute("INSERT INTO t VALUES (42)")
    src.commit()
    for i in range(4):
        res = backup(src, tmp_path / "bk", NOW + i * 86400, keep=2)
        assert res.ok and res.detail == "integrity_check ok"
    copies = sorted((tmp_path / "bk").glob("copilot-*.sqlite3"))
    assert len(copies) == 2 and res.pruned == 1
    assert sqlite3.connect(copies[-1]).execute("SELECT x FROM t").fetchone() == (42,)
    assert not list((tmp_path / "bk").glob("*.partial"))


def _cfg(d: Path, body: str):
    d.mkdir(exist_ok=True)
    (d / "account.toml").write_text(body, encoding="utf-8")


def test_release_baseline_then_config_change_is_reported(tmp_path):
    conn = sqlite3.connect(":memory:")
    _cfg(tmp_path / "cfg", "[personal]\ncontracts_per_trade_min = 5\n")
    first, ch = check(conn, tmp_path, tmp_path / "cfg", NOW, code="abc")
    assert first and ch == ["baseline recorded"]
    assert check(conn, tmp_path, tmp_path / "cfg", NOW + 30, code="abc") == (False, [])     # unchanged -> nothing
    _cfg(tmp_path / "cfg", "[personal]\ncontracts_per_trade_min = 1\n")
    first, ch = check(conn, tmp_path, tmp_path / "cfg", NOW + 60, code="abc")
    assert not first and ch == ["config account.toml:personal.contracts_per_trade_min: 5 -> 1"]
    first, ch = check(conn, tmp_path, tmp_path / "cfg", NOW + 90, code="def")
    assert ch == ["code changed (abc -> def)"]
    assert conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 3
    assert "account_state.toml" in json.loads(conn.execute("SELECT config FROM releases").fetchone()[0])  # MISSING noted


def test_diff_reports_added_and_removed_keys():
    assert diff({"a": 1, "b": 2}, {"a": 1, "c": 3}) == ["b: 2 -> (removed)", "c: (new) -> 3"]


def test_rotating_log(tmp_path):
    log = RotatingLog(tmp_path / "m.log", max_bytes=50, backups=2)
    for i in range(20):
        log.write(f"line {i:02d} " + "x" * 10)
    log.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["m.log", "m.log.1", "m.log.2"]


def test_backoff_and_crash_storm():
    assert [backoff(i) for i in range(7)] == [10, 30, 60, 120, 300, 300, 300]
    assert crash_storm([NOW - 60 * i for i in range(5)], NOW)
    assert not crash_storm([NOW - 3600 * i for i in range(5)], NOW)


def _installed_as_package() -> bool:
    """With the project pip-installed, `python -m copilot` would start the REAL monitor instead of the fake child."""
    import importlib.metadata
    try:
        importlib.metadata.distribution("trading-copilot")
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


@pytest.mark.skipif(_installed_as_package(), reason="project is pip-installed; run from a plain checkout")
def test_supervisor_restarts_crashed_child_and_alerts(tmp_path, monkeypatch):
    # child = a tiny script that prints and exits 3; the watchdog must log it, alert, and restart
    (tmp_path / "copilot").mkdir()
    (tmp_path / "copilot" / "__main__.py").write_text("import sys\nprint('hello from child')\nsys.exit(3)\n")
    alerts = []
    code = supervise(tmp_path, tmp_path / "logs" / "m.log", lambda k, t: alerts.append((k, t)),
                     max_restarts=1, sleep=lambda s: None)
    assert code == 3 and len(alerts) == 2 and "結束碼 3" in alerts[0][1] and "hello from child" in alerts[0][1]
    text = (tmp_path / "logs" / "m.log").read_text(encoding="utf-8")
    assert text.count("hello from child") == 2 and "restart in 10s" in text


@pytest.mark.skipif(_installed_as_package(), reason="project is pip-installed; run from a plain checkout")
def test_supervisor_stops_on_clean_exit(tmp_path):
    (tmp_path / "copilot").mkdir()
    (tmp_path / "copilot" / "__main__.py").write_text("print('bye')\n")
    alerts = []
    assert supervise(tmp_path, tmp_path / "m.log", lambda k, t: alerts.append(k), sleep=lambda s: None) == 0
    assert alerts == []
