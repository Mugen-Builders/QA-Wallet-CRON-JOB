"""Alerts with memory: open once, repeat while open (every ALERT_REPEAT_HOURS), close with a
"resolved" message. Everything raised in one loop iteration goes out as one Discord message."""
from __future__ import annotations

import time

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}


class Alerts:
    def __init__(self, db, log, outbox, repeat_hours: float):
        self.db, self.log, self.outbox = db, log, outbox
        self.repeat_s = repeat_hours * 3600
        self._pending: list[dict] = []

    def raise_(self, key: str, severity: str, title: str, body: str):
        now = time.time()
        row = self.db.q1("SELECT * FROM alerts WHERE key=?", (key,))
        if row is None or row["resolved_at"] is not None:
            self.db.x("INSERT INTO alerts(key,severity,title,body,opened_at,last_seen_at,last_sent_at,resolved_at,occurrences) "
                      "VALUES(?,?,?,?,?,?,?,NULL,1) ON CONFLICT(key) DO UPDATE SET severity=excluded.severity, "
                      "title=excluded.title, body=excluded.body, opened_at=excluded.opened_at, last_seen_at=excluded.last_seen_at, "
                      "last_sent_at=excluded.last_sent_at, resolved_at=NULL, occurrences=1",
                      (key, severity, title, body, now, now, now))
            self.db.x("INSERT INTO alert_history(key,severity,title,opened_at) VALUES(?,?,?,?)", (key, severity, title, now))
            self._pending.append({"state": "open", "key": key, "severity": severity, "title": title, "body": body})
            self.log.emit({"critical": "critical", "error": "error", "warning": "warn"}.get(severity, "info"),
                          "alert.open", title, key=key)
            return
        escalated = SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(row["severity"], 0)
        self.db.x("UPDATE alerts SET last_seen_at=?, occurrences=occurrences+1, body=?, severity=? WHERE key=?",
                  (now, body, severity if escalated else row["severity"], key))
        if escalated or (row["last_sent_at"] or 0) + self.repeat_s <= now:
            self.db.x("UPDATE alerts SET last_sent_at=? WHERE key=?", (now, key))
            age_h = (now - row["opened_at"]) / 3600
            self._pending.append({"state": "escalated" if escalated else "repeat", "key": key,
                                  "severity": severity if escalated else row["severity"], "title": title,
                                  "body": f"{body}\n(open for {age_h:.1f} h, seen {row['occurrences'] + 1} times)"})

    def resolve(self, key: str, note: str = ""):
        row = self.db.q1("SELECT * FROM alerts WHERE key=? AND resolved_at IS NULL", (key,))
        if row is None:
            return
        now = time.time()
        self.db.x("UPDATE alerts SET resolved_at=? WHERE key=?", (now, key))
        self.db.x("UPDATE alert_history SET resolved_at=? WHERE key=? AND resolved_at IS NULL", (now, key))
        dur = (now - row["opened_at"]) / 60
        self._pending.append({"state": "resolved", "key": key, "severity": "info", "title": f"Resolved: {row['title']}",
                              "body": f"{note}\n(open {dur:.0f} min)" if note else f"(open {dur:.0f} min)"})
        self.log.info("alert.resolved", row["title"], key=key, minutes=round(dur))

    def open_alerts(self):
        return self.db.q("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY opened_at")

    def flush(self):
        if not self._pending:
            return
        items, self._pending = self._pending, []
        items.sort(key=lambda a: -SEVERITY_ORDER.get(a["severity"], 0))
        for i in range(0, len(items), 10):  # Discord: at most 10 embeds per message
            self.outbox.alerts(items[i:i + 10])
