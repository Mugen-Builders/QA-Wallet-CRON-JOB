"""The canonical model: a Python port of the sequencer's scheduler fold over the qa-wallet engine.

It folds every InputBox input of the application, from anyone, in L1 order, exactly as the
machine does (qa-wallet-tester-app `src/seq_main.cpp` -> sequencer-core `Scheduler::process_input`
-> the engine through `application-engine.h`), and predicts:

- the execution order: directs go to the fridge and run when a frame's safe_block covers them or
  when they are MAX_WAIT_BLOCKS old; batches from the submitter are gated (decode, nonce, empty,
  structure, staleness), then each frame drains the covered directs and runs its user ops;
- the accounts-drive token ledger (every account, virtual ids included), user-op nonces, fees paid
  to the batch submitter, and the engine's progress counters (`state` inspect);
- every report and output a modelled entry emits, in order, so the node's outputs and reports for
  each input can be compared exactly.

Inputs that touch other assets (Ether, other ERC-20s, NFTs) or that the model cannot decode are
"unknown": counted (the machine counts them), token balances untouched (they cannot move the token),
and the input is marked incomplete so its emissions are checked as a subset, not exactly.

Sources (pinned): sequencer-core/src/scheduler/mod.rs, application/mod.rs, batch.rs (sequencer
v0.1.0-alpha.10); qa-wallet src/lib/engine.cpp, ledger.cpp, app_engine.cpp, src/seq_main.cpp.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from . import fee as F
from . import ssz
from . import userop as U

MAX_WAIT_BLOCKS = 1200
UINT96_MAX = (1 << 96) - 1
U32_MAX = 2**32 - 1
ANY = object()  # report field whose exact value the model does not predict
DROPPED_BATCH_REPORT = b"scheduler dropped invalid batch"


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


SEL_WITHDRAW_ERC20 = _sel("WithdrawErc20(address,uint256,bytes)")
SEL_TRANSFER_ERC20 = _sel("TransferErc20(address,bytes32,uint256,bytes)")
LIBCMA_SELECTORS = frozenset(_sel(s) for s in (
    "WithdrawEther(uint256,bytes)", "WithdrawErc20(address,uint256,bytes)", "WithdrawErc721(address,uint256,bytes)",
    "WithdrawErc1155Single(address,uint256,uint256,bytes)", "WithdrawErc1155Batch(address,uint256[],uint256[],bytes)",
    "TransferEther(bytes32,uint256,bytes)", "TransferErc20(address,bytes32,uint256,bytes)",
    "TransferErc721(address,bytes32,uint256,bytes)", "TransferErc1155Single(address,bytes32,uint256,uint256,bytes)",
    "TransferErc1155Batch(address,bytes32,uint256[],uint256[],bytes)"))
EVM_ADVANCE_SEL = _sel("EvmAdvance(uint256,address,address,uint256,uint256,uint256,uint256,bytes)")
VOUCHER_SEL = _sel("Voucher(address,uint256,bytes)")
NOTICE_SEL = _sel("Notice(bytes)")
TRANSFER_SEL = _sel("transfer(address,uint256)")


def wallet_id(addr: str) -> int:
    """libcma's 32-byte account id of a wallet: 12 zero bytes + the address."""
    return int(addr, 16)


def voucher_output(destination: str, value: int, payload: bytes) -> bytes:
    return VOUCHER_SEL + abi_encode(["address", "uint256", "bytes"], [destination, value, payload])


def notice_output(payload: bytes) -> bytes:
    return NOTICE_SEL + abi_encode(["bytes"], [payload])


def erc20_voucher(token: str, receiver: str, amount: int) -> bytes:
    return voucher_output(token, 0, TRANSFER_SEL + abi_encode(["address", "uint256"], [receiver, amount]))


