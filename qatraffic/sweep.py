"""Sweep leftover funds from the traffic wallets back to the funder.

For a short test run the traffic wallets accumulate a little Sepolia ETH (from gas top-ups) and
some of the ERC-20 (minted, then cycled through deposits/withdrawals). When the test ends this
returns both to the funder address:

    python -m qatraffic sweep             # dry run: show every balance and what would move
    python -m qatraffic sweep --execute   # actually send: ERC-20 first, then ETH

ERC-20 is swept before ETH so the ETH sweep can account for the gas the token transfer spent.
Funds still *inside* the app ledger (deposited, not yet withdrawn) are reported but NOT swept:
recovering those means issuing withdrawals and waiting for epochs to settle. The funder's own
balance is never touched.
"""
from __future__ import annotations

import time

from eth_account import Account
from web3 import Web3

from . import accounts as A
from . import contracts as C
from .chain import GWEI, L1
from .events import EventLog
from .node import Node, NodeError, NodeUnavailable

try:
    from eth_abi import encode as abi_encode
except ImportError:  # pragma: no cover
    abi_encode = None

ETH_SEND_GAS = 21_000
ERC20_TRANSFER_GAS = 70_000  # fallback when estimation fails
RECEIPT_TIMEOUT_S = 240
DUST_WEI = 10_000  # below this an ETH sweep costs more than it recovers; skip


class _NullDB:
    """L1/Node only call record_rpc; the sweep keeps no database of its own."""

    def record_rpc(self, *a, **k):
        pass


def _transfer_data(to: str, amount: int) -> bytes:
    return C.selector("transfer(address,uint256)") + abi_encode(["address", "uint256"],
                                                                 [Web3.to_checksum_address(to), amount])


def _fees(l1: L1, cfg):
    base = l1.base_fee()
    tip = int(cfg.tip_min_gwei * GWEI)
    cap = int(cfg.max_fee_gwei * GWEI)
    max_fee = min(base * 2 + tip, cap)
    if base + tip > cap:
        raise RuntimeError(f"base fee {base / GWEI:.3f} gwei is above the cap {cfg.max_fee_gwei} gwei; "
                           "raise QA_TRAFFIC_MAX_FEE_GWEI and retry")
    return max_fee, tip


