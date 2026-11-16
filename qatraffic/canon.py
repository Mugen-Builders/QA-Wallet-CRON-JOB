"""L1 -> canonical fold -> the bot's books.

Every InputBox input of the application (deposits, direct inputs and the sequencer's batches, from
anyone) is read from L1 up to the configured read tag (`safe` by default), stored, and folded in
order by fold.Fold. Each executed application input (an *entry*) is then tied back to what the bot
did:

- a deposit or an L1-route transfer/withdrawal is an InputBox input of ours (actions.l1_input);
  it executes when the scheduler drains it, which can be several inputs later;
- a sequencer-route transfer/withdrawal is a user op of ours (user_ops: sender + nonce); it
  executes inside the batch that carries it, or is skipped by the scheduler.

The fold is rebuilt from the stored inputs on every start, so nothing about it is persisted except
the inputs themselves; entries and outcomes are rewritten idempotently.
"""
from __future__ import annotations

import json
import time

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from . import fee as F
from .chain import Reverted, RpcAnswer, RpcUnavailable
from .fold import Fold, decode_evm_advance, wallet_id
from .node import NodeError, NodeUnavailable

INPUT_ADDED_TOPIC = keccak(text="InputAdded(address,uint256,bytes)")
DRAIN_MARGIN_BLOCKS = 6  # a direct at block b is in the sequencer's state once its safe head passes b + 5


class CanonError(RuntimeError):
    pass


