"""The canonical model: fee port, SSZ batches, EIP-712 user ops, the scheduler fold and the engine."""
import json

import pytest
from eth_abi import encode
from eth_account import Account

from qatraffic import contracts as C
from qatraffic import fee as F
from qatraffic import fold as FD
from qatraffic import ssz
from qatraffic import userop as U

CHAIN, APP = 31337, "0x" + "a1" * 20
TOKEN = "0x7a051EDffC0884cd88d4a377F4C87BE074CF6c81"
PORTAL = "0x3332DE61a8BB9aC84893b2f552Fe81C9a6dC5419"
SEQ_KEY = bytes.fromhex("2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6")
SEQ = Account.from_key(SEQ_KEY).address
KA, KB = bytes.fromhex("11" * 32), bytes.fromhex("22" * 32)
A, B = Account.from_key(KA).address, Account.from_key(KB).address
CARL = "0x" + "c4" * 20
USDC = 10**6


# ---------------------------------------------------------------- fee, ssz, eip-712
def test_fee_vectors_match_the_app_and_the_sequencer():
    assert F.MAX_EXPONENT == 17101
    assert [F.to_linear(n) for n in (0, 1, 100, 1356)] == [1, 1, 2, 38276]
    assert hex(F.to_linear(17101)) == "0xff853cbad77d81420c3129ed78d03683ecdfc2c89dd67c94"
    with pytest.raises(ValueError):
        F.to_linear(17102)
    prev = 0
    for n in range(0, F.MAX_EXPONENT, 97):
        v = F.to_linear(n)
        assert v >= prev
        prev = v


def test_ssz_roundtrip_and_strictness():
    op = ssz.WireUserOp(7, 1409, b"\x42", b"\xee" * 65)
    b = ssz.Batch(3, [ssz.Frame(100, 1356, [op, op]), ssz.Frame(105, 0, [])])
    enc = ssz.encode_batch(b)
    assert ssz.decode_batch(enc) == b
    assert ssz.decode_batch(ssz.encode_batch(ssz.Batch(9, []))) == ssz.Batch(9, [])
    for bad in (b"", enc[:11], enc + b"\x00", enc[:-1], b"\x00" * 8 + (13).to_bytes(4, "little") + b"\x00"):
        with pytest.raises(ssz.SszError):
            ssz.decode_batch(bad)


def test_eip712_matches_eth_account_and_recovers():
    sep = U.domain_separator(CHAIN, APP)
    sig = U.sign(KA, sep, 3, 1409, b"\x01\x02")
    ref = Account.sign_typed_data(KA, domain_data=U.domain(CHAIN, APP), message_types=U.TYPES,
                                  message_data={"nonce": 3, "max_fee": 1409, "data": b"\x01\x02"})
    assert sig == bytes(ref.signature)
    assert U.recover(sep, 3, 1409, b"\x01\x02", sig) == A
    assert U.recover(sep, 3, 1409, b"\x01\x02", sig[:64] + bytes([sig[64] - 27])) == A
    assert U.recover(sep, 4, 1409, b"\x01\x02", sig) != A           # another message
    assert U.recover(U.domain_separator(1, APP), 3, 1409, b"\x01\x02", sig) != A  # another chain
    assert U.recover(sep, 3, 1409, b"\x01\x02", sig[:64] + b"\x05") is None
    assert U.recover(sep, 3, 1409, b"\x01\x02", sig[:64]) is None


# ---------------------------------------------------------------- fold helpers
def deposit(who, amount, data=b""):
    return bytes.fromhex(TOKEN[2:]) + bytes.fromhex(who[2:]) + amount.to_bytes(32, "big") + data


def op(key, nonce, payload, max_fee=10):
    return ssz.WireUserOp(nonce, max_fee, payload, U.sign(key, U.domain_separator(CHAIN, APP), nonce, max_fee, payload))


def batch(nonce, *frames):
    return ssz.encode_batch(ssz.Batch(nonce, [ssz.Frame(sb, fee, list(ops)) for sb, fee, ops in frames]))


def new_fold():
    return FD.Fold(chain_id=CHAIN, app=APP, token=TOKEN, sequencer=SEQ, erc20_portal=PORTAL, watched=[A, B])


