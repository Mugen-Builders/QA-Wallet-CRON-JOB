"""Checks: the canonical fold (canon.py) against the node, the chain and the sequencer.

node_io   every input the node processed emitted exactly the outputs and reports the fold predicts
          (exactly when every entry of the input is modelled, as an ordered subset otherwise)
state     the app's `state` inspect equals the fold at the same input: executed count, scheduler
          nonce and queue, nonce table size, rejected count, clock, pinned token and submitter
balances  sampled accounts: inspect ledger_getBalance / ledger_getNonce == the fold at that input
custody   token.balanceOf(app) >= ledger total supply + every unexecuted voucher (all senders)
sequencer soft-confirmed ops land on L1 in time and are not dropped; batches keep coming; directs
          are drained; (optional) the sequencer's accepted checkpoint equals the fold
node      the node keeps up with L1
app       the application is not foreclosed and the node reports it OK

Inspect answers carry the processed-input count they reflect and the fold keeps a snapshot after
every input, so every comparison is between consistent snapshots, at any moment.
"""
from __future__ import annotations

import time

from . import contracts as C
from .chain import RpcUnavailable
from .fold import describe_report, report_matches
from .ingest import node_lag
from .node import NodeError, NodeUnavailable, un
from .seqclient import SeqRejected, SeqUnavailable

USDC = 10**6


def _b(h: str) -> bytes:
    return bytes.fromhex(h[2:] if h.startswith("0x") else h)


