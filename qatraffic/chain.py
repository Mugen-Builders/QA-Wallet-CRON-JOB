"""L1 access: an RPC client with failover and error classification, and a transaction sender with
per-account nonces, a fee policy, crash-safe bookkeeping and stuck-transaction replacement."""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass

import requests
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError, ProviderConnectionError, TimeExhausted, TransactionNotFound, Web3RPCError

from . import contracts as C

GWEI = 10**9
ETH = 10**18


class RpcUnavailable(Exception):
    """Every endpoint failed (timeouts, 5xx, 429, connection errors)."""


class Reverted(Exception):
    def __init__(self, reason: str | None, data=None):
        super().__init__(reason or "execution reverted")
        self.reason = reason
        self.data = data


class FeeTooHigh(Exception):
    pass


class RpcAnswer(ValueError):
    """A JSON-RPC error that is an answer, not an outage (nonce too low, underpriced, ...)."""


class InsufficientGas(Exception):
    pass


_TRANSIENT_RPC = ("rate limit", "too many requests", "limit exceeded", "exceeded", "capacity", "timeout",
                  "timed out", "header not found", "internal error", "try again", "unavailable", "-32005", "-32603")


def _classify(e: Exception) -> str:
    s = str(e).lower()
    if isinstance(e, requests.exceptions.Timeout) or "timed out" in s or "timeout" in s:
        return "timeout"
    if isinstance(e, requests.exceptions.ConnectionError) or "connection" in s:
        return "connection"
    if "429" in s or "too many requests" in s or "rate limit" in s or "exceeded" in s and "limit" in s:
        return "rate_limited"
    if any(c in s for c in ("502", "503", "504", "500", "bad gateway", "service unavailable")):
        return "server_error"
    return f"other:{type(e).__name__}"


