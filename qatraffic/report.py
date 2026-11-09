"""End-of-day report: what the traffic did, how the withdrawals went, what was checked, what it
cost, and how healthy the bot and its endpoints were. Built only from the database and chain reads
the bot already recorded."""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta

from .chain import ETH

USDC = 10**6
SPARK = "▁▂▃▄▅▆▇█"


def local_window(cfg, day) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=cfg.report_tz)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def sparkline(values) -> str:
    hi = max(values) or 1
    return "".join(SPARK[min(len(SPARK) - 1, int(v / hi * (len(SPARK) - 1) + 0.5))] if v else " " for v in values)


def _median(xs):
    return round(statistics.median(xs), 1) if xs else None


def build(ctx, health, funding, checks, day) -> dict:
    db, run = ctx.db, ctx.run_id
    t0, t1 = local_window(ctx.cfg, day)
    acts = db.q("SELECT * FROM actions WHERE run_id=? AND created_at>=? AND created_at<?", (run, t0, t1))
    by_kind: dict[str, int] = {}
    by_route: dict[str, int] = {}
    volume: dict[str, int] = {}
    for r in acts:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        if r["kind"] != "deposit":
            by_route[r["route"]] = by_route.get(r["route"], 0) + 1
        volume[r["kind"]] = volume.get(r["kind"], 0) + int(r["amount"])
    invalid = sum(1 for r in acts if r["intended"] == "reject")
    rejected = sum(1 for r in acts if r["outcome"] == "reject")
    unexpected = sum(1 for r in acts if r["outcome"] in ("accept", "reject") and r["outcome"] != r["intended"])
    not_executed = {st: sum(1 for r in acts if r["state"] == st) for st in ("pending", "skipped", "invalidated")}
    hourly = [0] * 24
    for r in acts:
        hourly[datetime.fromtimestamp(r["created_at"], ctx.cfg.report_tz).hour] += 1
    users = {r["slot"] for r in acts}
    # sessions carry virtual times; map the real window onto the virtual clock
    v0 = ctx.clock.start + (t0 - ctx.clock.start) * ctx.cfg.time_scale
    v1 = ctx.clock.start + (t1 - ctx.clock.start) * ctx.cfg.time_scale
    sess = {r["status"]: r["n"] for r in db.q(
        "SELECT status, COUNT(*) AS n FROM sessions WHERE run_id=? AND started_at>=? AND started_at<? GROUP BY status",
        (run, v0, v1))}
    missed = db.q1("SELECT COUNT(*) AS n FROM sessions WHERE run_id=? AND status='missed' AND planned_at>=? AND planned_at<?",
                   (run, v0, v1))["n"]

    vfound = db.q("SELECT * FROM vouchers WHERE run_id=? AND found_at>=? AND found_at<?", (run, t0, t1))
    vdue = db.q("SELECT * FROM vouchers WHERE run_id=? AND due_at>=? AND due_at<?", (run, t0, t1))
    vexec = db.q("SELECT * FROM vouchers WHERE run_id=? AND executed_at>=? AND executed_at<?", (run, t0, t1))
    wait_min = [(v["due_at"] - v["found_at"]) / 60 for v in vdue if v["due_at"] and v["found_at"]]
    claim_min = []
    for v in vexec:
        if v["due_at"] and v["executed_at"]:
            claim_min.append((v["executed_at"] - v["due_at"]) / 60)
    w2d = []
    for v in vdue:
        i = db.q1("SELECT created_at FROM actions WHERE id=?", (v["action_id"],)) if v["action_id"] else None
        if i and v["due_at"]:
            w2d.append((v["due_at"] - i["created_at"]) / 60)
    states = {r["state"]: r["n"] for r in db.q("SELECT state, COUNT(*) AS n FROM vouchers WHERE run_id=? GROUP BY state", (run,))}
    overdue = db.q1("SELECT COUNT(*) AS n FROM vouchers WHERE run_id=? AND state='waiting' AND overdue_alerted=1", (run,))["n"]
    late = sum(1 for v in vexec if v["late"])
    by_exec = {}
    for v in vexec:
        by_exec[v["executed_by"] or "?"] = by_exec.get(v["executed_by"] or "?", 0) + 1
    paid_out = sum(int(v["amount"]) for v in vexec if v["state"] in ("verified", "executed_by_other"))

    checks_rows = db.q("SELECT name, ok, COUNT(*) AS n FROM checks WHERE run_id=? AND ts>=? AND ts<? GROUP BY name, ok", (run, t0, t1))
    chk: dict[str, dict] = {}
    for r in checks_rows:
        chk.setdefault(r["name"], {"ok": 0, "fail": 0})["ok" if r["ok"] else "fail"] += r["n"]

    txs = db.q("SELECT kind, status, fee_wei, replaced FROM txs WHERE run_id=? AND sent_at>=? AND sent_at<?", (run, t0, t1))
    tx_status: dict[str, int] = {}
    for t in txs:
        tx_status[t["status"]] = tx_status.get(t["status"], 0) + 1
    fees = sum(int(t["fee_wei"]) for t in txs if t["fee_wei"])
    replaced = sum(1 for t in txs if t["replaced"])

    fb = db.q("SELECT amount, ts FROM balances WHERE who='funder' AND asset='eth' AND ts>=? AND ts<? ORDER BY ts", (t0, t1))
    funder_start = int(fb[0]["amount"]) if fb else None
    funder_end = int(fb[-1]["amount"]) if fb else None
    runway = funding.runway_days(funder_end) if funder_end is not None and ctx.funder else None

    ops = db.q("SELECT * FROM user_ops WHERE run_id=? AND signed_at>=? AND signed_at<?", (run, t0, t1))
    op_status: dict[str, int] = {}
    for o in ops:
        op_status[o["status"]] = op_status.get(o["status"], 0) + 1
    landed = [o for o in ops if o["landed_at"] and o["soft_at"]]
    fees_paid = db.q("SELECT fee_units FROM entries e JOIN user_ops u ON u.run_id=e.run_id AND lower(u.sender)=lower(e.sender) "
                     "AND u.nonce=e.nonce WHERE e.run_id=? AND e.source='user_op' AND u.status='executed' AND u.signed_at>=? "
                     "AND u.signed_at<?", (run, t0, t1))
    quotes = [r["fee"] for r in db.q("SELECT fee FROM seq_fees WHERE ts>=? AND ts<?", (t0, t1))]
    from . import fee as F
    batches = db.q("SELECT outcome, COUNT(*) AS n FROM l1_inputs WHERE run_id=? AND kind='batch' AND block_ts>=? AND block_ts<? "
                   "GROUP BY outcome", (run, t0, t1))
    drain = [r["b"] - r["l1_block"] for r in db.q(
        "SELECT a.l1_block, i.block AS b FROM actions a JOIN l1_inputs i ON i.run_id=a.run_id AND i.input_index=a.exec_input "
        "WHERE a.run_id=? AND a.l1_input IS NOT NULL AND a.created_at>=? AND a.created_at<?", (run, t0, t1))]
    node_io = {r["node_checked"]: r["n"] for r in db.q(
        "SELECT node_checked, COUNT(*) AS n FROM l1_inputs WHERE run_id=? AND block_ts>=? AND block_ts<? GROUP BY node_checked",
        (run, t0, t1))}
    sequencer = {
        "user_ops": len(ops), "by_status": op_status,
        "median_soft_ms": _median([(o["soft_at"] - o["signed_at"]) * 1000 for o in ops if o["soft_at"]]),
        "median_soft_to_landed_min": _median([(o["landed_at"] - o["soft_at"]) / 60 for o in landed]),
        "fees_paid_usdc": sum(int(r["fee_units"] or 0) for r in fees_paid) / USDC,
        "fee_quote_median_usdc": (F.to_linear(int(statistics.median(quotes))) / USDC) if quotes else None,
        "batches": {r["outcome"]: r["n"] for r in batches},
        "median_direct_drain_blocks": _median(drain),
        "inputs_checked_against_node": {"match": node_io.get(1, 0), "mismatch": node_io.get(2, 0), "pending": node_io.get(0, 0)},
    }

    rpc = {}
    for r in db.q("SELECT endpoint, SUM(ok) AS ok, SUM(err) AS err FROM rpc_stats WHERE hour>=? AND hour<? GROUP BY endpoint",
                  (int(t0 // 3600), int(t1 // 3600) + 1)):
        rpc[r["endpoint"]] = {"ok": r["ok"], "err": r["err"]}
    downtime = db.q("SELECT start, end, seconds FROM downtime WHERE end>=? AND start<?", (t0, t1))
    alerts_opened = db.q("SELECT severity, title, opened_at, resolved_at FROM alert_history WHERE opened_at>=? AND opened_at<?",
                         (t0, t1))
    open_now = [dict(key=a["key"], severity=a["severity"], title=a["title"]) for a in ctx.alerts.open_alerts()]
    target = db.q1("SELECT SUM(target_inputs) AS n FROM days WHERE run_id=? AND date>=? AND date<=?",
                   (run, datetime.fromtimestamp(t0).date().isoformat(), datetime.fromtimestamp(t1 - 1).date().isoformat()))

    return {
        "date": day.isoformat(), "tz": str(ctx.cfg.report_tz), "generated_at": time.time(), "run_id": run,
        "app": ctx.cfg.app_address,
        "traffic": {"inputs": len(acts), "by_kind": by_kind, "by_route": by_route,
                    "volume_usdc": {k: v / USDC for k, v in volume.items()},
                    "deliberately_invalid": invalid, "rejected": rejected, "unexpected_outcomes": unexpected,
                    "not_executed": not_executed,
                    "active_users": len(users), "hourly_local": hourly, "sessions": sess, "missed_sessions": missed,
                    "planned_target_utc_days": target["n"] if target else None},
        "vouchers": {"found": len(vfound), "became_due": len(vdue), "executed": len(vexec), "executed_by": by_exec,
                     "late_over_deadline": late, "paid_out_usdc": paid_out / USDC, "overdue_now": overdue,
                     "median_found_to_due_min": _median(wait_min), "median_withdraw_to_due_min": _median(w2d),
                     "median_due_to_executed_min": _median(claim_min), "all_states": states},
        "sequencer": sequencer,
        "checks": chk, "custody": checks.last_custody, "state": checks.last_state,
        "transactions": {"sent": len(txs), "by_status": tx_status, "replaced": replaced, "fees_eth": fees / ETH},
        "funder": {"start_eth": funder_start / ETH if funder_start is not None else None,
                   "end_eth": funder_end / ETH if funder_end is not None else None,
                   "runway_days": round(runway, 1) if runway else None},
        "health": {"uptime": round(health.uptime_share(t0, min(t1, time.time())), 4),
                   "downtime": [{"start": d["start"], "end": d["end"], "minutes": round(d["seconds"] / 60, 1)} for d in downtime],
                   "rpc": rpc, "alerts_opened": [dict(a) for a in alerts_opened], "alerts_open_now": open_now},
    }


def _eth(v) -> str:
    return "?" if v is None else f"{v:.5f}"


def to_discord(rep: dict) -> tuple[str, str, list[tuple[str, str, bool]]]:
    t, v, h, tx, f = rep["traffic"], rep["vouchers"], rep["health"], rep["transactions"], rep["funder"]
    sq = rep["sequencer"]
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(t["by_kind"].items())) or "none"
    routes = ", ".join(f"{k} {n}" for k, n in sorted(t["by_route"].items())) or "-"
    title = f"Daily report · {rep['date']} ({rep['tz']})"
    desc = (f"**{t['inputs']} actions** from {t['active_users']} users ({kinds}; transfers/withdrawals via {routes}).\n"
            f"Hourly (local): `{sparkline(t['hourly_local'])}`")
    chk = ", ".join(f"{k} {c['ok']}✓" + (f" {c['fail']}✗" if c["fail"] else "") for k, c in sorted(rep["checks"].items())) or "none ran"
    cust = rep.get("custody")
    cust_txt = (f"surplus {cust['surplus'] / USDC:.2f} USDC at block {cust['block']}" if cust else "no consistent snapshot today")
    rpc = ", ".join(f"{k} {r['err']} err/{r['ok'] + r['err']}" for k, r in sorted(h["rpc"].items())) or "-"
    fields = [
        ("Outcomes", f"rejected {t['rejected']} (deliberate {t['deliberately_invalid']}); unexpected **{t['unexpected_outcomes']}**"
                     + "".join(f"; {k} {n}" for k, n in t["not_executed"].items() if n), False),
        ("Sequencer", f"{sq['user_ops']} user ops (" + ", ".join(f"{k} {n}" for k, n in sq["by_status"].items())
                      + f"); soft in {sq['median_soft_ms']} ms, landed after {sq['median_soft_to_landed_min']} min (median); "
                      f"fees {sq['fees_paid_usdc']:.4f} USDC; batches " + (", ".join(f"{k} {n}" for k, n in sq["batches"].items()) or "none")
                      + f"; L1 inputs (deposits, directs) executed {sq['median_direct_drain_blocks']} blocks after inclusion", False),
        ("Node vs fold", f"inputs matched {sq['inputs_checked_against_node']['match']}, mismatched "
                         f"**{sq['inputs_checked_against_node']['mismatch']}**, not yet {sq['inputs_checked_against_node']['pending']}", True),
        ("Vouchers", f"found {v['found']}, due {v['became_due']}, executed {v['executed']} "
                     f"({', '.join(f'{k} {n}' for k, n in v['executed_by'].items()) or '-'}); paid {v['paid_out_usdc']:.2f} USDC; "
                     f"overdue now **{v['overdue_now']}**, past deadline {v['late_over_deadline']}", False),
        ("Timing (median)", f"withdrawal→due {v['median_withdraw_to_due_min']} min; due→executed {v['median_due_to_executed_min']} min", False),
        ("Checks", chk, False),
        ("Custody", cust_txt, False),
        ("Transactions", f"{tx['sent']} sent, " + ", ".join(f"{k} {n}" for k, n in tx["by_status"].items())
                         + f"; replaced {tx['replaced']}; fees {tx['fees_eth']:.6f} ETH", False),
        ("Funder", f"{_eth(f['start_eth'])} → {_eth(f['end_eth'])} ETH"
                   f"; runway {f['runway_days'] if f['runway_days'] is not None else 'n/a'} days", True),
        ("Uptime", f"{h['uptime'] * 100:.2f}%" + (f", {len(h['downtime'])} outage(s)" if h["downtime"] else ""), True),
        ("RPC errors", rpc, False),
        ("Alerts", f"opened today {len(h['alerts_opened'])}; open now {len(h['alerts_open_now'])}"
                   + ("".join(f"\n• {a['severity']}: {a['title']}" for a in h["alerts_open_now"][:6])), False),
    ]
    if t["missed_sessions"]:
        fields.insert(1, ("Missed sessions", str(t["missed_sessions"]), True))
    return title, desc, fields


def save_and_queue(ctx, outbox, rep: dict):
    body = json.dumps(rep, indent=1, default=str)
    ctx.db.x("INSERT OR REPLACE INTO reports(date,created_at,body) VALUES(?,?,?)", (rep["date"], time.time(), body))
    out = ctx.cfg.home / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"report-{rep['date']}.json").write_text(body)
    title, desc, fields = to_discord(rep)
    outbox.report(title, desc, fields, (f"report-{rep['date']}.json", body))
