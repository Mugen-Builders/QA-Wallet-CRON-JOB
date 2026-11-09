"""The sequencer's log-space fee encoding (base 129/128), bit-exact with
sequencer-core/src/fee.rs (v0.1.0-alpha.10) and qa-wallet-tester-app src/lib/fee.cpp.

A user op signs `max_fee` and pays the frame's `fee_price`, both exponents; the app charges
fee_to_linear(fee_price) units of the accounts-drive token. The conversion contract fixes the table,
the intermediate flooring and the ascending bit order, so this is a literal port, not a formula.
"""
from __future__ import annotations

FRAC_BITS = 64
_S = 1 << FRAC_BITS
_U256 = 1 << 256


def _table() -> list[int]:
    t = [129 << 57]  # (129/128) in fixed point
    for _ in range(1, 15):
        t.append(t[-1] * t[-1] >> FRAC_BITS)
    return t


TABLE = _table()


def _max_exponent() -> int:
    exp, val = 0, _S
    for i in reversed(range(15)):
        v = val * TABLE[i] >> FRAC_BITS
        if v < _U256:
            exp |= 1 << i
            val = v
    return exp


MAX_EXPONENT = _max_exponent()


def to_linear(exponent: int) -> int:
    """Token units charged for a frame fee exponent. Raises ValueError above MAX_EXPONENT."""
    if not 0 <= exponent <= MAX_EXPONENT:
        raise ValueError(f"fee exponent {exponent} outside [0, {MAX_EXPONENT}]")
    r = _S
    for i, entry in enumerate(TABLE):
        if exponent & (1 << i):
            r = r * entry >> FRAC_BITS
    return r >> FRAC_BITS
