"""Runtime context shared by every component, and run bootstrap.

A *run* is one continuous experiment against one (chain, genesis, app, token, account set). If
any of these changes (a new app address in the env file, a different seed or offset, a chain
reset) the current run is closed and a new one starts, so state from one deployment is never
applied to another.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import accounts as A
from .chain import L1, TxSender
from .node import Node
from .seqclient import Sequencer
from .personas import Profile, build_population, profile_from_json
from .schedule import Clock, utc_date


@dataclass
class Ctx:
    cfg: object
    db: object
    log: object
    l1: L1
    node: Node | None
    seq: Sequencer | None
    wallets: list
    profiles: list[Profile]
    run_id: int | None
    clock: Clock
    alerts: object = None
    sender: TxSender | None = None
    plan_seed: bytes = b""
    funder: object = None  # accounts.Wallet-like for the funder key
    paused_reason: str | None = None
    timing: dict = field(default_factory=dict)  # epoch_length, staging, block_time
    canon: object = None  # canon.Canon once the run is attached

    @property
    def keys(self) -> dict[str, bytes]:
        k = {w.address: w.key for w in self.wallets}
        if self.funder:
            k[self.funder.address] = self.funder.key
        return k

    def wallet(self, slot: int):
        return self.wallets[slot]

    def run_start_date(self):
        return utc_date(self.clock.start)


class FunderWallet:
    def __init__(self, key_hex: str):
        from eth_account import Account
        acct = Account.from_key(key_hex)
        self.address = acct.address
        self.key = bytes(acct.key)
        self.slot = None

    def __repr__(self):
        return f"FunderWallet({self.address})"


def population(cfg, wallets, seed) -> list[Profile]:
    return build_population(seed, len(wallets), initial_active_share=cfg.initial_active_share,
                            onboarding_days=cfg.onboarding_days, tip_min_gwei=cfg.tip_min_gwei,
                            tip_max_gwei=cfg.tip_max_gwei, l1_share=cfg.l1_route_share)


def bootstrap_run(cfg, db, log, l1: L1, wallets, profiles) -> tuple[int, float]:
    """Return (run_id, clock_start). Starts a new run when the identity changed."""
    chain_id = l1.chain_id()
    if chain_id != cfg.chain_id:
        raise RuntimeError(f"RPC chain id {chain_id} != QA_TRAFFIC_CHAIN_ID {cfg.chain_id}")
    genesis = l1.block(0)["hash"].to_0x_hex()
    pop = A.population_fingerprint(wallets)
    cur = db.q1("SELECT * FROM runs WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    identity = (chain_id, genesis, cfg.app_address.lower(), cfg.token.lower(), pop)
    if cur is not None:
        cur_id = (cur["chain_id"], cur["genesis_hash"], cur["app_address"].lower(), cur["token"].lower(), cur["population"])
        if cur_id == identity:
            start = float(db.get_meta(f"run:{cur['id']}:clock_start", cur["started_at"]))
            return cur["id"], start
        reason = []
        if cur_id[1] != genesis:
            reason.append("chain reset (genesis changed)")
        if cur_id[2] != identity[2]:
            reason.append("app address changed")
        if cur_id[3] != identity[3]:
            reason.append("token changed")
        if cur_id[4] != pop:
            reason.append("account set changed (entropy/offset/count)")
        db.x("UPDATE runs SET ended_at=?, end_reason=? WHERE id=?", (time.time(), "; ".join(reason) or "identity changed", cur["id"]))
        log.warn("run.ended", run_id=cur["id"], reason="; ".join(reason))
    now = time.time()
    with db.tx() as c:
        rid = c.execute("INSERT INTO runs(chain_id,genesis_hash,app_address,token,population,started_at) VALUES(?,?,?,?,?,?)",
                        (chain_id, genesis, cfg.app_address, cfg.token, pop, now)).lastrowid
        for w, p in zip(wallets, profiles):
            c.execute("INSERT INTO accounts(run_id,slot,derivation_index,address,persona,tz,profile) VALUES(?,?,?,?,?,?,?)",
                      (rid, w.slot, w.index, w.address, p.persona, p.tz, json.dumps(p.to_json())))
    db.set_meta(f"run:{rid}:clock_start", now)
    log.info("run.started", run_id=rid, app=cfg.app_address, accounts=len(wallets))
    return rid, now


def load_profiles(db, run_id) -> list[Profile]:
    return [profile_from_json(json.loads(r["profile"])) for r in
            db.q("SELECT profile FROM accounts WHERE run_id=? ORDER BY slot", (run_id,))]
