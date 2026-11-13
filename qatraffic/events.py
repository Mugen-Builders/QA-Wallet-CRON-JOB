"""Logging: one readable line per event on stdout (journald) and one JSON line per event in
logs/events-YYYY-MM-DD.jsonl (UTC day). Nothing secret is ever passed in: RPC endpoints are named
by role (l1, node, inspect, discord), never by URL, and keys never leave accounts.Wallet.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "critical": 50}
_URL_RE = re.compile(r"https?://[^\s\"']+")
_HEX_KEY_RE = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")


def scrub(text: str) -> str:
    """Belt and braces for free text (exception messages): strip URLs (RPC keys live in them) and
    any 32-byte hex blob (a private key and a tx hash have the same shape). Hashes the bot wants
    in the log are passed as structured fields, which are not scrubbed."""
    return _HEX_KEY_RE.sub("<hex32>", _URL_RE.sub("<url>", text))


class EventLog:
    def __init__(self, log_dir: Path, min_level: str = "info", stdout=None, retention_days: int = 14):
        self.dir = log_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.min = LEVELS.get(min_level, 20)
        self.out = stdout or sys.stdout
        self.retention_days = retention_days
        # Under systemd/journald the "<N>" priority prefix lets `journalctl -p` filter by level;
        # anywhere else (a plain container like fly.io, a terminal) it is just noise, so drop it.
        self.journald = bool(os.environ.get("JOURNAL_STREAM"))
        self.lock = threading.Lock()
        self._fh = None
        self._day = None
        self.counts: dict[str, int] = {}

    def _file(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            if self._fh:
                self._fh.close()
            path = self.dir / f"events-{day}.jsonl"
            self._fh = open(path, "a", buffering=1)
            os.chmod(path, 0o600)
            self._day = day
            self._prune()
        return self._fh

    def _prune(self):
        """Keep only the last `retention_days` daily files so logs do not grow without bound."""
        try:
            files = sorted(self.dir.glob("events-*.jsonl"))
        except OSError:
            return
        for p in files[:-self.retention_days] if self.retention_days > 0 else []:
            try:
                p.unlink()
            except OSError:
                pass

    def emit(self, level: str, event: str, msg: str = "", **fields):
        lv = LEVELS.get(level, 20)
        self.counts[level] = self.counts.get(level, 0) + 1
        if lv < self.min:
            return
        ts = time.time()
        rec = {"ts": round(ts, 3), "level": level, "event": event}
        if msg:
            rec["msg"] = scrub(msg)
        for k, v in fields.items():
            rec[k] = scrub(v) if isinstance(v, str) and k in ("error", "detail") else v
        line = json.dumps(rec, default=str, separators=(",", ":"))
        human = f"{level.upper():8} {event}" + (f" | {rec.get('msg')}" if msg else "")
        extra = " ".join(f"{k}={rec[k]}" for k in fields if k not in ("detail",))
        if extra:
            human += " | " + extra
        # systemd/journald priority prefix (<N>) so `journalctl -p warning` works; omitted elsewhere
        if self.journald:
            prio = {"debug": 7, "info": 6, "warn": 4, "error": 3, "critical": 2}.get(level, 6)
            human = f"<{prio}>{human}"
        with self.lock:
            try:
                self._file().write(line + "\n")
            except OSError:
                pass
            print(human, file=self.out, flush=True)

    def debug(self, event, msg="", **f):
        self.emit("debug", event, msg, **f)

    def info(self, event, msg="", **f):
        self.emit("info", event, msg, **f)

    def warn(self, event, msg="", **f):
        self.emit("warn", event, msg, **f)

    def error(self, event, msg="", **f):
        self.emit("error", event, msg, **f)

    def critical(self, event, msg="", **f):
        self.emit("critical", event, msg, **f)
