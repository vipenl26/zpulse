"""zpulse.memo: the three memo formats carried in shielded Zcash outputs.

All memos are single-line UTF-8 text, space separated, <= 512 bytes:

  ZP1 HB <vault_id> <seq> <ts> <sig_b64>              heartbeat (alive or duress)
      sig over b"zpulse|HB|<vault_id>|<seq>|<ts>"
  ZP1 CX <vault_id> <ts> <sig_b64>                    cancel
      sig over b"zpulse|CX|<vault_id>|<ts>"
  ZP1 SH <vault_id> <watcher_id> <share_b64> <sig_b64> share release
      sig over b"zpulse|SH|<vault_id>|<watcher_id>|" + share

A heartbeat signed by the principal key means ALIVE; the identical-looking
heartbeat signed by the duress key means DURESS. Base64 is standard alphabet.
"""
from __future__ import annotations

import base64
import binascii
import re

from .crypto import Identity, verify

PREFIX = "ZP1"
MAX_MEMO_BYTES = 512
VAULT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
WATCHER_ID_RE = re.compile(r"^[a-z0-9_-]{1,16}$")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s, validate=True)


def _check(text: str) -> str:
    assert len(text.encode()) <= MAX_MEMO_BYTES, f"memo too long: {len(text.encode())} bytes"
    return text


def _hb_msg(vault_id: str, seq: int, ts: int) -> bytes:
    return f"zpulse|HB|{vault_id}|{seq}|{ts}".encode()


def _cx_msg(vault_id: str, ts: int) -> bytes:
    return f"zpulse|CX|{vault_id}|{ts}".encode()


def _sh_msg(vault_id: str, watcher_id: str, share: bytes) -> bytes:
    return f"zpulse|SH|{vault_id}|{watcher_id}|".encode() + share


def _ids_ok(vault_id: str, watcher_id: str | None = None) -> None:
    if not VAULT_ID_RE.match(vault_id):
        raise ValueError(f"bad vault_id {vault_id!r}")
    if watcher_id is not None and not WATCHER_ID_RE.match(watcher_id):
        raise ValueError(f"bad watcher_id {watcher_id!r}")


def encode_heartbeat(identity: Identity, vault_id: str, seq: int, ts: int) -> str:
    _ids_ok(vault_id)
    sig = identity.sign(_hb_msg(vault_id, int(seq), int(ts)))
    return _check(f"{PREFIX} HB {vault_id} {int(seq)} {int(ts)} {_b64(sig)}")


def encode_cancel(identity: Identity, vault_id: str, ts: int) -> str:
    _ids_ok(vault_id)
    sig = identity.sign(_cx_msg(vault_id, int(ts)))
    return _check(f"{PREFIX} CX {vault_id} {int(ts)} {_b64(sig)}")


def encode_share(identity: Identity, vault_id: str, watcher_id: str, share: bytes) -> str:
    _ids_ok(vault_id, watcher_id)
    sig = identity.sign(_sh_msg(vault_id, watcher_id, share))
    return _check(f"{PREFIX} SH {vault_id} {watcher_id} {_b64(share)} {_b64(sig)}")


def parse(text: str | None) -> dict | None:
    """Parse a memo into a dict, or None if it is not a well-formed zpulse memo.

    HB -> {kind, vault_id, seq, ts, sig}; CX -> {kind, vault_id, ts, sig};
    SH -> {kind, vault_id, watcher_id, share, sig}. Signatures are NOT checked here.
    """
    if not text:
        return None
    parts = text.strip().split(" ")
    if len(parts) < 2 or parts[0] != PREFIX:
        return None
    kind = parts[1]
    try:
        if kind == "HB" and len(parts) == 6:
            m = {"kind": kind, "vault_id": parts[2], "seq": int(parts[3]),
                 "ts": int(parts[4]), "sig": _unb64(parts[5])}
        elif kind == "CX" and len(parts) == 5:
            m = {"kind": kind, "vault_id": parts[2], "ts": int(parts[3]), "sig": _unb64(parts[4])}
        elif kind == "SH" and len(parts) == 6:
            if not WATCHER_ID_RE.match(parts[3]):
                return None
            m = {"kind": kind, "vault_id": parts[2], "watcher_id": parts[3],
                 "share": _unb64(parts[4]), "sig": _unb64(parts[5])}
        else:
            return None
    except (ValueError, binascii.Error):
        return None
    if not VAULT_ID_RE.match(m["vault_id"]) or len(m["sig"]) != 64:
        return None
    return m


def classify_heartbeat(m: dict, principal_vk_b64: str, duress_vk_b64: str | None) -> str | None:
    """"alive" if signed by the principal, "duress" if by the duress key, else None."""
    if not m or m.get("kind") != "HB":
        return None
    msg = _hb_msg(m["vault_id"], m["seq"], m["ts"])
    if verify(principal_vk_b64, msg, m["sig"]):
        return "alive"
    if duress_vk_b64 and verify(duress_vk_b64, msg, m["sig"]):
        return "duress"
    return None


def verify_cancel(m: dict, principal_vk_b64: str) -> bool:
    return bool(m) and m.get("kind") == "CX" and verify(
        principal_vk_b64, _cx_msg(m["vault_id"], m["ts"]), m["sig"])


def verify_share(m: dict, watcher_vk_b64: str) -> bool:
    return bool(m) and m.get("kind") == "SH" and verify(
        watcher_vk_b64, _sh_msg(m["vault_id"], m["watcher_id"], m["share"]), m["sig"])
