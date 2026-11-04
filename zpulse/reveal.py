"""zpulse.reveal: anyone with the public manifest + ciphertext + release viewing key can do this.

  python3 -m zpulse.reveal --vault data/vaults/NAME/vault.json [--watch] [--poll 5] [--timeout 0]

Collects signed share memos (ZP1 SH) sent to the release address, and once
`threshold` distinct watchers have released, reconstructs the key and decrypts.
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

from . import crypto, memo, shamir
from .state import Paths, common_args, emit, fail, load_json, now, open_node, save_json, say, zwallet


def open_release_view(args, paths: Paths, m: dict):
    """mock: the release node itself. testnet: a VIEW-ONLY wallet built from release_ufvk."""
    if args.backend == "mock":
        return open_node("mock", f"release-{m['vault_id']}", paths)
    if not m.get("release_ufvk"):
        fail("manifest has no release_ufvk (was it created on the mock backend?)")
    node = f"reveal-{m['vault_id']}"
    w = zwallet(node)
    if not w.exists():
        say(f"importing release viewing key into view-only wallet {node} ...")
        w.init_fvk(m["release_ufvk"], m.get("release_birthday"))
    return open_node("testnet", node, paths)


def collect(chain, m: dict) -> dict:
    """{watcher_id: (share, txid)} for every validly signed share for this vault."""
    found: dict[str, tuple[bytes, str]] = {}
    for tx in chain.received(0):
        sm = memo.parse(tx.get("memo"))
        if not sm or sm["kind"] != "SH" or sm["vault_id"] != m["vault_id"]:
            continue
        w = m["watchers"].get(sm["watcher_id"])
        if w is None or not memo.verify_share(sm, w["vk"]):
            say(f"ignoring share with bad signature from {sm['watcher_id']!r} in {tx['txid'][:16]}")
            continue
        found.setdefault(sm["watcher_id"], (sm["share"], tx["txid"]))
    return found


def try_reveal(args, paths: Paths, m: dict, blob: bytes, chain) -> dict | None:
    chain.sync()
    found = collect(chain, m)
    t = m["threshold"]
    say(f"{len(found)} of {t} needed shares seen ({', '.join(sorted(found)) or 'none'})")
    if len(found) < t:  # combine() with < t shares silently returns garbage
        return None
    key = shamir.combine([s for s, _ in found.values()])
    try:
        plaintext = crypto.decrypt_vault(blob, key, m["vault_id"])
    except ValueError:
        fail("reconstructed key does not decrypt the vault (inconsistent shares?)")
    vid = m["vault_id"]
    paths.revealed.mkdir(parents=True, exist_ok=True)
    (paths.revealed / f"{vid}.txt").write_bytes(plaintext)
    meta = {"vault_id": vid, "name": m.get("name"), "shares_seen": sorted(found),
            "txids": {w: txid for w, (_, txid) in found.items()}, "revealed_ts": now()}
    save_json(paths.revealed / f"{vid}.json", meta)
    return {"ok": True, **meta, "plaintext": plaintext.decode("utf-8", "replace"),
            "plaintext_path": str(paths.revealed / f"{vid}.txt")}


def main(argv=None) -> None:
    p = common_args(argparse.ArgumentParser(prog="zpulse.reveal", description=__doc__,
                                            formatter_class=argparse.RawDescriptionHelpFormatter))
    p.add_argument("--vault", required=True, help="public manifest vault.json (vault.bin alongside)")
    p.add_argument("--watch", action="store_true", help="keep polling until revealed")
    p.add_argument("--poll", type=float, default=5)
    p.add_argument("--timeout", type=float, default=0, help="with --watch: give up after N s (0 = never)")
    args = p.parse_args(argv)
    paths = Paths(args.data)
    vpath = Path(args.vault)
    m = load_json(vpath)
    if m is None:
        fail(f"no manifest at {vpath}")
    if not m.get("watchers"):
        fail("vault is not sealed (no watchers in manifest)")
    blob = (vpath.parent / "vault.bin").read_bytes()
    if crypto.sha256_hex(blob) != m["ciphertext_sha256"]:
        fail("vault.bin does not match ciphertext_sha256 in the manifest")
    chain = open_release_view(args, paths, m)
    start = time.time()
    while True:
        res = try_reveal(args, paths, m, blob, chain)
        if res:
            say("=" * 60 + f"\nREVEALED vault {m.get('name')} ({m['vault_id']}):\n"
                + res["plaintext"].rstrip("\n") + "\n" + "=" * 60)
            emit(res)
            return
        if not args.watch:
            emit({"ok": False, "error": "not enough shares yet", "vault_id": m["vault_id"],
                  "threshold": m["threshold"]})
            sys.exit(2)
        if args.timeout and time.time() - start > args.timeout:
            emit({"ok": False, "error": "timeout", "vault_id": m["vault_id"]})
            sys.exit(3)
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
