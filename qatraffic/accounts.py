"""Deterministic accounts: 32 bytes of entropy -> BIP-39 mnemonic -> BIP-44 accounts.

The env file holds the entropy (never a mnemonic, never a human-chosen phrase). The mnemonic is
derived in memory on every start and never written to disk or logs. Two self-checks run before
any account is used:

1. the published BIP-39 vector (all-zero entropy, passphrase "TREZOR") must give the published seed;
2. every derived address must match an independent implementation (Foundry's `cast`, Rust),
   which reads the mnemonic from a 0600 file on tmpfs so it never appears on a command line.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from eth_account import Account
from eth_account.hdaccount import seed_from_mnemonic
from eth_account.hdaccount.mnemonic import Mnemonic
from eth_account.types import Language

Account.enable_unaudited_hdwallet_features()

BIP39_VECTOR_ENTROPY = bytes(32)
BIP39_VECTOR_PASSPHRASE = "TREZOR"
BIP39_VECTOR_SEED = (
    "bda85446c68413707090a52022edd26a1c9462295029f2e60cd7c4f2bbd3097170af7a4d73245cafa9c3cca8d561a7c3de6f5d4a10be8ed2a5e608d68f92fcc8"
)
DERIVATION = "m/44'/60'/0'/0/{}"


class SelfCheckError(Exception):
    pass


@dataclass(frozen=True)
class Wallet:
    index: int  # derivation index
    slot: int  # 0-based position in the traffic population
    address: str
    key: bytes  # private key bytes; never logged

    def __repr__(self) -> str:  # keep the key out of any repr
        return f"Wallet(slot={self.slot}, index={self.index}, address={self.address})"


def entropy_to_mnemonic(entropy: bytes) -> str:
    if len(entropy) != 32:
        raise ValueError("entropy must be 32 bytes")
    return Mnemonic(Language.ENGLISH).to_mnemonic(entropy)


def bip39_vector_check() -> None:
    m = entropy_to_mnemonic(BIP39_VECTOR_ENTROPY)
    words = m.split()
    if len(words) != 24 or words[0] != "abandon" or words[-1] != "art":
        raise SelfCheckError("BIP-39 vector: unexpected mnemonic for all-zero entropy")
    if seed_from_mnemonic(m, BIP39_VECTOR_PASSPHRASE).hex() != BIP39_VECTOR_SEED:
        raise SelfCheckError("BIP-39 vector: seed mismatch")


def derive(entropy: bytes, offset: int, count: int) -> list[Wallet]:
    mnemonic = entropy_to_mnemonic(entropy)
    out = []
    for slot in range(count):
        idx = offset + slot
        acct = Account.from_mnemonic(mnemonic, account_path=DERIVATION.format(idx))
        out.append(Wallet(index=idx, slot=slot, address=acct.address, key=bytes(acct.key)))
    return out


def cast_crosscheck(entropy: bytes, wallets: list[Wallet], cast_bin: str | None = None) -> int:
    """Re-derive every address with Foundry's cast. Returns how many were checked.

    Raises SelfCheckError on any mismatch. If cast is not installed, raises too: the bot does not
    run on a single unaudited implementation.
    """
    cast_bin = cast_bin or shutil.which("cast") or os.path.expanduser("~/.foundry/bin/cast")
    if not os.path.exists(cast_bin):
        raise SelfCheckError("Foundry `cast` not found; it is required for the derivation cross-check")
    base = os.environ.get("XDG_RUNTIME_DIR") or None  # tmpfs, private to the user
    with tempfile.TemporaryDirectory(dir=base, prefix="qatraffic-") as d:
        os.chmod(d, 0o700)
        path = os.path.join(d, "m")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, entropy_to_mnemonic(entropy).encode())
        finally:
            os.close(fd)
        for w in wallets:
            p = subprocess.run(
                [cast_bin, "wallet", "address", "--mnemonic", path, "--mnemonic-index", str(w.index)],
                capture_output=True, text=True, timeout=30,
            )
            got = p.stdout.strip()
            if p.returncode != 0 or got.lower() != w.address.lower():
                raise SelfCheckError(f"derivation mismatch with cast at index {w.index}")
        os.remove(path)
    return len(wallets)


def population_fingerprint(wallets: list[Wallet]) -> str:
    """Stable id of the account set (addresses only), stored in the DB to detect a seed change."""
    h = hashlib.sha256()
    for w in wallets:
        h.update(bytes.fromhex(w.address[2:]))
    return h.hexdigest()


def sub_seed(entropy: bytes, label: str) -> bytes:
    """Domain-separated seed for planners and RNGs; reveals nothing about the entropy."""
    return hashlib.sha256(b"qa-traffic/" + label.encode() + b"/" + entropy).digest()


def load_wallets(entropy: bytes, offset: int, count: int, crosscheck: bool = True) -> list[Wallet]:
    bip39_vector_check()
    wallets = derive(entropy, offset, count)
    if len({w.address for w in wallets}) != len(wallets):
        raise SelfCheckError("duplicate addresses derived")
    if crosscheck:
        cast_crosscheck(entropy, wallets)
    return wallets