def decode_evm_advance(data: bytes) -> dict:
    if data[:4] != EVM_ADVANCE_SEL:
        raise ValueError("not an EvmAdvance input")
    chain_id, app, sender, block, ts, _rand, index, payload = abi_decode(
        ["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256", "bytes"], data[4:])
    return {"chain_id": chain_id, "app": to_checksum_address(app), "sender": to_checksum_address(sender),
            "block": block, "timestamp": ts, "index": index, "payload": bytes(payload)}


@dataclass
class Entry:
    """One executed application input: one offset of the sequencer's application history."""
    offset: int
    exec_input: int                 # InputBox index of the input during which it executed
    source: str                     # direct | user_op
    sender: str
    block: int                      # direct: its inclusion block; user op: the frame's safe_block
    kind: str                       # deposit | transfer | withdraw | echo | other
    outcome: str                    # accept | reject | unknown
    direct_input: int | None = None
    batch_nonce: int | None = None
    frame: int | None = None
    op: int | None = None
    nonce: int | None = None
    max_fee: int | None = None
    fee: int | None = None          # frame fee exponent
    fee_units: int = 0              # token units paid to the batch submitter
    data: bytes = b""
    amount: int | None = None
    to_id: int | None = None
    reason: str | None = None
    reports: list = field(default_factory=list)  # predicted, None = unknown
    outputs: list = field(default_factory=list)  # predicted raw output bytes, None = unknown
    overdue: bool = False


@dataclass
class Skipped:
    """A user op of an executed batch that the scheduler skipped (not an application input)."""
    exec_input: int
    batch_nonce: int
    frame: int
    op: int
    sender: str | None
    nonce: int
    max_fee: int
    fee: int
    data: bytes
    reason: str


@dataclass
class InputResult:
    index: int
    sender: str
    block: int
    kind: str                       # direct | batch
    outcome: str                    # enqueued | executed | stale | rejected:<reason>
    batch_nonce: int | None
    entries: list[Entry]
    skipped: list[Skipped]
    reports: list                   # predicted, in order (None items = unknown)
    outputs: list
    complete: bool
    frames: int = 0
    ops: int = 0
    first_safe_block: int | None = None


@dataclass
class Snapshot:
    """Fold state after an input, restricted to what the checks compare."""
    index: int
    block: int
    executed: int
    next_batch_nonce: int
    queued_directs: int
    nonce_count: int
    rejected_min: int
    rejected_max: int
    last_block: int
    supply: int
    balances: dict                  # watched account id -> balance
    nonces: dict                    # watched address (lower) -> nonce