class Checks:
    def __init__(self, ctx):
        self.ctx = ctx
        self._rot = 0
        self.last_custody: dict | None = None
        self.last_state: dict | None = None

    def run(self):
        ctx = self.ctx
        if not ctx.cfg.app_configured or ctx.canon is None:
            return
        self.app_health()
        self.node_progress()
        self.node_io()
        self.state_and_balances()
        self.custody()
        self.sequencer()

    # ---- node outputs and reports, input by input ----
    def node_io(self, limit: int = 40):
        ctx = self.ctx
        canon = ctx.canon
        rows = ctx.db.q("SELECT input_index FROM l1_inputs WHERE run_id=? AND node_checked=0 AND node_status='ACCEPTED' "
                        "ORDER BY input_index LIMIT ?", (ctx.run_id, limit))
        bad, checked = [], 0
        for r in rows:
            i = r["input_index"]
            res = canon.results.get(i)
            if res is None:
                continue
            try:
                outs = [_b(o["raw_data"]) for o in ctx.node.outputs_for_input(i)]
                reps = [_b(o["raw_data"]) for o in ctx.node.reports_for_input(i)]
            except (NodeError, NodeUnavailable):
                return
            problems = self._compare(res, outs, reps)
            checked += 1
            ctx.db.x("UPDATE l1_inputs SET node_checked=?, node_note=? WHERE run_id=? AND input_index=?",
                     (2 if problems else 1, "; ".join(problems)[:500] or None, ctx.run_id, i))
            if problems:
                bad.append(f"input {i} ({res.kind} {res.outcome}): " + "; ".join(problems[:3]))
            else:
                ctx.db.x("UPDATE actions SET node_verified=1 WHERE run_id=? AND exec_input=? AND state='executed'",
                         (ctx.run_id, i))
        if not checked:
            return
        ctx.db.record_check(ctx.run_id, "node_io", not bad, {"checked": checked, "mismatches": bad[:20]})
        if bad:
            ctx.alerts.raise_("check.node_io", "critical", "The node's outputs/reports differ from the canonical fold",
                              f"{len(bad)} of {checked} inputs:\n" + "\n".join(bad[:6]))
            ctx.log.error("check.node_io", mismatches=bad[:20])
        else:
            ctx.alerts.resolve("check.node_io", f"{checked} more inputs match the fold exactly")

    @staticmethod
    def _compare(res, outs: list[bytes], reps: list[bytes]) -> list[str]:
        p: list[str] = []
        if res.complete:
            if len(outs) != len(res.outputs):
                p.append(f"{len(outs)} outputs, expected {len(res.outputs)}")
            for k, (got, want) in enumerate(zip(outs, res.outputs)):
                if got != want:
                    p.append(f"output {k} differs ({C.decode_output(got).kind} vs {C.decode_output(want).kind})")
            if len(reps) != len(res.reports):
                p.append(f"{len(reps)} reports, expected {len(res.reports)}")
            for k, (got, want) in enumerate(zip(reps, res.reports)):
                if not report_matches(want, got):
                    p.append(f"report {k}: {got[:120]!r} vs expected {describe_report(want)[:120]}")
            return p
        # partially modelled: every known prediction must appear, in order
        for name, got, want in (("output", outs, res.outputs), ("report", reps, res.reports)):
            j = 0
            for w in want:
                if w is None:
                    continue
                while j < len(got) and not (got[j] == w if isinstance(w, bytes) and name == "output" else report_matches(w, got[j])):
                    j += 1
                if j == len(got):
                    p.append(f"expected {name} not found: {describe_report(w)[:120] if name == 'report' else C.decode_output(w).kind}")
                    break
                j += 1
        return p

    # ---- state inspect, balances and nonces at one snapshot ----
    def state_and_balances(self):
        ctx = self.ctx
        canon = ctx.canon
        try:
            st = ctx.node.state()
        except (NodeError, NodeUnavailable, ValueError) as e:
            ctx.log.warn("check.state_inspect_failed", error=str(e)[:120])
            return
        pc = st["processed_input_count"]
        snap = canon.fold.snapshot_after(pc - 1)
        if pc > canon.fold.processed:
            return  # the node is ahead of the bot's L1 read head; compare next time
        problems = []
        if snap is not None:
            sched = st.get("scheduler") or {}
            want = {"executed_input_count": snap.executed, "next_batch_nonce": snap.next_batch_nonce,
                    "queued_directs": snap.queued_directs, "nonce_count": snap.nonce_count, "last_block": snap.last_block}
            got = {"executed_input_count": st.get("executed_input_count"), "next_batch_nonce": sched.get("next_batch_nonce"),
                   "queued_directs": sched.get("queued_directs"), "nonce_count": st.get("nonce_count"),
                   "last_block": st.get("last_block")}
            for k in want:
                if got[k] != want[k]:
                    problems.append(f"{k}: app {got[k]} vs fold {want[k]}")
            rc = st.get("rejected_count")
            if rc is None or not snap.rejected_min <= rc <= snap.rejected_max:
                problems.append(f"rejected_count: app {rc} vs fold {snap.rejected_min}..{snap.rejected_max}")
        if (st.get("token") or "").lower() != ctx.cfg.token.lower():
            problems.append(f"token {st.get('token')} != {ctx.cfg.token}")
        if (st.get("sequencer_address") or "").lower() != canon.sequencer.lower():
            problems.append(f"sequencer_address {st.get('sequencer_address')} != {canon.sequencer}")
        self.last_state = {"processed_inputs": pc, "app": {k: st.get(k) for k in ("executed_input_count", "rejected_count",
                                                                              "nonce_count", "last_block")},
                           "scheduler": st.get("scheduler"), "ok": not problems, "ts": time.time()}
        ctx.db.record_check(ctx.run_id, "state", not problems, {"processed_inputs": pc, "problems": problems})
        if problems:
            ctx.alerts.raise_("check.state", "critical", "The app's state report differs from the canonical fold",
                              f"after input {pc - 1}: " + "; ".join(problems))
        else:
            ctx.alerts.resolve("check.state", f"state matches the fold after input {pc - 1}")
        self.balances(pc)

    def balances(self, at_pc: int):
        ctx = self.ctx
        canon = ctx.canon
        n = len(ctx.wallets)
        k = ctx.cfg.balance_sample_per_check
        touched = [r["slot"] for r in ctx.db.q("SELECT DISTINCT slot FROM actions WHERE run_id=? AND executed_at>=?",
                                               (ctx.run_id, time.time() - 3600))]
        sample = list(dict.fromkeys(touched[:k // 2] + [(self._rot + i) % n for i in range(k)]))[:k]
        self._rot = (self._rot + k) % n
        bad, checked = [], 0
        for i, slot in enumerate(sample):
            addr = ctx.wallet(slot).address
            try:
                got, pc = ctx.node.ledger_balance(addr, ctx.cfg.token)
                nonce = None
                if i < 3:
                    nonce, pc2 = ctx.node.user_op_nonce(addr)
                    if pc2 != pc:
                        nonce = None
            except (NodeError, NodeUnavailable) as e:
                ctx.log.warn("check.balance_inspect_failed", slot=slot, error=str(e)[:120])
                return
            snap = canon.fold.snapshot_after(pc - 1)
            if pc > canon.fold.processed:
                continue
            checked += 1
            want = snap.balances.get(canon.watched_id(slot), 0) if snap else 0
            if got != want:
                bad.append(f"slot {slot}: app {got / USDC:.6f} vs fold {want / USDC:.6f} USDC (after input {pc - 1})")
            if nonce is not None:
                wn = snap.nonces.get(addr.lower(), 0) if snap else 0
                if nonce != wn:
                    bad.append(f"slot {slot}: user-op nonce app {nonce} vs fold {wn} (after input {pc - 1})")
        if not checked:
            return
        ctx.db.record_check(ctx.run_id, "balances", not bad, {"checked": checked, "mismatches": bad})
        if bad:
            ctx.alerts.raise_("check.balances", "critical", "In-app balances differ from the canonical fold", "\n".join(bad[:10]))
        else:
            ctx.alerts.resolve("check.balances", f"{checked} sampled balances match")

    # ---- custody ----
    def custody(self):
        ctx = self.ctx
        # Order matters: vouchers first, then the ledger snapshot (with the input count it reflects),
        # then the chain. Vouchers of inputs at or above that count are ignored (their amount is still
        # in the supply), executions before `head` are excluded via wasOutputExecuted(head), and
        # deposits after the snapshot only add to the surplus. So any deficit is real.
        try:
            unexec = ctx.node.list_unexecuted_vouchers()
            supply, pc = ctx.node.ledger_total_supply(ctx.cfg.token)
            head = ctx.l1.block_number()
            app_bal = ctx.l1.token_balance(ctx.cfg.app_address, head)
        except (RpcUnavailable, NodeError, NodeUnavailable) as e:
            ctx.log.warn("check.custody_skipped", error=str(e)[:120])
            return
        pending, executed_late = 0, 0
        for o in unexec:
            if un(o.get("input_index")) is None or un(o["input_index"]) >= pc:
                continue
            d = C.decode_output(_b(o["raw_data"]))
            if d.token and d.token.lower() == ctx.cfg.token.lower() and d.amount:
                try:
                    if ctx.l1.was_output_executed(un(o["index"]), head):
                        executed_late += 1  # node has not seen the execution yet
                        continue
                except RpcUnavailable:
                    return
                pending += d.amount
        owed = supply + pending
        surplus = app_bal - owed
        snap = ctx.canon.fold.snapshot_after(pc - 1) if pc <= ctx.canon.fold.processed else None
        fold_supply = snap.supply if snap else None
        self.last_custody = {"block": head, "processed_inputs": pc, "app_balance": app_bal, "ledger_supply": supply,
                             "fold_supply": fold_supply, "unexecuted_vouchers": pending, "surplus": surplus, "ts": time.time()}
        ok = surplus >= 0 and (fold_supply is None or fold_supply == supply)
        ctx.db.record_check(ctx.run_id, "custody", ok, self.last_custody)
        if surplus < 0:
            ctx.alerts.raise_("check.custody", "critical", "The app holds less TestUsdc than it owes",
                              f"block {head}: balance {app_bal / USDC:.6f}, owed {owed / USDC:.6f} "
                              f"(ledger {supply / USDC:.6f} + unexecuted vouchers {pending / USDC:.6f}); "
                              f"short by {-surplus / USDC:.6f} USDC")
        elif fold_supply is not None and fold_supply != supply:
            ctx.alerts.raise_("check.custody", "critical", "The app's token supply differs from the canonical fold",
                              f"after input {pc - 1}: app {supply / USDC:.6f} vs fold {fold_supply / USDC:.6f} USDC")
        else:
            ctx.alerts.resolve("check.custody", f"custody holds at block {head} (surplus {surplus / USDC:.6f} USDC)")

    # ---- the sequencer ----
    def sequencer(self):
        ctx, cfg = self.ctx, self.ctx.cfg
        canon = ctx.canon
        now = time.time()
        deadline = cfg.seq_landing_deadline_minutes * 60
        late = ctx.db.q("SELECT * FROM user_ops WHERE run_id=? AND status='soft' AND soft_at<? ORDER BY soft_at",
                        (ctx.run_id, now - 600))
        overdue = []
        nonces: dict[str, int] = {}
        for uo in late:
            try:
                if uo["sender"] not in nonces:
                    nonces[uo["sender"]] = ctx.seq.next_nonce(uo["sender"])
            except (SeqUnavailable, SeqRejected):
                break
            if nonces[uo["sender"]] <= uo["nonce"] and canon.fold.nonce(uo["sender"]) <= uo["nonce"]:
                # the sequencer no longer counts it and L1 never executed it: recovery dropped it
                with ctx.db.tx() as c:
                    c.execute("UPDATE user_ops SET status='invalidated', error='dropped by the sequencer' WHERE id=?", (uo["id"],))
                    c.execute("UPDATE actions SET state='invalidated', note='soft confirmation dropped' WHERE run_id=? "
                              "AND user_op_id=?", (ctx.run_id, uo["id"]))
                ctx.alerts.raise_("seq.invalidated", "warning", "The sequencer dropped soft-confirmed user ops",
                                  f"slot {uo['slot']} nonce {uo['nonce']} (soft-confirmed {(now - uo['soft_at']) / 60:.0f} min "
                                  f"ago): /nonce is back to {nonces[uo['sender']]} and L1 never executed it. Expected only "
                                  "after a sequencer recovery.")
                ctx.log.warn("userop.invalidated", slot=uo["slot"], nonce=uo["nonce"], next_nonce=nonces[uo["sender"]])
            elif now - uo["soft_at"] > deadline:
                overdue.append(uo)
        if overdue:
            worst = (now - overdue[0]["soft_at"]) / 60
            ctx.alerts.raise_("seq.landing", "critical" if worst > 2 * cfg.seq_landing_deadline_minutes else "warning",
                              "Soft-confirmed user ops are not landing on L1",
                              f"{len(overdue)} op(s) soft-confirmed more than {cfg.seq_landing_deadline_minutes:.0f} min ago "
                              f"are not in any executed batch; the oldest waits {worst:.0f} min (bot read head "
                              f"{canon.read_head}, last batch {canon.batch_silence_minutes() or 0:.0f} min ago)")
        else:
            ctx.alerts.resolve("seq.landing", "soft-confirmed ops are landing")
        silence = canon.batch_silence_minutes()
        waiting = ctx.db.q1("SELECT COUNT(*) AS n FROM user_ops WHERE run_id=? AND status='soft'", (ctx.run_id,))["n"]
        if silence is not None and silence > cfg.seq_batch_silence_minutes and (waiting or canon.fold.fridge):
            ctx.alerts.raise_("seq.batch_silence", "warning", "The sequencer has not posted a batch for a while",
                              f"last batch {silence:.0f} min ago (threshold {cfg.seq_batch_silence_minutes:.0f}); "
                              f"{waiting} soft op(s) and {len(canon.fold.fridge)} direct input(s) wait for one")
        else:
            ctx.alerts.resolve("seq.batch_silence", "batches are landing again")
        age = canon.direct_queue_age_blocks()
        if age is not None and age > cfg.seq_direct_wait_alert_blocks:
            ctx.alerts.raise_("seq.directs_waiting", "warning", "Direct inputs are waiting long for the sequencer",
                              f"the oldest bot direct input has waited {age} blocks (threshold "
                              f"{cfg.seq_direct_wait_alert_blocks}); the scheduler forces it after 1200")
        else:
            ctx.alerts.resolve("seq.directs_waiting", "direct inputs are drained again")
        canon.orphans()
        self._history()

    def _history(self):
        """Optional (operator egress): the sequencer's accepted checkpoint must equal the fold."""
        ctx = self.ctx
        if not ctx.seq.egress:
            return
        try:
            h = ctx.seq.history()
        except (SeqUnavailable, SeqRejected) as e:
            ctx.log.info("check.history_failed", error=str(e)[:120])  # an outage alerts as rpc.sequencer
            return
        ac = (h or {}).get("accepted_checkpoint")
        canon = ctx.canon
        if not ac or canon.read_head is None or ac["inclusion_block"] > canon.read_head:
            return
        snap = None
        for s in reversed(canon.fold.snapshots):
            if s.block <= ac["inclusion_block"]:
                snap = s
                break
        if snap is None:
            return
        problems = []
        if snap.executed != ac["executed_input_count"]:
            problems.append(f"executed_input_count sequencer {ac['executed_input_count']} vs fold {snap.executed}")
        if snap.next_batch_nonce != ac["next_batch_nonce"]:
            problems.append(f"next_batch_nonce sequencer {ac['next_batch_nonce']} vs fold {snap.next_batch_nonce}")
        dep = (h.get("deployment") or {}).get("batch_submitter_address", "")
        if dep and dep.lower() != canon.sequencer.lower():
            problems.append(f"batch submitter {dep} vs {canon.sequencer}")
        ctx.db.record_check(ctx.run_id, "seq_history", not problems,
                            {"inclusion_block": ac["inclusion_block"], "problems": problems})
        if problems:
            ctx.alerts.raise_("check.seq_history", "critical", "The sequencer's accepted checkpoint differs from the fold",
                              f"at block {ac['inclusion_block']}: " + "; ".join(problems))
        else:
            ctx.alerts.resolve("check.seq_history", f"accepted checkpoint at block {ac['inclusion_block']} matches")

    # ---- node and app ----
    def node_progress(self):
        ctx = self.ctx
        lag, waiting = node_lag(ctx)
        limit = ctx.cfg.node_input_lag_alert_minutes
        if lag is not None and lag > limit:
            ctx.alerts.raise_("node.lag", "warning" if lag < 3 * limit else "critical", "The node is not processing inputs",
                              f"{waiting} input(s) waiting; the oldest was included {lag:.0f} min ago (threshold {limit:.0f} min)")
        else:
            ctx.alerts.resolve("node.lag", "the node is processing inputs again")

    def app_health(self):
        ctx = self.ctx
        try:
            if ctx.l1.is_foreclosed():
                ctx.alerts.raise_("app.foreclosed", "critical", "The application is foreclosed",
                                  "Foreclosure is terminal. New deposits, transfers and withdrawals are paused; "
                                  "due vouchers are still executed.")
                ctx.paused_reason = "application foreclosed"
                return
        except RpcUnavailable:
            return
        try:
            app = ctx.node.application()
        except (NodeError, NodeUnavailable):
            return
        status = (app or {}).get("status")
        if status and status != "OK":
            ctx.alerts.raise_("app.node_status", "critical", f"The node reports the application as {status}",
                              (app.get("reason") or "")[:500])
            ctx.paused_reason = f"node app status {status}"
        else:
            ctx.alerts.resolve("app.node_status", "the node reports the application as OK")
            if ctx.paused_reason and ctx.paused_reason.startswith("node app status"):
                ctx.paused_reason = None
