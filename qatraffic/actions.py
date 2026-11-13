"""Sessions and steps: what a user actually does, decided when the session starts, carried out one
action at a time per account.

A session (planned by schedule.py) becomes a short list of steps from the account's balance at
that moment: mint TestUsdc if the wallet is short, approve the portal per the user's habit,
deposit through the portal (always an L1 transaction), pay a friend, cash out. Transfers and
withdrawals go one of two ways, per the user's habit:

- `seq`: a user op signed with EIP-712 and POSTed to the sequencer (no gas; the frame fee is paid
  in the token inside the app; soft-confirmed at once, executed on L1 when its batch lands);
- `l1`: an InputBox direct input (gas; executed when the scheduler drains it).

Each step waits for the previous one (mined, or soft-confirmed) plus a human think time. Every
payload is checked against the allowlist before it is signed.
"""
from __future__ import annotations

import json
import math
import time

from eth_abi import encode as abi_encode

from . import contracts as C
from . import fee as F
from . import userop as U
from .chain import FeeTooHigh, InsufficientGas, Reverted, RpcAnswer, RpcUnavailable
from .node import NodeError, NodeUnavailable
from .personas import PERSONAS, rng_for
from .schedule import think_time
from .seqclient import SeqRejected, SeqUnavailable

USDC = 10**6
MAX_UINT256 = 2**256 - 1
INPUT_KINDS = ("deposit", "transfer", "withdraw")
L1_KINDS = ("mint", "approve", "deposit", "execute", "gas_topup")
TERMINAL_STEP = ("mined", "skipped", "failed", "reverted", "done")
DRAIN_WAIT_MAX_S = 3600       # virtual seconds a user waits for a deposit to reach the sequencer
SEQ_RETRY_LIMIT = 4
SEQ_GIVE_UP_S = 1800          # virtual seconds of sequencer outage before a user changes plans


# ---- calldata ----
def cd_mint(amount):
    return C.selector("mint(uint256)") + abi_encode(["uint256"], [amount])


def cd_approve(spender, amount):
    return C.selector("approve(address,uint256)") + abi_encode(["address", "uint256"], [spender, amount])


def cd_deposit(token, app, amount, data=b""):
    return C.selector("depositErc20Tokens(address,address,uint256,bytes)") + abi_encode(
        ["address", "address", "uint256", "bytes"], [token, app, amount, data])


def cd_add_input(app, payload):
    C.assert_allowed_input(payload)
    return C.selector("addInput(address,bytes)") + abi_encode(["address", "bytes"], [app, payload])


def cd_execute(raw, index, siblings):
    return C.selector("executeOutput(bytes,(uint64,bytes32[]))") + abi_encode(
        ["bytes", "(uint64,bytes32[])"], [raw, (index, siblings)])


# ---- human amounts ----
def nice_amount(rng, usdc: float) -> int:
    usdc = max(1.0, usdc)
    if rng.random() < 0.72:
        if usdc >= 1000:
            step = rng.choice((100, 250, 500))
        elif usdc >= 100:
            step = rng.choice((10, 25, 50))
        elif usdc >= 20:
            step = rng.choice((5, 10))
        else:
            step = 1
        v = max(step, round(usdc / step) * step)
        return int(v * USDC)
    return int(round(usdc, 2) * USDC)


def persona_amount(rng, persona: str, scale: float = 1.0) -> float:
    spec = PERSONAS[persona]
    return rng.lognormvariate(math.log(spec.amount_median * scale), spec.amount_sigma)


