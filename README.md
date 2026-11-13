# qa-traffic-bot

Keeps a deployed **qa-wallet-tester-app** (sequencer variant; Cartesi Rollups v3, rollups-contracts
`v3.0.0-alpha.10`, Cartesi sequencer `v0.1.0-alpha.10`, rollups-node `v2.0.0-alpha.13`) alive with
organic user traffic for weeks. A population of deterministic accounts:
- deposits TestUsdc through the ERC-20 portal;
- transfers it inside the app and withdraws it, either as signed user ops through the app's
  sequencer or as direct inputs on L1;
- follows every withdrawal voucher until the contract says it can be executed, executes it like a
  user would, and verifies the payout.

Throughout, the bot checks the node, the sequencer and the chain against its own port of the
sequencer's scheduler, and reports to Discord.

It runs on this machine as a systemd **user** service. It starts at boot, is restarted on failure,
and is restarted by the watchdog if it hangs.

## What the traffic looks like

- **No fixed interval.** Sessions start at random times drawn from the sum of every user's
  local-time activity curve. Users live in about 30 time zones, so the app is used at every hour,
  including the operator's night. A small uniform floor guarantees that no hour is empty.
- **Volume.** 80–120 actions per UTC day (deposits, transfers and withdrawals), never below 80.
  There are occasional busy days (up to 150) and quiet days, a busier first week, and
  weekday/weekend differences. Mints, approvals, gas top-ups and voucher executions are extra L1
  transactions on top.
- **People, not a loop.** 60 accounts by default, each with a persona:

  | persona | share | behaviour |
  |---|---|---|
  | power | 10% | daily, transfer-heavy |
  | regular | 32% | a few times a week |
  | casual | 44% | now and then; goes quiet for 3–10 days and comes back |
  | merchant | 10% | receives payments, cashes out |
  | whale | 4% | rare, large amounts |

  Each user also has its own:
  - time zone, sleep pattern and weekend habits;
  - wallet tip and approval habit (exact, buffer or unlimited);
  - **route preference**: most people use the sequencer, and a few send everything through L1
    (`QA_TRAFFIC_L1_ROUTE_SHARE`, 15% of transfers and withdrawals on average);
  - 1–6 regular counterparties.

  40% of users join over the first three weeks instead of on day one.
- **Sessions.** A user opens the app and does 1–4 things, with human pauses between them. Examples:
  mint TestUsdc (faucet-style), approve, deposit, pay a friend, cash out. A payment often makes the
  receiver act soon after. Amounts are human: mostly round values like 10, 25 or 100 USDC,
  sometimes odd ones like 37.42.
- **Waiting like a person.** A user who deposits and then wants to pay through the sequencer waits
  until the deposit reaches the sequencer. That happens once the sequencer's safe head covers it,
  10–15 minutes on Sepolia. After an hour they give up and send it through L1.

  When the sequencer is down, users retry later, and some switch to L1. They switch only when no
  attempt can have reached the sequencer, so an operation is never sent twice.
- **Mistakes, kept minimal.** About 1% of transfers and withdrawals overdraw on purpose. In the
  sequencer variant they are counted no-ops: the fee and the nonce are consumed, an error report
  is kept, and no voucher is produced.
- **Gas.** An account gets ETH only when an L1 step needs it, at a random time and in a random
  amount, and sometimes from a friend. Sequencer users need no gas at all; they pay the frame fee
  in TestUsdc inside the app.
- **Reproducible.** Everything is a function of the seed and the date. `python3 -m qatraffic plan`
  previews the traffic, and a day can be regenerated exactly.

## How the bot talks to the app

```
deposit ──────────────── Erc20Portal (L1 tx) ───────────────┐
transfer / withdraw ─┬── POST /tx (EIP-712 user op) ── sequencer ── batch ──► InputBox ──► node / machine
                     └── InputBox.addInput (L1 direct input) ──────────────┘
```