class Feed:
    def __init__(self):
        self.f = new_fold()
        self.i = 0

    def __call__(self, sender, block, payload):
        r = self.f.process(self.i, sender, block, payload)
        self.i += 1
        return r


# ---------------------------------------------------------------- the scheduler
def test_worked_recovery_boundary_from_the_sequencer_docs():
    """docs/protocol/projection-replay.md "Worked recovery boundary", with real payloads."""
    feed = Feed()
    feed(PORTAL, 5, deposit(A, 100 * USDC))                                          # 0 D0
    feed(CARL, 12, b"\xde\xad")                                                       # 1 D1 (echo)
    r2 = feed(SEQ, 20, batch(0, (10, 0, [op(KA, 0, C.encode_transfer(TOKEN, B, 10 * USDC))])))  # 2 batch 0
    assert r2.outcome == "executed" and [e.kind for e in r2.entries] == ["deposit", "transfer"]
    feed(A, 20, C.encode_withdraw(TOKEN, 5 * USDC))                                   # 3 D2
    feed(PORTAL, 24, deposit(B, 1 * USDC))                                            # 4 D3
    r5 = feed(SEQ, 1230, b"\x01\x02\x03")                                             # 5 malformed batch
    assert r5.outcome == "rejected:decode"
    assert [(e.kind, e.direct_input, e.overdue) for e in r5.entries] == [("echo", 1, True), ("withdraw", 3, True),
                                                                          ("deposit", 4, True)]
    assert r5.reports[-1] == FD.DROPPED_BATCH_REPORT
    r6 = feed(SEQ, 1232, batch(1, (1228, 0, [op(KB, 0, C.encode_withdraw(TOKEN, 3 * USDC))])))  # 6 batch 1
    assert [(e.source, e.kind, e.outcome) for e in r6.entries] == [("user_op", "withdraw", "accept")]
    feed(PORTAL, 1235, deposit(B, 7))                                                 # 7 D4 queued
    f = feed.f
    assert f.engine.executed == 6 and f.next_batch_nonce == 2 and len(f.fridge) == 1
    assert [e.offset for e in f.entries] == list(range(6))
    assert f.balance(A) == 100 * USDC - 1 - 10 * USDC - 5 * USDC   # fee exponent 0 = 1 unit
    assert f.balance(B) == 10 * USDC + 1 * USDC - 1 - 3 * USDC
    assert f.balance(SEQ) == 2
    snap = f.snapshot_after(6)
    assert snap.executed == 6 and snap.queued_directs == 0 and snap.nonce_count == 2 and snap.last_block == 1228
    assert f.snapshot_after(7).queued_directs == 1
    assert r6.outputs == [FD.erc20_voucher(TOKEN, B, 3 * USDC)]


def test_batch_gates_and_nonce_consumption():
    feed = Feed()
    feed(PORTAL, 10, deposit(A, 50 * USDC))
    r = feed(SEQ, 20, batch(1, (15, 0, [])))
    assert r.outcome.startswith("rejected:wrong_nonce") and feed.f.next_batch_nonce == 0
    assert feed(SEQ, 21, batch(0)).outcome == "executed" and feed.f.next_batch_nonce == 1   # empty batch
    assert feed(SEQ, 22, batch(1, (23, 0, []))).outcome == "rejected:safe_block_above_inclusion"
    assert feed(SEQ, 23, batch(1, (20, 0, []), (19, 0, []))).outcome == "rejected:non_monotonic_safe_blocks"
    stale = feed(SEQ, 1300, batch(1, (99, 0, [op(KA, 0, C.encode_withdraw(TOKEN, USDC))])))
    assert stale.outcome == "stale" and feed.f.next_batch_nonce == 1 and feed.f.balance(A) == 50 * USDC - 0
    # the backstop already ran the overdue deposit before the stale batch was judged
    assert [e.kind for e in stale.entries] == ["deposit"]
    ok = feed(SEQ, 1301, batch(1, (1300, 0, [op(KA, 0, C.encode_withdraw(TOKEN, USDC))])))
    assert ok.outcome == "executed" and feed.f.next_batch_nonce == 2