class Engine:
    """The qa-wallet engine, accounts-drive token only."""

    def __init__(self, token: str, sequencer: str, erc20_portal: str, other_portals: set[str]):
        self.token = token.lower()
        self.sequencer = sequencer.lower()
        self.erc20_portal = erc20_portal.lower()
        self.other_portals = {p.lower() for p in other_portals}
        self.bal: dict[int, int] = {}
        self.nonces: dict[str, int] = {}
        self.executed = 0
        self.rejected = 0
        self.unknown = 0
        self.last_block = 0

    # ---- ledger ----
    def balance(self, account_id: int) -> int:
        return self.bal.get(account_id, 0)

    def _set(self, account_id: int, v: int):
        if v:
            self.bal[account_id] = v
        else:
            self.bal.pop(account_id, None)  # libcma prunes empty accounts

    def supply(self) -> int:
        return sum(self.bal.values())

    # ---- execution ----
    def _settle(self, e: Entry, block: int):
        if e.outcome == "reject":
            self.rejected += 1
            e.outputs = []  # a rejected input keeps its reports, drops its queued outputs
        elif e.outcome == "unknown":
            self.unknown += 1
        self.executed += 1
        self.last_block = max(self.last_block, block)

    def direct(self, e: Entry, payload: bytes):
        sender = e.sender.lower()
        if sender == self.erc20_portal:
            self._deposit(e, payload)
        elif sender in self.other_portals:
            e.kind, e.outcome, e.reports, e.outputs = "other", "unknown", [None], [None]
        else:
            self._payload(e, payload)
        self._settle(e, e.block)

    def user_op_invalid(self, sender: str, op, fee: int) -> str | None:
        """Scheduler + engine validation (validate_and_execute_user_op, Engine::validate_user_op)."""
        if op.max_fee < fee:
            return "max_fee below frame fee"
        expected = self.nonces.get(sender.lower(), 0)
        if op.nonce != expected:
            return f"bad nonce: expected {expected}, got {op.nonce}"
        if fee > F.MAX_EXPONENT:
            return "fee not representable"
        if self.balance(wallet_id(sender)) < F.to_linear(fee):
            return "fee balance"
        if op.nonce == U32_MAX:
            return "nonce exhausted"
        return None

    def user_op(self, e: Entry, op, fee: int):
        sender = e.sender.lower()
        e.fee_units = F.to_linear(fee)
        if sender != self.sequencer:
            sid, qid = wallet_id(sender), wallet_id(self.sequencer)
            self._set(sid, self.balance(sid) - e.fee_units)
            self._set(qid, self.balance(qid) + e.fee_units)
        self.nonces[sender] = op.nonce + 1
        self._payload(e, op.data)
        self._settle(e, e.block)

    def _deposit(self, e: Entry, p: bytes):
        e.kind = "deposit"
        if len(p) < 72:
            e.outcome, e.reason = "reject", "undecodable deposit"
            e.reports = [{"error": ANY, "code": ANY}]
            return
        token, depositor = to_checksum_address(p[:20]), to_checksum_address(p[20:40])
        amount, exec_data = int.from_bytes(p[40:72], "big"), p[72:]
        e.amount, e.to_id = amount, wallet_id(depositor)
        if token.lower() != self.token:
            e.outcome, e.reports, e.outputs = "unknown", [None], []
            return
        rep = {"deposit": "erc20", "sender": depositor.lower(), "token": token.lower(), "amount": str(amount),
               "exec_layer_data": "0x" + exec_data.hex(), "accounts_drive": True}
        bal = self.balance(e.to_id)
        if bal + amount > UINT96_MAX:
            e.outcome, e.reason = "reject", "accounts drive overflow"
            e.reports = [dict(rep, credited=False, error=ANY, code=ANY)]
            return
        self._set(e.to_id, bal + amount)
        e.outcome, e.reports = "accept", [dict(rep, credited=True)]

    def _payload(self, e: Entry, p: bytes):
        sel = p[:4]
        if len(p) < 4 or sel not in LIBCMA_SELECTORS:
            e.kind, e.outcome, e.outputs = "echo", "accept", [notice_output(p)]
            return
        sid = wallet_id(e.sender)
        if sel == SEL_TRANSFER_ERC20:
            e.kind = "transfer"
            try:
                token, to, amount, _ = abi_decode(["address", "bytes32", "uint256", "bytes"], p[4:])
            except Exception:  # noqa: BLE001 - libcma's decoder decides; it cannot move funds either way
                e.outcome, e.reports = "unknown", [None]
                return
            e.amount, e.to_id = amount, int.from_bytes(to, "big")
            if token.lower() != self.token:
                e.outcome, e.reports = "unknown", [None]
                return
            bal = self.balance(sid)
            if amount == 0 or e.to_id == sid:
                e.outcome, e.reports = "unknown", [None]
            elif bal == 0 or amount > bal:
                e.outcome, e.reason, e.reports = "reject", "overdraft", [{"error": ANY, "code": ANY}]
            elif self.balance(e.to_id) + amount > UINT96_MAX:
                e.outcome, e.reason, e.reports = "reject", "receiver overflow", [{"error": ANY, "code": ANY}]
            else:
                self._set(sid, bal - amount)
                self._set(e.to_id, self.balance(e.to_id) + amount)
                e.outcome = "accept"
                e.reports = [{"transfer": "ok", "to": "0x" + e.to_id.to_bytes(32, "big").hex(), "amount": str(amount),
                              "token": self.token}]
            return
        if sel == SEL_WITHDRAW_ERC20:
            e.kind = "withdraw"
            try:
                token, amount, _ = abi_decode(["address", "uint256", "bytes"], p[4:])
            except Exception:  # noqa: BLE001
                e.outcome, e.reports = "unknown", [None]
                return
            e.amount = amount
            if token.lower() != self.token:
                e.outcome, e.reports, e.outputs = "unknown", [None], [None]
                return
            bal = self.balance(sid)
            if amount == 0:
                e.outcome, e.reports, e.outputs = "unknown", [None], [None]
            elif bal == 0 or amount > bal:
                e.outcome, e.reason, e.reports = "reject", "overdraft", [{"error": ANY, "code": ANY}]
            else:
                self._set(sid, bal - amount)
                e.outcome, e.outputs = "accept", [erc20_voucher(to_checksum_address(self.token), e.sender, amount)]
            return
        e.kind, e.outcome, e.reports, e.outputs = "other", "unknown", [None], [None]


