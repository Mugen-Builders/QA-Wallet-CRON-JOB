"""Configuration, read from the environment (the systemd EnvironmentFile).

Secrets (RPC URLs, keys, entropy, webhook) are kept out of repr() so that a config object can be
logged or printed without leaking them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from eth_utils import to_checksum_address

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX32_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")

# Sepolia defaults (rollups-contracts v3.0.0-alpha.10, deterministic CREATE2, so the portals are the
# devnet addresses too; TestUsdc verified on-chain on 2026-11-03).
SEPOLIA_DEFAULTS = {
    "QA_TRAFFIC_CHAIN_ID": "11155111",
    "QA_TRAFFIC_INPUT_BOX": "0xEbE9f4Dfc04ae10bBeE663859c3dc5A23f94eA3C",
    "QA_TRAFFIC_ERC20_PORTAL": "0x3332DE61a8BB9aC84893b2f552Fe81C9a6dC5419",
    "QA_TRAFFIC_TOKEN": "0x0696259375E54AB38749e23ac5dc56fD10d51290",
    # the other portals: inputs from them touch assets the model does not track
    "QA_TRAFFIC_OTHER_PORTALS": ("0x035b11Be55656c6cfC822D1CaE568C1Af2e497b0,0x397c352d18DFf47CC8a6143403142cf7afd5Ff7E,"
                                 "0x585F56351A66f131E176a345662215C772f80451,0xee33550a22e3Cf6Cc265524dC9bcfD99D2307EBe"),
}


class ConfigError(Exception):
    pass


def _env(env, name, default=None):
    v = env.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().strip('"').strip("'")


def _addr(env, name, required=True, default=None):
    v = _env(env, name, default)
    if v is None:
        if required:
            raise ConfigError(f"{name} is required")
        return None
    if not ADDRESS_RE.match(v):
        raise ConfigError(f"{name} is not a 20-byte hex address")
    if v != v.lower() and v != v.upper()[:2].lower() + v[2:].upper() and to_checksum_address(v) != v:
        raise ConfigError(f"{name} has mixed case but a wrong EIP-55 checksum (typo?)")
    return to_checksum_address(v)


def _int(env, name, default):
    v = _env(env, name)
    if v is None:
        return default
    try:
        return int(v, 0)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer") from e


def _float(env, name, default):
    v = _env(env, name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number") from e


def _bool(env, name, default):
    v = _env(env, name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def read_env_file_key(path: Path, key: str) -> str | None:
    """Read ONE key from a KEY=VALUE env file without loading anything else from it."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            v = v.strip().strip('"').strip("'")
            return v or None
    return None


@dataclass
class Config:
    # paths
    home: Path
    # chain and contracts
    chain_id: int
    l1_rpc_urls: list[str] = field(repr=False)
    input_box: str
    erc20_portal: str
    token: str
    other_portals: list[str]
    app_address: str | None
    l1_read_tag: str
    l1_read_extra_confirmations: int
    log_chunk_blocks: int
    # node
    node_rpc_url: str | None = field(repr=False)
    node_inspect_url: str | None = field(repr=False)
    node_app: str | None
    # sequencer
    sequencer_url: str | None = field(repr=False)
    sequencer_egress_url: str | None = field(repr=False)
    batch_submitter: str | None
    seq_landing_deadline_minutes: float
    seq_batch_silence_minutes: float
    seq_direct_wait_alert_blocks: int
    max_op_fee_usdc: float
    # accounts
    entropy: bytes = field(repr=False)
    account_offset: int
    account_count: int
    funder_key: str | None = field(repr=False)
    # traffic shape
    daily_min: int
    daily_max: int
    busy_day_prob: float
    busy_day_max: int
    invalid_rate: float
    l1_route_share: float
    other_executor_share: float
    initial_active_share: float
    onboarding_days: int
    time_scale: float
    sweep_on_start: bool
    # vouchers
    exec_deadline_hours: float
    voucher_overdue_grace_minutes: float
    finality_lag_blocks: int
    block_time_seconds: float
    # fees and gas funding (ETH / gwei)
    max_fee_gwei: float
    tip_min_gwei: float
    tip_max_gwei: float
    gas_min_eth: float
    gas_topup_min_eth: float
    gas_topup_max_eth: float
    daily_gas_budget_eth: float
    funder_min_eth: float
    funder_drop_alert_eth: float
    runway_alert_days: float
    # loop cadence (seconds)
    loop_seconds: float
    reconcile_seconds: float
    checks_seconds: float
    balance_sample_per_check: int
    node_input_lag_alert_minutes: float
    rpc_failures_before_alert: int
    # notifications
    discord_webhook_url: str | None = field(repr=False)
    discord_prefix: str
    report_time: str
    report_tz: ZoneInfo
    alert_repeat_hours: float
    healthcheck_url: str | None = field(repr=False)

    @property
    def db_path(self) -> Path:
        return self.home / "state" / "qatraffic.sqlite3"

    @property
    def heartbeat_path(self) -> Path:
        return self.home / "state" / "heartbeat.json"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def app_configured(self) -> bool:
        return self.app_address is not None

    @property
    def node_app_id(self) -> str | None:
        return self.node_app or self.app_address


