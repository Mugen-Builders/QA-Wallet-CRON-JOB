"""Client for the Cartesi sequencer's HTTP API (README "API", v0.1.0-alpha.10).

Ingress (public, what a wallet uses): POST /tx, GET /fee, GET /nonce, GET /domain.
Egress (operator-only, optional): GET /history, GET /healthz, GET /readyz.

Errors carry one JSON shape {"ok": false, "code": ..., "message": ...}:
  400 BAD_REQUEST | INVALID_SIGNATURE   our bug (bad encoding or signature): never retried
  413 PAYLOAD_TOO_LARGE                 our bug
  422 EXECUTION_REJECTED                nonce / max_fee / fee balance; nothing was persisted
  429 OVERLOADED                        queue full: retry the same signed op later
  500 INTERNAL_ERROR, 503 UNAVAILABLE   outage: retry the same signed op later
A timeout is inconclusive: the op may be queued. The documented recovery is to look at
/nonce (above the op's nonce = included) and otherwise resubmit the same signed op.
"""
from __future__ import annotations

import re
import time

import requests

NONCE_RE = re.compile(r"bad nonce: expected (\d+), got (\d+)", re.I)


class SeqUnavailable(Exception):
    """Not delivered or not admitted (connection refused, 5xx, 429). Safe to resubmit the same op."""

    def __init__(self, msg, *, delivered: bool = False):
        super().__init__(msg)
        self.delivered = delivered  # True when the request may have reached the sequencer (timeout)


class SeqRejected(Exception):
    """A definitive refusal (4xx). `code` is the sequencer's error code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    @property
    def expected_nonce(self) -> int | None:
        m = NONCE_RE.search(self.message or "")
        return int(m.group(1)) if m else None

    @property
    def is_bug(self) -> bool:
        return self.status in (400, 413)


class Sequencer:
    def __init__(self, cfg, db, log):
        self.cfg, self.db, self.log = cfg, db, log
        self.url = cfg.sequencer_url.rstrip("/")
        self.egress = (cfg.sequencer_egress_url or "").rstrip("/") or None
        self.http = requests.Session()
        self.consecutive_failures = 0
        self.first_failure_at: float | None = None  # start of the current run of failures
        self.egress_failures = 0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None

    # ---- transport ----
    def _req(self, method, path, *, json_body=None, params=None, timeout=15.0, egress=False):
        base = self.egress if egress else self.url
        role = "sequencer_egress" if egress else "sequencer"
        try:
            r = self.http.request(method, base + path, json=json_body, params=params, timeout=timeout)
        except requests.exceptions.Timeout as e:
            self._fail(role, "timeout", egress)
            raise SeqUnavailable("timeout", delivered=method == "POST") from e
        except requests.exceptions.RequestException as e:
            self._fail(role, type(e).__name__, egress)
            raise SeqUnavailable(type(e).__name__) from e
        if r.status_code >= 500 or r.status_code == 429:
            self._fail(role, f"http {r.status_code}", egress)
            # 429 and 503 refuse before admission; a 500 is opaque, so treat it like a timeout
            raise SeqUnavailable(f"http {r.status_code} {self._code(r)}", delivered=method == "POST" and r.status_code == 500)
        if egress:
            self.egress_failures = 0
        else:
            self.consecutive_failures = 0
            self.first_failure_at = None
            self.last_ok_at = time.time()
        self.db.record_rpc(role, True)
        if r.status_code >= 400:
            raise SeqRejected(r.status_code, self._code(r), self._message(r))
        return r

    def _fail(self, role, what, egress):
        if egress:
            self.egress_failures += 1
        else:
            self.consecutive_failures += 1
            if self.first_failure_at is None:
                self.first_failure_at = time.time()
            self.last_error = what
        self.db.record_rpc(role, False, what)

    @staticmethod
    def _code(r) -> str:
        try:
            return str(r.json().get("code", ""))
        except ValueError:
            return ""

    @staticmethod
    def _message(r) -> str:
        try:
            return str(r.json().get("message", ""))[:300]
        except ValueError:
            return r.text[:300]

    # ---- ingress ----
    def domain(self) -> dict:
        return self._req("GET", "/domain").json()

    def fee(self) -> dict:
        """{"fee", "recommended_fee", "suggested_max_fee"} (log-space exponents)."""
        return self._req("GET", "/fee").json()

    def next_nonce(self, sender: str) -> int:
        return int(self._req("GET", "/nonce", params={"sender": sender}).json()["next_nonce"])

    def submit(self, sender: str, nonce: int, max_fee: int, data: bytes, signature: bytes) -> dict:
        body = {"message": {"nonce": nonce, "max_fee": max_fee, "data": "0x" + data.hex()},
                "signature": "0x" + signature.hex(), "sender": sender}
        return self._req("POST", "/tx", json_body=body, timeout=30.0).json()

    # ---- egress (optional) ----
    def history(self) -> dict | None:
        if not self.egress:
            return None
        return self._req("GET", "/history", egress=True).json()

    def healthz(self) -> dict | None:
        if not self.egress:
            return None
        return self._req("GET", "/healthz", egress=True).json()