def test_user_op_validation_and_business_failures():
    feed = Feed()
    feed(PORTAL, 10, deposit(A, 20 * USDC))
    fee = 1356  # 38276 units
    r = feed(SEQ, 12, batch(0, (10, fee, [
        op(KA, 1, C.encode_withdraw(TOKEN, USDC), max_fee=2000),                 # bad nonce: skipped
        op(KA, 0, C.encode_withdraw(TOKEN, USDC), max_fee=1000),                 # max_fee < fee: skipped
        op(KA, 0, C.encode_transfer(TOKEN, B, 25 * USDC), max_fee=2000),         # overdraft: counted no-op, fee paid
        op(KA, 1, C.encode_withdraw(TOKEN, 2 * USDC), max_fee=2000),             # ok
        op(KB, 0, C.encode_withdraw(TOKEN, 1), max_fee=2000),                    # no balance for the fee: skipped
        op(KA, 2, b"hello", max_fee=2000),                                        # echo
    ])))
    assert [s.reason.split(":")[0] for s in r.skipped] == ["bad nonce", "max_fee below frame fee", "fee balance"]
    assert [(e.kind, e.outcome) for e in r.entries] == [("deposit", "accept"), ("transfer", "reject"),
                                                       ("withdraw", "accept"), ("echo", "accept")]
    units = F.to_linear(fee)
    assert feed.f.balance(A) == 20 * USDC - 3 * units - 2 * USDC
    assert feed.f.balance(SEQ) == 3 * units and feed.f.nonce(A) == 3 and feed.f.nonce(B) == 0
    assert feed.f.engine.rejected == 1 and feed.f.engine.executed == 4
    assert r.outputs == [FD.erc20_voucher(TOKEN, A, 2 * USDC), FD.notice_output(b"hello")]
    assert len(r.reports) == 2 and r.complete
    assert FD.report_matches(r.reports[0], json.dumps({"deposit": "erc20", "sender": A.lower(), "token": TOKEN.lower(),
                                                       "amount": str(20 * USDC), "exec_layer_data": "0x",
                                                       "accounts_drive": True, "credited": True}).encode())
    assert FD.report_matches(r.reports[1], b'{"code":-12,"error":"transfer: insufficient funds"}')
    assert not FD.report_matches(r.reports[1], b'{"transfer":"ok"}')


def test_direct_rules_and_unknown_assets():
    feed = Feed()
    r = feed(A, 5, C.encode_withdraw(TOKEN, USDC))          # never deposited: rejected, still counted
    assert r.entries == [] and feed.f.engine.executed == 0
    r = feed(SEQ, 6, batch(0, (5, 0, [])))
    assert [(e.kind, e.outcome) for e in r.entries] == [("withdraw", "reject")] and r.outputs == []
    other = "0x" + "99" * 20
    feed(PORTAL, 7, bytes.fromhex(other[2:]) + bytes.fromhex(A[2:]) + (5).to_bytes(32, "big"))   # another ERC-20
    feed(A, 7, C.selector("WithdrawEther(uint256,bytes)") + encode(["uint256", "bytes"], [1, b""]))
    feed(A, 7, C.encode_transfer(TOKEN, A, 1))                # to self: not modelled
    r = feed(SEQ, 8, batch(1, (7, 0, [])))
    assert [e.outcome for e in r.entries] == ["unknown", "unknown", "unknown"] and not r.complete
    assert feed.f.balance(A) == 0 and feed.f.engine.executed == 4
    snap = feed.f.snapshot_after(feed.i - 1)
    assert snap.rejected_min == 1 and snap.rejected_max == 4
    # a virtual account id (not 12 zero bytes + address) is its own account
    feed(PORTAL, 9, deposit(A, 10))
    vid = (2**200).to_bytes(32, "big")
    feed(A, 9, C.selector(C.SIG_TRANSFER_ERC20) + encode(["address", "bytes32", "uint256", "bytes"], [TOKEN, vid, 4, b""]))
    feed(SEQ, 10, batch(2, (9, 0, [])))
    assert feed.f.balance(A) == 6 and feed.f.engine.balance(2**200) == 4