class Canon:
    def __init__(self, ctx):
        self.ctx = ctx
        self.fold: Fold | None = None
        self.results: dict[int, object] = {}
        self.sequencer: str | None = None
        self.deploy_block: int | None = None
        self.read_head: int | None = None   # last block fully ingested
        self.safe_head: int | None = None   # latest read-tag block seen
        self.addr_slot = {w.address.lower(): w.slot for w in ctx.wallets}
        self.id_slot = {wallet_id(w.address): w.slot for w in ctx.wallets}
        self._chunk = ctx.cfg.log_chunk_blocks
        self.last_batch_ts: float | None = None

    # ------------------------------------------------------------------ bootstrap
    def start(self):
        ctx, cfg = self.ctx, self.ctx.cfg
        self.sequencer = self._batch_submitter()
        self.deploy_block = ctx.l1._call(lambda w3: ctx.l1._c(w3, ctx.l1.app).functions.getDeploymentBlockNumber().call())
        self.fold = Fold(chain_id=cfg.chain_id, app=cfg.app_address, token=cfg.token, sequencer=self.sequencer,
                         erc20_portal=cfg.erc20_portal, other_portals=set(cfg.other_portals),
                         watched=[w.address for w in ctx.wallets])
        rows = ctx.db.q("SELECT * FROM l1_inputs WHERE run_id=? ORDER BY input_index", (ctx.run_id,))
        t0 = time.time()
        for r in rows:
            self._fold_one(r["input_index"], r["sender"], r["block"], bytes(r["payload"]), r["block_ts"], replay=True)
        self.read_head = int(ctx.db.get_meta(f"run:{ctx.run_id}:l1_read_head", self.deploy_block - 1))
        ctx.log.info("canon.ready", sequencer=self.sequencer, deploy_block=self.deploy_block, replayed=len(rows),
                     seconds=round(time.time() - t0, 2), executed=self.fold.engine.executed,
                     next_batch_nonce=self.fold.next_batch_nonce, queued=len(self.fold.fridge))

    def _batch_submitter(self) -> str:
        """The batch submitter the machine pinned at genesis (the node's `state` inspect), checked
        against the configured one when both exist."""
        ctx, cfg = self.ctx, self.ctx.cfg
        pinned = None
        try:
            st = ctx.node.state()
            pinned = to_checksum_address(st["sequencer_address"])
            if st.get("token", "").lower() != cfg.token.lower():
                ctx.alerts.raise_("app.token", "critical", "The app's accounts-drive token is not the configured token",
                                  f"the machine pinned {st.get('token')} at genesis, the bot uses {cfg.token}. Deposits "
                                  "of the bot's token would not be on the accounts drive. Traffic is paused.")
                ctx.paused_reason = "accounts-drive token mismatch"
            else:
                ctx.alerts.resolve("app.token", "the app's accounts-drive token is the configured token")
        except (NodeError, NodeUnavailable, KeyError, ValueError) as e:
            ctx.log.warn("canon.state_inspect_failed", error=str(e)[:160])
        if cfg.batch_submitter and pinned and cfg.batch_submitter.lower() != pinned.lower():
            raise CanonError(f"QA_TRAFFIC_BATCH_SUBMITTER {cfg.batch_submitter} != the machine's {pinned}")
        found = cfg.batch_submitter or pinned
        if not found:
            raise CanonError("batch submitter unknown: set QA_TRAFFIC_BATCH_SUBMITTER or make the node inspectable")
        return to_checksum_address(found)

    # ------------------------------------------------------------------ L1 sync
    def sync(self, max_inputs: int = 500) -> int:
        """Read new InputBox inputs up to the read tag and fold them. Returns how many were added."""
        ctx, cfg = self.ctx, self.ctx.cfg
        head = ctx.l1.block(cfg.l1_read_tag)["number"] - cfg.l1_read_extra_confirmations
        # the sequencer frames directs by the L1 *safe* head, whatever tag the fold reads
        self.safe_head = head + cfg.l1_read_extra_confirmations if cfg.l1_read_tag == "safe" else ctx.l1.block("safe")["number"]
        if head <= self.read_head:
            return 0
        added = 0
        frm = self.read_head + 1
        while frm <= head and added < max_inputs:
            to = min(head, frm + self._chunk - 1)
            try:
                logs = ctx.l1._call(lambda w3: w3.eth.get_logs({
                    "address": ctx.l1.input_box.address, "fromBlock": frm, "toBlock": to,
                    "topics": ["0x" + INPUT_ADDED_TOPIC.hex(), "0x" + bytes(12).hex() + cfg.app_address[2:].lower()]}),
                    retries=0)
            except (RpcAnswer, RpcUnavailable):
                # providers cap eth_getLogs ranges with errors the L1 client may class as an answer
                # or as an outage ("limit exceeded", -32005): narrow the window before giving up
                if self._chunk > 1:
                    self._chunk = max(1, self._chunk // 4)
                    ctx.log.warn("canon.log_range_shrunk", chunk=self._chunk)
                    continue
                raise
            for lg in sorted(logs, key=lambda x: (x["blockNumber"], x["logIndex"])):
                self._ingest_log(lg)
                added += 1
            self.read_head = to
            ctx.db.set_meta(f"run:{ctx.run_id}:l1_read_head", to)
            frm = to + 1
            if self._chunk < cfg.log_chunk_blocks:
                self._chunk = min(cfg.log_chunk_blocks, self._chunk * 2)
        if added:
            self._check_count()
        return added

    def _ingest_log(self, lg):
        ctx = self.ctx
        index = int.from_bytes(bytes(lg["topics"][2]), "big")
        if index != self.fold.processed:
            raise CanonError(f"InputBox index {index} arrived while {self.fold.processed} was expected (missed or reorged logs)")
        (raw,) = abi_decode(["bytes"], bytes(lg["data"]))
        adv = decode_evm_advance(bytes(raw))
        if adv["index"] != index or adv["app"].lower() != ctx.cfg.app_address.lower():
            raise CanonError(f"EvmAdvance of input {index} names index {adv['index']} / app {adv['app']}")
        ctx.db.x("INSERT OR REPLACE INTO l1_inputs(run_id,input_index,block,block_ts,tx_hash,sender,payload,seen_at) "
                 "VALUES(?,?,?,?,?,?,?,?)",
                 (ctx.run_id, index, adv["block"], adv["timestamp"], lg["transactionHash"].to_0x_hex(),
                  adv["sender"], adv["payload"], time.time()))
        self._fold_one(index, adv["sender"], adv["block"], adv["payload"], adv["timestamp"], replay=False)

    def _check_count(self):
        """InputBox.getNumberOfInputs(app) at the read head must equal what we folded."""
        ctx = self.ctx
        try:
            n = ctx.l1._call(lambda w3: ctx.l1._c(w3, ctx.l1.input_box).functions.getNumberOfInputs(
                ctx.cfg.app_address).call(block_identifier=self.read_head))
        except (RpcUnavailable, Reverted, RpcAnswer):
            return
        if n != self.fold.processed:
            ctx.alerts.raise_("canon.input_count", "critical", "The bot's view of the InputBox is incomplete",
                              f"getNumberOfInputs={n} at block {self.read_head}, folded {self.fold.processed}")
        else:
            ctx.alerts.resolve("canon.input_count", "InputBox count matches")

    # ------------------------------------------------------------------ fold + bookkeeping
    def _fold_one(self, index, sender, block, payload, block_ts, replay: bool):
        ctx = self.ctx
        res = self.fold.process(index, sender, block, payload)
        self.results[index] = res
        ctx.db.x("UPDATE l1_inputs SET kind=?, outcome=?, batch_nonce=?, entries=?, complete=?, executed_after=? "
                 "WHERE run_id=? AND input_index=?",
                 (res.kind, res.outcome, res.batch_nonce, len(res.entries), 1 if res.complete else 0,
                  self.fold.engine.executed, ctx.run_id, index))
        if res.kind == "batch":
            self.last_batch_ts = float(block_ts)
            if not replay:
                self._batch_events(res)
        for e in res.entries:
            slot = self.addr_slot.get(e.sender.lower())
            if e.kind == "deposit" and e.to_id is not None:
                slot = self.id_slot.get(e.to_id)  # the portal sends it; the depositor owns it
            ctx.db.x("INSERT OR REPLACE INTO entries(run_id,offset,exec_input,source,direct_input,batch_nonce,frame,op,sender,"
                     "slot,nonce,fee,fee_units,kind,amount,to_id,outcome,reason,block,overdue) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ctx.run_id, e.offset, e.exec_input, e.source, e.direct_input, e.batch_nonce, e.frame, e.op, e.sender,
                      slot, e.nonce, e.fee, str(e.fee_units), e.kind, None if e.amount is None else str(e.amount),
                      None if e.to_id is None else hex(e.to_id), e.outcome, e.reason, e.block, 1 if e.overdue else 0))
            if e.source == "direct" or slot is not None:
                self._bind_entry(e, slot, block_ts, replay)
        for s in res.skipped:
            if s.sender and s.sender.lower() in self.addr_slot:
                self._bind_skip(s, replay)

    def _bind_entry(self, e, slot, block_ts, replay):
        ctx = self.ctx
        if e.source == "direct":
            act = ctx.db.q1("SELECT * FROM actions WHERE run_id=? AND l1_input=?", (ctx.run_id, e.direct_input))
            if act is None:
                return  # not ours, or our receipt is not processed yet: bind_late() / orphans()
        else:
            uo = ctx.db.q1("SELECT * FROM user_ops WHERE run_id=? AND lower(sender)=? AND nonce=? AND status IN "
                           "('signed','soft','executed','rejected','invalidated') ORDER BY id DESC LIMIT 1",
                           (ctx.run_id, e.sender.lower(), e.nonce))
            if uo is None or bytes(uo["data"]) != e.data or uo["max_fee"] != e.max_fee:
                if not replay:
                    ctx.alerts.raise_(f"canon.foreign_op.{e.sender}.{e.nonce}", "critical",
                                      "A bot account has a user op the bot did not sign",
                                      f"slot {slot} nonce {e.nonce} executed in input {e.exec_input} ({e.kind}); "
                                      + ("no record" if uo is None else "content differs from what the bot signed"))
                return
            ctx.db.x("UPDATE user_ops SET status='executed', exec_input=?, offset=?, batch_nonce=?, fee_price=?, outcome=?, "
                     "landed_at=COALESCE(landed_at, ?) WHERE id=?",
                     (e.exec_input, e.offset, e.batch_nonce, e.fee, e.outcome, block_ts, uo["id"]))
            act = ctx.db.q1("SELECT * FROM actions WHERE run_id=? AND user_op_id=?", (ctx.run_id, uo["id"]))
            if act is None:
                return  # soft confirmation not recorded yet (crash window); recover() links it
        ctx.db.x("UPDATE actions SET state='executed', offset=?, exec_input=?, outcome=?, fee_units=?, exec_block=?, "
                 "executed_at=COALESCE(executed_at, ?) WHERE id=?",
                 (e.offset, e.exec_input, e.outcome, str(e.fee_units), e.block, block_ts, act["id"]))
        if not replay:
            if e.outcome != "unknown" and act["intended"] != e.outcome:
                ctx.log.warn("model.intent_differs", action=act["id"], slot=slot, kind=e.kind, route=act["route"],
                             intended=act["intended"], canonical=e.outcome, offset=e.offset)
            ctx.log.info("action.executed", action=act["id"], slot=slot, kind=e.kind, route=act["route"], outcome=e.outcome,
                         offset=e.offset, exec_input=e.exec_input, overdue=e.overdue or None,
                         fee_units=e.fee_units or None)

    def bind_late(self, l1_input: int):
        """The bot recorded an L1 input after the fold had already executed it."""
        ctx = self.ctx
        row = ctx.db.q1("SELECT e.*, i.block_ts FROM entries e JOIN l1_inputs i ON i.run_id=e.run_id AND "
                        "i.input_index=e.exec_input WHERE e.run_id=? AND e.direct_input=?", (ctx.run_id, l1_input))
        if row is None:
            return
        ctx.db.x("UPDATE actions SET state='executed', offset=?, exec_input=?, outcome=?, fee_units=?, exec_block=?, "
                 "executed_at=COALESCE(executed_at, ?) WHERE run_id=? AND l1_input=?",
                 (row["offset"], row["exec_input"], row["outcome"], row["fee_units"], row["block"], row["block_ts"],
                  ctx.run_id, l1_input))
        ctx.log.info("action.executed_late_bind", l1_input=l1_input, offset=row["offset"], outcome=row["outcome"])

    def orphans(self, grace_s: float = 900):
        """Executed directs of bot accounts (sent by them, or deposits crediting them) with no bot
        record. Two causes: (1) the bot lost/mis-tracked a record of its OWN input (e.g. a tx
        mis-marked 'dropped: nonce consumed' during an RPC wobble that actually landed) or (2) an
        outside party holds the key and moved funds. We self-heal (1) so no manual DB surgery is
        ever needed in production, and reserve the CRITICAL alert for (2):
          - a DEPOSIT credits our own account (funds in) -- never a theft vector, so always
            reconciled from chain into a recorded action;
          - any other direct that matches a terminal local step for the same slot/kind/amount is
            our own mis-tracked input -- reconciled, bound to that step;
          - a funds-OUT direct (transfer/withdraw) with NO local trace is genuinely foreign and
            stays CRITICAL.
        Reconciling only records movement that already happened on chain; it never sends anything."""
        ctx = self.ctx
        rows = ctx.db.q(
            "SELECT e.direct_input, e.slot, e.kind, e.offset, e.exec_input, e.amount, e.outcome, e.block, "
            "i.block_ts, i.block AS l1_block "
            "FROM entries e JOIN l1_inputs i ON i.run_id=e.run_id AND i.input_index=e.direct_input "
            "LEFT JOIN actions a ON a.run_id=e.run_id AND a.l1_input=e.direct_input "
            "WHERE e.run_id=? AND e.source='direct' AND e.slot IS NOT NULL AND e.kind!='echo' AND a.id IS NULL "
            "AND i.seen_at<?", (ctx.run_id, time.time() - grace_s))
        foreign = []
        for r in rows:
            step = self._originating_step(r)
            if r["kind"] == "deposit" or step is not None:
                self._reconcile_orphan(r, step)
            else:
                foreign.append(r)
        if foreign:
            ctx.alerts.raise_("canon.foreign_direct", "critical", "Bot accounts have inputs the bot has no record of",
                              "; ".join(f"input {r['direct_input']} slot {r['slot']} ({r['kind']})" for r in foreign[:8])
                              + ". A funds-leaving input with no local trace -- the bot lost a record, "
                              "or someone else holds these keys. Investigate the slot's key.")
        else:
            ctx.alerts.resolve("canon.foreign_direct", "every bot input is accounted for")

    def _originating_step(self, r) -> dict | None:
        """The terminal local step this on-chain input most likely came from: same slot and kind,
        no action yet, and (when the step records an amount) the same amount. Evidence the input
        was the bot's own -- e.g. step 'failed'/'reverted' while the tx actually landed under a
        racing hash. None means no local trace at all."""
        ctx = self.ctx
        want = str(r["amount"]) if r["amount"] is not None else None
        for s in ctx.db.q("SELECT * FROM steps WHERE run_id=? AND slot=? AND kind=? "
                          "AND status IN ('failed','reverted','skipped') ORDER BY id DESC",
                          (ctx.run_id, r["slot"], r["kind"])):
            if ctx.db.q1("SELECT 1 FROM actions WHERE step_id=? LIMIT 1", (s["id"],)):
                continue
            try:
                amt = json.loads(s["params"]).get("amount")
            except (ValueError, TypeError):
                amt = None
            if want is None or amt is None or str(amt) == want:
                return s
        return None

    def _reconcile_orphan(self, r, step):
        """Record an on-chain input that credits/comes from a bot account but has no local action,
        so the books match the chain and the CRITICAL clears. Binds to its originating step when
        found, else synthesises a minimal step (actions.step_id is NOT NULL). Idempotent: the
        UNIQUE(run_id,l1_input) index means a second pass over the same input is a no-op."""
        ctx = self.ctx
        if ctx.db.q1("SELECT 1 FROM actions WHERE run_id=? AND l1_input=? LIMIT 1", (ctx.run_id, r["direct_input"])):
            return
        now = time.time()
        if step is None:
            step_id = ctx.db.x(
                "INSERT INTO steps(session_id,run_id,seq,kind,route,slot,params,status,due_at,note,created_at,updated_at) "
                "VALUES(NULL,?,0,?,'l1',?,?,'mined',?,?,?,?)",
                (ctx.run_id, r["kind"], r["slot"], json.dumps({"amount": str(r["amount"]), "reconciled": True}),
                 now, "synthetic: reconciled from chain (no local step)", now, now)).lastrowid
        else:
            step_id = step["id"]
            ctx.db.x("UPDATE steps SET status='mined', input_index=?, "
                     "note=COALESCE(note,'') || ' | reconciled: input landed despite the local drop', updated_at=? WHERE id=?",
                     (r["direct_input"], now, step_id))
        ctx.db.x(
            "INSERT OR IGNORE INTO actions(run_id,step_id,slot,kind,route,amount,counterparty,intended,l1_input,l1_block,"
            "created_at,state,offset,exec_input,outcome,exec_block,executed_at,note) "
            "VALUES(?,?,?,?,'l1',?,?,?,?,?,?,'executed',?,?,?,?,?,?)",
            (ctx.run_id, step_id, r["slot"], r["kind"], str(r["amount"]), None, r["outcome"] or "accept",
             r["direct_input"], r["l1_block"], now, r["offset"], r["exec_input"], r["outcome"],
             r["block"], r["block_ts"] or now,
             "reconciled from chain: input landed with no local record (nonce race / mis-marked drop)"))
        ctx.log.warn("canon.reconciled_orphan", input=r["direct_input"], slot=r["slot"], kind=r["kind"],
                     amount=str(r["amount"]), from_step=step_id, synthetic=step is None)

    def _bind_skip(self, s, replay):
        ctx = self.ctx
        uo = ctx.db.q1("SELECT * FROM user_ops WHERE run_id=? AND lower(sender)=? AND nonce=? AND status IN ('soft','signed') "
                       "ORDER BY id DESC LIMIT 1", (ctx.run_id, s.sender.lower(), s.nonce))
        if uo is None or bytes(uo["data"]) != s.data:
            return
        ctx.db.x("UPDATE user_ops SET status='skipped', exec_input=?, batch_nonce=?, fee_price=?, error=? WHERE id=?",
                 (s.exec_input, s.batch_nonce, s.fee, s.reason, uo["id"]))
        ctx.db.x("UPDATE actions SET state='skipped', note=? WHERE run_id=? AND user_op_id=?", (s.reason, ctx.run_id, uo["id"]))
        if not replay and uo["status"] == "soft":
            ctx.alerts.raise_("seq.soft_skipped", "critical", "A soft-confirmed user op was skipped on L1",
                              f"slot {self.addr_slot[s.sender.lower()]} nonce {s.nonce}: the sequencer confirmed it, the "
                              f"scheduler skipped it in batch {s.batch_nonce} (input {s.exec_input}): {s.reason}")

    def _batch_events(self, res):
        ctx = self.ctx
        if res.outcome == "stale":
            ctx.alerts.raise_("seq.batch_stale", "warning", "A sequencer batch landed stale",
                              f"batch {res.batch_nonce} (input {res.index}, first frame safe block {res.first_safe_block}, "
                              f"included at {res.block}): its {res.ops} user op(s) did not execute; the sequencer has to "
                              "replace it at the same nonce")
        elif res.outcome.startswith("rejected"):
            ctx.alerts.raise_("seq.batch_rejected", "critical", "The scheduler rejected a sequencer batch",
                              f"input {res.index} from the batch submitter: {res.outcome}")
        ctx.log.info("canon.batch", input_index=res.index, batch_nonce=res.batch_nonce, outcome=res.outcome,
                     frames=res.frames, ops=res.ops, executed=len(res.entries), skipped=len(res.skipped))

    # ------------------------------------------------------------------ planning view
    def spendable(self, slot: int, route: str, upper: bool = False) -> int:
        """What `slot` can spend now without risking an overdraft in any order the scheduler may
        pick: executed balance, minus every pending debit (and the worst-case fee of pending user
        ops), plus pending credits that are certain to execute first. With upper=True: the most
        the account can possibly hold when the next input executes (every pending credit, no
        pending debit), which a deliberate overdraft must exceed."""
        ctx = self.ctx
        addr = ctx.wallet(slot).address
        bal = self.fold.balance(addr)
        for a in ctx.db.q("SELECT a.*, u.max_fee FROM actions a LEFT JOIN user_ops u ON u.id=a.user_op_id "
                          "WHERE a.run_id=? AND a.state='pending' AND (a.slot=? OR a.counterparty=?)", (ctx.run_id, slot, slot)):
            amount = int(a["amount"])
            if a["slot"] == slot:
                if a["kind"] == "deposit":
                    if upper or route == "l1" or (a["l1_block"] is not None and self.safe_head is not None
                                                  and a["l1_block"] + DRAIN_MARGIN_BLOCKS <= self.safe_head):
                        bal += amount
                    continue
                if upper:
                    continue
                if a["route"] == "seq" and a["max_fee"] is not None:
                    bal -= F.to_linear(a["max_fee"])
                if a["intended"] == "accept":
                    bal -= amount
            elif a["kind"] == "transfer" and (upper or (a["route"] == "seq" and a["intended"] == "accept")):
                bal += amount  # soft-confirmed by a friend: ordered before anything we send now
        return max(0, bal)

    def has_pending_deposit(self, slot: int) -> bool:
        return self.ctx.db.q1("SELECT 1 FROM actions WHERE run_id=? AND slot=? AND kind='deposit' AND state='pending' LIMIT 1",
                              (self.ctx.run_id, slot)) is not None

    def pending_nonce(self, slot: int) -> int:
        """The nonce the next user op of `slot` should carry: canonical nonce + soft ops not landed."""
        ctx = self.ctx
        n = ctx.db.q1("SELECT COUNT(*) AS n FROM user_ops WHERE run_id=? AND slot=? AND status='soft'",
                      (ctx.run_id, slot))["n"]
        return self.fold.nonce(ctx.wallet(slot).address) + n

    def direct_queue_age_blocks(self, slot_only: bool = True) -> int | None:
        """Age in blocks of the oldest queued direct (ours only by default) at the read head."""
        if self.safe_head is None:
            return None
        rows = [d for d in self.fold.queued() if not slot_only or d[1].lower() in self.addr_slot]
        return (self.safe_head - rows[0][2]) if rows else None

    def batch_silence_minutes(self) -> float | None:
        if self.last_batch_ts is None:
            return None
        return (time.time() - self.last_batch_ts) / 60

    def watched_id(self, slot: int) -> int:
        return wallet_id(self.ctx.wallet(slot).address)

    def results_json(self, index: int) -> str:
        r = self.results.get(index)
        return json.dumps({"outcome": r.outcome, "entries": len(r.entries)} if r else {})
