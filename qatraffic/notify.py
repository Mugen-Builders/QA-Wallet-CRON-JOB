"""Discord delivery through an incoming webhook, via a persistent outbox.

Messages are queued in SQLite first and delivered with retries, so a Discord outage or a restart
loses nothing. 429 responses honour retry_after. The webhook URL (its token is in the path) is
never logged; failures are logged as "discord" plus the HTTP status.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

COLORS = {"critical": 0xD32F2F, "error": 0xE64A19, "warning": 0xF9A825, "info": 0x2E7D32, "report": 0x1565C0,
          "lifecycle": 0x546E7A}
TAG = {"critical": "CRITICAL", "error": "ERROR", "warning": "WARNING", "info": "OK"}


def _ts(t=None) -> str:
    return datetime.fromtimestamp(t or time.time(), timezone.utc).isoformat()


class Outbox:
    MAX_ATTEMPTS = 30

    def __init__(self, db, log, webhook_url: str | None, prefix: str):
        self.db, self.log = db, log
        self.url = webhook_url
        self.prefix = prefix
        self.session = requests.Session()
        self.last_error: str | None = None
        self.consecutive_failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _enqueue(self, kind: str, body: dict, attachment: tuple[str, str] | None = None):
        self.db.x("INSERT INTO outbox(created_at,kind,payload,attachment_name,attachment,next_attempt_at) VALUES(?,?,?,?,?,?)",
                  (time.time(), kind, json.dumps(body), attachment[0] if attachment else None,
                   attachment[1] if attachment else None, time.time()))

    # ---- message builders ----
    def alerts(self, items: list[dict]):
        embeds = []
        for a in items:
            sev = a["severity"]
            label = {"open": TAG.get(sev, sev.upper()), "repeat": f"STILL {TAG.get(sev, sev.upper())}",
                     "escalated": f"ESCALATED {TAG.get(sev, sev.upper())}", "resolved": "RESOLVED"}[a["state"]]
            embeds.append({"title": f"{label} · {a['title']}"[:256], "description": a["body"][:3900],
                           "color": COLORS.get("info" if a["state"] == "resolved" else sev, COLORS["info"]),
                           "footer": {"text": a["key"][:200]}, "timestamp": _ts()})
        worst = max((a["severity"] for a in items if a["state"] != "resolved"), default="info",
                    key=lambda s: ["info", "warning", "error", "critical"].index(s) if s in ("info", "warning", "error", "critical") else 0)
        content = self.prefix + (" alert" if worst != "info" else " update")
        self._enqueue("alert", {"content": content, "embeds": embeds, "allowed_mentions": {"parse": []}})

    def lifecycle(self, title: str, body: str, fields: list[tuple[str, str]] | None = None):
        embed = {"title": title[:256], "description": body[:3900], "color": COLORS["lifecycle"], "timestamp": _ts()}
        if fields:
            embed["fields"] = [{"name": k[:256], "value": (v or "-")[:1024], "inline": True} for k, v in fields[:25]]
        self._enqueue("lifecycle", {"content": self.prefix, "embeds": [embed], "allowed_mentions": {"parse": []}})

    def report(self, title: str, description: str, fields: list[tuple[str, str, bool]], attachment: tuple[str, str] | None):
        embed = {"title": title[:256], "description": description[:3900], "color": COLORS["report"], "timestamp": _ts(),
                 "fields": [{"name": k[:256], "value": (v or "-")[:1024], "inline": inline} for k, v, inline in fields[:25]]}
        self._enqueue("report", {"content": f"{self.prefix} daily report", "embeds": [embed],
                                 "allowed_mentions": {"parse": []}}, attachment)

    # ---- staleness pruning ----
    @staticmethod
    def _is_resolved_embed(e: dict) -> bool:
        # outbox.alerts() always prefixes a resolve embed's title with "RESOLVED ·".
        return str(e.get("title", "")).startswith("RESOLVED ·")

    @staticmethod
    def _embed_key(e: dict) -> str | None:
        # outbox.alerts() stores the alert key verbatim in the embed footer.
        return (e.get("footer") or {}).get("text")

    def _prune_stale(self, now: float):
        """Drop queued *firing* alert embeds that are no longer worth delivering, so reconnecting
        after an outage (or a restart off a persistent disk) does not replay hours of stale alerts:
          (a) the alert has since resolved — the firing message is moot; its separate "resolved"
              message still goes out, so the Discord timeline closes cleanly;
          (b) a newer queued firing message for the same key exists — collapse the
              open/repeat/escalated ladder down to its latest state (one message per key).
        "RESOLVED ·" embeds are always kept. A row left with no embeds is marked sent (dropped)
        without posting; a partially-pruned row keeps its surviving embeds and has its header
        ("alert" vs "update") recomputed. Non-alert rows (lifecycle, report) are untouched."""
        rows = self.db.q("SELECT id, created_at, payload FROM outbox "
                         "WHERE sent_at IS NULL AND kind='alert' AND attempts < ? ORDER BY id",
                         (self.MAX_ATTEMPTS,))
        if not rows:
            return
        resolved = {r["key"] for r in self.db.q("SELECT key FROM alerts WHERE resolved_at IS NOT NULL")}
        # newest row id carrying a live firing embed for each still-open key
        newest_firing: dict[str, int] = {}
        parsed = []
        for r in rows:
            try:
                body = json.loads(r["payload"])
            except (ValueError, TypeError):
                continue
            embeds = body.get("embeds") or []
            parsed.append((r, body, embeds))
            for e in embeds:
                if self._is_resolved_embed(e):
                    continue
                k = self._embed_key(e)
                if k and k not in resolved:
                    newest_firing[k] = r["id"]
        dropped = collapsed = 0
        for r, body, embeds in parsed:
            kept = []
            for e in embeds:
                if self._is_resolved_embed(e):
                    kept.append(e)
                    continue
                k = self._embed_key(e)
                if k is None:  # unclassifiable — keep it, a dupe beats a lost alert
                    kept.append(e)
                    continue
                if k in resolved:  # alert resolved while this waited -> moot
                    dropped += 1
                    continue
                if newest_firing.get(k) != r["id"]:  # a fresher firing message exists -> collapse
                    collapsed += 1
                    continue
                kept.append(e)
            if len(kept) == len(embeds):
                continue
            if not kept:
                self.db.x("UPDATE outbox SET sent_at=?, last_error=? WHERE id=?",
                          (now, "superseded: stale alerts pruned on drain", r["id"]))
            else:
                has_firing = any(not self._is_resolved_embed(e) for e in kept)
                body = dict(body, embeds=kept, content=self.prefix + (" alert" if has_firing else " update"))
                self.db.x("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(body), r["id"]))
        if dropped or collapsed:
            self.log.info("discord.pruned_stale", dropped=dropped, collapsed=collapsed)

    # ---- delivery ----
    def flush(self, max_messages: int = 5):
        if not self.url:
            return
        now = time.time()
        self._prune_stale(now)
        rows = self.db.q("SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < ? AND next_attempt_at <= ? ORDER BY id LIMIT ?",
                         (self.MAX_ATTEMPTS, now, max_messages))
        for r in rows:
            ok, retry_after, err = self._post(json.loads(r["payload"]), r["attachment_name"], r["attachment"])
            if ok:
                self.db.x("UPDATE outbox SET sent_at=?, attempts=attempts+1 WHERE id=?", (time.time(), r["id"]))
                self.db.record_rpc("discord", True)
                self.consecutive_failures = 0
                continue
            self.consecutive_failures += 1
            self.last_error = err
            self.db.record_rpc("discord", False, err)
            backoff = retry_after if retry_after is not None else min(1800, 30 * 2 ** min(r["attempts"], 6))
            self.db.x("UPDATE outbox SET attempts=attempts+1, next_attempt_at=?, last_error=? WHERE id=?",
                      (time.time() + backoff, err, r["id"]))
            self.log.warn("discord.send_failed", error=err, attempt=r["attempts"] + 1)
            break  # keep ordering; try again later

    def _post(self, body: dict, att_name, att_text):
        try:
            if att_name:
                files = {"files[0]": (att_name, att_text.encode(), "application/json")}
                body = dict(body, attachments=[{"id": 0, "filename": att_name}])
                r = self.session.post(self.url, data={"payload_json": json.dumps(body)}, files=files, timeout=20)
            else:
                r = self.session.post(self.url, json=body, timeout=20)
        except requests.exceptions.RequestException as e:
            return False, None, f"discord {type(e).__name__}"
        if r.status_code in (200, 204):
            return True, None, None
        if r.status_code == 429:
            try:
                ra = float(r.json().get("retry_after", 5))
            except ValueError:
                ra = 5.0
            return False, ra + 0.5, "discord 429"
        return False, None, f"discord http {r.status_code}"

    def pending_count(self) -> int:
        return self.db.q1("SELECT COUNT(*) AS n FROM outbox WHERE sent_at IS NULL AND attempts < ?", (self.MAX_ATTEMPTS,))["n"]
