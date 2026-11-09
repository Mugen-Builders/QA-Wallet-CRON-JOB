"""Follow the node: the completion status of every InputBox input of the app (all senders).

In the sequencer variant the machine accepts every input: a business failure is a counted no-op
inside an accepted input, so REJECTED, like the terminal statuses, means the machine did something
the scheduler fold never does.
"""
from __future__ import annotations

import time

from .node import NodeError, NodeUnavailable, un

ERROR_STATUSES = ("EXCEPTION", "MACHINE_HALTED", "OVERFLOW", "UNEXPECTED_YIELD", "INVALID_OUTPUTS_ROOT")


def sync_inputs(ctx, limit: int = 200) -> dict:
    """Record the node's status for inputs it has processed. Returns counters."""
    try:
        count = ctx.node.processed_input_count()
    except (NodeError, NodeUnavailable):
        return {"error": True}
    ctx.db.set_meta(f"run:{ctx.run_id}:node_processed", count)
    rows = ctx.db.q("SELECT input_index FROM l1_inputs WHERE run_id=? AND node_status IS NULL AND input_index<? "
                    "ORDER BY input_index LIMIT ?", (ctx.run_id, count, limit))
    seen = 0
    for r in rows:
        try:
            inp = ctx.node.get_input(r["input_index"])
        except (NodeError, NodeUnavailable):
            break
        status = (inp or {}).get("status")
        if not status or status == "NONE":
            continue
        seen += 1
        ctx.db.x("UPDATE l1_inputs SET node_status=? WHERE run_id=? AND input_index=?", (status, ctx.run_id, r["input_index"]))
        if status in ERROR_STATUSES:
            ctx.alerts.raise_("app.terminal", "critical", f"The application stopped: input {r['input_index']} -> {status}",
                              "The machine reached a terminal state. No further input can be processed. Traffic is paused.")
            ctx.paused_reason = f"application terminal ({status})"
        elif status != "ACCEPTED":
            ctx.alerts.raise_(f"node.input_status.{r['input_index']}", "critical", "The node did not accept an input",
                              f"input {r['input_index']} is {status}; the sequencer variant accepts every input "
                              f"(epoch {un(inp.get('epoch_index'))})")
    return {"seen": seen, "node_count": count}


def node_lag(ctx) -> tuple[float | None, int]:
    """(age in minutes of the oldest input the node has not processed yet, how many are waiting)."""
    count = int(ctx.db.get_meta(f"run:{ctx.run_id}:node_processed", 0))
    r = ctx.db.q1("SELECT MIN(block_ts) AS t, COUNT(*) AS n FROM l1_inputs WHERE run_id=? AND input_index>=?",
                  (ctx.run_id, count))
    if not r or r["t"] is None:
        return None, 0
    return (time.time() - r["t"]) / 60, r["n"]
