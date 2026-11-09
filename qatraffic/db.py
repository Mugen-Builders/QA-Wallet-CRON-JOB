"""SQLite state: the bot's memory between iterations and across restarts.

The chain and the node are the ground truth; this database holds what only the bot knows: the
plan, the intent of every action, expected outcomes, the voucher lifecycle, alerts and health.
Every write that matters is in a transaction; a transaction is written BEFORE the external side
effect it describes (a step is `sending` before eth_sendRawTransaction) so a crash can be
reconciled on the next start without double-sending.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    chain_id INTEGER NOT NULL,
    genesis_hash TEXT NOT NULL,
    app_address TEXT NOT NULL,
    token TEXT NOT NULL,
    population TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    run_id INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    derivation_index INTEGER NOT NULL,
    address TEXT NOT NULL,
    persona TEXT NOT NULL,
    tz TEXT NOT NULL,
    profile TEXT NOT NULL,          -- JSON: weights, chronotype, habits, friends, onboarding
    PRIMARY KEY (run_id, slot)
);

CREATE TABLE IF NOT EXISTS days (
    run_id INTEGER NOT NULL,
    date TEXT NOT NULL,             -- UTC date of the virtual day
    target_inputs INTEGER NOT NULL,
    factor REAL NOT NULL,
    note TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, date)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    planned_at REAL NOT NULL,       -- virtual time
    slot INTEGER NOT NULL,
    intent TEXT NOT NULL,
    counterparty INTEGER,
    planned_inputs INTEGER NOT NULL,
    origin TEXT NOT NULL,           -- plan | followup | catchup
    status TEXT NOT NULL,           -- planned | running | done | missed | skipped | failed
    started_at REAL,
    finished_at REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS sessions_due ON sessions (run_id, status, planned_at);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    run_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,             -- mint | approve | deposit | transfer | withdraw | execute | gas_topup | inspect
    route TEXT,                     -- transfer / withdraw: seq (signed user op) | l1 (InputBox direct input)
    slot INTEGER,                   -- acting account (NULL = funder)
    params TEXT NOT NULL,           -- JSON
    expected TEXT,                  -- accept | reject | NULL (non-input)
    invalid INTEGER NOT NULL DEFAULT 0,
    due_at REAL NOT NULL,           -- virtual time
    status TEXT NOT NULL,           -- pending | sending | sent | mined | reverted | failed | skipped
    tx_id INTEGER,
    input_index INTEGER,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS steps_due ON steps (run_id, status, due_at);

CREATE TABLE IF NOT EXISTS txs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    sender TEXT NOT NULL,
    slot INTEGER,                   -- NULL = funder
    step_id INTEGER,                -- the step this tx carries out
    nonce INTEGER NOT NULL,
    kind TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    value TEXT NOT NULL,
    data TEXT NOT NULL,
    gas INTEGER NOT NULL,
    max_fee INTEGER NOT NULL,
    tip INTEGER NOT NULL,
    hashes TEXT NOT NULL,           -- JSON list, newest last (replacements)
    status TEXT NOT NULL,           -- signing | pending | mined | reverted | dropped
    block INTEGER,
    gas_used INTEGER,
    effective_gas_price INTEGER,
    fee_wei TEXT,
    revert TEXT,
    sent_at REAL NOT NULL,
    mined_at REAL,
    replaced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS txs_pending ON txs (run_id, status);
CREATE INDEX IF NOT EXISTS txs_step ON txs (step_id);
CREATE UNIQUE INDEX IF NOT EXISTS txs_nonce ON txs (run_id, sender, nonce);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    step_id INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    kind TEXT NOT NULL,             -- deposit | transfer | withdraw
    route TEXT NOT NULL,            -- l1 | seq
    amount TEXT NOT NULL,
    counterparty INTEGER,
    intended TEXT NOT NULL,         -- accept | reject (a deliberate overdraft)
    l1_input INTEGER,               -- InputBox index (deposits and the l1 route)
    l1_block INTEGER,
    user_op_id INTEGER,             -- the seq route
    created_at REAL NOT NULL,       -- mined (l1) or soft-confirmed (seq)
    state TEXT NOT NULL,            -- pending | executed | skipped | invalidated
    offset INTEGER,                 -- application history offset once executed
    exec_input INTEGER,             -- InputBox index of the input that executed it
    outcome TEXT,                   -- accept | reject | unknown (canonical fold)
    fee_units TEXT,
    exec_block INTEGER,
    executed_at REAL,               -- L1 timestamp of the executing input
    node_verified INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS actions_state ON actions (run_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS actions_l1 ON actions (run_id, l1_input) WHERE l1_input IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS actions_op ON actions (run_id, user_op_id) WHERE user_op_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS user_ops (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    step_id INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    sender TEXT NOT NULL,
    nonce INTEGER NOT NULL,
    max_fee INTEGER NOT NULL,
    quote_fee INTEGER,              -- GET /fee "fee" when signed
    data BLOB NOT NULL,
    signature BLOB NOT NULL,
    status TEXT NOT NULL,           -- signed | soft | executed | skipped | rejected | invalidated | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error TEXT,
    signed_at REAL NOT NULL,
    soft_at REAL,
    landed_at REAL,                 -- L1 timestamp of the batch that executed it
    exec_input INTEGER,
    offset INTEGER,
    batch_nonce INTEGER,
    fee_price INTEGER,
    outcome TEXT,
    landing_alerted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS user_ops_status ON user_ops (run_id, status);
CREATE INDEX IF NOT EXISTS user_ops_sender ON user_ops (run_id, sender, nonce);

CREATE TABLE IF NOT EXISTS l1_inputs (
    run_id INTEGER NOT NULL,
    input_index INTEGER NOT NULL,
    block INTEGER NOT NULL,
    block_ts INTEGER NOT NULL,
    tx_hash TEXT NOT NULL,
    sender TEXT NOT NULL,
    payload BLOB NOT NULL,
    kind TEXT,                      -- direct | batch
    outcome TEXT,                   -- enqueued | executed | stale | rejected:<reason>
    batch_nonce INTEGER,
    entries INTEGER,
    complete INTEGER,
    executed_after INTEGER,         -- application executed-input count after this input
    node_status TEXT,
    node_checked INTEGER NOT NULL DEFAULT 0,   -- 0 not yet | 1 matches | 2 mismatch
    node_note TEXT,
    seen_at REAL NOT NULL,
    PRIMARY KEY (run_id, input_index)
);

CREATE TABLE IF NOT EXISTS entries (
    run_id INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    exec_input INTEGER NOT NULL,
    source TEXT NOT NULL,           -- direct | user_op
    direct_input INTEGER,
    batch_nonce INTEGER,
    frame INTEGER,
    op INTEGER,
    sender TEXT NOT NULL,
    slot INTEGER,                   -- bot account, if any
    nonce INTEGER,
    fee INTEGER,
    fee_units TEXT,
    kind TEXT NOT NULL,
    amount TEXT,
    to_id TEXT,
    outcome TEXT NOT NULL,
    reason TEXT,
    block INTEGER NOT NULL,
    overdue INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, offset)
);

CREATE TABLE IF NOT EXISTS seq_fees (
    ts REAL NOT NULL,
    fee INTEGER NOT NULL,
    recommended INTEGER,
    suggested INTEGER
);

CREATE TABLE IF NOT EXISTS vouchers (
    run_id INTEGER NOT NULL,
    output_index INTEGER NOT NULL,
    input_index INTEGER NOT NULL,   -- the InputBox input that emitted it (a batch for user ops)
    action_id INTEGER,
    slot INTEGER NOT NULL,          -- owner
    kind TEXT NOT NULL,             -- voucher
    raw_output TEXT NOT NULL,
    amount TEXT NOT NULL,
    receiver TEXT NOT NULL,
    epoch_index INTEGER,
    state TEXT NOT NULL,            -- waiting | due | executing | executed | verified | executed_by_other | failed
    found_at REAL NOT NULL,
    eta_at REAL,                    -- wall-clock estimate of due time
    due_at REAL,                    -- wall clock when validateOutput first passed
    due_block INTEGER,
    planned_exec_at REAL,           -- virtual time
    deadline_at REAL,               -- virtual time
    executor_slot INTEGER,
    exec_step_id INTEGER,
    exec_tx_hash TEXT,
    executed_at REAL,
    executed_by TEXT,               -- owner | other_bot | external
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    overdue_alerted INTEGER NOT NULL DEFAULT 0,
    late INTEGER NOT NULL DEFAULT 0,
    verified_at REAL,
    PRIMARY KEY (run_id, output_index)
);
CREATE INDEX IF NOT EXISTS vouchers_state ON vouchers (run_id, state);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    run_id INTEGER,
    name TEXT NOT NULL,
    ok INTEGER NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_ts ON checks (ts);

CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    opened_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    last_sent_at REAL,
    resolved_at REAL,
    occurrences INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alert_history (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    opened_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,             -- alert | report | lifecycle
    payload TEXT NOT NULL,          -- JSON (Discord body)
    attachment_name TEXT,
    attachment TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    sent_at REAL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at REAL NOT NULL,
    last_beat REAL NOT NULL,
    last_ok REAL,
    pid INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS downtime (
    id INTEGER PRIMARY KEY,
    start REAL NOT NULL,
    end REAL NOT NULL,
    seconds REAL NOT NULL,
    cause TEXT
);

CREATE TABLE IF NOT EXISTS rpc_stats (
    hour INTEGER NOT NULL,          -- unix hour
    endpoint TEXT NOT NULL,         -- l1 | node | inspect | sequencer | sequencer_egress | discord
    ok INTEGER NOT NULL DEFAULT 0,
    err INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (hour, endpoint)
);

CREATE TABLE IF NOT EXISTS balances (
    ts REAL NOT NULL,
    who TEXT NOT NULL,              -- funder | slot:N | app
    asset TEXT NOT NULL,            -- eth | token
    amount TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS balances_who ON balances (who, asset, ts);

CREATE TABLE IF NOT EXISTS reports (
    date TEXT PRIMARY KEY,          -- local date in the report time zone
    created_at REAL NOT NULL,
    body TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._retire_v1(path)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        cur = self.get_meta("schema_version")
        if cur is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(cur) > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {cur} is newer than this code ({SCHEMA_VERSION})")
        path.chmod(0o600)

    @staticmethod
    def _retire_v1(path: Path):
        """Schema 1 belonged to the classic qa-tester-app build (no sequencer). Its runs cannot be
        continued against the sequencer variant, so the file is kept aside and a new one starts."""
        if not path.exists():
            return
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            conn.close()
        if row is not None and int(row[0]) == 1:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(path) + suffix)
                if p.exists():
                    p.rename(Path(f"{path}.v1-{stamp}.bak{suffix}"))

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        return self.conn.execute(sql, args)

    def get_meta(self, key, default=None):
        r = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key, value):
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))

    # ---- small helpers used everywhere ----
    def record_rpc(self, endpoint: str, ok: bool, error: str | None = None):
        hour = int(time.time() // 3600)
        if ok:
            self.x("INSERT INTO rpc_stats(hour,endpoint,ok) VALUES(?,?,1) "
                   "ON CONFLICT(hour,endpoint) DO UPDATE SET ok=ok+1", (hour, endpoint))
        else:
            self.x("INSERT INTO rpc_stats(hour,endpoint,err,last_error) VALUES(?,?,1,?) "
                   "ON CONFLICT(hour,endpoint) DO UPDATE SET err=err+1, last_error=excluded.last_error",
                   (hour, endpoint, (error or "")[:300]))

    def record_check(self, run_id, name: str, ok: bool, details: dict):
        self.x("INSERT INTO checks(ts,run_id,name,ok,details) VALUES(?,?,?,?,?)",
               (time.time(), run_id, name, 1 if ok else 0, json.dumps(details, default=str)))

    def record_balance(self, who: str, asset: str, amount: int):
        self.x("INSERT INTO balances(ts,who,asset,amount) VALUES(?,?,?,?)", (time.time(), who, asset, str(amount)))
