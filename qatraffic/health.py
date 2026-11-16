"""Liveness: heartbeat (DB row + state/heartbeat.json), systemd notify/watchdog, downtime windows,
and an optional external dead-man's-switch ping (QA_TRAFFIC_HEALTHCHECK_URL, off by default)."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        self._httpd = None

    def serve_http(self):
        """Start a tiny HTTP liveness endpoint in a daemon thread (off unless QA_TRAFFIC_HEALTH_PORT).

        A GET to any path returns 200 while the loop is making progress and 503 once `last_ok` is
        stale -- i.e. the loop is wedged even though the process (and the systemd watchdog, if any)
        still appear up. On fly.io, where there is no systemd watchdog, wiring fly.toml's [[checks]]
        at this port is what restarts a frozen Machine. Bound to 0.0.0.0 for the platform prober.
        """
        port = getattr(self.cfg, "health_port", 0)
        if not port:
            return None
        max_stale = getattr(self.cfg, "health_max_stale_seconds", 600.0)
        health = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # keep the prober out of the logs
                pass

            def do_GET(self):
                now = time.time()
                # Before the first OK beat, allow a startup grace measured from process start.
                ref = health.last_ok if health.last_ok is not None else health.started
                age = now - ref
                ok = age < max_stale
                body = json.dumps({
                    "ok": ok, "last_ok_age_s": round(age, 1), "max_stale_s": max_stale,
                    "pid": os.getpid(), "uptime_s": round(now - health.started, 1),
                }).encode()
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_HEAD = do_GET

        try:
            self._httpd = ThreadingHTTPServer(("0.0.0.0", int(port)), Handler)
        except OSError as e:
            self.log.error("health.http_bind_failed", port=port, error=str(e))
            return None
        threading.Thread(target=self._httpd.serve_forever, name="health-http", daemon=True).start()
        self.log.info("health.http_serving", port=int(port), max_stale_s=max_stale)
        return self._httpd

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
