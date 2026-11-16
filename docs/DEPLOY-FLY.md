# Deploying qa-traffic-bot on fly.io

qa-traffic-bot is a **single always-on process**, not a cron/scheduled job. On fly.io it runs as
**one resident Machine** with a persistent volume. It keeps in-memory and SQLite state and
schedules its own traffic internally, so it must stay running and there must be **exactly one
instance**.

> ⚠️ **One instance only.** The bot shares wallets and nonces across its single process. A second
> Machine would double-spend gas/funds and corrupt nonce tracking. Never `fly scale count > 1`,
> never enable autoscaling, and keep the volume (which pins the app to one Machine) in place.

Files in this repo for the deploy: [`Dockerfile`](../Dockerfile), [`.dockerignore`](../.dockerignore),
[`fly.toml`](../fly.toml). They are reviewed templates — edit `app` and `primary_region` before
first deploy.

---

## 1. Prerequisites
- `flyctl` installed and logged in (`fly auth login`).
- The app is deployed and you have its config: app address, node RPC + inspect URLs, sequencer URL,
  token, and the funder key + entropy. Your local `config.env` already has these — you will copy
  the **secret** ones into fly secrets below.
- Pick a `primary_region` close to your RPC / node / sequencer to keep latency down.

## 2. Create the app and volume
```bash
fly launch --no-deploy --copy-config --name <your-app-name>   # reconciles fly.toml, creates the app
# set primary_region in fly.toml to your chosen region first, then:
fly volumes create qa_traffic_data --size 1 --region <same-region>
```
The volume mounts at `/data`; `QA_TRAFFIC_HOME=/data` (set in `fly.toml [env]`) puts the SQLite DB,
heartbeat and logs there so they survive restarts.

## 3. Set secrets (never bake these into the image or fly.toml)
Set each from the value in your local `config.env` — do not commit or echo them into logs:
```bash
fly secrets set \
  QA_TRAFFIC_L1_RPC_URL='...' \
  QA_TRAFFIC_L1_RPC_URL_FALLBACK='...' \
  QA_TRAFFIC_ENTROPY='...' \
  QA_TRAFFIC_FUNDER_KEY='...' \
  QA_TRAFFIC_DISCORD_WEBHOOK_URL='...'
```
Treat the node/sequencer URLs as secret too if they embed a token:
`QA_TRAFFIC_NODE_RPC_URL`, `QA_TRAFFIC_NODE_INSPECT_URL`, `QA_TRAFFIC_SEQUENCER_URL`.

> `QA_TRAFFIC_DISCORD_ENV_FILE` (the shared-file webhook source used on the bare-metal host) does
> **not** work on fly — there is no shared env file there. Set `QA_TRAFFIC_DISCORD_WEBHOOK_URL`
> directly as a secret.

Non-secret config (app address, token, portals, traffic shape, cadence) can go in `fly.toml [env]`
or `fly secrets set` — your choice. See [`config.env.example`](../config.env.example) for the full
list and defaults.

## 4. Generate entropy once (if you do not already have it)
Entropy must be stable across restarts (it derives the wallets). Generate locally and store as a
secret — do not let a fresh container invent a new one:
```bash
python3 -m qatraffic new-entropy     # prints 64 hex chars; set as QA_TRAFFIC_ENTROPY secret
```

## 5. Deploy
```bash
fly deploy
fly scale count 1       # enforce exactly one Machine
```

## 6. Verify
```bash
fly status                       # one Machine, health check "passing"
fly logs                         # expect: service.start -> accounts.ok -> run.attached
fly ssh console -C 'wget -qO- http://localhost:8080/health'   # {"ok": true, ...}
```
Healthy signs: `run.attached` appears, actions/batches start flowing, and the `/health` endpoint
returns 200. If attach cannot reach the RPC/node yet, the bot logs `attach.failed` and **retries**
(it no longer wedges — see the attach-robustness fix in `service.py`).

## 7. The health check is the watchdog
On bare metal, systemd (`Type=notify`, `WatchdogSec`) restarts a hung process. fly.io has no
systemd, and this bot's loop swallows errors and never exits — so process-liveness alone cannot
catch a freeze. The HTTP liveness endpoint fills that gap: it returns **503** once the loop has
made no progress for `QA_TRAFFIC_HEALTH_MAX_STALE_SECONDS` (default 600s), and the `fly.toml`
`[checks]` entry makes fly restart the Machine. Keep the window comfortably longer than the
slowest normal iteration (a large catch-up after downtime can take a few minutes).

## 8. Operating
```bash
fly logs                                   # live logs
fly ssh console                            # shell; run `python3 -m qatraffic status` etc.
fly deploy                                 # ship a new version (state on the volume persists)
```
Updates redeploy in place; the volume (DB, history, voucher tracking) persists across deploys.

---

## Alternative: ephemeral (no volume)
If you deliberately run without a volume (each restart is a cold, fresh instance), set
`QA_TRAFFIC_SWEEP_ON_START=true` so leftover wallet funds are swept back to the funder on every
(re)start and re-funded lazily. This loses all history and re-plans from scratch each restart, and
churns gas — **not recommended**. Prefer the volume setup above. Do not mix the two
(`SWEEP_ON_START=true` with a persistent volume would sweep funds mid-run after a crash/restart).