class L1:
    """Thin wrapper over web3 with endpoint failover. Every call goes through `_call` so failures
    are counted per role for alerting and the daily report."""

    def __init__(self, cfg, db, log):
        self.cfg, self.db, self.log = cfg, db, log
        self.w3s = [Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 20})) for u in cfg.l1_rpc_urls]
        self.active = 0
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        w3 = self.w3s[0]
        self.input_box = w3.eth.contract(address=Web3.to_checksum_address(cfg.input_box), abi=C.abi("InputBox"))
        self.portal = w3.eth.contract(address=Web3.to_checksum_address(cfg.erc20_portal), abi=C.abi("Erc20Portal"))
        self.token = w3.eth.contract(address=Web3.to_checksum_address(cfg.token), abi=C.abi("TestUsdc"))
        self.app = (w3.eth.contract(address=Web3.to_checksum_address(cfg.app_address), abi=C.abi("Application"))
                    if cfg.app_address else None)
        self._consensus = None

    @property
    def w3(self) -> Web3:
        return self.w3s[self.active]

    def _call(self, fn, *, retries: int = 2):
        """Run fn(w3) on the active endpoint, failing over and backing off on transient errors.
        Reverts are not transient and are raised immediately as Reverted."""
        last = None
        attempts = max(1, retries + 1) * len(self.w3s)
        for attempt in range(attempts):
            w3 = self.w3s[self.active]
            try:
                out = fn(w3)
                self.consecutive_failures = 0
                self.last_ok_at = time.time()
                self.db.record_rpc("l1", True)
                return out
            except ContractLogicError as e:
                self.db.record_rpc("l1", True)
                raise Reverted(C.decode_revert(getattr(e, "data", None)) or str(e), getattr(e, "data", None)) from e
            except TransactionNotFound:
                raise
            except Web3RPCError as e:
                # JSON-RPC errors are answers (nonce too low, underpriced, insufficient funds) unless
                # they are a provider limit or an internal error, which are treated as outages.
                if not any(t in str(e).lower() for t in _TRANSIENT_RPC):
                    self.db.record_rpc("l1", True)
                    raise RpcAnswer(str(e)) from e
                kind = _classify(e) if _classify(e) != "other" else "rpc_error"
                last = e
                self.last_error = kind
                self.db.record_rpc("l1", False, kind)
                if len(self.w3s) > 1:
                    self.active = (self.active + 1) % len(self.w3s)
                time.sleep(min(8.0, 0.5 * (2 ** (attempt // len(self.w3s)))) + random.random() * 0.3)
            except (requests.exceptions.RequestException, ProviderConnectionError, TimeExhausted, OSError) as e:
                kind = _classify(e)
                last = e
                self.last_error = kind
                self.db.record_rpc("l1", False, kind)
                if len(self.w3s) > 1:
                    self.active = (self.active + 1) % len(self.w3s)
                time.sleep(min(8.0, 0.5 * (2 ** (attempt // len(self.w3s)))) + random.random() * 0.3)
        self.consecutive_failures += 1
        raise RpcUnavailable(f"l1 rpc unavailable ({_classify(last) if last else 'unknown'})")

    # ---- reads ----
    def chain_id(self) -> int:
        return self._call(lambda w3: w3.eth.chain_id)

    def block_number(self) -> int:
        return self._call(lambda w3: w3.eth.block_number)

    def block(self, ident="latest"):
        return self._call(lambda w3: w3.eth.get_block(ident))

    def eth_balance(self, addr: str, block="latest") -> int:
        return self._call(lambda w3: w3.eth.get_balance(Web3.to_checksum_address(addr), block_identifier=block))

    def token_balance(self, addr: str, block="latest") -> int:
        return self._call(lambda w3: self._c(w3, self.token).functions.balanceOf(Web3.to_checksum_address(addr))
                          .call(block_identifier=block))

    def token_allowance(self, owner: str, spender: str) -> int:
        return self._call(lambda w3: self._c(w3, self.token).functions.allowance(
            Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)).call())

    def number_of_inputs(self, block="latest") -> int:
        return self._call(lambda w3: self._c(w3, self.input_box).functions.getNumberOfInputs(
            Web3.to_checksum_address(self.cfg.app_address)).call(block_identifier=block))

    def was_output_executed(self, index: int, block="latest") -> bool:
        return self._call(lambda w3: self._c(w3, self.app).functions.wasOutputExecuted(index).call(block_identifier=block))

    def is_foreclosed(self) -> bool:
        return self._call(lambda w3: self._c(w3, self.app).functions.isForeclosed().call())

    def validate_output(self, raw: bytes, index: int, siblings: list[bytes]) -> None:
        """Raises Reverted (e.g. InvalidOutputsMerkleRoot) when the output is not yet executable."""
        self._call(lambda w3: self._c(w3, self.app).functions.validateOutput(raw, (index, siblings)).call())

    def consensus(self):
        if self._consensus is None:
            addr = self._call(lambda w3: self._c(w3, self.app).functions.getOutputsMerkleRootValidator().call())
            self._consensus = self.w3.eth.contract(address=addr, abi=C.abi("Consensus"))
        return self._consensus

    def consensus_timing(self) -> tuple[int | None, int | None]:
        """(epoch length, claim staging period) in blocks, or None when the validator is not an
        Authority/Quorum (e.g. PRT), in which case due times are estimated from the node only."""
        try:
            c = self.consensus()
            el = self._call(lambda w3: self._c(w3, c).functions.getEpochLength().call())
            sp = self._call(lambda w3: self._c(w3, c).functions.getClaimStagingPeriod().call())
            return el, sp
        except (Reverted, ValueError):
            return None, None

    def receipt(self, tx_hash: str):
        def f(w3):
            try:
                return w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                return None
        return self._call(f)

    def tx_known(self, tx_hash: str) -> bool:
        def f(w3):
            try:
                return w3.eth.get_transaction(tx_hash) is not None
            except TransactionNotFound:
                return False
        return self._call(f)

    def nonce(self, addr: str, block="pending") -> int:
        return self._call(lambda w3: w3.eth.get_transaction_count(Web3.to_checksum_address(addr), block))

    def base_fee(self) -> int:
        b = self.block("latest")
        return int(b.get("baseFeePerGas", 0))

    def estimate_gas(self, tx: dict) -> int:
        return self._call(lambda w3: w3.eth.estimate_gas(tx))

    def call(self, tx: dict, block="latest"):
        return self._call(lambda w3: w3.eth.call(tx, block_identifier=block))

    def send_raw(self, raw: bytes) -> str:
        return self._call(lambda w3: w3.eth.send_raw_transaction(raw).to_0x_hex(), retries=1)

    @staticmethod
    def _c(w3, contract):
        """Rebind a contract object to the endpoint currently in use."""
        return w3.eth.contract(address=contract.address, abi=contract.abi)


@dataclass
class Fees:
    max_fee: int
    tip: int


class TxSender:
    """Sends transactions for the traffic accounts and the funder.

    Crash safety: a txs row (with the signed raw transaction) is committed before broadcast. On
    restart `recover()` rebroadcasts rows the chain never saw, and marks rows whose nonce was used
    by something else as dropped.
    """

    STUCK_AFTER_S = 8 * 60
    MAX_REPLACEMENTS = 4

    def __init__(self, cfg, db, log, l1: L1, run_id: int):
        self.cfg, self.db, self.log, self.l1, self.run_id = cfg, db, log, l1, run_id
        self._next_nonce: dict[str, int] = {}

    # ---- fees ----
    def fees(self, tip_wei: int, urgent: bool = False) -> Fees:
        base = self.l1.base_fee()
        cap = int(self.cfg.max_fee_gwei * GWEI)
        tip = tip_wei * (2 if urgent else 1)
        max_fee = base * 2 + tip
        if base + tip > cap:
            raise FeeTooHigh(f"base fee {base / GWEI:.3f} gwei above cap {self.cfg.max_fee_gwei} gwei")
        return Fees(min(max_fee, cap), tip)

    # ---- nonces ----
    def _nonce_for(self, addr: str) -> int:
        chain_pending = self.l1.nonce(addr, "pending")
        row = self.db.q1("SELECT MAX(nonce) AS n FROM txs WHERE run_id=? AND sender=? AND status IN ('pending','mined','reverted')",
                         (self.run_id, addr))
        db_next = (row["n"] + 1) if row and row["n"] is not None else 0
        local = self._next_nonce.get(addr, 0)
        return max(chain_pending, db_next, local)

    # ---- sending ----
    def send(self, key: bytes, sender: str, slot, kind: str, to: str, data: bytes = b"", value: int = 0,
             tip_wei: int = 0, gas: int | None = None, gas_mult: float = 1.25, urgent: bool = False,
             step_id: int | None = None) -> int:
        """Sign, record and broadcast. Returns the txs.id. Raises Reverted when estimation reverts,
        InsufficientGas when the sender cannot pay, FeeTooHigh above the fee cap."""
        fees = self.fees(tip_wei, urgent)
        txd = {"from": sender, "to": Web3.to_checksum_address(to), "value": value, "data": data,
               "chainId": self.cfg.chain_id}
        if gas is None:
            gas = int(self.l1.estimate_gas(txd) * gas_mult) + 5_000
        bal = self.l1.eth_balance(sender)
        if bal < gas * fees.max_fee + value:
            raise InsufficientGas(f"balance {bal} < {gas * fees.max_fee + value}")
        nonce = self._nonce_for(sender)
        signed = self._sign(key, txd, nonce, gas, fees)
        h = signed.hash.to_0x_hex()
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO txs(run_id,sender,slot,step_id,nonce,kind,to_addr,value,data,gas,max_fee,tip,hashes,status,sent_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, sender, slot, step_id, nonce, kind, txd["to"], str(value), "0x" + data.hex(), gas, fees.max_fee,
                 fees.tip, json.dumps([{"hash": h, "raw": signed.raw_transaction.to_0x_hex()}]), "pending", time.time()))
            tx_id = cur.lastrowid
        self._next_nonce[sender] = nonce + 1
        self._broadcast(tx_id, signed.raw_transaction, h, sender)
        return tx_id

    def _sign(self, key, txd, nonce, gas, fees):
        tx = {"to": txd["to"], "value": txd["value"], "data": txd["data"], "chainId": txd["chainId"],
              "nonce": nonce, "gas": gas, "maxFeePerGas": fees.max_fee, "maxPriorityFeePerGas": fees.tip, "type": 2}
        return Account.sign_transaction(tx, key)

    def _broadcast(self, tx_id, raw, h, sender):
        try:
            self.l1.send_raw(raw)
            self.log.info("tx.sent", tx_id=tx_id, hash=h, sender=sender)
        except ValueError as e:
            msg = str(e).lower()
            if "already known" in msg or "known transaction" in msg:
                return
            if "nonce too low" in msg:
                # someone (a previous crashed run) used the nonce: let recover() decide
                self._next_nonce.pop(sender, None)
                self.db.x("UPDATE txs SET status='dropped', revert=? WHERE id=?", ("nonce too low at broadcast", tx_id))
                raise
            if "insufficient funds" in msg:
                self._next_nonce.pop(sender, None)
                self.db.x("UPDATE txs SET status='dropped', revert=? WHERE id=?", ("insufficient funds", tx_id))
                raise InsufficientGas(str(e)) from e
            self.db.x("UPDATE txs SET status='dropped', revert=? WHERE id=?", (str(e)[:200], tx_id))
            self._next_nonce.pop(sender, None)
            raise
        except RpcUnavailable:
            # stays 'pending' with its raw tx stored; poll()/recover() rebroadcasts it
            self.log.warn("tx.broadcast_deferred", tx_id=tx_id, hash=h)

    # ---- tracking ----
    def poll(self, keys: dict[str, bytes]) -> list[tuple[int, object]]:
        """Check every pending tx. Returns (tx id, receipt or None) for those that reached a final
        state this call (None = dropped)."""
        done = []
        for row in self.db.q("SELECT * FROM txs WHERE run_id=? AND status='pending' ORDER BY id", (self.run_id,)):
            hashes = json.loads(row["hashes"])
            rcpt = None
            for ent in reversed(hashes):
                rcpt = self.l1.receipt(ent["hash"])
                if rcpt is not None:
                    break
            if rcpt is not None:
                fee = int(rcpt["gasUsed"]) * int(rcpt.get("effectiveGasPrice", 0))
                status = "mined" if rcpt["status"] == 1 else "reverted"
                self.db.x("UPDATE txs SET status=?, block=?, gas_used=?, effective_gas_price=?, fee_wei=?, mined_at=?, "
                          "hashes=? WHERE id=?",
                          (status, rcpt["blockNumber"], rcpt["gasUsed"], rcpt.get("effectiveGasPrice", 0), str(fee),
                           time.time(), json.dumps(self._mark_mined(hashes, rcpt["transactionHash"].to_0x_hex())), row["id"]))
                if status == "reverted":
                    self.db.x("UPDATE txs SET revert=? WHERE id=?", (self._revert_reason(row, rcpt), row["id"]))
                done.append((row["id"], rcpt))
                continue
            # not mined: dropped, never broadcast, or stuck?
            chain_latest = self.l1.nonce(row["sender"], "latest")
            if chain_latest > row["nonce"]:
                # the nonce is used but none of our hashes has a receipt: replaced/dropped externally
                self.db.x("UPDATE txs SET status='dropped', revert='nonce consumed by another transaction' WHERE id=?",
                          (row["id"],))
                done.append((row["id"], None))
                continue
            newest = hashes[-1]
            if not self.l1.tx_known(newest["hash"]):
                try:
                    self.l1.send_raw(bytes.fromhex(newest["raw"][2:]))
                    self.log.info("tx.rebroadcast", tx_id=row["id"], hash=newest["hash"])
                except (ValueError, RpcUnavailable) as e:
                    self.log.warn("tx.rebroadcast_failed", tx_id=row["id"], error=str(e)[:200])
                continue
            if time.time() - row["sent_at"] > self.STUCK_AFTER_S * (1 + row["replaced"]) and row["replaced"] < self.MAX_REPLACEMENTS:
                key = keys.get(row["sender"])
                if key:
                    self._replace(row, hashes, key)
        return done

    @staticmethod
    def _mark_mined(hashes, mined_hash):
        for h in hashes:
            h["mined"] = h["hash"].lower() == mined_hash.lower()
            h.pop("raw", None) if h["mined"] else None
        return hashes

    def _replace(self, row, hashes, key):
        try:
            base = self.l1.base_fee()
        except RpcUnavailable:
            return
        tip = max(int(row["tip"] * 1.25) + 1, row["tip"] + 1)
        max_fee = max(int(row["max_fee"] * 1.25) + 1, base * 2 + tip)
        cap = int(self.cfg.max_fee_gwei * GWEI)
        if max_fee > cap:
            self.log.warn("tx.stuck_fee_cap", tx_id=row["id"], base_gwei=base / GWEI)
            return
        txd = {"to": row["to_addr"], "value": int(row["value"]), "data": bytes.fromhex(row["data"][2:]),
               "chainId": self.cfg.chain_id}
        signed = self._sign(key, txd, row["nonce"], row["gas"], Fees(max_fee, tip))
        h = signed.hash.to_0x_hex()
        hashes.append({"hash": h, "raw": signed.raw_transaction.to_0x_hex()})
        self.db.x("UPDATE txs SET hashes=?, max_fee=?, tip=?, replaced=replaced+1 WHERE id=?",
                  (json.dumps(hashes), max_fee, tip, row["id"]))
        try:
            self.l1.send_raw(signed.raw_transaction)
            self.log.info("tx.replaced", tx_id=row["id"], hash=h, tip=tip, max_fee=max_fee)
        except (ValueError, RpcUnavailable) as e:
            self.log.warn("tx.replace_failed", tx_id=row["id"], error=str(e)[:200])

    def _revert_reason(self, row, rcpt) -> str:
        try:
            self.l1.call({"from": row["sender"], "to": row["to_addr"], "value": int(row["value"]),
                          "data": row["data"]}, block=rcpt["blockNumber"] - 1)
            return "reverted (no reason on replay)"
        except Reverted as e:
            return e.reason or "reverted"
        except Exception as e:  # noqa: BLE001
            return f"reverted (replay failed: {type(e).__name__})"

    def recover(self):
        """Startup: make sure every 'pending' row is either on chain, re-broadcast, or dropped."""
        n = self.db.q1("SELECT COUNT(*) AS n FROM txs WHERE run_id=? AND status='pending'", (self.run_id,))["n"]
        if n:
            self.log.info("tx.recover", pending=n)