def test_uint96_cap_on_deposits():
    feed = Feed()
    feed(PORTAL, 1, deposit(A, FD.UINT96_MAX))
    feed(PORTAL, 1, deposit(A, 1))
    r = feed(SEQ, 2, batch(0, (1, 0, [])))
    assert [(e.kind, e.outcome) for e in r.entries] == [("deposit", "accept"), ("deposit", "reject")]
    assert r.reports[1]["credited"] is False


def test_inspect_amount_parsing_both_drivers():
    from qatraffic.node import Node, NodeError

    def res(status, *payloads):
        return {"status": status, "reports": [{"payload": "0x" + p.hex()} for p in payloads]}
    assert Node._amount(res("Accepted", (42).to_bytes(32, "big"))) == 42
    not_found = b'{"code":-1004,"error":"retrieve account: Account not found"}'
    assert Node._amount(res("Accepted", not_found)) == 0       # sequencer driver: accepted + error report
    assert Node._amount(res("Rejected", not_found)) == 0       # classic driver: rejected
    with pytest.raises(NodeError):
        Node._amount(res("Accepted", b'{"error":"unknown inspect: x"}'))
    with pytest.raises(NodeError):
        Node._amount(res("Accepted", b"\x01" * 31))


# ---------------------------------------------------------------- canon bookkeeping
class _W:
    def __init__(self, slot, key):
        self.slot, self.key = slot, key
        self.address = Account.from_key(key).address


def _canon(tmp_path):
    from types import SimpleNamespace

    from qatraffic.alerts import Alerts
    from qatraffic.canon import Canon
    from qatraffic.db import DB
    from qatraffic.events import EventLog
    from qatraffic.notify import Outbox
    db = DB(tmp_path / "s.sqlite3")
    log = EventLog(tmp_path / "logs", min_level="error")
    cfg = SimpleNamespace(chain_id=CHAIN, app_address=APP, token=TOKEN, erc20_portal=PORTAL, other_portals=[],
                          log_chunk_blocks=100)
    ctx = SimpleNamespace(cfg=cfg, db=db, log=log, run_id=1, wallets=[_W(0, KA), _W(1, KB)],
                          alerts=Alerts(db, log, Outbox(db, log, None, "[t]"), 6))
    ctx.wallet = lambda s: ctx.wallets[s]
    c = Canon(ctx)
    c.fold = FD.Fold(chain_id=CHAIN, app=APP, token=TOKEN, sequencer=SEQ, erc20_portal=PORTAL, watched=[A, B])
    c.sequencer, c.read_head, c.safe_head = SEQ, 0, 0
    return c, ctx


def _input(c, ctx, index, sender, block, payload):
    ctx.db.x("INSERT INTO l1_inputs(run_id,input_index,block,block_ts,tx_hash,sender,payload,seen_at) VALUES(1,?,?,?,?,?,?,0)",
             (index, block, 1000 + block, "0x00", sender, payload))
    c._fold_one(index, sender, block, payload, 1000 + block, replay=False)


def _action(ctx, step, slot, kind, route, amount, l1_input=None, l1_block=None, counterparty=None, user_op_id=None):
    ctx.db.x("INSERT INTO actions(run_id,step_id,slot,kind,route,amount,counterparty,intended,l1_input,l1_block,user_op_id,"
             "created_at,state) VALUES(1,?,?,?,?,?,?,'accept',?,?,?,0,'pending')",
             (step, slot, kind, route, str(amount), counterparty, l1_input, l1_block, user_op_id))


