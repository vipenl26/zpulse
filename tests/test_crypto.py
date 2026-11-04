"""Tests for zpulse.crypto, zpulse.shamir and zpulse.memo (no network)."""
from __future__ import annotations

import itertools
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zpulse import crypto, memo, shamir  # noqa: E402

VID = "0123456789abcdef"


def test_vault_roundtrip_and_tamper():
    key = crypto.random_key()
    blob = crypto.encrypt_vault(b"the dossier", key, VID)
    assert crypto.decrypt_vault(blob, key, VID) == b"the dossier"
    for bad in (
        lambda: crypto.decrypt_vault(blob[:-1] + bytes([blob[-1] ^ 1]), key, VID),
        lambda: crypto.decrypt_vault(blob, crypto.random_key(), VID),
        lambda: crypto.decrypt_vault(blob, key, "fedcba9876543210"),
    ):
        try:
            bad()
            raise AssertionError("tamper not detected")
        except ValueError:
            pass
    assert len(crypto.sha256_hex(b"x")) == 64


def test_identity_sign_verify_save_load():
    a, b = crypto.Identity.generate(), crypto.Identity.generate()
    sig = a.sign(b"msg")
    assert len(sig) == 64
    assert crypto.verify(a.verify_key_b64, b"msg", sig)
    assert not crypto.verify(b.verify_key_b64, b"msg", sig)
    assert not crypto.verify(a.verify_key_b64, b"msg2", sig)
    assert not crypto.verify("not base64!!", b"msg", sig)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sub", "id.key")
        a.save(p)
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
        assert crypto.Identity.load(p).verify_key_b64 == a.verify_key_b64


def test_seal_unseal():
    w, other = crypto.Identity.generate(), crypto.Identity.generate()
    sealed = crypto.seal_to(w.verify_key_b64, b"share bytes")
    assert crypto.unseal(w, sealed) == b"share bytes"
    try:
        crypto.unseal(other, sealed)
        raise AssertionError("wrong identity unsealed")
    except Exception as e:  # nacl CryptoError
        assert not isinstance(e, AssertionError)


def test_gf256_field():
    # 0x53 * 0xCA = 0x01 in the AES field (FIPS-197 example)
    assert shamir._gmul(0x53, 0xCA) == 0x01
    assert shamir._mul(0x57, 0x83) == 0xC1
    for a in range(1, 256):
        assert shamir._gmul(a, shamir._gdiv(1, a)) == 1


def test_shamir_any_t_of_n():
    secret = crypto.random_key()
    for t, n in [(1, 1), (2, 3), (3, 5), (5, 5)]:
        shares = shamir.split(secret, t, n)
        assert len(shares) == n and all(len(s) == 33 for s in shares)
        for combo in itertools.combinations(shares, t):
            assert shamir.combine(list(reversed(combo))) == secret
        if t > 1:
            for combo in itertools.combinations(shares, t - 1):
                assert shamir.combine(list(combo)) != secret
        assert shamir.combine(shares) == secret  # more than t is fine


def test_shamir_randomized_and_duplicates():
    secret = b"\x00" * 32
    a, b = shamir.split(secret, 3, 5), shamir.split(secret, 3, 5)
    assert a != b  # fresh random coefficients each time
    assert shamir.combine([a[0], a[0], a[1], a[2]]) == secret
    try:
        shamir.split(secret, 4, 3)
        raise AssertionError
    except ValueError:
        pass


def test_memo_heartbeat_alive_duress_forged():
    p, d, x = (crypto.Identity.generate() for _ in range(3))
    text = memo.encode_heartbeat(p, VID, 7, 1_800_000_000)
    assert text.startswith("ZP1 HB " + VID + " 7 1800000000 ")
    assert len(text.encode()) <= 512
    m = memo.parse(text)
    assert m["kind"] == "HB" and m["seq"] == 7 and m["ts"] == 1_800_000_000
    assert memo.classify_heartbeat(m, p.verify_key_b64, d.verify_key_b64) == "alive"
    md = memo.parse(memo.encode_heartbeat(d, VID, 8, 1_800_000_100))
    assert memo.classify_heartbeat(md, p.verify_key_b64, d.verify_key_b64) == "duress"
    assert memo.classify_heartbeat(md, p.verify_key_b64, None) is None
    mx = memo.parse(memo.encode_heartbeat(x, VID, 9, 1))
    assert memo.classify_heartbeat(mx, p.verify_key_b64, d.verify_key_b64) is None
    # replaying a sig onto a different seq fails
    m["seq"] = 99
    assert memo.classify_heartbeat(m, p.verify_key_b64, d.verify_key_b64) is None


def test_memo_cancel_and_share():
    p, w = crypto.Identity.generate(), crypto.Identity.generate()
    c = memo.parse(memo.encode_cancel(p, VID, 123))
    assert c["kind"] == "CX" and memo.verify_cancel(c, p.verify_key_b64)
    assert not memo.verify_cancel(c, w.verify_key_b64)
    share = shamir.split(crypto.random_key(), 3, 5)[2]
    text = memo.encode_share(w, VID, "ngo-1", share)
    assert len(text.encode()) <= 512
    s = memo.parse(text)
    assert s["kind"] == "SH" and s["watcher_id"] == "ngo-1" and s["share"] == share
    assert memo.verify_share(s, w.verify_key_b64)
    assert not memo.verify_share(s, p.verify_key_b64)
    s["watcher_id"] = "evil"
    assert not memo.verify_share(s, w.verify_key_b64)


def test_memo_parse_rejects_junk():
    for junk in [None, "", "hello", "ZP1", "ZP1 HB", "ZP1 HB short 1 2 AAAA",
                 "ZP2 HB 0123456789abcdef 1 2 " + "A" * 88,
                 "ZP1 HB 0123456789abcdef x 2 " + "A" * 86 + "==",
                 "ZP1 SH 0123456789abcdef BAD! AAAA " + "A" * 86 + "=="]:
        assert memo.parse(junk) is None, junk
    try:
        memo.encode_share(crypto.Identity.generate(), VID, "Bad Id", b"x")
        raise AssertionError
    except ValueError:
        pass


def _run_all() -> int:
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
