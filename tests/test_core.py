"""Unit tests: derivation, encodings, the schedule, alerts, the Discord outbox (the fold: test_fold.py)."""
import json
import os
import shutil
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from qatraffic import accounts as A
from qatraffic import config as cfgmod
from qatraffic import contracts as C
from qatraffic.alerts import Alerts
from qatraffic.db import DB
from qatraffic.events import EventLog, scrub
from qatraffic.notify import Outbox
from qatraffic.personas import build_population
from qatraffic.schedule import MIN_GAP_S, day_target, exec_delay, plan_day
from qatraffic.personas import rng_for

ENTROPY = bytes.fromhex("5b0d8e0c7a3f0c1e9a7b2c4d6e8f10213243546576879809a1b2c3d4e5f60718")


def make_cfg(tmp_path, **over):
    env = {"QA_TRAFFIC_HOME": str(tmp_path), "QA_TRAFFIC_ENTROPY": ENTROPY.hex()}
    env.update(over)
    return cfgmod.load(env, require_secrets=False)


# ---------------------------------------------------------------- accounts
def test_bip39_vector():
    A.bip39_vector_check()


def test_derivation_is_deterministic_and_matches_cast():
    w1 = A.derive(ENTROPY, 0, 5)
    w2 = A.derive(ENTROPY, 0, 5)
    assert [w.address for w in w1] == [w.address for w in w2]
    assert len({w.address for w in w1}) == 5
    assert A.derive(ENTROPY, 3, 1)[0].address == w1[3].address
    if shutil.which("cast") or os.path.exists(os.path.expanduser("~/.foundry/bin/cast")):
        assert A.cast_crosscheck(ENTROPY, w1) == 5
    assert "key" not in repr(w1[0])


def test_config_rejects_bad_entropy(tmp_path):
    with pytest.raises(cfgmod.ConfigError):
        make_cfg(tmp_path, QA_TRAFFIC_ENTROPY="00" * 32)
    with pytest.raises(cfgmod.ConfigError):
        make_cfg(tmp_path, QA_TRAFFIC_ENTROPY="abcd")
    with pytest.raises(cfgmod.ConfigError):
        make_cfg(tmp_path, QA_TRAFFIC_INVALID_RATE="0.2")


def test_webhook_read_from_other_env_file_only_that_key(tmp_path):
    f = tmp_path / "other.env"
    f.write_text("SMTP_PASSWORD=hunter2\nDISCORD_WEBHOOK_URL=https://discord.example/api/webhooks/1/zq9WebhookSecret\nX=1\n")
    cfg = make_cfg(tmp_path, QA_TRAFFIC_DISCORD_ENV_FILE=str(f))
    assert cfg.discord_webhook_url.endswith("/1/zq9WebhookSecret")
    assert "hunter2" not in repr(cfg) and "zq9WebhookSecret" not in repr(cfg)
    assert ENTROPY.hex() not in repr(cfg)


# ---------------------------------------------------------------- encodings
def test_selectors_match_libcma():
    assert C.selector(C.SIG_TRANSFER_ERC20).hex() == "03d61dcd"
    assert C.selector(C.SIG_WITHDRAW_ERC20).hex() == "4f94d342"
    assert C.VOUCHER_SEL.hex() == "237a816f"
    assert C.selector("depositErc20Tokens(address,address,uint256,bytes)").hex() == "766afa3a"


def test_only_modelled_inputs_are_sent():
    # anything else would be echoed by the app as a notice instead of executed
    for payload in (C.selector("withdrawErc20Safe(address,address,uint256,bytes)") + b"\x00" * 128,
                    C.selector("WithdrawEther(uint256,bytes)") + b"\x00" * 96, b"\x42", b""):
        with pytest.raises(RuntimeError):
            C.assert_allowed_input(payload)
    C.assert_allowed_input(C.encode_withdraw("0x" + "11" * 20, 5))
    C.assert_allowed_input(C.encode_transfer("0x" + "11" * 20, "0x" + "22" * 20, 5))


def test_account_id_and_transfer_encoding():
    addr = "0x" + "ab" * 20
    assert C.account_id(addr) == b"\x00" * 12 + bytes.fromhex("ab" * 20)
    p = C.encode_transfer("0x" + "11" * 20, addr, 123)
    assert p[:4].hex() == "03d61dcd" and p[4 + 32 + 12:4 + 64] == bytes.fromhex("ab" * 20)


def test_decode_vouchers():
    from eth_abi import encode
    token, to = "0x" + "22" * 20, "0x" + "33" * 20
    inner = C.ERC20_TRANSFER_SEL + encode(["address", "uint256"], [to, 7])
    raw = C.VOUCHER_SEL + encode(["address", "uint256", "bytes"], [token, 0, inner])
    d = C.decode_output(raw)
    assert d.kind == "voucher" and d.token.lower() == token and d.receiver.lower() == to and d.amount == 7
    safe = "0x" + "44" * 20
    inner = C.SAFE_TRANSFER_SEL + encode(["address", "address", "uint256"], [token, to, 9])
    raw = C.DELEGATE_CALL_VOUCHER_SEL + encode(["address", "bytes"], [safe, inner])
    d = C.decode_output(raw)
    assert d.kind == "delegate_call_voucher" and d.destination.lower() == safe and d.amount == 9
    assert C.decode_output(b"\x01\x02").kind == "unknown"


def test_revert_decoding():
    from eth_abi import encode
    data = C.selector("InvalidOutputsMerkleRoot(bytes32)") + encode(["bytes32"], [b"\x01" * 32])
    assert C.decode_revert(data).startswith("InvalidOutputsMerkleRoot")