def load(env=None, require_secrets: bool = True) -> Config:
    """Build a Config from `env` (default os.environ).

    `require_secrets=False` lets read-only commands (plan preview, status) run without keys.
    """
    env = dict(os.environ if env is None else env)
    for k, v in SEPOLIA_DEFAULTS.items():
        env.setdefault(k, v)

    home = Path(_env(env, "QA_TRAFFIC_HOME", str(PROJECT_ROOT / "var"))).expanduser()

    rpc = [u for u in (_env(env, "QA_TRAFFIC_L1_RPC_URL"), _env(env, "QA_TRAFFIC_L1_RPC_URL_FALLBACK")) if u]
    if require_secrets and not rpc:
        raise ConfigError("QA_TRAFFIC_L1_RPC_URL is required")

    ent_hex = _env(env, "QA_TRAFFIC_ENTROPY")
    if ent_hex is None:
        if require_secrets:
            raise ConfigError("QA_TRAFFIC_ENTROPY is required (32 random bytes, 64 hex characters)")
        entropy = b""
    else:
        if not HEX32_RE.match(ent_hex):
            raise ConfigError("QA_TRAFFIC_ENTROPY must be exactly 32 bytes (64 hex characters)")
        entropy = bytes.fromhex(ent_hex[2:] if ent_hex.startswith("0x") else ent_hex)
        if len(set(entropy)) < 8:
            raise ConfigError("QA_TRAFFIC_ENTROPY looks non-random; generate it with secrets.token_hex(32)")

    funder = _env(env, "QA_TRAFFIC_FUNDER_KEY")
    if funder is not None and not HEX32_RE.match(funder):
        raise ConfigError("QA_TRAFFIC_FUNDER_KEY must be a 32-byte hex private key")

    app = _addr(env, "QA_TRAFFIC_APP_ADDRESS", required=False)
    node_rpc = _env(env, "QA_TRAFFIC_NODE_RPC_URL")
    node_inspect = _env(env, "QA_TRAFFIC_NODE_INSPECT_URL")
    if app and require_secrets and not (node_rpc and node_inspect):
        raise ConfigError("QA_TRAFFIC_NODE_RPC_URL and QA_TRAFFIC_NODE_INSPECT_URL are required once the app is set")
    seq_url = _env(env, "QA_TRAFFIC_SEQUENCER_URL")
    if app and require_secrets and not seq_url:
        raise ConfigError("QA_TRAFFIC_SEQUENCER_URL is required once the app is set")
    others = []
    for a in (_env(env, "QA_TRAFFIC_OTHER_PORTALS", "") or "").split(","):
        if a.strip():
            others.append(_addr({"X": a.strip()}, "X"))
    read_tag = _env(env, "QA_TRAFFIC_L1_READ_TAG", "safe")
    if read_tag not in ("safe", "finalized", "latest"):
        raise ConfigError("QA_TRAFFIC_L1_READ_TAG must be safe, finalized or latest")

    webhook = _env(env, "QA_TRAFFIC_DISCORD_WEBHOOK_URL")
    if webhook is None:
        src = _env(env, "QA_TRAFFIC_DISCORD_ENV_FILE")
        if src:
            webhook = read_env_file_key(Path(src).expanduser(), "DISCORD_WEBHOOK_URL")
    if not _bool(env, "QA_TRAFFIC_DISCORD_ENABLED", True):
        webhook = None

    report_time = _env(env, "QA_TRAFFIC_REPORT_TIME", "23:55")
    if not re.match(r"^\d{2}:\d{2}$", report_time):
        raise ConfigError("QA_TRAFFIC_REPORT_TIME must be HH:MM")
    try:
        tz = ZoneInfo(_env(env, "QA_TRAFFIC_REPORT_TZ", "Africa/Lagos"))
    except Exception as e:  # noqa: BLE001 - zoneinfo raises several types
        raise ConfigError("QA_TRAFFIC_REPORT_TZ is not a known IANA time zone") from e

    cfg = Config(
        home=home,
        chain_id=_int(env, "QA_TRAFFIC_CHAIN_ID", 11155111),
        l1_rpc_urls=rpc,
        input_box=_addr(env, "QA_TRAFFIC_INPUT_BOX"),
        erc20_portal=_addr(env, "QA_TRAFFIC_ERC20_PORTAL"),
        token=_addr(env, "QA_TRAFFIC_TOKEN"),
        other_portals=others,
        app_address=app,
        l1_read_tag=read_tag,
        l1_read_extra_confirmations=_int(env, "QA_TRAFFIC_L1_READ_EXTRA_CONFIRMATIONS", 0),
        log_chunk_blocks=_int(env, "QA_TRAFFIC_LOG_CHUNK_BLOCKS", 2000),
        node_rpc_url=node_rpc,
        node_inspect_url=node_inspect,
        node_app=_env(env, "QA_TRAFFIC_NODE_APP"),
        sequencer_url=seq_url,
        sequencer_egress_url=_env(env, "QA_TRAFFIC_SEQUENCER_EGRESS_URL"),
        batch_submitter=_addr(env, "QA_TRAFFIC_BATCH_SUBMITTER", required=False),
        seq_landing_deadline_minutes=_float(env, "QA_TRAFFIC_SEQ_LANDING_DEADLINE_MINUTES", 240.0),
        seq_batch_silence_minutes=_float(env, "QA_TRAFFIC_SEQ_BATCH_SILENCE_MINUTES", 180.0),
        seq_direct_wait_alert_blocks=_int(env, "QA_TRAFFIC_SEQ_DIRECT_WAIT_ALERT_BLOCKS", 600),
        max_op_fee_usdc=_float(env, "QA_TRAFFIC_MAX_OP_FEE_USDC", 0.5),
        entropy=entropy,
        account_offset=_int(env, "QA_TRAFFIC_ACCOUNT_OFFSET", 0),
        account_count=_int(env, "QA_TRAFFIC_ACCOUNT_COUNT", 60),
        funder_key=funder,
        daily_min=_int(env, "QA_TRAFFIC_DAILY_MIN", 80),
        daily_max=_int(env, "QA_TRAFFIC_DAILY_MAX", 120),
        busy_day_prob=_float(env, "QA_TRAFFIC_BUSY_DAY_PROB", 0.07),
        busy_day_max=_int(env, "QA_TRAFFIC_BUSY_DAY_MAX", 150),
        invalid_rate=_float(env, "QA_TRAFFIC_INVALID_RATE", 0.01),
        l1_route_share=_float(env, "QA_TRAFFIC_L1_ROUTE_SHARE", 0.15),
        other_executor_share=_float(env, "QA_TRAFFIC_OTHER_EXECUTOR_SHARE", 0.1),
        initial_active_share=_float(env, "QA_TRAFFIC_INITIAL_ACTIVE_SHARE", 0.6),
        onboarding_days=_int(env, "QA_TRAFFIC_ONBOARDING_DAYS", 21),
        time_scale=_float(env, "QA_TRAFFIC_TIME_SCALE", 1.0),
        sweep_on_start=_bool(env, "QA_TRAFFIC_SWEEP_ON_START", False),
        exec_deadline_hours=_float(env, "QA_TRAFFIC_EXEC_DEADLINE_HOURS", 24.0),
        voucher_overdue_grace_minutes=_float(env, "QA_TRAFFIC_VOUCHER_OVERDUE_GRACE_MINUTES", 60.0),
        finality_lag_blocks=_int(env, "QA_TRAFFIC_FINALITY_LAG_BLOCKS", 96),
        block_time_seconds=_float(env, "QA_TRAFFIC_BLOCK_TIME_SECONDS", 12.0),
        max_fee_gwei=_float(env, "QA_TRAFFIC_MAX_FEE_GWEI", 30.0),
        tip_min_gwei=_float(env, "QA_TRAFFIC_TIP_MIN_GWEI", 0.001),
        tip_max_gwei=_float(env, "QA_TRAFFIC_TIP_MAX_GWEI", 0.05),
        gas_min_eth=_float(env, "QA_TRAFFIC_GAS_MIN_ETH", 0.0005),
        gas_topup_min_eth=_float(env, "QA_TRAFFIC_GAS_TOPUP_MIN_ETH", 0.001),
        gas_topup_max_eth=_float(env, "QA_TRAFFIC_GAS_TOPUP_MAX_ETH", 0.004),
        daily_gas_budget_eth=_float(env, "QA_TRAFFIC_DAILY_GAS_BUDGET_ETH", 0.05),
        funder_min_eth=_float(env, "QA_TRAFFIC_FUNDER_MIN_ETH", 0.05),
        funder_drop_alert_eth=_float(env, "QA_TRAFFIC_FUNDER_DROP_ALERT_ETH", 0.05),
        runway_alert_days=_float(env, "QA_TRAFFIC_RUNWAY_ALERT_DAYS", 5.0),
        loop_seconds=_float(env, "QA_TRAFFIC_LOOP_SECONDS", 10.0),
        reconcile_seconds=_float(env, "QA_TRAFFIC_RECONCILE_SECONDS", 75.0),
        checks_seconds=_float(env, "QA_TRAFFIC_CHECKS_SECONDS", 600.0),
        balance_sample_per_check=_int(env, "QA_TRAFFIC_BALANCE_SAMPLE", 12),
        node_input_lag_alert_minutes=_float(env, "QA_TRAFFIC_NODE_INPUT_LAG_ALERT_MINUTES", 45.0),
        rpc_failures_before_alert=_int(env, "QA_TRAFFIC_RPC_FAILURES_BEFORE_ALERT", 5),
        discord_webhook_url=webhook,
        discord_prefix=_env(env, "QA_TRAFFIC_DISCORD_PREFIX", "[cartesi-traffic]"),
        report_time=report_time,
        report_tz=tz,
        alert_repeat_hours=_float(env, "QA_TRAFFIC_ALERT_REPEAT_HOURS", 6.0),
        healthcheck_url=_env(env, "QA_TRAFFIC_HEALTHCHECK_URL"),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if not 1 <= cfg.account_count <= 500:
        raise ConfigError("QA_TRAFFIC_ACCOUNT_COUNT must be between 1 and 500")
    if cfg.account_offset < 0:
        raise ConfigError("QA_TRAFFIC_ACCOUNT_OFFSET must be >= 0")
    if not 1 <= cfg.daily_min <= cfg.daily_max <= cfg.busy_day_max:
        raise ConfigError("need 1 <= DAILY_MIN <= DAILY_MAX <= BUSY_DAY_MAX")
    for name in ("invalid_rate", "l1_route_share", "other_executor_share", "busy_day_prob", "initial_active_share"):
        v = getattr(cfg, name)
        if not 0.0 <= v <= 1.0:
            raise ConfigError(f"{name} must be within [0, 1]")
    if cfg.invalid_rate > 0.05:
        raise ConfigError("QA_TRAFFIC_INVALID_RATE above 0.05 is not 'minimal'; refusing")
    if cfg.time_scale <= 0:
        raise ConfigError("QA_TRAFFIC_TIME_SCALE must be > 0")
    if cfg.gas_topup_min_eth > cfg.gas_topup_max_eth:
        raise ConfigError("GAS_TOPUP_MIN_ETH must be <= GAS_TOPUP_MAX_ETH")
    if cfg.tip_min_gwei > cfg.tip_max_gwei:
        raise ConfigError("TIP_MIN_GWEI must be <= TIP_MAX_GWEI")
    if cfg.log_chunk_blocks < 1:
        raise ConfigError("QA_TRAFFIC_LOG_CHUNK_BLOCKS must be >= 1")
    if cfg.max_op_fee_usdc <= 0:
        raise ConfigError("QA_TRAFFIC_MAX_OP_FEE_USDC must be > 0")