def test_canon_binds_deposits_to_the_depositor_and_projects_spendable(tmp_path):
    c, ctx = _canon(tmp_path)
    _action(ctx, 1, 0, "deposit", "l1", 50 * USDC, l1_input=0, l1_block=10)
    _input(c, ctx, 0, PORTAL, 10, deposit(A, 50 * USDC))
    # queued, not executed: the l1 route may count it (FIFO), the sequencer only once it covers block 10
    assert c.spendable(0, "l1") == 50 * USDC and c.spendable(0, "seq") == 0
    c.safe_head = 16
    assert c.spendable(0, "seq") == 50 * USDC
    _input(c, ctx, 1, SEQ, 20, batch(0, (12, 0, [])))
    act = ctx.db.q1("SELECT * FROM actions WHERE step_id=1")
    assert act["state"] == "executed" and act["exec_input"] == 1 and act["outcome"] == "accept"
    assert c.spendable(0, "l1") == 50 * USDC and c.spendable(0, "seq") == 50 * USDC   # not counted twice
    # a soft-confirmed seq withdrawal of 20 (max fee 1409) is a pending debit; a friend's soft transfer a credit
    ctx.db.x("INSERT INTO user_ops(id,run_id,step_id,slot,sender,nonce,max_fee,data,signature,status,signed_at) "
             "VALUES(7,1,2,0,?,0,1409,x'',x'','soft',0)", (A,))
    _action(ctx, 2, 0, "withdraw", "seq", 20 * USDC, user_op_id=7)
    ctx.db.x("INSERT INTO user_ops(id,run_id,step_id,slot,sender,nonce,max_fee,data,signature,status,signed_at) "
             "VALUES(8,1,3,1,?,0,1409,x'',x'','soft',0)", (B,))
    _action(ctx, 3, 1, "transfer", "seq", 5 * USDC, counterparty=0, user_op_id=8)
    assert c.spendable(0, "seq") == 50 * USDC - 20 * USDC - F.to_linear(1409) + 5 * USDC
    assert c.spendable(0, "seq", upper=True) == 55 * USDC
    assert c.pending_nonce(0) == 1


def test_canon_late_bind_reconcile_and_foreign(tmp_path):
    c, ctx = _canon(tmp_path)

    # Normal late receipt (within the reconcile grace): the bot records the action, then bind_late
    # marks it executed once the deposit is folded.
    _input(c, ctx, 0, PORTAL, 10, deposit(A, 9 * USDC))
    _input(c, ctx, 1, SEQ, 20, batch(0, (12, 0, [])))                 # executes the deposit at block 10
    _action(ctx, 1, 0, "deposit", "l1", 9 * USDC, l1_input=0, l1_block=10)
    c.bind_late(0)
    assert ctx.db.q1("SELECT state FROM actions WHERE step_id=1")["state"] == "executed"

    # A deposit crediting a bot account with NO local record self-heals (funds came IN, never a
    # theft vector): orphans() records it and raises no CRITICAL, and is idempotent.
    _input(c, ctx, 2, PORTAL, 30, deposit(B, 4 * USDC))
    _input(c, ctx, 3, SEQ, 40, batch(1, (32, 0, [])))                 # executes the deposit at block 30
    c.orphans(grace_s=-1)
    rec = ctx.db.q1("SELECT * FROM actions WHERE run_id=1 AND l1_input=2")
    assert rec is not None and rec["kind"] == "deposit" and rec["state"] == "executed"
    assert ctx.db.q1("SELECT 1 FROM alerts WHERE key='canon.foreign_direct' AND resolved_at IS NULL") is None
    c.orphans(grace_s=-1)
    assert ctx.db.q1("SELECT COUNT(*) AS n FROM actions WHERE l1_input=2")["n"] == 1   # no duplicate

    # A withdrawal (funds OUT) from a bot account with NO local trace is genuinely foreign: CRITICAL.
    _input(c, ctx, 4, A, 50, C.encode_withdraw(TOKEN, 2 * USDC))
    _input(c, ctx, 5, SEQ, 60, batch(2, (52, 0, [])))                # executes the direct withdraw
    c.orphans(grace_s=-1)
    al = ctx.db.q1("SELECT severity FROM alerts WHERE key='canon.foreign_direct' AND resolved_at IS NULL")
    assert al is not None and al["severity"] == "critical"

    # The same direct, but matching a terminal local step for that slot/kind/amount, is the bot's
    # own mis-tracked input -> reconciled (bound to the step), clearing the CRITICAL.
    ctx.db.x("INSERT INTO steps(run_id,seq,kind,route,slot,params,status,due_at,created_at,updated_at) "
             "VALUES(1,0,'withdraw','l1',0,?,'failed',0,0,0)", (json.dumps({"amount": str(2 * USDC)}),))
    c.orphans(grace_s=-1)
    assert ctx.db.q1("SELECT 1 FROM actions WHERE run_id=1 AND l1_input=4") is not None
    assert ctx.db.q1("SELECT resolved_at FROM alerts WHERE key='canon.foreign_direct'")["resolved_at"] is not None