- **User ops.** The bot reads `GET /fee`, `GET /nonce` and checks `GET /domain` against the chain
  and the app, like a wallet. It signs `UserOp(uint32 nonce,uint16 max_fee,bytes data)` and POSTs
  it.
  - The signed op is stored before it is sent.
  - A timeout is resolved the documented way: `/nonce` above the op's nonce means it was included;
    otherwise the same signed op is resubmitted.
  - A `200` is a soft confirmation. The op then has to land on L1 inside a batch.
- **Direct inputs** are queued by the scheduler and executed when a frame's `safe_block` covers
  them, or after 1200 blocks. A deposit therefore executes in a later input than the one that
  carried it.

## The canonical model

The bot reads every InputBox input of the app from L1 up to the `safe` block. That includes other
people's inputs and the sequencer's batches. It folds them with a Python port of the scheduler and
the engine:
- the sequencer's `Scheduler::process_input`, with the overdue backstop, the batch gates (decode,
  nonce, empty, structure, staleness), drain-before-ops, user-op validation and fees;
- the qa-wallet engine, with the accounts-drive token ledger, nonces, the progress counters and
  every output and report.

The fee conversion, the SSZ batch codec and the EIP-712 recovery match the pinned sources bit for
bit (tests). The fold is rebuilt from the stored inputs on every start.

From the fold the bot knows:
- the canonical order and outcome of each of its actions;
- which input executed it;
- which output is its voucher;
- the exact state of the app after every input.

Inputs on other assets are counted but not modelled. That covers Ether, other ERC-20s and NFTs. A
self-transfer and a zero amount are not modelled either. Such an input is checked as an ordered
subset instead of exactly.

## Withdrawal vouchers

```
withdraw (user op or direct) ──► executed in input X (the batch, or the input whose drain ran it)
   ──► the node lists output k of input X: token.transfer(owner, amount)   (k known from the fold)
   ──► waiting (epoch not settled)
   ──► due: Application.validateOutput(output, proof) passes on chain
   ──► owner executes after a human delay (2 min to 3 h usually; asleep → after waking; hard limit 24 h)
       10% are executed by a different account (anyone can execute)
   ──► verified: the receipt has OutputExecuted(index) and exactly one Transfer(app → owner, amount)
```

Only the contract decides when a voucher is "due", so this works for Authority, Quorum and PRT. The
bot estimates each voucher's due time from the epoch and the claim staging period, and alerts when
a voucher is still not executable an hour past that estimate.

## Checks

| check | what | when |
|---|---|---|
| node I/O | every input the node processed emitted exactly the outputs and reports the fold predicts, in order | every 10 min |
| state | the app's `state` inspect equals the fold after the same input: executed count, scheduler nonce and queue, nonce table, rejected count, clock, pinned token and batch submitter | every 10 min |
| balances | sampled accounts: `ledger_getBalance` and `ledger_getNonce` equal the fold after the same input | every 10 min |
| custody | `token.balanceOf(app)` ≥ ledger total supply + every unexecuted voucher (all senders); the supply equals the fold's | every 10 min |
| soft confirmations | every soft-confirmed op lands in an executed batch in time. An op the scheduler skipped, an op the sequencer dropped (`/nonce` went back), and an op from a bot account the bot did not sign are each alerted | every 10 min |
| batches | stale or rejected batches; no batch for too long while work waits; direct inputs waiting long for a covering frame | continuously |
| sequencer checkpoint | (optional, operator egress) `/history`'s accepted checkpoint equals the fold | every 10 min |
| vouchers | position and content, timeliness, single payout of the right amount to the right receiver | continuously |
| node | the oldest unprocessed input is younger than 45 min; every input is ACCEPTED | every 10 min |
| app | not foreclosed; the node reports it OK (otherwise traffic pauses) | every 10 min |

Every inspect answer carries the processed-input count it reflects, and the fold keeps a snapshot
after every input. So every comparison is between consistent snapshots, at any moment.

The bot only ever sends `TransferErc20` and `WithdrawErc20`. Any other payload would be echoed by
the app instead of executed, so it is refused before signing.

