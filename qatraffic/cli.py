"""Command line: python -m qatraffic <command>

  run            the service (systemd ExecStart)
  status         health from the heartbeat and the database (exit 0 ok, 1 stale, 2 never ran)
  selfcheck      config, account derivation, RPC, contracts, node, Discord config; sends nothing
  accounts       the account population (addresses, personas, time zones; never keys)
  plan           preview the traffic the seed produces (no chain access)
  report         build a daily report from the database (prints; --send queues it to Discord)
  discord-test   post one test message to the Discord webhook (outward: explicit only)
  sweep          return leftover wallet ETH and TUSD to the funder (--execute to send)
  new-entropy    print 32 fresh random bytes for QA_TRAFFIC_ENTROPY
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import accounts as A
from . import config as cfgmod


def _cfg(require_secrets=True):
    try:
        return cfgmod.load(require_secrets=require_secrets)
    except cfgmod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_run(_):
    from .service import Service
    Service(_cfg()).run()


def cmd_new_entropy(_):
    print(secrets.token_hex(32))


def cmd_sweep(args):
    from . import sweep
    cfg = _cfg(require_secrets=True)
    if not cfg.funder_key:
        sys.exit("QA_TRAFFIC_FUNDER_KEY is not set")
    sys.exit(sweep.run(cfg, execute=args.execute, to=args.to))


def cmd_accounts(args):
    cfg = _cfg(require_secrets=False)
    if not cfg.entropy:
        sys.exit("QA_TRAFFIC_ENTROPY is not set")
    from .runtime import population
    wallets = A.load_wallets(cfg.entropy, cfg.account_offset, cfg.account_count, crosscheck=not args.no_crosscheck)
    profiles = population(cfg, wallets, A.sub_seed(cfg.entropy, "population"))
    print(f"{'slot':>4} {'index':>5} {'address':42} {'persona':9} {'tz':32} {'joins':>5} friends")
    for w, p in zip(wallets, profiles):
        print(f"{w.slot:>4} {w.index:>5} {w.address:42} {p.persona:9} {p.tz:32} {'d' + str(p.onboard_day):>5} {p.friends}")
    print(f"\npopulation fingerprint {A.population_fingerprint(wallets)[:16]}; "
          f"derivation cross-checked with cast: {'no' if args.no_crosscheck else 'yes'}")


def cmd_plan(args):
    """Simulate the plan for N days and print what the traffic looks like."""
    cfg = _cfg(require_secrets=False)
    if not cfg.entropy:
        sys.exit("QA_TRAFFIC_ENTROPY is not set")
    from .runtime import population
    from .schedule import day_target, plan_day
    wallets = A.derive(cfg.entropy, cfg.account_offset, cfg.account_count)
    profiles = population(cfg, wallets, A.sub_seed(cfg.entropy, "population"))
    seed = A.sub_seed(cfg.entropy, "plan")
    start = date.fromisoformat(args.start) if args.start else datetime.now(timezone.utc).date()
    hourly = [0] * 24
    per_day = []
    per_slot = Counter()
    intents = Counter()
    origins = Counter()
    gaps = []
    for k in range(args.days):
        d = start + timedelta(days=k)
        target, factor, note = day_target(cfg, seed, d, k)
        ss = plan_day(cfg, seed, d, start, profiles, target)
        ins = sum(s.planned_inputs for s in ss)
        per_day.append((d, target, len(ss), ins, len({s.slot for s in ss}), note))
        times = sorted(s.planned_at for s in ss)
        gaps += [b - a for a, b in zip(times, times[1:])]
        for s in ss:
            hourly[datetime.fromtimestamp(s.planned_at, cfg.report_tz).hour] += s.planned_inputs
            per_slot[s.slot] += 1
            intents[s.intent] += 1
            origins[s.origin] += 1
    print(f"Plan preview: {args.days} days from {start} (UTC days), {cfg.account_count} accounts, "
          f"target {cfg.daily_min}-{cfg.daily_max}/day\n")
    print(f"{'date':10} {'target':>6} {'sessions':>8} {'inputs':>6} {'users':>5}  note")
    for d, t, n, ins, u, note in per_day:
        print(f"{d.isoformat():10} {t:>6} {n:>8} {ins:>6} {u:>5}  {note}")
    tot = sum(hourly) or 1
    print(f"\nInputs by hour of day in {cfg.report_tz} (all days):")
    hi = max(hourly) or 1
    for h in range(24):
        print(f"  {h:02d}:00 {hourly[h] / args.days:6.1f}/day  {'#' * int(40 * hourly[h] / hi)}")
    night = sum(hourly[h] for h in range(0, 6))
    print(f"\n00:00-06:00 local carries {100 * night / tot:.1f}% of inputs; quietest hour {min(hourly) / args.days:.1f}/day, "
          f"busiest {max(hourly) / args.days:.1f}/day")
    if gaps:
        gaps.sort()
        print(f"gap between consecutive sessions: median {gaps[len(gaps) // 2] / 60:.1f} min, "
              f"p90 {gaps[int(len(gaps) * 0.9)] / 60:.1f} min, max {gaps[-1] / 60:.1f} min")
    by_persona = Counter()
    for slot, n in per_slot.items():
        by_persona[profiles[slot].persona] += n
    print("sessions by persona: " + ", ".join(f"{k} {v}" for k, v in by_persona.most_common()))
    print("sessions by intent:  " + ", ".join(f"{k} {v}" for k, v in intents.most_common()))
    print("sessions by origin:  " + ", ".join(f"{k} {v}" for k, v in origins.most_common()))
    counts = sorted(per_slot.values(), reverse=True)
    print(f"sessions per account over the period: max {counts[0] if counts else 0}, "
          f"median {counts[len(counts) // 2] if counts else 0}, accounts never active {cfg.account_count - len(per_slot)}")


def cmd_selfcheck(_):
    cfg = _cfg()
    from .chain import L1, RpcUnavailable
    from .db import DB
    from .events import EventLog
    rows = []

    def check(name, fn):
        try:
            rows.append(("PASS", name, fn() or ""))
        except Exception as e:  # noqa: BLE001
            rows.append(("FAIL", name, f"{type(e).__name__}: {str(e)[:160]}"))

    check("BIP-39 published vector", lambda: A.bip39_vector_check() or "seed matches")
    wallets = []

    def derive():
        wallets.extend(A.load_wallets(cfg.entropy, cfg.account_offset, cfg.account_count,
                                      crosscheck=cfg.wallet_crosscheck))
        matched = "every address matches cast" if cfg.wallet_crosscheck != "off" and (
            shutil.which("cast") or os.path.exists(os.path.expanduser("~/.foundry/bin/cast"))
        ) else f"cast cross-check {cfg.wallet_crosscheck} (not run)"
        return f"{len(wallets)} accounts, {matched}"
    check("account derivation", derive)
    cfg.home.mkdir(parents=True, exist_ok=True)
    db = DB(cfg.db_path)
    log = EventLog(cfg.log_dir, min_level="warn")
    l1 = L1(cfg, db, log)

    def chain():
        cid = l1.chain_id()
        if cid != cfg.chain_id:
            raise RuntimeError(f"chain id {cid} != {cfg.chain_id}")
        return f"chain {cid}, head {l1.block_number()}"
    check("L1 RPC", chain)

    def code(addr, label):
        def f():
            c = l1._call(lambda w3: w3.eth.get_code(addr))
            if len(c) == 0:
                raise RuntimeError(f"no code at {addr}")
            return f"{label} {addr} ({len(c)} bytes)"
        return f
    for addr, label in ((cfg.input_box, "InputBox"), (cfg.erc20_portal, "Erc20Portal"), (cfg.token, "token")):
        check(f"contract {label}", code(addr, label))

    def token():
        t = l1.token
        sym = l1._call(lambda w3: L1._c(w3, t).functions.symbol().call())
        dec = l1._call(lambda w3: L1._c(w3, t).functions.decimals().call())
        if dec != 6:
            raise RuntimeError(f"decimals {dec}, the bot assumes 6")
        return f"{sym}, {dec} decimals"
    check("token metadata", token)
    if cfg.funder_key:
        from .runtime import FunderWallet
        fw = FunderWallet(cfg.funder_key)
        check("funder", lambda: f"{fw.address} holds {l1.eth_balance(fw.address) / 1e18:.5f} ETH")
    else:
        rows.append(("WARN", "funder", "QA_TRAFFIC_FUNDER_KEY not set (accounts cannot get gas)"))
    if cfg.app_configured:
        check("app contract", code(cfg.app_address, "Application"))
        check("app not foreclosed", lambda: "ok" if not l1.is_foreclosed() else (_ for _ in ()).throw(RuntimeError("foreclosed")))
        check("consensus timing", lambda: "epoch length %s, claim staging %s blocks" % l1.consensus_timing())
        from .node import Node
        node = Node(cfg, db, log)

        def nodeapp():
            a = node.application()
            return f"status {a.get('status')}, processed {node.processed_input_count()}, epoch_length {a.get('epoch_length')}"
        check("node JSON-RPC", nodeapp)
        check("node inspect", lambda: "ledger total supply %s (processed %s)" % node.ledger_total_supply(cfg.token))

        def app_state():
            st = node.state()
            if st.get("token", "").lower() != cfg.token.lower():
                raise RuntimeError(f"the app pinned token {st.get('token')} at genesis, not {cfg.token}")
            if cfg.batch_submitter and st.get("sequencer_address", "").lower() != cfg.batch_submitter.lower():
                raise RuntimeError(f"the app pinned batch submitter {st.get('sequencer_address')}")
            sch = st.get("scheduler") or {}
            return (f"token ok, submitter {st.get('sequencer_address')}, executed {st.get('executed_input_count')}, "
                    f"next batch nonce {sch.get('next_batch_nonce')}, queued directs {sch.get('queued_directs')}")
        check("app state (sequencer variant)", app_state)
        from .seqclient import Sequencer
        seq = Sequencer(cfg, db, log)

        def seq_domain():
            d = seq.domain()
            if d.get("chainId") != cfg.chain_id or str(d.get("verifyingContract", "")).lower() != cfg.app_address.lower():
                raise RuntimeError(f"domain {d} is not chain {cfg.chain_id} / app {cfg.app_address}")
            return f"{d.get('name')} v{d.get('version')}, chain {d.get('chainId')}"
        check("sequencer /domain", seq_domain)

        def seq_fee():
            from . import fee as F
            q = seq.fee()
            return (f"fee {q['fee']} ({F.to_linear(int(q['fee'])) / 1e6:.6f} USDC), suggested max "
                    f"{q['suggested_max_fee']} ({F.to_linear(int(q['suggested_max_fee'])) / 1e6:.6f} USDC)")
        check("sequencer /fee", seq_fee)
        if cfg.sequencer_egress_url:
            check("sequencer /history (egress)", lambda: "accepted checkpoint %s" % (seq.history() or {}).get("accepted_checkpoint"))
    else:
        rows.append(("WAIT", "app", "QA_TRAFFIC_APP_ADDRESS not set; the bot idles until it is"))
    rows.append(("PASS" if cfg.discord_webhook_url else "WARN", "Discord webhook",
                 "configured (not contacted)" if cfg.discord_webhook_url else "not configured: alerts only in logs"))
    width = max(len(r[1]) for r in rows)
    for st, name, info in rows:
        print(f"{st:4}  {name:{width}}  {info}")
    sys.exit(1 if any(r[0] == "FAIL" for r in rows) else 0)


def cmd_status(args):
    cfg = _cfg(require_secrets=False)
    path = cfg.heartbeat_path
    if not path.exists():
        print("never ran (no heartbeat file)")
        sys.exit(2)
    hb = json.loads(path.read_text())
    age = time.time() - hb["last_beat"]
    stale = age > max(120, 6 * cfg.loop_seconds)
    print(f"state      {hb.get('state')}{'  (STALE)' if stale else ''}")
    print(f"last beat  {age:.0f} s ago; last fully ok {time.time() - hb['last_ok']:.0f} s ago" if hb.get("last_ok") else
          f"last beat  {age:.0f} s ago")
    print(f"summary    {hb.get('summary')}")
    for k in ("actions_today_utc", "soft_ops_waiting", "canon", "vouchers", "pending_txs", "open_alerts", "outbox_pending", "next_session_in_s", "paused"):
        if k in hb:
            print(f"{k:10} {hb[k]}")
    if args.verbose:
        from .db import DB
        db = DB(cfg.db_path)
        for a in db.q("SELECT key, severity, title, opened_at FROM alerts WHERE resolved_at IS NULL ORDER BY opened_at"):
            print(f"  open alert [{a['severity']}] {a['title']} ({(time.time() - a['opened_at']) / 60:.0f} min)")
    sys.exit(1 if stale else 0)


def _offline_ctx(cfg):
    from .alerts import Alerts
    from .chain import L1
    from .db import DB
    from .events import EventLog
    from .notify import Outbox
    from .runtime import Ctx, FunderWallet
    from .schedule import Clock
    db = DB(cfg.db_path)
    log = EventLog(cfg.log_dir, min_level="warn")
    run = db.q1("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    if run is None:
        sys.exit("no run recorded yet")
    outbox = Outbox(db, log, cfg.discord_webhook_url, cfg.discord_prefix)
    alerts = Alerts(db, log, outbox, cfg.alert_repeat_hours)
    start = float(db.get_meta(f"run:{run['id']}:clock_start", run["started_at"]))
    ctx = Ctx(cfg=cfg, db=db, log=log, l1=L1(cfg, db, log) if cfg.l1_rpc_urls else None, node=None, seq=None, wallets=[], profiles=[],
              run_id=run["id"], clock=Clock(start, cfg.time_scale), alerts=alerts,
              funder=FunderWallet(cfg.funder_key) if cfg.funder_key else None)
    return ctx, outbox


def cmd_report(args):
    cfg = _cfg(require_secrets=False)
    from . import report as R
    from .checks import Checks
    from .funding import Funding
    from .health import Health
    ctx, outbox = _offline_ctx(cfg)
    day = date.fromisoformat(args.date) if args.date else datetime.now(cfg.report_tz).date()
    rep = R.build(ctx, Health(cfg, ctx.db, ctx.log), Funding(ctx), Checks(ctx), day)
    print(json.dumps(rep, indent=1, default=str))
    if args.send:
        R.save_and_queue(ctx, outbox, rep)
        outbox.flush()
        print("queued and flushed" if outbox.pending_count() == 0 else "queued; delivery pending (see logs)")


def cmd_discord_test(_):
    cfg = _cfg(require_secrets=False)
    if not cfg.discord_webhook_url:
        sys.exit("no Discord webhook configured")
    from .db import DB
    from .events import EventLog
    from .notify import Outbox
    db = DB(cfg.db_path)
    ob = Outbox(db, EventLog(cfg.log_dir), cfg.discord_webhook_url, cfg.discord_prefix)
    ob.lifecycle("Test message", "qa-traffic-bot can post to this channel. No action needed.")
    ob.flush()
    left = ob.pending_count()
    print("sent" if left == 0 else f"not sent yet ({ob.last_error}); it stays queued and the service retries it")
    sys.exit(0 if left == 0 else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="qatraffic", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    s = sub.add_parser("status")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_status)
    sub.add_parser("selfcheck").set_defaults(fn=cmd_selfcheck)
    s = sub.add_parser("accounts")
    s.add_argument("--no-crosscheck", action="store_true")
    s.set_defaults(fn=cmd_accounts)
    s = sub.add_parser("plan")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--start", help="first UTC day (YYYY-MM-DD), default today")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("report")
    s.add_argument("--date", help="local day in QA_TRAFFIC_REPORT_TZ (YYYY-MM-DD), default today")
    s.add_argument("--send", action="store_true", help="also queue it to Discord")
    s.set_defaults(fn=cmd_report)
    sub.add_parser("discord-test").set_defaults(fn=cmd_discord_test)
    s = sub.add_parser("sweep")
    s.add_argument("--execute", action="store_true", help="actually send (default: dry run)")
    s.add_argument("--to", default=None, help="destination address (default: the funder)")
    s.set_defaults(fn=cmd_sweep)
    sub.add_parser("new-entropy").set_defaults(fn=cmd_new_entropy)
    args = p.parse_args(argv)
    args.fn(args)
