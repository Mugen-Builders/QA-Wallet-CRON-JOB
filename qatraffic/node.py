"""Client for the rollups-node (v2.0.0-alpha.13) JSON-RPC API and the inspect endpoint.

JSON-RPC (POST <node>/rpc): numbers are hex strings ("UnsignedInteger": ^0x[0-9a-f]{1,16}$) and
results come wrapped in {"data": ...}. Inspect (POST <inspect>/<app>): the raw payload is the
request body; the response is {"status", "reports":[{"payload":"0x.."}], "processed_input_count"}.
"""
from __future__ import annotations

import json
import random
import time

import requests


class NodeUnavailable(Exception):
    pass


class NodeError(Exception):
    """The node answered with a JSON-RPC error (e.g. input not found yet)."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def hx(n: int) -> str:
    return hex(int(n))


def un(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)


class Node:
    def __init__(self, cfg, db, log):
        self.cfg, self.db, self.log = cfg, db, log
        self.rpc_url = (cfg.node_rpc_url or "").rstrip("/")
        self.inspect_url = (cfg.node_inspect_url or "").rstrip("/")
        self.app = cfg.node_app_id
        self.session = requests.Session()
        self.consecutive_failures = {"node": 0, "inspect": 0}
        self.last_ok_at = {"node": None, "inspect": None}
        self._id = 0

    def _post(self, role, url, *, data=None, json_body=None, retries=2, timeout=25):
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.session.post(url, data=data, json=json_body, timeout=timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"http {r.status_code}")
                self.consecutive_failures[role] = 0
                self.last_ok_at[role] = time.time()
                self.db.record_rpc(role, True)
                return r
            except requests.exceptions.RequestException as e:
                last = e
                self.db.record_rpc(role, False, type(e).__name__ + ":" + str(e)[:80])
                time.sleep(min(6.0, 0.6 * 2**attempt) + random.random() * 0.3)
        self.consecutive_failures[role] += 1
        raise NodeUnavailable(f"{role} unavailable ({type(last).__name__})")

    def rpc(self, method: str, params: dict):
        self._id += 1
        r = self._post("node", self.rpc_url, json_body={"jsonrpc": "2.0", "id": self._id, "method": method,
                                                         "params": params})
        try:
            body = r.json()
        except ValueError as e:
            raise NodeUnavailable("node returned non-JSON") from e
        if "error" in body and body["error"]:
            err = body["error"]
            raise NodeError(err.get("code"), err.get("message", ""))
        res = body.get("result")
        return res.get("data") if isinstance(res, dict) and "data" in res else res

    # ---- typed calls ----
    def application(self):
        return self.rpc("cartesi_getApplication", {"application": self.app})

    def processed_input_count(self) -> int:
        return un(self.rpc("cartesi_getProcessedInputCount", {"application": self.app}))

    def last_accepted_epoch(self) -> int | None:
        try:
            return un(self.rpc("cartesi_getLastAcceptedEpochIndex", {"application": self.app}))
        except NodeError:
            return None

    def get_input(self, index: int):
        """Returns the input dict, or None when the node has not seen it yet."""
        try:
            return self.rpc("cartesi_getInput", {"application": self.app, "input_index": hx(index)})
        except NodeError as e:
            if "not found" in e.message.lower() or e.code in (-32004, -32001, 404):
                return None
            raise

    def _list_all(self, method: str, params: dict, page: int = 500) -> list[dict]:
        out, offset = [], 0
        while True:
            res = self.rpc(method, dict(params, application=self.app, limit=page, offset=offset)) or []
            out.extend(res)
            if len(res) < page:
                return out
            offset += page

    def outputs_for_input(self, index: int) -> list[dict]:
        """Every output of one input, in output-index order (a batch can carry many)."""
        return sorted(self._list_all("cartesi_listOutputs", {"input_index": hx(index)}), key=lambda o: un(o["index"]))

    def reports_for_input(self, index: int) -> list[dict]:
        return sorted(self._list_all("cartesi_listReports", {"input_index": hx(index)}), key=lambda o: un(o["index"]))

    def get_output(self, index: int):
        try:
            return self.rpc("cartesi_getOutput", {"application": self.app, "output_index": hx(index)})
        except NodeError as e:
            if "not found" in e.message.lower():
                return None
            raise

    def get_epoch(self, index: int):
        try:
            return self.rpc("cartesi_getEpoch", {"application": self.app, "epoch_index": hx(index)})
        except NodeError as e:
            if "not found" in e.message.lower():
                return None
            raise

    def list_unexecuted_vouchers(self, page: int = 200):
        """Every voucher the node knows that has not been executed (all senders, not only the
        bot's): needed for the custody check."""
        out, offset = [], 0
        sels = ["0x237a816f", "0x10321e8b"]
        while True:
            res = self.rpc("cartesi_listOutputs", {"application": self.app, "output_type": sels, "executed": False,
                                                   "limit": page, "offset": offset})
            res = res or []
            out.extend(res)
            if len(res) < page:
                return out
            offset += page

    # ---- inspect ----
    def inspect(self, payload: bytes | str):
        body = payload.encode() if isinstance(payload, str) else payload
        r = self._post("inspect", f"{self.inspect_url}/{self.app}", data=body, timeout=30)
        if r.status_code >= 400:
            raise NodeError(r.status_code, r.text[:200])
        return r.json()

    def ledger_balance(self, account: str, token: str) -> tuple[int, int]:
        """(balance, processed_input_count). An account libcma has never seen is 'Account not
        found' (rejected inspect); that is a zero balance."""
        res = self.inspect(json.dumps({"method": "ledger_getBalance", "params": [account, token]}))
        return self._amount(res), int(res.get("processed_input_count", 0))

    def state(self) -> dict:
        """The app's raw `state` inspect (the sequencer's STATE_INSPECT_QUERY): progress, the pinned
        batch submitter and token, scheduler status; plus the node's processed_input_count."""
        res = self.inspect(b"state")
        reports = res.get("reports") or []
        if res.get("status") != "Accepted" or not reports:
            raise NodeError(res.get("status"), "state inspect rejected")
        st = json.loads(bytes.fromhex(reports[-1]["payload"][2:]))
        if "error" in st or "executed_input_count" not in st:
            raise NodeError("Accepted", f"state inspect answered {str(st)[:160]}")
        st["processed_input_count"] = int(res.get("processed_input_count", 0))
        return st

    def user_op_nonce(self, account: str) -> tuple[int, int]:
        """(next user-op nonce the engine expects, processed_input_count)."""
        res = self.inspect(json.dumps({"method": "ledger_getNonce", "params": [account]}))
        return self._amount(res), int(res.get("processed_input_count", 0))

    def ledger_total_supply(self, token: str) -> tuple[int, int]:
        res = self.inspect(json.dumps({"method": "ledger_getTotalSupply", "params": [token]}))
        return self._amount(res), int(res.get("processed_input_count", 0))

    @staticmethod
    def _amount(res) -> int:
        """A libcma amount is one 32-byte big-endian report. The sequencer variant's driver accepts
        every inspect, so a failure arrives as Accepted with a JSON error report (the classic driver
        rejects it): "Account not found" means a zero balance in both."""
        status = res.get("status")
        reports = res.get("reports") or []
        raw = [bytes.fromhex(r["payload"][2:]) for r in reports if r.get("payload")]
        if status == "Accepted" and len(raw) == 1 and len(raw[0]) == 32:
            return int.from_bytes(raw[0], "big")
        text = " ".join(b.decode("utf-8", "replace") for b in raw)
        if status in ("Accepted", "Rejected") and "not found" in text.lower():
            return 0
        raise NodeError(status, f"unexpected inspect result: {text[:160]}")