## Discord

It reuses the Starknet monitor's webhook. Only the `DISCORD_WEBHOOK_URL` line is read from that
file, and none of its other secrets. Every message starts with `[cartesi-traffic]`.

- **Alerts** are sent when they open, repeated every 6 h while still open, sent again at once if
  they escalate, and followed by a "Resolved" message. They cover:
  - L1 RPC, node, inspect or sequencer down;
  - node lagging;
  - the model and the app disagreeing (node I/O, state, balances, supply);
  - soft confirmations not landing, skipped or dropped;
  - stale or rejected batches, batch silence, directs waiting;
  - wrong pinned token, sequencer domain mismatch;
  - vouchers overdue, missing or failing;
  - custody shortfall;
  - funder low, funder dropping unexpectedly, short runway, or gas budget exceeded;
  - gas or sequencer fees above the cap;
  - app foreclosed or terminal;
  - bot loop errors.
- **Lifecycle messages:** started, back online after N minutes down, stopped.
- **End-of-day report** at 23:55 Africa/Lagos, with the full JSON attached. It covers:
  - actions by kind and route, and an hourly sparkline;
  - expected and unexpected outcomes;
  - the sequencer: user ops by status, soft-confirmation latency, soft→landed time, fees paid,
    batches by outcome, direct drain time;
  - node-vs-fold results;
  - voucher counts and median timings;
  - check results and custody;
  - L1 transactions, fees and the funder's balance and runway;
  - uptime and downtime, RPC errors, and open alerts.

Messages are queued in SQLite first, so a Discord outage loses nothing. The webhook URL is never
logged.

## Is it alive?

