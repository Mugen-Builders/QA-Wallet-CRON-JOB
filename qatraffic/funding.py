"""Gas for the traffic accounts, and the funder's health.

Accounts get ETH only when they need it (lazily, at varying times and in varying amounts, never as
a batch), sometimes from a friend instead of the funder. TestUsdc is never funded: each account
mints its own, like a faucet. The funder's balance is watched for: below a floor, an unexplained
drop (spending the bot did not do), and runway at the current burn.
"""
from __future__ import annotations

import json
import time

from .chain import ETH, RpcUnavailable
from .personas import rng_for


class Funding:
    MONITOR_EVERY_S = 300

    def __init__(self, ctx):
        self.ctx = ctx
        self._eth: dict[int, tuple[float, int]] = {}  # slot -> (ts, wei)
        self._last_monitor = 0.0

    def eth(self, slot: int, max_age: float = 120) -> int:
        ts, wei = self._eth.get(slot, (0, 0))
        if time.time() - ts > max_age:
            wei = self.ctx.l1.eth_balance(self.ctx.wallet(slot).address)
            self._eth[slot] = (time.time(), wei)
        return wei

    def _pending_topup(self, slot) -> bool:
        return self.ctx.db.q1("SELECT 1 FROM steps WHERE run_id=? AND kind='gas_topup' AND status IN ('pending','sending','sent') "
                              "AND json_extract(params,'$.to')=? LIMIT 1", (self.ctx.run_id, slot)) is not None

    def ensure_gas(self, slot: int, force: bool = False) -> bool:
        """True when the account can pay for a session. Otherwise schedules a top-up (once)."""
        ctx = self.ctx
        floor = int(ctx.cfg.gas_min_eth * ETH)
        try:
            bal = self.eth(slot, max_age=0 if force else 120)
        except RpcUnavailable:
            return False
        if bal >= floor and not force:
            return True
        if self._pending_topup(slot):
            return False
        if ctx.funder is None:
            ctx.alerts.raise_("funding.no_funder", "critical", "No funder key configured",
                              "QA_TRAFFIC_FUNDER_KEY is not set; accounts cannot get gas")
            return False
        rng = rng_for(ctx.plan_seed, "topup", slot, int(time.time() // 3600))
        amount = int(rng.uniform(ctx.cfg.gas_topup_min_eth, ctx.cfg.gas_topup_max_eth) * ETH)
        amount = (amount // 10**12) * 10**12  # ~6 significant decimals, like a person typing an amount
        source = None
        prof = ctx.profiles[slot]
        if prof.friends and rng.random() < 0.15:
            for f in rng.sample(prof.friends, len(prof.friends)):
                try:
                    if self.eth(f) > amount * 3 + floor:
                        source = f
                        break
                except RpcUnavailable:
                    break
        delay = rng.uniform(20, 240)  # someone notices they need gas and sends it a bit later
        now_v = ctx.clock.now()
        ctx.db.x("INSERT INTO steps(session_id,run_id,seq,kind,slot,params,due_at,status,created_at,updated_at) "
                 "VALUES(NULL,?,0,'gas_topup',?,?,?,'pending',?,?)",
                 (ctx.run_id, source, json.dumps({"to": slot, "value": amount, "from": "friend" if source is not None else "funder"}),
                  now_v + delay, time.time(), time.time()))
        ctx.log.info("gas.topup_planned", slot=slot, value_eth=amount / ETH, source="friend" if source is not None else "funder")
        return False

    def on_topup_mined(self, slot: int):
        self._eth.pop(slot, None)

    # ---- funder monitor ----
    def monitor(self, force: bool = False):
        ctx = self.ctx
        if ctx.funder is None:
            return
        if not force and time.time() - self._last_monitor < self.MONITOR_EVERY_S:
            return
        self._last_monitor = time.time()
        try:
            bal = ctx.l1.eth_balance(ctx.funder.address)
        except RpcUnavailable:
            return
        ctx.db.record_balance("funder", "eth", bal)
        floor = int(ctx.cfg.funder_min_eth * ETH)
        if bal < floor:
            ctx.alerts.raise_("funder.low", "critical" if bal < floor // 2 else "warning", "Funder balance is low",
                              f"funder holds {bal / ETH:.5f} ETH (floor {ctx.cfg.funder_min_eth} ETH). "
                              f"{self._runway_text(bal)}")
        else:
            ctx.alerts.resolve("funder.low", f"funder back to {bal / ETH:.5f} ETH")
        # unexplained drop over the last hour
        hour_ago = ctx.db.q1("SELECT amount FROM balances WHERE who='funder' AND asset='eth' AND ts<=? ORDER BY ts DESC LIMIT 1",
                             (time.time() - 3600,))
        if hour_ago:
            prev = int(hour_ago["amount"])
            spent_by_bot = self._funder_spend_since(time.time() - 3600)
            unexplained = prev - bal - spent_by_bot
            if unexplained > int(ctx.cfg.funder_drop_alert_eth * ETH):
                ctx.alerts.raise_("funder.drop", "critical", "Funder balance dropped unexpectedly",
                                  f"{unexplained / ETH:.5f} ETH left the funder in the last hour beyond what the bot spent "
                                  f"({spent_by_bot / ETH:.5f} ETH). Someone else may be using the key.")
            else:
                ctx.alerts.resolve("funder.drop", "no unexplained spending in the last hour")
        # runway
        days = self.runway_days(bal)
        if days is not None and days < ctx.cfg.runway_alert_days:
            ctx.alerts.raise_("funder.runway", "warning", "Funder runway is short", self._runway_text(bal))
        elif days is not None:
            ctx.alerts.resolve("funder.runway", self._runway_text(bal))
        # bot-wide gas spend
        spent = self.gas_spent_since(time.time() - 86400)
        if spent > int(ctx.cfg.daily_gas_budget_eth * ETH):
            ctx.alerts.raise_("gas.budget", "warning", "Daily gas budget exceeded",
                              f"{spent / ETH:.5f} ETH in fees over 24 h (budget {ctx.cfg.daily_gas_budget_eth} ETH)")
        else:
            ctx.alerts.resolve("gas.budget", "gas spend back under the daily budget")

    def _funder_spend_since(self, since):
        ctx = self.ctx
        rows = ctx.db.q("SELECT value, fee_wei, gas, max_fee, status FROM txs WHERE run_id=? AND sender=? AND sent_at>=? "
                        "AND status IN ('mined','reverted','pending')", (ctx.run_id, ctx.funder.address, since))
        total = 0
        for r in rows:
            total += int(r["value"]) if r["status"] != "reverted" else 0
            total += int(r["fee_wei"]) if r["fee_wei"] else r["gas"] * r["max_fee"]
        return total

    def gas_spent_since(self, since) -> int:
        r = self.ctx.db.q("SELECT fee_wei FROM txs WHERE run_id=? AND mined_at>=? AND fee_wei IS NOT NULL",
                          (self.ctx.run_id, since))
        return sum(int(x["fee_wei"]) for x in r)

    def daily_burn(self) -> int:
        """Average funder outflow per day over the last 7 days (top-ups + its own fees)."""
        ctx = self.ctx
        since = time.time() - 7 * 86400
        first = ctx.db.q1("SELECT MIN(sent_at) AS t FROM txs WHERE run_id=? AND sender=?", (ctx.run_id, ctx.funder.address))
        if not first or first["t"] is None:
            return 0
        span = max(1.0, (time.time() - max(since, first["t"])) / 86400)
        return int(self._funder_spend_since(since) / span)

    def runway_days(self, bal) -> float | None:
        burn = self.daily_burn()
        return None if burn <= 0 else bal / burn

    def _runway_text(self, bal) -> str:
        d = self.runway_days(bal)
        burn = self.daily_burn()
        if d is None:
            return "No spending history yet, so no runway estimate."
        return f"At {burn / ETH:.5f} ETH/day that lasts about {d:.1f} days."
