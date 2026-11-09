"""SSZ codec for the sequencer's batch envelope (sequencer-core/src/batch.rs, v0.1.0-alpha.10):

    Batch      { nonce: u64, frames: Vec<Frame> }
    Frame      { user_ops: Vec<WireUserOp>, safe_block: u64, fee_price: u16 }
    WireUserOp { nonce: u32, max_fee: u16, data: Vec<u8>, signature: Vec<u8> }

Decoding follows ethereum_ssz 0.10's rules exactly (sanitize_offset, the first-offset rule of
containers and lists), because the scheduler treats a payload it cannot decode as a rejected batch
that does not consume the nonce: a laxer or stricter decoder would fork the model from the machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field

OFFSET = 4


class SszError(ValueError):
    pass


@dataclass
class WireUserOp:
    nonce: int
    max_fee: int
    data: bytes
    signature: bytes


@dataclass
class Frame:
    safe_block: int
    fee_price: int
    user_ops: list[WireUserOp] = field(default_factory=list)


@dataclass
class Batch:
    nonce: int
    frames: list[Frame] = field(default_factory=list)


def _read_offset(b: bytes, at: int) -> int:
    if at + OFFSET > len(b):
        raise SszError("offset out of bounds")
    return int.from_bytes(b[at:at + OFFSET], "little")


def _sanitize(offset: int, previous: int | None, n: int, fixed: int | None) -> int:
    if fixed is not None and offset < fixed:
        raise SszError("offset into fixed portion")
    if previous is None and fixed is not None and offset != fixed:
        raise SszError("offset skips variable bytes")
    if offset > n:
        raise SszError("offset out of bounds")
    if previous is not None and previous > offset:
        raise SszError("offsets are decreasing")
    return offset


def _container(b: bytes, layout: list) -> list:
    """layout: ints (fixed field widths) or None (variable field). Returns raw slices."""
    items: list = []
    offsets: list[tuple[int, int]] = []  # (position in items, offset)
    idx = 0
    for width in layout:
        if width is None:
            off = _sanitize(_read_offset(b, idx), offsets[-1][1] if offsets else None, len(b), None)
            offsets.append((len(items), off))
            items.append(b"")
            idx += OFFSET
        else:
            if idx + width > len(b):
                raise SszError("invalid byte length")
            items.append(b[idx:idx + width])
            idx += width
    if not offsets:
        if len(b) != idx:
            raise SszError("invalid byte length")
        return items
    if offsets[0][1] != idx:
        raise SszError("offset into fixed portion" if offsets[0][1] < idx else "offset skips variable bytes")
    for k, (pos, off) in enumerate(offsets):
        end = offsets[k + 1][1] if k + 1 < len(offsets) else len(b)
        items[pos] = b[off:end]
    return items


def _variable_list(b: bytes, item):
    if not b:
        return []
    first = _read_offset(b, 0)
    _sanitize(first, None, len(b), first)
    if first % OFFSET or first < OFFSET:
        raise SszError("invalid list fixed bytes length")
    n = first // OFFSET
    out, off = [], first
    for i in range(1, n + 1):
        if i == n:
            sl = b[off:]
        else:
            start = off
            off = _sanitize(_read_offset(b, i * OFFSET), off, len(b), first)
            sl = b[start:off]
        out.append(item(sl))
    return out


def _u(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _user_op(b: bytes) -> WireUserOp:
    nonce, max_fee, data, sig = _container(b, [4, 2, None, None])
    return WireUserOp(_u(nonce), _u(max_fee), bytes(data), bytes(sig))


def _frame(b: bytes) -> Frame:
    ops, safe_block, fee_price = _container(b, [None, 8, 2])
    return Frame(_u(safe_block), _u(fee_price), _variable_list(ops, _user_op))


def decode_batch(b: bytes) -> Batch:
    nonce, frames = _container(bytes(b), [8, None])
    return Batch(_u(nonce), _variable_list(frames, _frame))


# ---- encoding (tests and rehearsal tooling) ----
def _enc_container(fields: list) -> bytes:
    """fields: ("f", bytes) fixed or ("v", bytes) variable."""
    fixed_len = sum(len(v) if k == "f" else OFFSET for k, v in fields)
    head, tail = b"", b""
    for k, v in fields:
        if k == "f":
            head += v
        else:
            head += (fixed_len + len(tail)).to_bytes(4, "little")
            tail += v
    return head + tail


def _enc_list(items: list[bytes]) -> bytes:
    head, tail = b"", b""
    for it in items:
        head += (OFFSET * len(items) + len(tail)).to_bytes(4, "little")
        tail += it
    return head + tail


def encode_batch(batch: Batch) -> bytes:
    frames = []
    for f in batch.frames:
        ops = [_enc_container([("f", o.nonce.to_bytes(4, "little")), ("f", o.max_fee.to_bytes(2, "little")),
                               ("v", o.data), ("v", o.signature)]) for o in f.user_ops]
        frames.append(_enc_container([("v", _enc_list(ops)), ("f", f.safe_block.to_bytes(8, "little")),
                                      ("f", f.fee_price.to_bytes(2, "little"))]))
    return _enc_container([("f", batch.nonce.to_bytes(8, "little")), ("v", _enc_list(frames))])