- `systemctl --user status qa-traffic-bot`: the status line shows the bot's own summary.
- `journalctl --user -u qa-traffic-bot -f`: one line per event, with journald priorities (`-p warning`).
- `var/logs/events-YYYY-MM-DD.jsonl`: one JSON object per event.
- `var/state/heartbeat.json`: rewritten every loop.
- `python3 -m qatraffic status [-v]`: exit 0 = healthy, 1 = stale, 2 = never ran.
- The watchdog restarts the process if the loop stops for 3 minutes. On every start the bot posts
  how long it was down. Real-time alerting while the machine itself is off needs the optional
  `QA_TRAFFIC_HEALTHCHECK_URL` (an external dead-man's switch).

## Setup

```sh
git clone https://github.com/Mugen-Builders/QA-Wallet-CRON-JOB.git && cd QA-Wallet-CRON-JOB
pip install --user -r requirements.txt     # exact pins
python3 -m qatraffic new-entropy            # 32 random bytes for QA_TRAFFIC_ENTROPY
scripts/install-user-unit.sh                # creates ~/.config/qa-traffic-bot/config.env (600)
$EDITOR ~/.config/qa-traffic-bot/config.env # RPC URL, entropy, funder key (from the deployer)
scripts/install-user-unit.sh --start        # selfcheck, enable at boot, start
```

Until `QA_TRAFFIC_APP_ADDRESS` is set, the bot idles: it posts that it is waiting and watches the
funder balance. Once the app is deployed, edit `config.env`:
- the app address;
- the node's RPC and inspect URLs;
- the sequencer's public URL;
- optionally its operator URL.

Then run `python3 -m qatraffic selfcheck` and `systemctl --user restart qa-traffic-bot`.

The self-check confirms that the app pinned the configured token and batch submitter, and that the
sequencer signs for this chain and app. Changing the app address, the token, the seed or the
account set starts a new *run*; nothing from the old one is reused.

Useful commands (with the env loaded: `set -a; . ~/.config/qa-traffic-bot/config.env; set +a`):

```sh
python3 -m qatraffic selfcheck              # every dependency, sends nothing
python3 -m qatraffic accounts               # the population (addresses, personas, zones; no keys)
python3 -m qatraffic plan --days 7          # preview: per-day totals, hour-of-day histogram, gaps
python3 -m qatraffic report [--date D] [--send]
python3 -m qatraffic discord-test           # posts one test message
python3 -m qatraffic sweep                  # dry run: wallet ETH/TUSD + app-ledger balances
python3 -m qatraffic sweep --execute        # return wallet ETH and TUSD to the funder (teardown)
#   or set QA_TRAFFIC_SWEEP_ON_START=true to sweep automatically on every (re)start (headless hosts)
scripts/uninstall-user-unit.sh              # stop and remove the service (keeps config and state)
```

## Accounts and secrets

`QA_TRAFFIC_ENTROPY` (32 random bytes) maps to a 24-word BIP-39 mnemonic, and accounts are derived
on `m/44'/60'/0'/0/(offset+i)`. The same keys sign L1 transactions and EIP-712 user ops. On every
start the bot runs two self-checks, and it refuses to run if either fails:
1. the published BIP-39 vector;
2. every address re-derived by Foundry's `cast`, which reads the mnemonic from a 0600 file on
   tmpfs, never from the command line.

The mnemonic is never written or logged. Keys, RPC URLs and the webhook are kept out of every log,
and free-text error messages are scrubbed of URLs and 32-byte hex values.

## Layout

```
qatraffic/config.py     env parsing and validation (secrets kept out of repr)
qatraffic/accounts.py   entropy -> mnemonic -> accounts, self-checks
qatraffic/personas.py   population: personas, zones, habits, route preference, friend graph
qatraffic/schedule.py   virtual clock, daily targets, session plan, execution delays
qatraffic/actions.py    sessions -> steps -> L1 transactions or sequencer user ops (allowlist)
qatraffic/fee.py        the sequencer's log-space fee (bit-exact port)
qatraffic/ssz.py        the batch envelope codec (ethereum_ssz rules)
qatraffic/userop.py     EIP-712 user ops: sign, recover
qatraffic/fold.py       the canonical model: scheduler fold + qa-wallet engine
qatraffic/canon.py      L1 InputBox sync -> fold -> the bot's actions and user ops; spendable view
qatraffic/seqclient.py  sequencer API client (ingress; optional egress)
qatraffic/chain.py      L1 client (failover, error classes) and tx sender (nonces, fees, replacement)
qatraffic/node.py       rollups-node JSON-RPC + inspect client
qatraffic/ingest.py     input statuses from the node
qatraffic/vouchers.py   voucher lifecycle
qatraffic/checks.py     node I/O, state, balances, custody, sequencer, node and app checks
qatraffic/funding.py    gas top-ups and funder monitoring
qatraffic/alerts.py     alert state; notify.py Discord outbox; report.py daily report
qatraffic/health.py     heartbeat, systemd notify/watchdog, downtime
qatraffic/service.py    the loop; cli.py commands
```

State lives in `var/state/qatraffic.sqlite3` (WAL; 0600).
- An L1 step is written as `sending` before its transaction is broadcast, and the signed
  transaction is stored.
- A user op is stored signed before it is POSTed.

After a crash, nothing is sent twice and nothing is lost. A database from the earlier classic-app
build (schema 1) is moved aside on first start.

## Assumptions and limits

- **Target build.** It targets the qa-wallet-tester-app **sequencer** variant, a rollups-node v2
  (alpha.13) style API (`cartesi_*` JSON-RPC and `/inspect`), and the sequencer's public API. The
  classic variant (no sequencer) is not supported any more.
- **Token.** Only TestUsdc, the accounts-drive asset, is used. The token the app pinned at genesis
  must be the configured one; the bot pauses otherwise.
- **Other people's inputs** are folded like anyone's, so they do not break the model. Inputs on
  assets the model does not track are checked as a subset.
- **Sequencer recovery.** A recovery that replaces soft-confirmed ops is detected and reported
  (`/nonce` going back), not repaired. The canonical model is L1, so it stays correct either way.
- **Funding trail.** The ETH funding trail is public on-chain: anyone can trace gas back to the
  funder.
