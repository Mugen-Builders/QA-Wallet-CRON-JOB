"""The long-running service (systemd Type=notify). One single-threaded loop:

  every LOOP_SECONDS   heartbeat + watchdog, plan days ahead, poll transactions, start due
                       sessions, send due steps, schedule due voucher executions, flush Discord
  every RECONCILE      L1: new InputBox inputs folded by the canonical scheduler; node: input
                       statuses, voucher discovery, due detection; funder monitor
  every CHECKS         node outputs/reports, state, balances, custody, sequencer, node, app
  at REPORT_TIME       end-of-day report

A single thread means nonces, sessions and the database never race each other.
"""
from __future__ import annotations

import signal
import time
import traceback
from datetime import datetime, timedelta

from . import accounts as A
from . import report as R
from .actions import Engine
from .alerts import Alerts
from .canon import Canon, CanonError
from .chain import ETH, L1, RpcUnavailable, TxSender
from .checks import Checks
from .db import DB
from .events import EventLog
from .funding import Funding
from .health import Health, sd_notify
from .ingest import sync_inputs
from .node import Node, NodeError, NodeUnavailable
from .notify import Outbox
from .personas import rng_for
from .runtime import Ctx, FunderWallet, bootstrap_run, load_profiles, population
from .seqclient import Sequencer, SeqRejected, SeqUnavailable
from .schedule import Clock, day_start, day_target, plan_day, utc_date
from .vouchers import Vouchers

