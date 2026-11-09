"""EIP-712 user operations for the Cartesi sequencer (README "Fixed protocol identity"):

    domain  { name: "CartesiAppSequencer", version: "1", chainId, verifyingContract: app }
    type    UserOp(uint32 nonce,uint16 max_fee,bytes data)

`sign` produces what POST /tx expects; `recover` is the scheduler's sender recovery for ops read
back out of L1 batches (65-byte r||s||v, v in {0,1,27,28} or EIP-155 style, as alloy's
Signature::from_raw accepts; anything else makes the op unrecoverable and the scheduler skips it).
"""
from __future__ import annotations

from eth_keys import keys
from eth_utils import keccak, to_checksum_address

DOMAIN_NAME = "CartesiAppSequencer"
DOMAIN_VERSION = "1"
_DOMAIN_TYPEHASH = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
_USEROP_TYPEHASH = keccak(text="UserOp(uint32 nonce,uint16 max_fee,bytes data)")
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
U32_MAX = 2**32 - 1

TYPES = {"UserOp": [{"name": "nonce", "type": "uint32"}, {"name": "max_fee", "type": "uint16"},
                    {"name": "data", "type": "bytes"}]}


def domain(chain_id: int, app: str) -> dict:
    return {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": int(chain_id),
            "verifyingContract": to_checksum_address(app)}


def domain_separator(chain_id: int, app: str) -> bytes:
    return keccak(_DOMAIN_TYPEHASH + keccak(text=DOMAIN_NAME) + keccak(text=DOMAIN_VERSION)
                  + int(chain_id).to_bytes(32, "big") + bytes(12) + bytes.fromhex(app[2:]))


def signing_hash(separator: bytes, nonce: int, max_fee: int, data: bytes) -> bytes:
    struct = keccak(_USEROP_TYPEHASH + nonce.to_bytes(32, "big") + max_fee.to_bytes(32, "big") + keccak(data))
    return keccak(b"\x19\x01" + separator + struct)


def sign(key: bytes, separator: bytes, nonce: int, max_fee: int, data: bytes) -> bytes:
    """65-byte r||s||v with v in {27, 28}."""
    sig = keys.PrivateKey(key).sign_msg_hash(signing_hash(separator, nonce, max_fee, data))
    return sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([27 + sig.v])


def _parity(v: int) -> int | None:
    if v in (0, 1):
        return v
    if v in (27, 28):
        return v - 27
    if v >= 35:
        return (v - 35) % 2
    return None


def recover(separator: bytes, nonce: int, max_fee: int, data: bytes, signature: bytes) -> str | None:
    """The signer's checksum address, or None when the signature cannot be recovered."""
    if len(signature) != 65:
        return None
    parity = _parity(signature[64])
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    if parity is None or not (0 < r < SECP256K1_N and 0 < s < SECP256K1_N):
        return None
    try:
        pub = keys.Signature(vrs=(parity, r, s)).recover_public_key_from_msg_hash(
            signing_hash(separator, nonce, max_fee, data))
    except Exception:  # noqa: BLE001 - eth_keys raises BadSignature and ValidationError
        return None
    return pub.to_checksum_address()