def _send(l1: L1, cfg, key: bytes, nonce: int, to: str, value: int, data: bytes, gas: int,
          max_fee: int, tip: int) -> str:
    tx = {"to": Web3.to_checksum_address(to), "value": value, "data": data, "chainId": cfg.chain_id,
          "nonce": nonce, "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip, "type": 2}
    signed = Account.sign_transaction(tx, key)
    return l1.send_raw(signed.raw_transaction)


def _wait(l1: L1, hashes: list[str], log) -> dict[str, bool]:
    """Wait for every receipt; return {hash: success}."""
    out: dict[str, bool] = {}
    deadline = time.time() + RECEIPT_TIMEOUT_S
    pending = list(hashes)
    while pending and time.time() < deadline:
        still = []
        for h in pending:
            try:
                r = l1.receipt(h)
            except Exception:
                r = None
            if r is None:
                still.append(h)
            else:
                out[h] = (r.get("status") == 1)
        pending = still
        if pending:
            time.sleep(3)
    for h in pending:
        out[h] = False  # timed out, treat as not confirmed
    return out


def _usdc(cfg, v: int) -> str:
    return f"{v / 10 ** 6:.6f}"


def run(cfg, execute: bool, to: str | None = None) -> int:
    log = EventLog(cfg.log_dir)
    db = _NullDB()
    l1 = L1(cfg, db, log)
    wallets = A.derive(cfg.entropy, cfg.account_offset, cfg.account_count)
    funder = Account.from_key(cfg.funder_key).address
    dest = Web3.to_checksum_address(to) if to else funder

    # optional: app-ledger balances, for information only
    node = None
    try:
        node = Node(cfg, db, log)
    except Exception:
        node = None

    print(f"sweep target: {dest}")
    print(f"{'slot':>4}  {'address':42}  {'ETH':>20}  {'wallet TUSD':>14}  {'app-ledger TUSD':>16}")
    rows = []
    tot_eth = tot_tok = tot_ledger = 0
    for w in wallets:
        eth = l1.eth_balance(w.address)
        tok = l1.token_balance(w.address)
        ledger = 0
        if node is not None:
            try:
                ledger = node.ledger_balance(w.address, cfg.token)[0]
            except (NodeError, NodeUnavailable, Exception):
                ledger = -1  # unknown
        rows.append((w, eth, tok))
        tot_eth += eth
        tot_tok += tok
        if ledger >= 0:
            tot_ledger += ledger
        led = "?" if ledger < 0 else _usdc(cfg, ledger)
        print(f"{w.slot:>4}  {w.address:42}  {eth / 10 ** 18:>20.18f}  {_usdc(cfg, tok):>14}  {led:>16}")
    print(f"{'':>4}  {'TOTAL':42}  {tot_eth / 10 ** 18:>20.18f}  {_usdc(cfg, tot_tok):>14}  {_usdc(cfg, tot_ledger):>16}")

    if not execute:
        print("\ndry run — nothing sent. Re-run with --execute to sweep wallet ETH and TUSD to the target.")
        if tot_ledger > 0:
            print(f"note: {_usdc(cfg, tot_ledger)} TUSD is still inside the app ledger; sweeping it means "
                  "withdrawing first (vouchers + epoch settlement), which this command does not do.")
        return 0

    max_fee, tip = _fees(l1, cfg)
    print(f"\nexecuting (maxFeePerGas {max_fee / GWEI:.3f} gwei, tip {tip / GWEI:.4f} gwei) -> {dest}")

    # pass 1: ERC-20 out of every wallet that holds some
    tok_hashes = []
    for w, eth, tok in rows:
        if tok <= 0:
            continue
        data = _transfer_data(dest, tok)
        try:
            gas = int(l1.estimate_gas({"from": w.address, "to": Web3.to_checksum_address(cfg.token), "data": data}) * 1.25)
        except Exception:
            gas = ERC20_TRANSFER_GAS
        if eth < gas * max_fee:
            print(f"  slot {w.slot}: SKIP TUSD — wallet has no ETH for gas ({eth} wei)")
            continue
        try:
            h = _send(l1, cfg, w.key, l1.nonce(w.address, "pending"), cfg.token, 0, data, gas, max_fee, tip)
            tok_hashes.append((w, h))
            print(f"  slot {w.slot}: TUSD {_usdc(cfg, tok)} -> {h}")
        except Exception as e:
            print(f"  slot {w.slot}: TUSD send failed: {type(e).__name__}: {str(e)[:120]}")
    res = _wait(l1, [h for _, h in tok_hashes], log)
    for w, h in tok_hashes:
        if not res.get(h):
            print(f"  slot {w.slot}: WARNING TUSD tx not confirmed: {h}")

    # pass 2: ETH out of every wallet, leaving only the gas for its own sweep tx
    eth_hashes = []
    reserve = ETH_SEND_GAS * max_fee
    for w, _eth, _tok in rows:
        bal = l1.eth_balance(w.address)
        value = bal - reserve
        if value <= DUST_WEI:
            continue
        try:
            h = _send(l1, cfg, w.key, l1.nonce(w.address, "pending"), dest, value, b"", ETH_SEND_GAS, max_fee, tip)
            eth_hashes.append((w, h, value))
            print(f"  slot {w.slot}: ETH {value / 10 ** 18:.18f} -> {h}")
        except Exception as e:
            print(f"  slot {w.slot}: ETH send failed: {type(e).__name__}: {str(e)[:120]}")
    res = _wait(l1, [h for _, h, _ in eth_hashes], log)
    swept_eth = 0
    for w, h, value in eth_hashes:
        if res.get(h):
            swept_eth += value
        else:
            print(f"  slot {w.slot}: WARNING ETH tx not confirmed: {h}")

    print(f"\nswept {len(tok_hashes)} TUSD transfer(s) and {len(eth_hashes)} ETH transfer(s) to {dest}")
    print(f"ETH confirmed back to target: {swept_eth / 10 ** 18:.18f}")
    print(f"funder {dest} now holds {l1.eth_balance(dest) / 10 ** 18:.18f} ETH, "
          f"{_usdc(cfg, l1.token_balance(dest))} TUSD")
    if tot_ledger > 0:
        print(f"note: {_usdc(cfg, tot_ledger)} TUSD remains inside the app ledger (not swept).")
    return 0
