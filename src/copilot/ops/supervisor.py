"""Watchdog for the monitoring loop (spec Phase 16).

Runs `python -u -m copilot run` as a child process, writes its output to a size-rotated log, and restarts it if it
exits unexpectedly (with back-off). Every unexpected exit is reported over Telegram (HEALTH, never muted) with the last
log lines. A clean exit (code 0, e.g. Ctrl+C) ends supervision. The supervisor itself never touches the broker.
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections import deque
from pathlib import Path

MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
BACKOFF = (10, 30, 60, 120, 300)        # seconds before restart #1, #2, ...
STABLE_SECONDS = 600                    # a child that ran this long resets the back-off
CRASH_STORM = (5, 1800)                 # >= 5 unexpected exits within 30 min -> one extra alert


class RotatingLog:
    def __init__(self, path: Path, max_bytes: int = MAX_LOG_BYTES, backups: int = LOG_BACKUPS):
        self.path, self.max_bytes, self.backups = path, max_bytes, backups
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")

    def write(self, line: str) -> None:
        self.f.write(line if line.endswith("\n") else line + "\n")
        self.f.flush()
        if self.f.tell() >= self.max_bytes:
            self.rotate()

    def rotate(self) -> None:
        self.f.close()
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self.f = open(self.path, "a", encoding="utf-8")

    def close(self) -> None:
        self.f.close()


def backoff(restarts_in_row: int) -> int:
    return BACKOFF[min(restarts_in_row, len(BACKOFF) - 1)]


def crash_storm(exits: list[int], now: int) -> bool:
    n, window = CRASH_STORM
    return sum(1 for t in exits if now - t <= window) >= n


def supervise(src_dir: Path, log_path: Path, notify, child_args: list[str] | None = None,
              max_restarts: int | None = None, sleep=time.sleep) -> int:
    """notify(key, text): best-effort alert. Returns the child's final exit code."""
    log = RotatingLog(log_path)
    args = [sys.executable, "-u", "-m", "copilot", "run"] + (child_args or [])
    in_row, exits, storm_alerted, restarts = 0, [], False, 0
    try:
        while True:
            started = time.time()
            log.write(f"[supervisor] starting: {' '.join(args[1:])}")
            p = subprocess.Popen(args, cwd=src_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", bufsize=1)
            tail: deque[str] = deque(maxlen=8)
            for line in p.stdout:
                log.write(line.rstrip("\n"))
                tail.append(line.rstrip("\n"))
            code = p.wait()
            code = code - 2 ** 32 if code >= 2 ** 31 else code      # Windows reports -1 as 4294967295
            ran = time.time() - started
            if code == 0:
                log.write("[supervisor] monitor exited cleanly -> supervision ends")
                return 0
            now = int(time.time())
            exits.append(now)
            in_row = 0 if ran >= STABLE_SECONDS else in_row
            wait = backoff(in_row)
            in_row += 1
            log.write(f"[supervisor] monitor exited with code {code} after {ran:.0f}s -> restart in {wait}s")
            notify(f"HEALTH:crash:{now}",
                   f"監控程式當掉了（結束碼 {code}，跑了 {ran / 60:.0f} 分鐘），{wait} 秒後自動重啟。\n"
                   "重啟前暫停提醒和交易追蹤。\n\n最後幾行紀錄：\n" + "\n".join(tail))
            if crash_storm(exits, now) and not storm_alerted:
                storm_alerted = True
                notify(f"HEALTH:crash_storm:{now}",
                       f"監控程式一直當掉（{CRASH_STORM[1] // 60} 分鐘內 {CRASH_STORM[0]} 次以上）。會繼續自動重試，"
                       "但需要檢查（紀錄在 data\\logs\\monitor.log），請告訴 Claude。")
            restarts += 1
            if max_restarts is not None and restarts > max_restarts:
                return code
            sleep(wait)
    finally:
        log.close()


def telegram_notifier(db_path: Path):
    """Alerts through the same outbox as the monitor (recorded even when Telegram is unreachable)."""
    from ..database.store import connect
    from ..notifications.telegram import Outbox, TelegramSender

    def notify(key: str, text: str) -> None:
        try:
            try:
                sender = TelegramSender()
            except RuntimeError:
                sender = None
            with connect(db_path) as conn:
                Outbox(conn, sender).post("HEALTH", key, text, int(time.time()))
        except Exception as e:                       # alerting must never kill the watchdog
            print(f"[supervisor] could not send alert: {e!r}", file=sys.stderr)
    return notify