CATCHUP_WINDOW_S = 2 * 3600  # sessions missed by less than this are retried a little later


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = False
        self.log = EventLog(cfg.log_dir, min_level=cfg.log_level, retention_days=cfg.log_retention_days)
        self.db = DB(cfg.db_path)
        self.outbox = Outbox(self.db, self.log, cfg.discord_webhook_url, cfg.discord_prefix)
        self.alerts = Alerts(self.db, self.log, self.outbox, cfg.alert_repeat_hours)
        self.health = Health(cfg, self.db, self.log)
        self.ctx: Ctx | None = None
        self._t = {"reconcile": 0.0, "checks": 0.0, "plan": 0.0, "floor": 0.0}

    # ---------------------------------------------------------------- startup
    def _signal(self, *_):
        self.stop = True

    def start(self):
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        cfg = self.cfg
        gap = self.health.startup_gap()
        self.log.info("service.start", version=__import__("qatraffic").__version__, accounts=cfg.account_count,
                      app=cfg.app_address or "not set", gap_s=round(gap) if gap else None)
        wallets = A.load_wallets(cfg.entropy, cfg.account_offset, cfg.account_count, crosscheck=True)
        self.log.info("accounts.ok", count=len(wallets), first=wallets[0].address, last=wallets[-1].address,
                      fingerprint=A.population_fingerprint(wallets)[:16])
        seed = A.sub_seed(cfg.entropy, "plan")
        profiles = population(cfg, wallets, A.sub_seed(cfg.entropy, "population"))
        l1 = L1(cfg, self.db, self.log)
        funder = FunderWallet(cfg.funder_key) if cfg.funder_key else None
        self.ctx = Ctx(cfg=cfg, db=self.db, log=self.log, l1=l1, node=Node(cfg, self.db, self.log) if cfg.app_configured else None,
                       seq=Sequencer(cfg, self.db, self.log) if cfg.app_configured else None, wallets=wallets, profiles=profiles, run_id=None, clock=Clock(time.time(), cfg.time_scale),
                       alerts=self.alerts, plan_seed=seed, funder=funder)
        sd_notify("READY=1\nSTATUS=starting")
        self._announce_start(gap)

    def _announce_start(self, gap):
        cfg = self.cfg
        fields = [("Accounts", str(cfg.account_count)), ("App", cfg.app_address or "not set yet"),
                  ("Target", f"{cfg.daily_min}-{cfg.daily_max} inputs/day")]
        if gap is None:
            self.outbox.lifecycle("Bot started", "First start of the traffic bot on this machine.", fields)
        elif gap > max(120.0, 6 * cfg.loop_seconds):
            last = datetime.fromtimestamp(time.time() - gap, cfg.report_tz).strftime("%Y-%m-%d %H:%M %Z")
            self.outbox.lifecycle("Back online", f"The bot was down for **{gap / 60:.0f} min** (last heartbeat {last}).", fields)
        else:
            self.outbox.lifecycle("Restarted", f"Restarted after {gap:.0f} s.", fields)

    def _attach_run(self) -> bool:
        """Bind to the deployed app. Returns False while waiting (app not set / no code yet)."""
        ctx = self.ctx
        if ctx.run_id is not None:
            return True
        cfg = self.cfg
        if not cfg.app_configured:
            ctx.paused_reason = "QA_TRAFFIC_APP_ADDRESS not set"
            return False
        try:
            code = ctx.l1._call(lambda w3: w3.eth.get_code(cfg.app_address))
        except RpcUnavailable:
            return False
        if len(code) == 0:
            ctx.paused_reason = "no contract at QA_TRAFFIC_APP_ADDRESS yet"
            self.alerts.raise_("app.not_deployed", "warning", "The configured app address has no code",
                               f"{cfg.app_address} has no contract on chain {cfg.chain_id}; waiting")
            return False
        self.alerts.resolve("app.not_deployed", "contract found")
        if not self._sequencer_identity():
            return False
        run_id, clock_start = bootstrap_run(cfg, self.db, self.log, ctx.l1, ctx.wallets, ctx.profiles)
        ctx.run_id = run_id
        ctx.clock = Clock(clock_start, cfg.time_scale)
        ctx.profiles = load_profiles(self.db, run_id) or ctx.profiles
        ctx.sender = TxSender(cfg, self.db, self.log, ctx.l1, run_id)
        el, sp = ctx.l1.consensus_timing()
        ctx.timing = {"epoch_length": el, "staging": sp}
        ctx.paused_reason = None
        canon = Canon(ctx)
        try:
            canon.start()
        except (CanonError, NodeError, NodeUnavailable) as e:
            ctx.run_id = None
            ctx.paused_reason = f"canonical model: {e}"
            self.alerts.raise_("canon.start", "error", "The bot cannot build its model of the app", str(e)[:400])
            return False
        self.alerts.resolve("canon.start", "model built")
        ctx.canon = canon
        self.log.info("run.attached", run_id=run_id, epoch_length=el, staging=sp, sequencer=canon.sequencer)
        self.funding = Funding(ctx)
        self.vouchers = Vouchers(ctx)
        self.engine = Engine(ctx, self.funding, self.vouchers)
        self.checks = Checks(ctx)
        self.engine.recover()
        self._handle_missed_sessions()
        if self.cfg.sweep_on_start:
            self._sweep_on_start()
        return True

    def _sweep_on_start(self):
        """Return leftover wallet ETH and TUSD to the funder, once, right after attaching.

        Used on a headless host (e.g. fly.io) where there is no terminal to run `qatraffic sweep`:
        every (re)start reclaims the wallets, then the usual lazy top-ups refill them before each
        session. The transfers go through TxSender, so they are recorded and confirmed by the normal
        loop (no blocking wait, no watchdog risk). A wallet with an in-flight tx this run is skipped,
        and empty wallets cost nothing — so a crash-loop does not churn gas once the wallets are bare.
        """
        from .chain import FeeTooHigh, GWEI, InsufficientGas, Reverted, RpcAnswer, RpcUnavailable
        from .sweep import ERC20_TRANSFER_GAS, ETH_SEND_GAS, _transfer_data, DUST_WEI
        ctx = self.ctx
        if ctx.funder is None:
            return
        dest = ctx.funder.address
        tip = int(self.cfg.tip_min_gwei * GWEI)
        try:
            max_fee = ctx.sender.fees(tip).max_fee
        except FeeTooHigh as e:
            self.log.warn("sweep.skip", reason=str(e))
            return
        n_tok = n_eth = 0
        tok_total = eth_total = 0
        for w in ctx.wallets:
            if self.db.q1("SELECT 1 FROM txs WHERE run_id=? AND sender=? AND status IN ('pending','sending') LIMIT 1",
                          (ctx.run_id, w.address)):
                continue  # don't race a tx we are about to rebroadcast
            try:
                tok = ctx.l1.token_balance(w.address)
                bal = ctx.l1.eth_balance(w.address)
            except RpcUnavailable:
                continue
            sent_tok = False
            if tok > 0:
                try:
                    ctx.sender.send(w.key, w.address, w.slot, "sweep_tusd", self.cfg.token,
                                    data=_transfer_data(dest, tok), gas=ERC20_TRANSFER_GAS, tip_wei=tip)
                    sent_tok = True
                    n_tok += 1
                    tok_total += tok
                except (Reverted, RpcAnswer, RpcUnavailable, InsufficientGas) as e:
                    self.log.warn("sweep.tusd_failed", slot=w.slot, error=str(e)[:120])
            # reserve gas for both the (pending) TUSD tx and this ETH tx
            reserve = ((ERC20_TRANSFER_GAS if sent_tok else 0) + ETH_SEND_GAS) * max_fee
            value = bal - reserve
            if value > DUST_WEI:
                try:
                    ctx.sender.send(w.key, w.address, w.slot, "sweep_eth", dest, value=value,
                                    gas=ETH_SEND_GAS, tip_wei=tip)
                    n_eth += 1
                    eth_total += value
                except (Reverted, RpcAnswer, RpcUnavailable, InsufficientGas) as e:
                    self.log.warn("sweep.eth_failed", slot=w.slot, error=str(e)[:120])
        if n_tok or n_eth:
            self.log.info("sweep.on_start", tusd_transfers=n_tok, eth_transfers=n_eth,
                          tusd=tok_total / 10 ** 6, eth=eth_total / 10 ** 18, dest=dest)
            self.outbox.lifecycle("Swept on start",
                                  f"Returned leftover balances to the funder before running: "
                                  f"**{tok_total / 10 ** 6:.6f} TUSD** from {n_tok} wallet(s), "
                                  f"**{eth_total / 10 ** 18:.6f} ETH** from {n_eth} wallet(s). "
                                  "Wallets refill lazily before their next session.")

    def _sequencer_identity(self) -> bool:
        """The sequencer must sign for this chain and this app (GET /domain), like a wallet checks
        eth_chainId before signing."""
        ctx = self.ctx
        try:
            d = ctx.seq.domain()
        except (SeqUnavailable, SeqRejected) as e:
            ctx.paused_reason = f"sequencer unreachable: {e}"
            return False
        want = {"name": "CartesiAppSequencer", "version": "1", "chainId": self.cfg.chain_id,
                "verifyingContract": self.cfg.app_address.lower()}
        got = {"name": d.get("name"), "version": d.get("version"), "chainId": d.get("chainId"),
               "verifyingContract": str(d.get("verifyingContract", "")).lower()}
        if got != want:
            ctx.paused_reason = "sequencer domain mismatch"
            self.alerts.raise_("seq.domain", "critical", "The sequencer signs for a different chain or app",
                               f"GET /domain = {d}; the bot expects chain {self.cfg.chain_id}, app {self.cfg.app_address}")
            return False
        self.alerts.resolve("seq.domain", "sequencer domain matches")
        return True

    # ---------------------------------------------------------------- planning
    def _ensure_plans(self):
        ctx = self.ctx
        now_v = ctx.clock.now()
        today = utc_date(now_v)
        for d in (today, today + timedelta(days=1)):
            if self.db.q1("SELECT 1 FROM days WHERE run_id=? AND date=?", (ctx.run_id, d.isoformat())):
                continue
            day_no = (d - ctx.run_start_date()).days
            target, factor, note = day_target(self.cfg, ctx.plan_seed, d, day_no)
            # the first (partial) day gets the share of its target that is still ahead of us
            busy = {r["slot"]: r["planned_at"] for r in self.db.q(
                "SELECT slot, MAX(planned_at) AS planned_at FROM sessions WHERE run_id=? AND status IN ('planned','running') "
                "GROUP BY slot", (ctx.run_id,))}
            sessions = plan_day(self.cfg, ctx.plan_seed, d, ctx.run_start_date(), ctx.profiles, target, busy)
            sessions = [s for s in sessions if s.planned_at >= now_v - 60]
            with self.db.tx() as c:
                c.execute("INSERT INTO days(run_id,date,target_inputs,factor,note,created_at) VALUES(?,?,?,?,?,?)",
                          (ctx.run_id, d.isoformat(), target, factor, note, time.time()))
                for s in sessions:
                    c.execute("INSERT INTO sessions(run_id,date,planned_at,slot,intent,counterparty,planned_inputs,origin,status) "
                              "VALUES(?,?,?,?,?,?,?,?,'planned')",
                              (ctx.run_id, d.isoformat(), s.planned_at, s.slot, s.intent, s.counterparty, s.planned_inputs, s.origin))
            self.log.info("plan.day", date=d.isoformat(), target=target, sessions=len(sessions), note=note,
                          planned_inputs=sum(s.planned_inputs for s in sessions))

    def _handle_missed_sessions(self):
        """After downtime: sessions a little overdue are retried later, older ones are missed."""
        ctx = self.ctx
        now_v = ctx.clock.now()
        rows = self.db.q("SELECT * FROM sessions WHERE run_id=? AND status='planned' AND planned_at < ?",
                         (ctx.run_id, now_v - 15 * 60))
        missed = retried = 0
        for s in rows:
            late = now_v - s["planned_at"]
            if late > CATCHUP_WINDOW_S:
                self.db.x("UPDATE sessions SET status='missed', note='bot was down' WHERE id=?", (s["id"],))
                missed += 1
            else:
                rng = rng_for(ctx.plan_seed, "catchup", s["id"])
                if rng.random() < 0.7:  # most users come back later, some give up
                    self.db.x("UPDATE sessions SET planned_at=?, origin='catchup' WHERE id=?",
                              (now_v + rng.uniform(2, 60) * 60, s["id"]))
                    retried += 1
                else:
                    self.db.x("UPDATE sessions SET status='missed', note='user gave up during downtime' WHERE id=?", (s["id"],))
                    missed += 1
        if rows:
            self.log.info("sessions.after_downtime", missed=missed, retried=retried)

    def _ensure_daily_floor(self):
        """Late in the UTC day, top up the plan if failures or downtime would leave the day below
        DAILY_MIN inputs. The extra sessions are spread over the hours that remain."""
        ctx = self.ctx
        now_v = ctx.clock.now()
        d = utc_date(now_v)
        if d == ctx.run_start_date():
            return  # the run's first day starts part-way through; do not force it to the full floor
        t0 = day_start(d)
        frac = (now_v - t0) / 86400
        if frac < 0.5:
            return
        done = self.db.q1("SELECT COUNT(*) AS n FROM steps s JOIN sessions x ON x.id=s.session_id WHERE x.run_id=? AND x.date=? "
                          "AND s.kind IN ('deposit','transfer','withdraw') AND s.status IN ('mined','done')",
                          (ctx.run_id, d.isoformat()))["n"]
        ahead = self.db.q1("SELECT COALESCE(SUM(planned_inputs),0) AS n FROM sessions WHERE run_id=? AND date=? AND "
                           "status IN ('planned','running')", (ctx.run_id, d.isoformat()))["n"]
        need = self.cfg.daily_min - done - ahead
        if need <= 0:
            return
        rng = rng_for(ctx.plan_seed, "floor", d.isoformat(), int(now_v // 3600))
        remaining = t0 + 86400 - now_v - 600
        if remaining < 1800:
            return
        active = [p for p in ctx.profiles if p.onboard_day <= (d - ctx.run_start_date()).days]
        added = 0
        while added < need:
            p = rng.choice(active)
            intent = rng.choice(["pay", "topup", "cashout", "topup_pay"])
            n = 2 if intent == "topup_pay" else 1
            cp = rng.choice(p.friends) if p.friends and intent in ("pay", "topup_pay") else None
            self.db.x("INSERT INTO sessions(run_id,date,planned_at,slot,intent,counterparty,planned_inputs,origin,status) "
                      "VALUES(?,?,?,?,?,?,?,'catchup','planned')",
                      (ctx.run_id, d.isoformat(), now_v + rng.uniform(300, remaining), p.slot, intent, cp, n))
            added += n
        self.log.info("plan.floor_topup", date=d.isoformat(), added_inputs=added, done=done, ahead=ahead)

    # ---------------------------------------------------------------- loop
    def run(self):
        self.start()
        last_report_check = 0.0
        while not self.stop:
            t_iter = time.time()
            ok = True
            try:
                if self._attach_run():
                    self._iteration()
                else:
                    self.funding_monitor_idle()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                ok = False
                self.log.error("loop.error", error=f"{type(e).__name__}: {e}", detail=traceback.format_exc()[-1500:])
                self.alerts.raise_("bot.loop_error", "error", "The bot loop hit an error",
                                   f"{type(e).__name__}: {str(e)[:300]}")
            try:
                if time.time() - last_report_check > 30:
                    last_report_check = time.time()
                    self._maybe_report()
                self._watch_endpoints()
                self.alerts.flush()
                self.outbox.flush()
            except Exception as e:  # noqa: BLE001
                ok = False
                self.log.error("loop.error_tail", error=f"{type(e).__name__}: {e}")
            self.health.beat(ok, self._status())
            elapsed = time.time() - t_iter
            if elapsed > 120:
                self.log.warn("loop.slow", seconds=round(elapsed))
            end = time.time() + max(1.0, self.cfg.loop_seconds - elapsed)
            while not self.stop and time.time() < end:
                time.sleep(0.5)
        self._shutdown()

    def _iteration(self):
        ctx = self.ctx
        now = time.time()
        if now - self._t["plan"] > min(600.0, 21600.0 / self.cfg.time_scale):
            self._t["plan"] = now
            self._ensure_plans()
        if now - self._t["floor"] > min(1800.0, 21600.0 / self.cfg.time_scale):
            self._t["floor"] = now
            self._ensure_daily_floor()
        for tx_id, rcpt in ctx.sender.poll(ctx.keys):
            self.engine.on_tx_final(tx_id, rcpt)
        if now - self._t["reconcile"] > self.cfg.reconcile_seconds:
            self._t["reconcile"] = now
            try:
                ctx.canon.sync()
            except RpcUnavailable as e:
                self.log.warn("reconcile.l1_partial", error=type(e).__name__)
            except CanonError as e:
                self.alerts.raise_("canon.sync", "critical", "The bot's L1 input stream broke", str(e)[:400])
                ctx.paused_reason = "canonical model out of sync"
            try:
                sync_inputs(ctx)
                self.vouchers.discover()
                self.vouchers.update()
            except (NodeError, NodeUnavailable, RpcUnavailable) as e:
                self.log.warn("reconcile.partial", error=type(e).__name__)
            self._sample_fee()
            self.funding.monitor()
        self.vouchers.schedule()
        self.engine.start_due_sessions()
        self.engine.run_due_steps()
        if now - self._t["checks"] > self.cfg.checks_seconds:
            self._t["checks"] = now
            self.checks.run()

    def _sample_fee(self):
        """GET /fee: the sequencer's public liveness signal, and the fee history for the report."""
        ctx = self.ctx
        try:
            q = ctx.seq.fee()
        except (SeqUnavailable, SeqRejected):
            return
        self.db.x("INSERT INTO seq_fees(ts,fee,recommended,suggested) VALUES(?,?,?,?)",
                  (time.time(), int(q["fee"]), q.get("recommended_fee"), q.get("suggested_max_fee")))

    def funding_monitor_idle(self):
        """While waiting for the app, keep watching the funder balance (not its spending: the
        deployer may be using the same key to deploy)."""
        ctx = self.ctx
        if ctx is None or ctx.funder is None:
            return
        if time.time() - self._t.get("idle_funder", 0) < 900:
            return
        self._t["idle_funder"] = time.time()
        try:
            bal = ctx.l1.eth_balance(ctx.funder.address)
        except RpcUnavailable:
            return
        self.db.record_balance("funder", "eth", bal)
        if bal < int(self.cfg.funder_min_eth * ETH):
            self.alerts.raise_("funder.low", "warning", "Funder balance is low",
                               f"funder holds {bal / ETH:.5f} ETH (floor {self.cfg.funder_min_eth} ETH)")
        else:
            self.alerts.resolve("funder.low", f"funder holds {bal / ETH:.5f} ETH")

    def _watch_endpoints(self):
        ctx = self.ctx
        n = self.cfg.rpc_failures_before_alert
        if ctx is None:
            return
        if ctx.l1.consecutive_failures >= n:
            self.alerts.raise_("rpc.l1", "critical" if ctx.l1.consecutive_failures >= 4 * n else "warning",
                               "L1 RPC is failing", f"{ctx.l1.consecutive_failures} consecutive failed calls "
                               f"({ctx.l1.last_error}); traffic waits until it recovers")
        elif ctx.l1.consecutive_failures == 0:
            self.alerts.resolve("rpc.l1", "L1 RPC answering again")
        if ctx.seq:
            f = ctx.seq.consecutive_failures
            down_for = time.time() - (ctx.seq.first_failure_at or time.time())
            if f >= n and down_for >= 120:
                self.alerts.raise_("rpc.sequencer", "critical" if f >= 4 * n else "warning", "The sequencer is unreachable",
                                   f"{f} consecutive failed calls over {down_for / 60:.0f} min ({ctx.seq.last_error}); "
                                   "users retry later or fall back to L1")
            elif f == 0:
                self.alerts.resolve("rpc.sequencer", "sequencer answering again")
        if ctx.node:
            for role, label in (("node", "Node JSON-RPC"), ("inspect", "Node inspect endpoint")):
                f = ctx.node.consecutive_failures[role]
                if f >= n:
                    self.alerts.raise_(f"rpc.{role}", "critical" if f >= 4 * n else "warning", f"{label} is failing",
                                       f"{f} consecutive failed calls")
                elif f == 0:
                    self.alerts.resolve(f"rpc.{role}", f"{label} answering again")

    def _maybe_report(self):
        cfg, ctx = self.cfg, self.ctx
        if ctx is None or ctx.run_id is None:
            return
        now_local = datetime.now(cfg.report_tz)
        hh, mm = map(int, cfg.report_time.split(":"))
        if (now_local.hour, now_local.minute) < (hh, mm):
            return
        day = now_local.date()
        if self.db.q1("SELECT 1 FROM reports WHERE date=?", (day.isoformat(),)):
            return
        self.funding.monitor(force=True)
        rep = R.build(ctx, self.health, self.funding, self.checks, day)
        R.save_and_queue(ctx, self.outbox, rep)
        self.log.info("report.queued", date=day.isoformat(), inputs=rep["traffic"]["inputs"])

    def _status(self) -> dict:
        ctx = self.ctx
        if ctx is None or ctx.run_id is None:
            reason = ctx.paused_reason if ctx else "starting"
            return {"state": "waiting", "summary": f"waiting: {reason}", "paused": reason}
        db, run = self.db, ctx.run_id
        now_v = ctx.clock.now()
        nxt = db.q1("SELECT MIN(planned_at) AS t FROM sessions WHERE run_id=? AND status='planned'", (run,))["t"]
        v = {r["state"]: r["n"] for r in db.q("SELECT state, COUNT(*) AS n FROM vouchers WHERE run_id=? AND state IN "
                                              "('waiting','due','executing') GROUP BY state", (run,))}
        today = db.q1("SELECT COUNT(*) AS n FROM actions WHERE run_id=? AND created_at>=?", (run, day_start(utc_date(time.time()))))["n"]
        pending_tx = db.q1("SELECT COUNT(*) AS n FROM txs WHERE run_id=? AND status='pending'", (run,))["n"]
        soft = db.q1("SELECT COUNT(*) AS n FROM user_ops WHERE run_id=? AND status='soft'", (run,))["n"]
        summary = (f"run {run}; {today} actions today; {soft} soft ops not landed; vouchers waiting {v.get('waiting', 0)} "
                   f"due {v.get('due', 0)}"
                   + (f"; PAUSED: {ctx.paused_reason}" if ctx.paused_reason else ""))
        return {"state": "paused" if ctx.paused_reason else "running", "summary": summary, "run_id": run,
                "actions_today_utc": today, "soft_ops_waiting": soft,
                "canon": {"read_head": ctx.canon.read_head, "inputs": ctx.canon.fold.processed,
                          "executed": ctx.canon.fold.engine.executed, "queued_directs": len(ctx.canon.fold.fridge)}
                if ctx.canon else None, "next_session_in_s": round(ctx.clock.real_delay(nxt - now_v)) if nxt else None,
                "vouchers": v, "pending_txs": pending_tx, "open_alerts": len(self.alerts.open_alerts()),
                "outbox_pending": self.outbox.pending_count(), "paused": ctx.paused_reason}

    def _shutdown(self):
        self.log.info("service.stop")
        sd_notify("STOPPING=1")
        self.outbox.lifecycle("Bot stopped", "The service is shutting down (SIGTERM).")
        try:
            self.alerts.flush()
            self.outbox.flush(max_messages=3)
        except Exception:  # noqa: BLE001
            pass
        self.db.close()
