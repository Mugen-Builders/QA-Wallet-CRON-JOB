"""Withdrawal vouchers: from the withdraw input to a verified payout.

waiting  -> the node has the voucher; its epoch is not settled (or the proof is not out yet)
due      -> validateOutput(output, proof) passes on chain: anyone can execute it now
executing-> an executeOutput transaction is in flight
executed / verified -> mined; verified = the receipt carries OutputExecuted(index) and exactly one
            Transfer(app -> receiver, amount) of the token
executed_by_other   -> someone outside the bot executed it (still verified from that tx)
failed   -> execution kept failing (alerted)

"Due" is decided only by the contract (validateOutput), never by the node's epoch status, so the
tracker works for Authority, Quorum and PRT alike. The ETA is an estimate used for alerting.
"""
from __future__ import annotations

import json
import time

from . import contracts as C
from .chain import Reverted, RpcUnavailable
from .fold import erc20_voucher
from .node import NodeError, NodeUnavailable, un
from .personas import rng_for
from .schedule import exec_delay

USDC = 10**6


def _b(hexstr) -> bytes:
    return bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)


class Vouchers:
    def __init__(self, ctx):
        self.ctx = ctx

    # ---- discovery ------------------------------------------------------------------------
    def discover(self):
        """Create voucher rows for the bot's withdrawals the canonical fold executed.

        Under the sequencer a withdrawal's voucher belongs to the InputBox input that EXECUTED it
        (the batch carrying the user op, or the input whose drain ran the direct), not to the input
        that submitted it. When every entry of that input is modelled, the voucher's position among
        the input's outputs is known exactly; otherwise it is matched by content."""
        ctx = self.ctx
        try:
            processed = ctx.node.processed_input_count()
        except (NodeError, NodeUnavailable):
            return
        rows = ctx.db.q("SELECT a.* FROM actions a LEFT JOIN vouchers v ON v.run_id=a.run_id AND v.action_id=a.id "
                        "WHERE a.run_id=? AND a.kind='withdraw' AND a.state='executed' AND a.outcome='accept' "
                        "AND v.output_index IS NULL AND a.exec_input < ? ORDER BY a.offset", (ctx.run_id, processed))
        for a in rows:
            try:
                outs = ctx.node.outputs_for_input(a["exec_input"])
            except (NodeError, NodeUnavailable):
                return
            owner = ctx.wallet(a["slot"]).address
            expect = erc20_voucher(ctx.cfg.token, owner, int(a["amount"]))
            pos = self._position(a["exec_input"], a["offset"])
            o = None
            if pos is not None:
                if pos < len(outs):
                    o = outs[pos]
                    if _b(o["raw_data"]) != expect:
                        ctx.alerts.raise_(f"voucher.content.{a['id']}", "critical", "Voucher content does not match the withdrawal",
                                          f"action {a['id']} (slot {a['slot']}, {int(a['amount']) / USDC:.2f} USDC): output "
                                          f"{un(o['index'])} of input {a['exec_input']} is not token.transfer(owner, amount)")
            else:
                taken = {r["output_index"] for r in ctx.db.q("SELECT output_index FROM vouchers WHERE run_id=? AND input_index=?",
                                                             (ctx.run_id, a["exec_input"]))}
                o = next((x for x in outs if un(x["index"]) not in taken and _b(x["raw_data"]) == expect), None)
            if o is None:
                seen = ctx.db.q1("SELECT seen_at FROM l1_inputs WHERE run_id=? AND input_index=?", (ctx.run_id, a["exec_input"]))
                age = time.time() - (seen["seen_at"] if seen else time.time())
                if age > 900:
                    ctx.alerts.raise_(f"voucher.missing.{a['id']}", "critical", "An executed withdrawal produced no voucher",
                                      f"action {a['id']} (slot {a['slot']}, {a['route']}, {int(a['amount']) / USDC:.2f} USDC) "
                                      f"executed in input {a['exec_input']}, which the node has processed, but the node lists "
                                      f"no matching voucher ({len(outs)} outputs for that input)")
                continue
            raw = _b(o["raw_data"])
            dec = C.decode_output(raw)
            idx = un(o["index"])
            eta = self._eta(un(o.get("epoch_index")))
            ctx.db.x("INSERT OR IGNORE INTO vouchers(run_id,output_index,input_index,action_id,slot,kind,raw_output,amount,"
                     "receiver,epoch_index,state,found_at,eta_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ctx.run_id, idx, a["exec_input"], a["id"], a["slot"], dec.kind, "0x" + raw.hex(),
                      str(dec.amount if dec.amount is not None else 0), dec.receiver or "", un(o.get("epoch_index")),
                      "waiting", time.time(), eta))
            ctx.log.info("voucher.found", output_index=idx, action=a["id"], exec_input=a["exec_input"], route=a["route"],
                         amount=dec.amount, eta_min=round((eta - time.time()) / 60, 1) if eta else None)

    def _position(self, exec_input: int, offset: int) -> int | None:
        """Index of this entry's output among the outputs of `exec_input`, if every earlier entry of
        that input has a known output count."""
        res = self.ctx.canon.results.get(exec_input)
        if res is None:
            return None
        k = 0
        for e in res.entries:
            if e.offset == offset:
                return k
            if None in e.outputs:
                return None
            k += len(e.outputs)
        return None

    def _eta(self, epoch_index):
        """Wall-clock estimate of when the voucher becomes executable."""
        ctx = self.ctx
        el, sp = ctx.timing.get("epoch_length"), ctx.timing.get("staging")
        if epoch_index is None:
            return None
        try:
            ep = ctx.node.get_epoch(epoch_index)
            head = ctx.l1.block_number()
        except (NodeError, NodeUnavailable, RpcUnavailable):
            return None
        if not ep:
            return None
        bt = ctx.cfg.block_time_seconds
        staged = un(ep.get("staged_at_block"))
        last = un(ep.get("last_block"))
        if staged and sp is not None:
            target = staged + sp + 2
        elif sp is None and last is not None:
            # PRT (Dave): the consensus contract exposes no staging period, so there is no
            # on-chain claim+dispute window to read. Estimate executability as the epoch's last
            # block plus the configured settlement budget (measured ~18-19 h on this deployment),
            # instead of the ~finality-lag fallback that assumed near-immediate executability and
            # fired the overdue warning hours before a PRT epoch could realistically settle.
            # Unclamped on purpose: once an epoch is past the budget the estimate goes into the
            # past, which is what lets _maybe_overdue flag a genuinely stuck PRT epoch.
            return time.time() + (last + int(ctx.cfg.prt_settle_hours * 3600 / bt) - head) * bt
        elif last is not None:
            target = last + ctx.cfg.finality_lag_blocks + sp + 4
        else:
            return None
        return time.time() + max(0, target - head) * bt

    # ---- due detection ----------------------------------------------------------------------
    def update(self):
        ctx = self.ctx
        now = time.time()
        any_overdue = False
        for v in ctx.db.q("SELECT * FROM vouchers WHERE run_id=? AND state='waiting' ORDER BY output_index", (ctx.run_id,)):
            try:
                if ctx.l1.was_output_executed(v["output_index"]):
                    self._mark_external(v)
                    continue
                o = ctx.node.get_output(v["output_index"])
            except (RpcUnavailable, NodeError, NodeUnavailable):
                return  # incomplete sweep: leave the alert state untouched
            sib = (o or {}).get("output_hashes_siblings")
            if sib:
                try:
                    ctx.l1.validate_output(_b(v["raw_output"]), v["output_index"], [_b(x) for x in sib])
                except Reverted:
                    # The node has the output proof but the on-chain claim is not accepted yet
                    # (a PRT epoch still in its dispute/settlement window). Refresh the estimate
                    # the same way as the no-proof case before judging it overdue.
                    v = self._refresh_eta(v, now)
                    any_overdue |= self._maybe_overdue(v, now)
                    continue
                except RpcUnavailable:
                    return
                self._mark_due(v)
            else:
                v = self._refresh_eta(v, now)
                any_overdue |= self._maybe_overdue(v, now)
        # Reached the end only if the sweep completed (RPC errors return early above): if nothing is
        # currently past its estimate, clear the overdue warning.
        if not any_overdue:
            ctx.alerts.resolve("voucher.overdue", "no voucher is past its executability estimate")

    def _refresh_eta(self, v, now):
        """Re-estimate a waiting voucher's executability time when the stored one is missing or
        stale. If no fresh estimate can be produced (e.g. the epoch has no last block yet) and the
        stored one is already in the past, drop it to None so a leftover stale value cannot fire a
        false overdue; it will be recomputed on a later cycle. Returns the (possibly updated) row."""
        ctx = self.ctx
        if not (v["eta_at"] is None or (now - v["found_at"] > 1800 and v["eta_at"] < now)):
            return v
        eta = self._eta(v["epoch_index"])
        if eta:
            ctx.db.x("UPDATE vouchers SET eta_at=? WHERE run_id=? AND output_index=?", (eta, ctx.run_id, v["output_index"]))
            return dict(v, eta_at=eta)
        if v["eta_at"] is not None and v["eta_at"] < now:
            ctx.db.x("UPDATE vouchers SET eta_at=NULL WHERE run_id=? AND output_index=?", (ctx.run_id, v["output_index"]))
            return dict(v, eta_at=None)
        return v

    def _maybe_overdue(self, v, now) -> bool:
        """Return whether this waiting voucher is currently past its executability estimate. Also
        clears a stale overdue flag when a refreshed estimate (e.g. after PRT recalibration) puts
        the voucher back in the future, so the alert can resolve."""
        ctx = self.ctx
        if v["eta_at"] is None:
            if v["overdue_alerted"]:
                ctx.db.x("UPDATE vouchers SET overdue_alerted=0 WHERE run_id=? AND output_index=?", (ctx.run_id, v["output_index"]))
            return False
        grace = ctx.cfg.voucher_overdue_grace_minutes * 60
        if now <= v["eta_at"] + grace:
            if v["overdue_alerted"]:
                ctx.db.x("UPDATE vouchers SET overdue_alerted=0 WHERE run_id=? AND output_index=?", (ctx.run_id, v["output_index"]))
            return False
        late_min = (now - v["eta_at"]) / 60
        ctx.alerts.raise_("voucher.overdue", "warning", "Vouchers are not executable past their expected time",
                          self._overdue_summary())
        if not v["overdue_alerted"]:
            ctx.db.x("UPDATE vouchers SET overdue_alerted=1 WHERE run_id=? AND output_index=?", (ctx.run_id, v["output_index"]))
            ctx.log.warn("voucher.overdue", output_index=v["output_index"], late_min=round(late_min))
        return True

    def _overdue_summary(self):
        ctx = self.ctx
        now = time.time()
        grace = ctx.cfg.voucher_overdue_grace_minutes * 60
        rows = ctx.db.q("SELECT output_index, eta_at, epoch_index FROM vouchers WHERE run_id=? AND state='waiting' "
                        "AND eta_at IS NOT NULL AND eta_at < ?", (ctx.run_id, now - grace))
        if not rows:
            return "none"
        worst = max(now - r["eta_at"] for r in rows)
        epochs = sorted({r["epoch_index"] for r in rows if r["epoch_index"] is not None})
        return (f"{len(rows)} voucher(s) still not executable; worst is {worst / 3600:.1f} h past its estimate; "
                f"epochs {epochs[:8]}{'…' if len(epochs) > 8 else ''}. Check the node's claims for these epochs.")

    def _mark_due(self, v):
        ctx = self.ctx
        now_v = ctx.clock.now()
        prof = ctx.profiles[v["slot"]]
        rng = rng_for(ctx.plan_seed, "voucher", v["output_index"])
        delay = exec_delay(rng, prof, now_v, ctx.cfg.exec_deadline_hours)
        executor = v["slot"]
        if rng.random() < ctx.cfg.other_executor_share:
            others = [p.slot for p in ctx.profiles if p.slot != v["slot"] and p.onboard_day <= (ctx.clock.now() - ctx.clock.start) / 86400]
            if others:
                executor = rng.choice(others)
        head = None
        try:
            head = ctx.l1.block_number()
        except RpcUnavailable:
            pass
        ctx.db.x("UPDATE vouchers SET state='due', due_at=?, due_block=?, planned_exec_at=?, deadline_at=?, executor_slot=? "
                 "WHERE run_id=? AND output_index=?",
                 (time.time(), head, now_v + delay, now_v + ctx.cfg.exec_deadline_hours * 3600, executor,
                  ctx.run_id, v["output_index"]))
        ctx.log.info("voucher.due", output_index=v["output_index"], waited_min=round((time.time() - v["found_at"]) / 60, 1),
                     exec_in_min=round(delay / 60, 1), executor=executor, owner=v["slot"])
        if not ctx.db.q1("SELECT 1 FROM vouchers WHERE run_id=? AND state='waiting' AND overdue_alerted=1 LIMIT 1", (ctx.run_id,)):
            ctx.alerts.resolve("voucher.overdue", "every overdue voucher is now executable")

    def _mark_external(self, v):
        ctx = self.ctx
        ctx.db.x("UPDATE vouchers SET state='executed_by_other', executed_by='external', executed_at=? "
                 "WHERE run_id=? AND output_index=?", (time.time(), ctx.run_id, v["output_index"]))
        ctx.log.info("voucher.executed_externally", output_index=v["output_index"])

    # ---- execution scheduling ---------------------------------------------------------------
    def claimable_by(self, slot, now_v):
        return self.ctx.db.q("SELECT * FROM vouchers WHERE run_id=? AND state='due' AND slot=? AND executor_slot=? "
                             "AND exec_step_id IS NULL AND planned_exec_at > ?", (self.ctx.run_id, slot, slot, now_v))

    def schedule(self):
        """Create execute steps for vouchers whose planned time (or hard deadline) has come."""
        ctx = self.ctx
        now_v = ctx.clock.now()
        for v in ctx.db.q("SELECT * FROM vouchers WHERE run_id=? AND state='due' AND exec_step_id IS NULL "
                          "AND (planned_exec_at <= ? OR deadline_at <= ?)", (ctx.run_id, now_v, now_v)):
            executor = v["executor_slot"]
            late = now_v >= v["deadline_at"]
            with ctx.db.tx() as c:
                sid = c.execute("INSERT INTO steps(session_id,run_id,seq,kind,slot,params,due_at,status,created_at,updated_at) "
                                "VALUES(NULL,?,0,'execute',?,?,?,'pending',?,?)",
                                (ctx.run_id, executor, json.dumps({"output_index": v["output_index"]}), now_v,
                                 time.time(), time.time())).lastrowid
                c.execute("UPDATE vouchers SET exec_step_id=?, late=? WHERE run_id=? AND output_index=?",
                          (sid, 1 if late else 0, ctx.run_id, v["output_index"]))
            if late:
                ctx.log.warn("voucher.deadline", output_index=v["output_index"], executor=executor)

    def prepare_execution(self, output_index, executor_slot):
        """(to, calldata, gas multiplier) for executeOutput, or None if it no longer applies."""
        ctx = self.ctx
        v = ctx.db.q1("SELECT * FROM vouchers WHERE run_id=? AND output_index=?", (ctx.run_id, output_index))
        if v is None or v["state"] not in ("due", "executing"):
            return None
        o = ctx.node.get_output(output_index)
        sib = (o or {}).get("output_hashes_siblings")
        if not sib:
            return None
        from .actions import cd_execute
        return ctx.cfg.app_address, cd_execute(_b(v["raw_output"]), output_index, [_b(x) for x in sib]), 1.3

    def on_exec_sent(self, output_index, step_id, executor_slot):
        ctx = self.ctx
        ctx.db.x("UPDATE vouchers SET state='executing', exec_step_id=?, executor_slot=?, attempts=attempts+1 "
                 "WHERE run_id=? AND output_index=?", (step_id, executor_slot, ctx.run_id, output_index))

    def on_exec_estimate_revert(self, output_index, reason):
        ctx = self.ctx
        if reason and "OutputNotReexecutable" in reason:
            v = ctx.db.q1("SELECT * FROM vouchers WHERE run_id=? AND output_index=?", (ctx.run_id, output_index))
            self._mark_external(v)
            return
        self._retry_later(output_index, f"estimate reverted: {reason}")

    def on_exec_dropped(self, output_index):
        self._retry_later(output_index, "execution tx dropped")

    def on_exec_reverted(self, output_index, reason):
        ctx = self.ctx
        try:
            if ctx.l1.was_output_executed(output_index):
                v = ctx.db.q1("SELECT * FROM vouchers WHERE run_id=? AND output_index=?", (ctx.run_id, output_index))
                self._mark_external(v)
                return
        except RpcUnavailable:
            pass
        self._retry_later(output_index, f"execution reverted: {reason}")

    def _retry_later(self, output_index, why):
        ctx = self.ctx
        v = ctx.db.q1("SELECT * FROM vouchers WHERE run_id=? AND output_index=?", (ctx.run_id, output_index))
        if v["attempts"] >= 5:
            ctx.db.x("UPDATE vouchers SET state='failed', last_error=? WHERE run_id=? AND output_index=?",
                     (why[:300], ctx.run_id, output_index))
            ctx.alerts.raise_(f"voucher.failed.{output_index}", "critical", "A voucher could not be executed",
                              f"output {output_index} ({int(v['amount']) / USDC:.2f} USDC to slot {v['slot']}): {why}")
            return
        backoff = 600 * max(1, v["attempts"])
        ctx.db.x("UPDATE vouchers SET state='due', exec_step_id=NULL, planned_exec_at=?, last_error=? "
                 "WHERE run_id=? AND output_index=?", (ctx.clock.now() + backoff, why[:300], ctx.run_id, output_index))
        ctx.log.warn("voucher.retry", output_index=output_index, why=why[:160], attempt=v["attempts"])

    def on_exec_mined(self, output_index, receipt, executor_slot):
        ctx = self.ctx
        v = ctx.db.q1("SELECT * FROM vouchers WHERE run_id=? AND output_index=?", (ctx.run_id, output_index))
        executed = C.executed_outputs_from_receipt(receipt, ctx.cfg.app_address)
        transfers = C.erc20_transfers_from_receipt(receipt, ctx.cfg.token)
        expect = (ctx.cfg.app_address.lower(), v["receiver"].lower(), int(v["amount"]))
        got = [(f.lower(), t.lower(), a) for f, t, a in transfers]
        ok = executed == [output_index] and got == [expect]
        by = "owner" if executor_slot == v["slot"] else "other_bot"
        ctx.db.x("UPDATE vouchers SET state=?, executed_at=?, executed_by=?, exec_tx_hash=?, verified_at=? "
                 "WHERE run_id=? AND output_index=?",
                 ("verified" if ok else "executed", time.time(), by, receipt["transactionHash"].to_0x_hex(),
                  time.time() if ok else None, ctx.run_id, output_index))
        if ok:
            ctx.log.info("voucher.verified", output_index=output_index, executed_by=by, amount=int(v["amount"]),
                         due_to_exec_min=round((time.time() - (v["due_at"] or time.time())) / 60, 1))
        else:
            ctx.alerts.raise_(f"voucher.payout.{output_index}", "critical", "Voucher executed but the payout is wrong",
                              f"output {output_index}: OutputExecuted={executed}, transfers={got}, expected {expect}")