class Engine:
    def __init__(self, ctx, funding, vouchers):
        self.ctx = ctx
        self.funding = funding
        self.vouchers = vouchers
        self._separator = U.domain_separator(ctx.cfg.chain_id, ctx.cfg.app_address)

    # ------------------------------------------------------------------ sessions
    def start_due_sessions(self):
        ctx = self.ctx
        now_v = ctx.clock.now()
        busy = {r["slot"] for r in ctx.db.q("SELECT slot FROM sessions WHERE run_id=? AND status='running'", (ctx.run_id,))}
        for s in ctx.db.q("SELECT * FROM sessions WHERE run_id=? AND status='planned' AND planned_at<=? ORDER BY planned_at LIMIT 20",
                          (ctx.run_id, now_v)):
            if s["slot"] in busy:
                continue
            if ctx.paused_reason:
                self._defer(s, now_v, f"paused: {ctx.paused_reason}")
                continue
            try:
                steps = self._plan_steps(s)
                if any(k in L1_KINDS or p.get("route") == "l1" for k, p, _ in steps) and not self.funding.ensure_gas(s["slot"]):
                    self._defer(s, now_v, "waiting for gas")
                    continue
                self._persist(s, steps, now_v)
                busy.add(s["slot"])
            except (RpcUnavailable, NodeUnavailable) as e:
                self._defer(s, now_v, f"rpc: {type(e).__name__}")

    def _defer(self, s, now_v, note):
        waited = now_v - s["planned_at"]
        if waited > 6 * 3600:
            self.ctx.db.x("UPDATE sessions SET status='missed', note=? WHERE id=?", (f"gave up after 6h ({note})", s["id"]))
            self.ctx.log.warn("session.missed", session=s["id"], slot=s["slot"], reason=note)
            return
        rng = rng_for(self.ctx.plan_seed, "defer", s["id"], int(waited))
        self.ctx.db.x("UPDATE sessions SET planned_at=?, note=? WHERE id=?",
                      (now_v + rng.uniform(4, 12) * 60, note, s["id"]))

    def _plan_steps(self, s) -> list[tuple[str, dict, bool]]:
        ctx = self.ctx
        slot, intent = s["slot"], s["intent"]
        prof = ctx.profiles[slot]
        w = ctx.wallet(slot)
        rng = rng_for(ctx.plan_seed, "session", s["id"])
        bal = ctx.canon.spendable(slot, "l1")
        wallet_tok = ctx.l1.token_balance(w.address)
        allowance = ctx.l1.token_allowance(w.address, ctx.cfg.erc20_portal)
        steps: list[tuple[str, dict, bool]] = []  # (kind, params, invalid)
        now_v = ctx.clock.now()

        # a user who opens the app with a claimable withdrawal often claims it right away
        for v in self.vouchers.claimable_by(slot, now_v):
            if rng.random() < 0.6:
                steps.append(("execute", {"output_index": v["output_index"]}, False))

        def route():
            return "l1" if rng.random() < prof.l1_route_bias else "seq"

        def deposit(amount):
            nonlocal wallet_tok, allowance, bal
            if wallet_tok < amount:
                mint = nice_amount(rng, max(amount - wallet_tok, amount * rng.uniform(1.0, 4.0)) / USDC)
                steps.append(("mint", {"amount": mint}, False))
                wallet_tok += mint
            if allowance < amount:
                habit = prof.approve_habit
                appr = (MAX_UINT256 if habit == "max" else
                        amount if habit == "exact" else nice_amount(rng, amount / USDC * rng.uniform(2, 5)))
                steps.append(("approve", {"amount": appr}, False))
                allowance = appr
            steps.append(("deposit", {"amount": amount}, False))
            wallet_tok -= amount
            allowance = allowance - amount if allowance != MAX_UINT256 else allowance
            bal += amount

        def transfer(to_slot):
            nonlocal bal
            if to_slot is None or to_slot == slot or bal < 2 * USDC:
                return False
            if rng.random() < 0.5:
                amt = nice_amount(rng, bal / USDC * rng.uniform(0.1, 0.5))
            else:
                amt = nice_amount(rng, persona_amount(rng, prof.persona, 0.5))
            amt = min(amt, bal - USDC)
            memo = (b"inv-" + str(rng.randint(1000, 99999)).encode()) if rng.random() < 0.08 else b""
            steps.append(("transfer", {"amount": amt, "to": to_slot, "memo": memo.hex(), "route": route()}, False))
            bal -= amt
            return True

        def withdraw():
            nonlocal bal
            if bal < 2 * USDC:
                return False
            r = rng.random()
            if r < 0.25:
                amt = bal - USDC  # "everything", minus a little left for fees
            elif r < 0.45:
                amt = max(USDC, bal // 2)
            else:
                amt = min(bal - USDC, nice_amount(rng, persona_amount(rng, prof.persona, 0.7)))
            steps.append(("withdraw", {"amount": amt, "route": route()}, False))
            bal -= amt
            return True

        def friend():
            if s["counterparty"] is not None and rng.random() < 0.8:
                return s["counterparty"]
            return rng.choice(prof.friends) if prof.friends else None

        dep_amount = nice_amount(rng, persona_amount(rng, prof.persona))
        if intent == "check":
            steps.append(("inspect", {}, False))
        elif intent == "topup":
            deposit(dep_amount)
        elif intent in ("pay", "pay2"):
            if bal < 2 * USDC:
                deposit(dep_amount)
            transfer(friend())
            if intent == "pay2":
                others = [f for f in prof.friends if f != s["counterparty"]]
                transfer(rng.choice(others) if others else friend())
        elif intent == "topup_pay":
            deposit(dep_amount)
            transfer(friend())
        elif intent == "cashout":
            if not withdraw():
                if prof.persona == "merchant":
                    steps.append(("inspect", {}, False))
                else:
                    deposit(dep_amount)
        elif intent == "pay_cashout":
            if bal < 2 * USDC:
                deposit(dep_amount)
            transfer(friend())
            withdraw()
        if not steps:
            steps.append(("inspect", {}, False))

        # minimal deliberate mistakes: an overdraft on a transfer or a withdrawal
        cand = [i for i, (k, _, _) in enumerate(steps) if k in ("transfer", "withdraw")]
        if cand and rng.random() < ctx.cfg.invalid_rate * len(cand):
            i = rng.choice(cand)
            k, p, _ = steps[i]
            steps[i] = (k, dict(p, overdraft=nice_amount(rng, rng.uniform(1, 25))), True)
        return steps

    def _persist(self, s, steps, now_v):
        ctx = self.ctx
        rng = rng_for(ctx.plan_seed, "session-think", s["id"])
        with ctx.db.tx() as c:
            c.execute("UPDATE sessions SET status='running', started_at=?, note=NULL WHERE id=?", (now_v, s["id"]))
            for seq, (kind, params, invalid) in enumerate(steps):
                think = 0 if seq == 0 else think_time(rng)
                c.execute("INSERT INTO steps(session_id,run_id,seq,kind,route,slot,params,expected,invalid,due_at,status,"
                          "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (s["id"], ctx.run_id, seq, kind, params.get("route"), s["slot"], json.dumps(dict(params, think=think)),
                           ("reject" if invalid else "accept") if kind in INPUT_KINDS else None,
                           1 if invalid else 0, now_v if seq == 0 else now_v + think, "pending", time.time(), time.time()))
        ctx.log.info("session.start", session=s["id"], slot=s["slot"], persona=ctx.profiles[s["slot"]].persona,
                     intent=s["intent"], steps=[k + (f"/{p['route']}" if p.get("route") else "") for k, p, _ in steps])

    # ------------------------------------------------------------------ steps
    def run_due_steps(self, max_sends: int = 12):
        ctx = self.ctx
        now_v = ctx.clock.now()
        inflight = {r["slot"] for r in ctx.db.q(
            "SELECT DISTINCT slot FROM steps WHERE run_id=? AND status IN ('sending','sent') AND slot IS NOT NULL", (ctx.run_id,))}
        sent = 0
        # 'sending' is only resumed for user ops (a signed op waiting to be redelivered); an L1 step
        # in 'sending' may already have a broadcast transaction and is settled by recover()
        for st in ctx.db.q("SELECT * FROM steps WHERE run_id=? AND (status='pending' OR (status='sending' AND route='seq')) "
                           "AND due_at<=? ORDER BY due_at LIMIT 60", (ctx.run_id, now_v)):
            if sent >= max_sends:
                break
            resuming = st["status"] == "sending"  # a signed user op waiting to be (re)delivered
            if st["slot"] is not None and st["slot"] in inflight and not resuming:
                continue
            if st["session_id"] is not None and st["seq"] > 0:
                prev = ctx.db.q1("SELECT status FROM steps WHERE session_id=? AND seq=?", (st["session_id"], st["seq"] - 1))
                if prev and prev["status"] not in TERMINAL_STEP:
                    continue
            if self._execute_step(st, now_v):
                sent += 1
                if st["slot"] is not None:
                    inflight.add(st["slot"])

    def _execute_step(self, st, now_v) -> bool:
        """Returns True when a transaction or a user op was sent."""
        ctx = self.ctx
        p = json.loads(st["params"])
        kind = st["kind"]
        if kind in ("transfer", "withdraw") and st["route"] == "seq":
            return self._user_op_step(st, p, now_v)
        try:
            if kind == "inspect":
                w = ctx.wallet(st["slot"])
                try:
                    bal, _ = ctx.node.ledger_balance(w.address, ctx.cfg.token)
                    note = f"balance {bal / USDC:.2f}"
                except (NodeError, NodeUnavailable) as e:
                    note = f"inspect failed: {type(e).__name__}"
                self._finish(st, "done", note)
                return False
            if kind == "execute":
                plan = self.vouchers.prepare_execution(p["output_index"], st["slot"])
                if plan is None:
                    self._finish(st, "skipped", "voucher not executable by this step")
                    return False
                to, data, gas_mult = plan
            elif kind == "gas_topup":
                to, data, gas_mult = ctx.wallet(p["to"]).address, b"", 1.25
            else:
                to, data, gas_mult = self._calldata(st, p, now_v)
                if to is None:
                    if data:
                        self._finish(st, "skipped", data)
                    return False
            sender = ctx.funder if st["slot"] is None else ctx.wallet(st["slot"])
            tip = int((ctx.profiles[st["slot"]].tip_gwei if st["slot"] is not None else ctx.cfg.tip_max_gwei) * 1e9)
            ctx.db.x("UPDATE steps SET status='sending', updated_at=? WHERE id=?", (time.time(), st["id"]))
            # gas_topup is a plain value transfer; estimate it rather than assuming 21000, because
            # funding a never-seen account costs more on chains that price account creation (some
            # testnets charge ~200k gas the first time an address receives value).
            tx_id = ctx.sender.send(sender.key, sender.address, st["slot"], kind, to, data,
                                    value=int(p.get("value", 0)), tip_wei=tip, gas=None,
                                    gas_mult=gas_mult, step_id=st["id"])
            ctx.db.x("UPDATE steps SET status='sent', tx_id=?, updated_at=? WHERE id=?", (tx_id, time.time(), st["id"]))
            if kind == "execute":
                self.vouchers.on_exec_sent(p["output_index"], st["id"], st["slot"])
            return True
        except InsufficientGas:
            ctx.db.x("UPDATE steps SET status='pending', due_at=?, note='waiting for gas', updated_at=? WHERE id=?",
                     (now_v + 300, time.time(), st["id"]))
            if st["slot"] is not None:
                self.funding.ensure_gas(st["slot"], force=True)
            return False
        except FeeTooHigh as e:
            ctx.db.x("UPDATE steps SET status='pending', due_at=?, note=?, updated_at=? WHERE id=?",
                     (now_v + 600, str(e)[:120], time.time(), st["id"]))
            ctx.alerts.raise_("fees.high", "warning", "Gas fees above the cap", str(e))
            return False
        except Reverted as e:
            if kind == "execute":
                self.vouchers.on_exec_estimate_revert(p["output_index"], e.reason)
                self._finish(st, "skipped", f"estimate reverted: {e.reason}")
            else:
                self._finish(st, "failed", f"estimate reverted: {e.reason}")
                ctx.alerts.raise_(f"step.revert.{kind}", "warning", f"{kind} would revert",
                                  f"step {st['id']} (slot {st['slot']}): {e.reason}")
            return False
        except (RpcUnavailable, NodeUnavailable):
            ctx.db.x("UPDATE steps SET status='pending', due_at=?, updated_at=? WHERE id=?",
                     (now_v + 60, time.time(), st["id"]))
            return False
        except RpcAnswer as e:
            ctx.db.x("UPDATE steps SET status='pending', due_at=?, note=?, updated_at=? WHERE id=?",
                     (now_v + 90, f"rpc answer: {str(e)[:100]}", time.time(), st["id"]))
            return False

    def _amount(self, st, p, spend: int, upper: int):
        """Final amount for a transfer/withdrawal against what is spendable now; None to skip."""
        if st["invalid"]:
            return upper + int(p["overdraft"])
        if spend < USDC:
            return None
        return min(int(p.get("amount", 0)), spend)

    def _payload(self, kind, p, amount) -> bytes:
        cfg = self.ctx.cfg
        if kind == "transfer":
            payload = C.encode_transfer(cfg.token, self.ctx.wallet(p["to"]).address, amount, bytes.fromhex(p.get("memo", "")))
        elif kind == "withdraw":
            payload = C.encode_withdraw(cfg.token, amount)
        else:
            raise ValueError(kind)
        C.assert_allowed_input(payload)
        return payload

    def _save_amount(self, st, p, amount):
        if amount != int(p.get("amount", 0)):
            p["amount"] = amount
            self.ctx.db.x("UPDATE steps SET params=? WHERE id=?", (json.dumps(p), st["id"]))

    def _calldata(self, st, p, now_v):
        ctx = self.ctx
        cfg = ctx.cfg
        kind = st["kind"]
        slot = st["slot"]
        amount = int(p.get("amount", 0))
        if kind == "mint":
            return cfg.token, cd_mint(amount), 1.25
        if kind == "approve":
            return cfg.token, cd_approve(cfg.erc20_portal, amount), 1.25
        if kind == "deposit":
            return cfg.erc20_portal, cd_deposit(cfg.token, cfg.app_address, amount), 1.3
        amount = self._amount(st, p, ctx.canon.spendable(slot, "l1"), ctx.canon.spendable(slot, "l1", upper=True))
        if amount is None:
            return None, "balance changed before sending", 0
        self._save_amount(st, p, amount)
        return cfg.input_box, cd_add_input(cfg.app_address, self._payload(kind, p, amount)), 1.3

    # ------------------------------------------------------------------ the sequencer route
    def _user_op_step(self, st, p, now_v) -> bool:
        ctx = self.ctx
        slot = st["slot"]
        w = ctx.wallet(slot)
        uo = ctx.db.q1("SELECT * FROM user_ops WHERE step_id=? AND status='signed' ORDER BY id DESC LIMIT 1", (st["id"],))
        try:
            if uo is not None and uo["attempts"] > 0:
                # a resubmission: if the sequencer already counts it, an earlier attempt landed
                if ctx.seq.next_nonce(w.address) > uo["nonce"]:
                    return self._soft(st, p, uo, {"nonce": uo["nonce"]}, note="included by an earlier attempt")
            if uo is None:
                uo = self._sign(st, p, now_v)
                if uo is None:
                    return False
            res = ctx.seq.submit(w.address, uo["nonce"], uo["max_fee"], bytes(uo["data"]), bytes(uo["signature"]))
            return self._soft(st, p, uo, res)
        except SeqUnavailable as e:
            self._seq_unavailable(st, p, uo, e, now_v)
            return False
        except SeqRejected as e:
            self._seq_rejected(st, p, uo, e, now_v)
            return False

    def _sign(self, st, p, now_v):
        """Quote, size, sign and record the op ('signed') before anything is sent."""
        ctx, cfg = self.ctx, self.ctx.cfg
        slot = st["slot"]
        w = ctx.wallet(slot)
        quote = ctx.seq.fee()
        ctx.db.x("INSERT INTO seq_fees(ts,fee,recommended,suggested) VALUES(?,?,?,?)",
                 (time.time(), int(quote["fee"]), quote.get("recommended_fee"), quote.get("suggested_max_fee")))
        max_fee = int(quote["suggested_max_fee"])
        fee_units = F.to_linear(max_fee)
        if fee_units > cfg.max_op_fee_usdc * USDC:
            ctx.alerts.raise_("seq.fee_high", "warning", "Sequencer fees above the cap",
                              f"suggested max fee {fee_units / USDC:.4f} USDC per op (cap {cfg.max_op_fee_usdc}); "
                              "users wait or use L1")
            self._wait(st, now_v + 600, "sequencer fee above the cap")
            return None
        ctx.alerts.resolve("seq.fee_high", f"sequencer fee back to {fee_units / USDC:.4f} USDC")
        spend = ctx.canon.spendable(slot, "seq") - fee_units
        if spend < (0 if st["invalid"] else USDC):
            return self._not_yet(st, p, now_v)
        upper = ctx.canon.spendable(slot, "seq", upper=True)
        amount = self._amount(st, p, spend, upper)
        if amount is None:
            return self._not_yet(st, p, now_v)
        self._save_amount(st, p, amount)
        payload = self._payload(st["kind"], p, amount)
        nonce = ctx.seq.next_nonce(w.address)
        expect = ctx.canon.pending_nonce(slot)
        if nonce != expect:
            ctx.log.warn("seq.nonce_hint_differs", slot=slot, sequencer=nonce, model=expect)
        sig = U.sign(w.key, self._separator, nonce, max_fee, payload)
        with ctx.db.tx() as c:
            uid = c.execute("INSERT INTO user_ops(run_id,step_id,slot,sender,nonce,max_fee,quote_fee,data,signature,status,"
                            "signed_at) VALUES(?,?,?,?,?,?,?,?,?,'signed',?)",
                            (ctx.run_id, st["id"], slot, w.address, nonce, max_fee, int(quote["fee"]), payload, sig,
                             time.time())).lastrowid
            c.execute("UPDATE steps SET status='sending', updated_at=? WHERE id=?", (time.time(), st["id"]))
        return ctx.db.q1("SELECT * FROM user_ops WHERE id=?", (uid,))

    def _not_yet(self, st, p, now_v):
        """Not enough spendable yet. A deposit on its way to the sequencer is worth waiting for; after
        an hour the user gives up on the sequencer and sends it through L1 instead."""
        ctx = self.ctx
        waited = now_v - (p.get("drain_wait_from") or now_v)
        if ctx.canon.has_pending_deposit(st["slot"]) and waited < DRAIN_WAIT_MAX_S:
            if "drain_wait_from" not in p:
                p["drain_wait_from"] = now_v
                ctx.db.x("UPDATE steps SET params=? WHERE id=?", (json.dumps(p), st["id"]))
            self._wait(st, now_v + 120, "waiting for the deposit to reach the sequencer")
        elif ctx.canon.has_pending_deposit(st["slot"]):
            ctx.db.x("UPDATE steps SET route='l1', note='deposit still not usable on the sequencer: using L1', due_at=?, "
                     "updated_at=? WHERE id=?", (now_v + 60, time.time(), st["id"]))
            ctx.log.info("step.route_fallback", step=st["id"], slot=st["slot"], reason="deposit not drained")
        else:
            self._finish(st, "skipped", "balance changed before sending")
        return None

    def _wait(self, st, due_v, note):
        self.ctx.db.x("UPDATE steps SET status='pending', due_at=?, note=?, updated_at=? WHERE id=?",
                      (due_v, note, time.time(), st["id"]))

    def _soft(self, st, p, uo, res, note=None) -> bool:
        ctx = self.ctx
        if int(res.get("nonce", -1)) != uo["nonce"]:
            ctx.alerts.raise_("seq.ack_mismatch", "error", "The sequencer acknowledged a different nonce",
                              f"slot {st['slot']}: signed nonce {uo['nonce']}, response {res}")
        now = time.time()
        with ctx.db.tx() as c:
            c.execute("UPDATE user_ops SET status='soft', soft_at=?, attempts=attempts+1, http_status=200 WHERE id=?",
                      (now, uo["id"]))
            c.execute("INSERT OR IGNORE INTO actions(run_id,step_id,slot,kind,route,amount,counterparty,intended,user_op_id,"
                      "created_at,state) VALUES(?,?,?,?,'seq',?,?,?,?,?,'pending')",
                      (ctx.run_id, st["id"], st["slot"], st["kind"], str(p["amount"]), p.get("to"),
                       "reject" if st["invalid"] else "accept", uo["id"], now))
        ctx.log.info("userop.soft", step=st["id"], slot=st["slot"], kind=st["kind"], nonce=uo["nonce"], max_fee=uo["max_fee"],
                     amount=p["amount"], invalid=bool(st["invalid"]), latency_ms=round((now - uo["signed_at"]) * 1000),
                     note=note)
        self._finish(st, "done", f"soft-confirmed nonce {uo['nonce']}")
        return True

    def _seq_unavailable(self, st, p, uo, e, now_v):
        """Not admitted. Resubmit the same signed op later; a user who keeps failing switches to L1,
        but only when no attempt can have reached the sequencer (a timeout might have)."""
        ctx = self.ctx
        if uo is None:
            # nothing signed yet (the quote or the nonce failed): wait a while, then do what people
            # do when an app's fast path is down: some use L1, the rest give up for now
            since = p.setdefault("seq_down_since", now_v)
            if now_v - since > SEQ_GIVE_UP_S:
                rng = rng_for(ctx.plan_seed, "seq-down", st["id"])
                if rng.random() < 0.5:
                    ctx.db.x("UPDATE steps SET route='l1', params=?, due_at=?, note='sequencer down: using L1', updated_at=? "
                             "WHERE id=?", (json.dumps(p), now_v + 60, time.time(), st["id"]))
                    ctx.log.info("step.route_fallback", step=st["id"], slot=st["slot"], reason="sequencer down")
                else:
                    self._finish(st, "skipped", "sequencer down: user gave up")
                return
            ctx.db.x("UPDATE steps SET params=? WHERE id=?", (json.dumps(p), st["id"]))
            self._wait(st, now_v + 120, f"sequencer unavailable: {e}")
            return
        delivered = 1 if e.delivered else 0
        ctx.db.x("UPDATE user_ops SET attempts=attempts+1, error=?, http_status=? WHERE id=?",
                 (str(e)[:200], -1 if delivered else 0, uo["id"]))
        p["maybe_delivered"] = bool(p.get("maybe_delivered")) or bool(delivered)
        attempts = uo["attempts"] + 1
        rng = rng_for(ctx.plan_seed, "seq-fallback", st["id"], attempts)
        if attempts >= SEQ_RETRY_LIMIT and not p["maybe_delivered"] and rng.random() < 0.5:
            with ctx.db.tx() as c:
                c.execute("UPDATE user_ops SET status='failed' WHERE id=?", (uo["id"],))
                c.execute("UPDATE steps SET status='pending', route='l1', params=?, due_at=?, note=?, updated_at=? WHERE id=?",
                          (json.dumps(p), now_v + 60, "sequencer unavailable: using L1", time.time(), st["id"]))
            ctx.log.info("step.route_fallback", step=st["id"], slot=st["slot"], reason="sequencer unavailable")
            return
        backoff = min(1800, 60 * 2 ** min(attempts, 5))
        ctx.db.x("UPDATE steps SET status='sending', params=?, due_at=?, note=?, updated_at=? WHERE id=?",
                 (json.dumps(p), now_v + backoff, f"sequencer unavailable ({attempts}x): {str(e)[:80]}", time.time(), st["id"]))

    def _seq_rejected(self, st, p, uo, e: SeqRejected, now_v):
        ctx = self.ctx
        if uo is not None:
            ctx.db.x("UPDATE user_ops SET status='rejected', http_status=?, error=?, attempts=attempts+1 WHERE id=?",
                     (e.status, f"{e.code}: {e.message}"[:300], uo["id"]))
            if uo["attempts"] > 0 and e.expected_nonce is not None and e.expected_nonce == uo["nonce"] + 1:
                # an earlier attempt of this same op was included (resubmission after a timeout)
                ctx.db.x("UPDATE user_ops SET status='signed' WHERE id=?", (uo["id"],))
                self._soft(st, p, ctx.db.q1("SELECT * FROM user_ops WHERE id=?", (uo["id"],)), {"nonce": uo["nonce"]},
                           note="bad-nonce answer to a resubmission")
                return
        if e.is_bug:
            self._finish(st, "failed", f"sequencer refused the request: {e}")
            ctx.alerts.raise_("seq.request_refused", "error", "The sequencer refused a user op as malformed",
                              f"step {st['id']} slot {st['slot']}: {e}")
            return
        tries = int(p.get("seq_rejections", 0)) + 1
        p["seq_rejections"] = tries
        ctx.log.warn("userop.rejected", step=st["id"], slot=st["slot"], status=e.status, code=e.code, message=e.message[:160],
                     attempt=tries)
        if tries >= 3:
            self._finish(st, "failed", f"sequencer rejected the op {tries}x: {e.message[:120]}")
            ctx.alerts.raise_("seq.rejections", "warning", "The sequencer keeps rejecting user ops",
                              f"step {st['id']} slot {st['slot']}: {e}")
            return
        ctx.db.x("UPDATE steps SET status='pending', params=?, due_at=?, note=?, updated_at=? WHERE id=?",
                 (json.dumps(p), now_v + 90, f"rejected: {e.message[:80]}", time.time(), st["id"]))

    # ------------------------------------------------------------------ bookkeeping
    def _finish(self, st, status, note=None):
        ctx = self.ctx
        with ctx.db.tx() as c:
            c.execute("UPDATE steps SET status=?, note=COALESCE(?, note), updated_at=? WHERE id=?",
                      (status, note, time.time(), st["id"]))
            if status in ("failed", "reverted") and st["session_id"] is not None:
                c.execute("UPDATE steps SET status='skipped', note='previous step failed', updated_at=? "
                          "WHERE session_id=? AND seq>? AND status='pending'", (time.time(), st["session_id"], st["seq"]))
            if st["session_id"] is not None:
                self._schedule_next(c, st)
                self._close_session_if_done(c, st["session_id"], failed=status in ("failed", "reverted"))

    def _schedule_next(self, c, st):
        nxt = c.execute("SELECT * FROM steps WHERE session_id=? AND seq=?", (st["session_id"], st["seq"] + 1)).fetchone()
        if nxt and nxt["status"] == "pending":
            think = json.loads(nxt["params"]).get("think", 30)
            c.execute("UPDATE steps SET due_at=? WHERE id=?", (self.ctx.clock.now() + think, nxt["id"]))

    def _close_session_if_done(self, c, session_id, failed=False):
        open_steps = c.execute("SELECT COUNT(*) AS n FROM steps WHERE session_id=? AND status NOT IN "
                               "('mined','skipped','failed','reverted','done')", (session_id,)).fetchone()["n"]
        if open_steps == 0:
            bad = c.execute("SELECT COUNT(*) AS n FROM steps WHERE session_id=? AND status IN ('failed','reverted')",
                            (session_id,)).fetchone()["n"]
            c.execute("UPDATE sessions SET status=?, finished_at=? WHERE id=? AND status='running'",
                      ("failed" if bad else "done", self.ctx.clock.now(), session_id))

    # ------------------------------------------------------------------ tx results
    def on_tx_final(self, tx_id: int, receipt):
        ctx = self.ctx
        st = ctx.db.q1("SELECT * FROM steps WHERE tx_id=?", (tx_id,))
        if st is None:
            return
        tx = ctx.db.q1("SELECT * FROM txs WHERE id=?", (tx_id,))
        kind = st["kind"]
        if tx["status"] == "dropped":
            attempts = json.loads(st["params"]).get("attempts", 0) + 1
            if attempts <= 3:
                p = dict(json.loads(st["params"]), attempts=attempts)
                ctx.db.x("UPDATE steps SET status='pending', tx_id=NULL, params=?, due_at=?, note='resend after drop', "
                         "updated_at=? WHERE id=?", (json.dumps(p), ctx.clock.now() + 30, time.time(), st["id"]))
            else:
                self._finish(st, "failed", "dropped 3 times")
            if kind == "execute":
                self.vouchers.on_exec_dropped(json.loads(st["params"])["output_index"])
            return
        if tx["status"] == "reverted":
            if kind == "execute":
                self.vouchers.on_exec_reverted(json.loads(st["params"])["output_index"], tx["revert"])
                self._finish(st, "reverted", tx["revert"])
                return
            self._finish(st, "reverted", tx["revert"])
            ctx.alerts.raise_(f"tx.reverted.{kind}", "warning", f"A {kind} transaction reverted",
                              f"tx {tx['hashes'] and json.loads(tx['hashes'])[-1]['hash']} slot {st['slot']}: {tx['revert']}")
            return
        # mined, status 1
        if kind in INPUT_KINDS:
            idxs = C.input_indexes_from_receipt(receipt, ctx.cfg.input_box, ctx.cfg.app_address)
            if len(idxs) != 1:
                self._finish(st, "failed", f"expected one InputAdded log, found {len(idxs)}")
                ctx.alerts.raise_("tx.no_input", "error", "Input transaction without an InputAdded event",
                                  f"step {st['id']}: {len(idxs)} logs")
                return
            p = json.loads(st["params"])
            ctx.db.x("INSERT OR IGNORE INTO actions(run_id,step_id,slot,kind,route,amount,counterparty,intended,l1_input,"
                     "l1_block,created_at,state) VALUES(?,?,?,?,'l1',?,?,?,?,?,?,'pending')",
                     (ctx.run_id, st["id"], st["slot"], kind, str(p["amount"]), p.get("to"),
                      "reject" if st["invalid"] else "accept", idxs[0], receipt["blockNumber"], time.time()))
            ctx.db.x("UPDATE steps SET input_index=? WHERE id=?", (idxs[0], st["id"]))
            ctx.canon.bind_late(idxs[0])
            ctx.log.info("input.mined", step=st["id"], slot=st["slot"], kind=kind, input_index=idxs[0],
                         amount=p["amount"], invalid=bool(st["invalid"]))
        elif kind == "execute":
            self.vouchers.on_exec_mined(json.loads(st["params"])["output_index"], receipt, st["slot"])
        elif kind == "gas_topup":
            self.funding.on_topup_mined(json.loads(st["params"])["to"])
        self._finish(st, "mined")

    def recover(self):
        """Startup: L1 steps left in 'sending' either have a txs row (then they were sent) or were
        never signed (then they can be retried). User-op steps in 'sending' keep their signed op:
        the next attempt first asks /nonce whether it was already included."""
        ctx = self.ctx
        for st in ctx.db.q("SELECT * FROM steps WHERE run_id=? AND status='sending'", (ctx.run_id,)):
            if st["route"] == "seq" and st["kind"] in ("transfer", "withdraw"):
                uo = ctx.db.q1("SELECT * FROM user_ops WHERE step_id=? AND status='signed' ORDER BY id DESC LIMIT 1", (st["id"],))
                if uo is None:
                    ctx.db.x("UPDATE steps SET status='pending' WHERE id=?", (st["id"],))
                elif uo["attempts"] == 0:
                    ctx.db.x("UPDATE user_ops SET attempts=1, error='crash before the answer' WHERE id=?", (uo["id"],))
                continue
            tx = ctx.db.q1("SELECT id FROM txs WHERE step_id=? ORDER BY id DESC LIMIT 1", (st["id"],))
            if tx:
                ctx.db.x("UPDATE steps SET status='sent', tx_id=? WHERE id=?", (tx["id"], st["id"]))
            else:
                ctx.db.x("UPDATE steps SET status='pending' WHERE id=?", (st["id"],))
        # soft ops whose action row was not written (crash between the 200 and the commit)
        for uo in ctx.db.q("SELECT u.* FROM user_ops u LEFT JOIN actions a ON a.user_op_id=u.id WHERE u.run_id=? "
                           "AND u.status IN ('soft','executed') AND a.id IS NULL", (ctx.run_id,)):
            st = ctx.db.q1("SELECT * FROM steps WHERE id=?", (uo["step_id"],))
            p = json.loads(st["params"])
            ctx.db.x("INSERT OR IGNORE INTO actions(run_id,step_id,slot,kind,route,amount,counterparty,intended,user_op_id,"
                     "created_at,state) VALUES(?,?,?,?,'seq',?,?,?,?,?,'pending')",
                     (ctx.run_id, st["id"], st["slot"], st["kind"], str(p["amount"]), p.get("to"),
                      "reject" if st["invalid"] else "accept", uo["id"], uo["soft_at"] or time.time()))
        # txs already final whose step was not updated (crash between poll and on_tx_final)
        for row in ctx.db.q("SELECT t.id FROM txs t JOIN steps s ON s.tx_id=t.id WHERE t.run_id=? AND "
                            "t.status IN ('mined','reverted','dropped') AND s.status='sent'", (ctx.run_id,)):
            rc = None
            tx = ctx.db.q1("SELECT * FROM txs WHERE id=?", (row["id"],))
            if tx["status"] != "dropped":
                mined = [h for h in json.loads(tx["hashes"]) if h.get("mined")]
                rc = ctx.l1.receipt(mined[0]["hash"]) if mined else None
            self.on_tx_final(row["id"], rc)
