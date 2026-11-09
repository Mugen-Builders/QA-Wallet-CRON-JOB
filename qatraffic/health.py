"""Liveness: heartbeat (DB row + state/heartbeat.json), systemd notify/watchdog, downtime windows,
and an optional external dead-man's-switch ping (QA_TRAFFIC_HEALTHCHECK_URL, off by default)."""
from __future__ import annotations

import json
import os
import socket
import time

import requests


def sd_notify(msg: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False


class Health:
    PING_EVERY_S = 300

    def __init__(self, cfg, db, log):
        self.cfg, self.db, self.log = cfg, db, log
        self.started = time.time()
        self._last_ping = 0.0
        self.last_ok = None

    def startup_gap(self) -> float | None:
        """Seconds since the previous process's last heartbeat (None on first start). Records a
        downtime window when the gap is longer than a few loop intervals."""
        row = self.db.q1("SELECT * FROM heartbeats WHERE id=1")
        now = time.time()
        gap = None
        if row:
            gap = now - row["last_beat"]
            if gap > max(120.0, 6 * self.cfg.loop_seconds):
                self.db.x("INSERT INTO downtime(start,end,seconds,cause) VALUES(?,?,?,?)",
                          (row["last_beat"], now, gap, "process not running"))
        self.db.x("INSERT INTO heartbeats(id,started_at,last_beat,last_ok,pid) VALUES(1,?,?,NULL,?) "
                  "ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at, last_beat=excluded.last_beat, pid=excluded.pid",
                  (now, now, os.getpid()))
        return gap

    def beat(self, ok: bool, status: dict):
        now = time.time()
        if ok:
            self.last_ok = now
        self.db.x("UPDATE heartbeats SET last_beat=?, last_ok=COALESCE(?, last_ok) WHERE id=1", (now, now if ok else None))
        doc = dict(status, last_beat=now, last_ok=self.last_ok, pid=os.getpid(), started_at=self.started)
        path = self.cfg.heartbeat_path
        tmp = path.with_suffix(".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        os.replace(tmp, path)
        sd_notify("WATCHDOG=1\nSTATUS=" + status.get("summary", "running")[:200])
        if self.cfg.healthcheck_url and ok and now - self._last_ping > self.PING_EVERY_S:
            self._last_ping = now
            try:
                requests.get(self.cfg.healthcheck_url, timeout=10)
            except requests.exceptions.RequestException:
                self.log.warn("healthcheck.ping_failed")

    def uptime_share(self, start: float, end: float) -> float:
        down = 0.0
        for r in self.db.q("SELECT start, end FROM downtime WHERE end>=? AND start<=?", (start, end)):
            down += min(end, r["end"]) - max(start, r["start"])
        span = max(1.0, end - start)
        return max(0.0, 1 - down / span)
