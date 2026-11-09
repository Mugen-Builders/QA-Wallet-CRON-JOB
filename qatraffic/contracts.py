"""Contract ABIs (rollups-contracts v3.0.0-alpha.10), qa-wallet-tester-app input encoders, output
decoders.

The app acts on libcma's 10 withdraw/transfer selectors (libcma v0.1.0-alpha.11) and echoes every
other payload back as a notice. The bot sends exactly two of them, as an InputBox direct input or
as the `data` of a sequencer user op; anything else is refused before signing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, keccak, to_checksum_address

ABI_DIR = Path(__file__).resolve().parent / "abis"


@lru_cache(maxsize=None)
def abi(name: str) -> list:
    return json.loads((ABI_DIR / f"{name}.json").read_text())


def selector(signature: str) -> bytes:
    return function_signature_to_4byte_selector(signature)


def topic(signature: str) -> bytes:
    return keccak(text=signature)


# --- advance inputs the bot sends ------------------------------------------------------------

SIG_TRANSFER_ERC20 = "TransferErc20(address,bytes32,uint256,bytes)"  # libcma 0x03d61dcd
SIG_WITHDRAW_ERC20 = "WithdrawErc20(address,uint256,bytes)"  # libcma 0x4f94d342
ALLOWED_SELECTORS = frozenset(selector(s) for s in (SIG_TRANSFER_ERC20, SIG_WITHDRAW_ERC20))


def account_id(address: str) -> bytes:
    """libcma account id of a wallet: 12 zero bytes + the address (ledger_impl.cpp treats an id
    with 12 leading zero bytes as that wallet address)."""
    return b"\x00" * 12 + bytes.fromhex(address[2:])


def encode_transfer(token: str, receiver: str, amount: int, memo: bytes = b"") -> bytes:
    return selector(SIG_TRANSFER_ERC20) + abi_encode(
        ["address", "bytes32", "uint256", "bytes"], [token, account_id(receiver), amount, memo]
    )


def encode_withdraw(token: str, amount: int, memo: bytes = b"") -> bytes:
    return selector(SIG_WITHDRAW_ERC20) + abi_encode(["address", "uint256", "bytes"], [token, amount, memo])


def assert_allowed_input(payload: bytes) -> None:
    """Last line of defence before a payload reaches InputBox.addInput or a user op: anything that
    is not one of the two modelled ledger inputs would be echoed by the app, not executed."""
    if bytes(payload[:4]) not in ALLOWED_SELECTORS:
        raise RuntimeError("refusing to send an input the bot does not model")


# --- outputs ---------------------------------------------------------------------------------

VOUCHER_SEL = selector("Voucher(address,uint256,bytes)")  # 0x237a816f
DELEGATE_CALL_VOUCHER_SEL = selector("DelegateCallVoucher(address,bytes)")  # 0x10321e8b
NOTICE_SEL = selector("Notice(bytes)")  # 0xc258d6e5
ERC20_TRANSFER_SEL = selector("transfer(address,uint256)")  # 0xa9059cbb
SAFE_TRANSFER_SEL = selector("safeTransfer(address,address,uint256)")


@dataclass(frozen=True)
class DecodedOutput:
    kind: str  # "voucher" | "delegate_call_voucher" | "notice" | "unknown"
    destination: str | None = None
    value: int = 0
    payload: bytes = b""
    # token payout carried by the output, when it is one the bot understands
    token: str | None = None
    receiver: str | None = None
    amount: int | None = None


def decode_output(raw: bytes) -> DecodedOutput:
    sel, body = bytes(raw[:4]), bytes(raw[4:])
    try:
        if sel == VOUCHER_SEL:
            dest, value, payload = abi_decode(["address", "uint256", "bytes"], body)
            dest = to_checksum_address(dest)
            out = DecodedOutput("voucher", dest, value, payload)
            if payload[:4] == ERC20_TRANSFER_SEL and value == 0:
                to, amount = abi_decode(["address", "uint256"], payload[4:])
                out = DecodedOutput("voucher", dest, value, payload, dest, to_checksum_address(to), amount)
            return out
        if sel == DELEGATE_CALL_VOUCHER_SEL:
            dest, payload = abi_decode(["address", "bytes"], body)
            dest = to_checksum_address(dest)
            out = DecodedOutput("delegate_call_voucher", dest, 0, payload)
            if payload[:4] == SAFE_TRANSFER_SEL:
                token, to, amount = abi_decode(["address", "address", "uint256"], payload[4:])
                out = DecodedOutput("delegate_call_voucher", dest, 0, payload, to_checksum_address(token),
                                    to_checksum_address(to), amount)
            return out
        if sel == NOTICE_SEL:
            return DecodedOutput("notice", payload=abi_decode(["bytes"], body)[0])
    except Exception:  # noqa: BLE001 - malformed output: report as unknown
        pass
    return DecodedOutput("unknown", payload=bytes(raw))


# --- logs ------------------------------------------------------------------------------------

TOPIC_INPUT_ADDED = topic("InputAdded(address,uint256,bytes)")
TOPIC_ERC20_TRANSFER = topic("Transfer(address,address,uint256)")
TOPIC_OUTPUT_EXECUTED = topic("OutputExecuted(uint64,bytes)")


def _t(x) -> bytes:
    return bytes(x) if not isinstance(x, str) else bytes.fromhex(x[2:] if x.startswith("0x") else x)


def input_indexes_from_receipt(receipt, input_box: str, app: str) -> list[int]:
    """InputAdded(appContract indexed, index indexed, input) logs for `app` in a receipt."""
    out = []
    app_topic = b"\x00" * 12 + bytes.fromhex(app[2:])
    for log in receipt["logs"]:
        if log["address"].lower() != input_box.lower() or not log["topics"]:
            continue
        topics = [_t(t) for t in log["topics"]]
        if topics[0] == TOPIC_INPUT_ADDED and len(topics) >= 3 and topics[1] == app_topic:
            out.append(int.from_bytes(topics[2], "big"))
    return out


def erc20_transfers_from_receipt(receipt, token: str) -> list[tuple[str, str, int]]:
    out = []
    for log in receipt["logs"]:
        if log["address"].lower() != token.lower() or len(log["topics"]) != 3:
            continue
        topics = [_t(t) for t in log["topics"]]
        if topics[0] != TOPIC_ERC20_TRANSFER:
            continue
        frm = to_checksum_address(topics[1][12:])
        to = to_checksum_address(topics[2][12:])
        out.append((frm, to, int.from_bytes(_t(log["data"]), "big")))
    return out


def executed_outputs_from_receipt(receipt, app: str) -> list[int]:
    out = []
    for log in receipt["logs"]:
        if log["address"].lower() != app.lower() or len(log["topics"]) < 2:
            continue
        topics = [_t(t) for t in log["topics"]]
        if topics[0] == TOPIC_OUTPUT_EXECUTED:
            out.append(int.from_bytes(topics[1], "big"))
    return out


# --- revert decoding -------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _error_table() -> dict[bytes, tuple[str, list[str]]]:
    table = {}
    for name in ("Application", "InputBox", "Erc20Portal", "Consensus", "TestUsdc"):
        for e in abi(name):
            if e["type"] != "error":
                continue
            types = [i["type"] for i in e.get("inputs", [])]
            sig = f"{e['name']}({','.join(types)})"
            table[selector(sig)] = (e["name"], types)
    table[selector("Error(string)")] = ("Error", ["string"])
    return table


def decode_revert(data) -> str | None:
    if data is None:
        return None
    if isinstance(data, str):
        if not data.startswith("0x"):
            return data
        try:
            data = bytes.fromhex(data[2:])
        except ValueError:
            return data
    if len(data) < 4:
        return None
    ent = _error_table().get(bytes(data[:4]))
    if not ent:
        return "0x" + bytes(data[:4]).hex()
    name, types = ent
    try:
        args = abi_decode(types, bytes(data[4:])) if types else ()
        if name == "Error":
            return f"Error({args[0]!r})"
        short = [("0x" + a.hex()[:16] + "…") if isinstance(a, bytes) and len(a) > 8 else a for a in args]
        return f"{name}({', '.join(str(a) for a in short)})"
    except Exception:  # noqa: BLE001
        return name