# ---------------------------------------------------------------- schedule
def _plan_month(tmp_path, days=30, count=60, **over):
    cfg = make_cfg(tmp_path, QA_TRAFFIC_ACCOUNT_COUNT=str(count), **over)
    wallets = A.derive(ENTROPY, 0, count)
    seed = A.sub_seed(ENTROPY, "plan")
    profiles = build_population(A.sub_seed(ENTROPY, "population"), count, initial_active_share=0.6, onboarding_days=21,
                                tip_min_gwei=0.001, tip_max_gwei=0.05, l1_share=0.15)
    start = date(2026, 11, 4)
    out = []
    for k in range(days):
        d = start + timedelta(days=k)
        target, _, _ = day_target(cfg, seed, d, k)
        out.append((d, target, plan_day(cfg, seed, d, start, profiles, target)))
    return cfg, profiles, out


def test_daily_floor_every_hour_and_determinism(tmp_path):
    cfg, profiles, month = _plan_month(tmp_path)
    hours = [0] * 24
    for d, target, ss in month:
        planned = sum(s.planned_inputs for s in ss)
        assert planned >= cfg.daily_min, (d, planned)
        assert planned <= cfg.busy_day_max + 6
        for s in ss:
            hours[datetime.fromtimestamp(s.planned_at, timezone.utc).hour] += s.planned_inputs
    assert min(hours) > 0.02 * sum(hours) / 24 * 24 / 24, hours  # every UTC hour has traffic
    assert min(hours) >= 0.35 * (sum(hours) / 24)
    _, _, again = _plan_month(tmp_path)
    assert [(s.planned_at, s.slot, s.intent) for s in month[3][2]] == [(s.planned_at, s.slot, s.intent) for s in again[3][2]]
    # days differ from each other
    assert [s.slot for s in month[1][2]][:20] != [s.slot for s in month[2][2]][:20]


def test_no_user_double_session_and_late_joiners(tmp_path):
    cfg, profiles, month = _plan_month(tmp_path, days=21)
    for _, _, ss in month:
        by = {}
        for s in ss:
            for t in by.get(s.slot, []):
                assert abs(t - s.planned_at) >= MIN_GAP_S - 1
            by.setdefault(s.slot, []).append(s.planned_at)
    first_day = {s.slot for s in month[0][2]}
    later = {p.slot for p in profiles if p.onboard_day > 0}
    assert not (first_day & later), "a late joiner acted on day 0"
    assert later & {s.slot for _, _, ss in month[10:] for s in ss}, "late joiners never show up"


def test_exec_delay_within_deadline(tmp_path):
    profiles = build_population(A.sub_seed(ENTROPY, "population"), 60, initial_active_share=0.6, onboarding_days=21,
                                tip_min_gwei=0.001, tip_max_gwei=0.05, l1_share=0.15)
    base = datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp()
    for i in range(2000):
        rng = rng_for(b"x", i)
        p = profiles[i % 60]
        d = exec_delay(rng, p, base + i * 997, 24)
        assert 0 < d <= 24 * 3600 - 600


# ---------------------------------------------------------------- alerts + outbox
class _Hook(BaseHTTPRequestHandler):
    calls = []
    fail_first = 0

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        _Hook.calls.append((self.headers.get("Content-Type"), body))
        if _Hook.fail_first > 0:
            _Hook.fail_first -= 1
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"retry_after": 0.01}')
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def hook():
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    _Hook.calls = []
    yield f"http://127.0.0.1:{srv.server_port}/api/webhooks/1/secret"
    srv.shutdown()


def test_alert_lifecycle_and_discord_delivery(tmp_path, hook, monkeypatch):
    db = DB(tmp_path / "s.sqlite3")
    log = EventLog(tmp_path / "logs", stdout=open(os.devnull, "w"))
    ob = Outbox(db, log, hook, "[t]")
    al = Alerts(db, log, ob, repeat_hours=6)
    al.raise_("rpc.l1", "warning", "L1 down", "x")
    al.raise_("rpc.l1", "warning", "L1 down", "x again")  # suppressed: same alert, inside the repeat window
    al.flush()
    ob.flush()
    assert len(_Hook.calls) == 1
    body = json.loads(_Hook.calls[0][1])
    assert body["content"].startswith("[t]") and "WARNING" in body["embeds"][0]["title"]
    al.raise_("rpc.l1", "critical", "L1 down", "worse")  # escalation goes out at once
    al.resolve("rpc.l1", "back")
    al.flush()
    ob.flush()
    titles = [e["title"] for c in _Hook.calls[1:] for e in json.loads(c[1])["embeds"]]
    assert any("ESCALATED" in t for t in titles) and any("RESOLVED" in t for t in titles)
    # 429 is retried after retry_after, nothing is lost
    _Hook.fail_first = 1
    ob.lifecycle("t", "b")
    ob.flush()
    assert ob.pending_count() == 1
    import time
    time.sleep(0.6)
    ob.flush()
    assert ob.pending_count() == 0
    # attachments go out as multipart with payload_json
    ob.report("r", "d", [("a", "b", True)], ("report.json", "{}"))
    ob.flush()
    assert _Hook.calls[-1][0].startswith("multipart/form-data")
    # the webhook token never reaches the event log
    logs = "".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    assert "secret" not in logs


def test_scrub():
    assert "<url>" in scrub("failed https://eth-sepolia.example/v3/abcdef")
    assert "<hex32>" in scrub("key 0x" + "ab" * 32)