class Fold:
    """Scheduler<EngineApp> over Engine. Feed InputBox inputs in index order with `process`."""

    def __init__(self, *, chain_id: int, app: str, token: str, sequencer: str, erc20_portal: str,
                 other_portals: set[str] = frozenset(), watched: list[str] = ()):
        self.chain_id = chain_id
        self.app = to_checksum_address(app)
        self.sequencer = sequencer.lower()
        self.separator = U.domain_separator(chain_id, self.app)
        self.engine = Engine(token, sequencer, erc20_portal, set(other_portals))
        self.fridge: deque = deque()  # (input_index, sender, block, payload)
        self.next_batch_nonce = 0
        self.processed = 0
        self.watched_ids = [wallet_id(a) for a in watched] + [wallet_id(sequencer)]
        self.watched_addrs = [a.lower() for a in watched]
        self.snapshots: list[Snapshot] = []
        self.entries: list[Entry] = []

    # ---- queries ----
    def balance(self, addr: str) -> int:
        return self.engine.balance(wallet_id(addr))

    def nonce(self, addr: str) -> int:
        return self.engine.nonces.get(addr.lower(), 0)

    def queued(self) -> list[tuple[int, str, int, bytes]]:
        return list(self.fridge)

    def snapshot_after(self, index: int) -> Snapshot | None:
        """State after InputBox input `index` (None before the first input)."""
        if index < 0 or index >= len(self.snapshots):
            return None
        return self.snapshots[index]

    # ---- the fold ----
    def process(self, index: int, sender: str, block: int, payload: bytes) -> InputResult:
        if index != self.processed:
            raise ValueError(f"input {index} out of order (expected {self.processed})")
        entries: list[Entry] = []
        skipped: list[Skipped] = []
        self._overdue(index, block, entries)
        if sender.lower() != self.sequencer:
            self.fridge.append((index, sender, block, payload))
            res = InputResult(index, sender, block, "direct", "enqueued", None, entries, skipped, [], [], True)
        else:
            res = self._batch(index, sender, block, payload, entries, skipped)
        reports, outputs = [], []
        complete = True
        for e in entries:
            reports.extend(e.reports)
            outputs.extend(e.outputs)
            complete &= e.outcome != "unknown" and None not in e.reports and None not in e.outputs
        if res.outcome.startswith("rejected"):
            reports.append(DROPPED_BATCH_REPORT)
        res.reports, res.outputs, res.complete = reports, outputs, complete
        self.processed += 1
        self.entries.extend(entries)
        eng = self.engine
        self.snapshots.append(Snapshot(
            index=index, block=block, executed=eng.executed, next_batch_nonce=self.next_batch_nonce,
            queued_directs=len(self.fridge), nonce_count=len(eng.nonces), rejected_min=eng.rejected,
            rejected_max=eng.rejected + eng.unknown, last_block=eng.last_block, supply=eng.supply(),
            balances={i: eng.balance(i) for i in self.watched_ids},
            nonces={a: eng.nonces.get(a, 0) for a in self.watched_addrs}))
        return res

    def _run_direct(self, exec_input: int, item, entries: list, overdue: bool):
        d_index, d_sender, d_block, d_payload = item
        e = Entry(offset=self.engine.executed, exec_input=exec_input, source="direct", sender=d_sender, block=d_block,
                  kind="", outcome="", direct_input=d_index, overdue=overdue)
        self.engine.direct(e, d_payload)
        entries.append(e)

    def _overdue(self, exec_input: int, block: int, entries: list):
        while self.fridge and block - self.fridge[0][2] >= MAX_WAIT_BLOCKS:
            self._run_direct(exec_input, self.fridge.popleft(), entries, True)

    def _drain(self, exec_input: int, safe_block: int, entries: list):
        while self.fridge and self.fridge[0][2] <= safe_block:
            self._run_direct(exec_input, self.fridge.popleft(), entries, False)

    def _batch(self, index, sender, block, payload, entries, skipped) -> InputResult:
        def result(outcome, batch=None):
            return InputResult(index, sender, block, "batch", outcome, batch.nonce if batch else None, entries, skipped,
                               [], [], True, frames=len(batch.frames) if batch else 0,
                               ops=sum(len(f.user_ops) for f in batch.frames) if batch else 0,
                               first_safe_block=batch.frames[0].safe_block if batch and batch.frames else None)
        try:
            batch = ssz.decode_batch(payload)
        except ssz.SszError:
            return result("rejected:decode")
        if batch.nonce != self.next_batch_nonce:
            return result(f"rejected:wrong_nonce(expected {self.next_batch_nonce}, got {batch.nonce})", batch)
        if not batch.frames:
            self.next_batch_nonce += 1
            return result("executed", batch)
        prev = None
        for f in batch.frames:
            if f.safe_block > block:
                return result("rejected:safe_block_above_inclusion", batch)
            if prev is not None and f.safe_block < prev:
                return result("rejected:non_monotonic_safe_blocks", batch)
            prev = f.safe_block
        if block - batch.frames[0].safe_block >= MAX_WAIT_BLOCKS:
            return result("stale", batch)
        for fi, f in enumerate(batch.frames):
            self._drain(index, f.safe_block, entries)
            for oi, op in enumerate(f.user_ops):
                who = U.recover(self.separator, op.nonce, op.max_fee, op.data, op.signature)
                why = "unrecoverable signature" if who is None else self.engine.user_op_invalid(who, op, f.fee_price)
                if why:
                    skipped.append(Skipped(index, batch.nonce, fi, oi, who, op.nonce, op.max_fee, f.fee_price, op.data, why))
                    continue
                e = Entry(offset=self.engine.executed, exec_input=index, source="user_op", sender=who, block=f.safe_block,
                          kind="", outcome="", batch_nonce=batch.nonce, frame=fi, op=oi, nonce=op.nonce,
                          max_fee=op.max_fee, fee=f.fee_price, data=op.data)
                self.engine.user_op(e, op, f.fee_price)
                entries.append(e)
        self.next_batch_nonce += 1
        return result("executed", batch)


def report_matches(predicted, payload: bytes) -> bool:
    """Does a node report payload match a predicted report (dict with ANY wildcards, raw bytes, None)?"""
    if predicted is None:
        return True
    if isinstance(predicted, bytes):
        return payload == predicted
    try:
        got = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(got, dict) or set(got) != set(predicted):
        return False
    return all(v is ANY or got[k] == v for k, v in predicted.items())


def describe_report(predicted) -> str:
    if predicted is None:
        return "<unknown>"
    if isinstance(predicted, bytes):
        return predicted.decode("utf-8", "replace")
    return json.dumps({k: ("*" if v is ANY else v) for k, v in predicted.items()}, sort_keys=True)
