"""zpulse.principal: the journalist's side. init -> (watchers enroll) -> seal -> pulse ... [cancel].

  python3 -m zpulse.principal init   --name NAME --file PATH --threshold 3 --interval 120 --grace 30 [--heartbeat-zat 20000]
  python3 -m zpulse.principal seal   --name NAME
  python3 -m zpulse.principal pulse  --name NAME [--duress]
  python3 -m zpulse.principal cancel --name NAME
  python3 -m zpulse.principal status --name NAME
"""
from __future__ import annotations

import argparse
import base64
import re
import secrets

from . import crypto, memo, shamir
from .state import Paths, common_args, emit, fail, load_json, now, open_node, save_json, say, zwallet

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def principal_node(backend: str, name: str) -> str:
    return f"principal-{name}" if backend == "mock" else "principal"


def _vault(args) -> tuple[Paths, "Path", dict]:  # noqa: F821
    paths = Paths(args.data)
    vdir = paths.vault_dir(args.name)
    manifest = load_json(vdir / "vault.json")
    if manifest is None:
        fail(f"no vault named {args.name!r} in {paths.data}")
    return paths, vdir, manifest


# ------------------------------------------------------------------ commands

def cmd_init(args) -> None:
    if not NAME_RE.match(args.name):
        fail("vault name must match [A-Za-z0-9_-]{1,32}")
    if args.threshold < 1:
        fail("threshold must be >= 1")
    paths = Paths(args.data)
    vdir = paths.vault_dir(args.name)
    if (vdir / "vault.json").exists():
        fail(f"vault {args.name!r} already exists at {vdir}; refusing to overwrite")
    plaintext = open(args.file, "rb").read()
    vault_id = secrets.token_hex(8)
    key = crypto.random_key()
    blob = crypto.encrypt_vault(plaintext, key, vault_id)

    # release node: anyone holding its viewing key can watch shares arrive
    release_node = f"release-{vault_id}"
    release_ufvk = release_birthday = None
    if args.backend == "testnet":
        w = zwallet(release_node)
        say(f"creating testnet release wallet {release_node} ...")
        w.init()
        release_ufvk = w.ufvk()
        try:
            release_birthday = open_node(args.backend, principal_node(args.backend, args.name), paths).height()
        except Exception as e:  # best effort; reveal then scans from wallet default
            say(f"warning: could not read chain height for release birthday: {e}")
    release_address = open_node(args.backend, release_node, paths).new_address("release")

    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "enroll").mkdir(exist_ok=True)
    (vdir / "packets").mkdir(exist_ok=True)
    crypto.Identity.generate().save(vdir / "principal.key")
    crypto.Identity.generate().save(vdir / "duress.key")
    (vdir / "vault.bin").write_bytes(blob)
    (vdir / "seq").write_text("0")
    save_json(vdir / ".vaultkey", {"key": base64.b64encode(key).decode()}, mode=0o600)
    manifest = {
        "vault_id": vault_id, "name": args.name, "threshold": args.threshold, "n": 0,
        "interval_s": args.interval, "grace_s": args.grace, "created_ts": now(), "sealed_ts": None,
        "principal_vk": crypto.Identity.load(vdir / "principal.key").verify_key_b64,
        "release_address": release_address, "release_ufvk": release_ufvk,
        "release_birthday": release_birthday,
        "ciphertext_sha256": crypto.sha256_hex(blob), "watchers": {},
        "heartbeat_zat": args.heartbeat_zat,
    }
    save_json(vdir / "vault.json", manifest)
    say(f"vault {args.name} ({vault_id}) created; {len(plaintext)} bytes encrypted.")
    say(f"next: each watcher runs  python3 -m zpulse.watcher enroll --id <w> --vault {vdir / 'vault.json'}")
    emit({"ok": True, "vault_id": vault_id, "vault": str(vdir / "vault.json"),
          "release_address": release_address, "ciphertext_sha256": manifest["ciphertext_sha256"]})


def cmd_seal(args) -> None:
    paths, vdir, manifest = _vault(args)
    if manifest.get("sealed_ts"):
        fail("vault is already sealed")
    kf = load_json(vdir / ".vaultkey")
    if kf is None:
        fail("vault key missing (.vaultkey); cannot seal")
    enrolls = []
    for f in sorted((vdir / "enroll").glob("*.json")):
        e = load_json(f)
        if e.get("vault_id", manifest["vault_id"]) != manifest["vault_id"]:
            fail(f"{f.name} is for another vault")
        if not memo.WATCHER_ID_RE.match(e["watcher_id"]):
            fail(f"bad watcher id in {f.name}")
        enrolls.append(e)
    n, t = len(enrolls), manifest["threshold"]
    if n < t:
        fail(f"only {n} watchers enrolled, threshold is {t}")
    key = base64.b64decode(kf["key"])
    shares = shamir.split(key, t, n)
    manifest["n"] = n
    manifest["watchers"] = {e["watcher_id"]: {"address": e["address"], "vk": e["vk"]} for e in enrolls}
    manifest["sealed_ts"] = now()
    save_json(vdir / "vault.json", manifest)
    duress_vk = crypto.Identity.load(vdir / "duress.key").verify_key_b64
    for e, share in zip(enrolls, shares):
        save_json(vdir / "packets" / f"{e['watcher_id']}.json", {
            "vault_id": manifest["vault_id"], "watcher_id": e["watcher_id"],
            "share_sealed": crypto.seal_to(e["vk"], share), "duress_vk": duress_vk,
            "manifest": manifest})
    (vdir / ".vaultkey").unlink()  # the only plaintext copy of the key is gone
    say(f"sealed {t}-of-{n}: packets for {', '.join(manifest['watchers'])}; vault key deleted.")
    emit({"ok": True, "vault_id": manifest["vault_id"], "threshold": t, "n": n,
          "packets": [str(vdir / "packets" / f"{w}.json") for w in manifest["watchers"]]})


def _broadcast(args, paths, manifest, memo_text: str) -> str:
    node = open_node(args.backend, principal_node(args.backend, args.name), paths)
    outs = [(w["address"], manifest["heartbeat_zat"], memo_text) for w in manifest["watchers"].values()]
    try:
        node.sync()  # on testnet: pick up change from the previous heartbeat before spending
        return node.send(outs)
    except Exception as e:
        fail(f"send failed: {e}")


def cmd_pulse(args) -> None:
    paths, vdir, manifest = _vault(args)
    if not manifest.get("sealed_ts"):
        fail("vault is not sealed yet")
    seq = int((vdir / "seq").read_text() or 0) + 1
    ts = now()
    ident = crypto.Identity.load(vdir / ("duress.key" if args.duress else "principal.key"))
    text = memo.encode_heartbeat(ident, manifest["vault_id"], seq, ts)
    txid = _broadcast(args, paths, manifest, text)
    (vdir / "seq").write_text(str(seq))
    say(f"heartbeat #{seq} sent to {manifest['n']} watchers in one tx {txid[:16]}..."
        + ("  [DURESS]" if args.duress else ""))
    emit({"ok": True, "txid": txid, "seq": seq, "ts": ts})


def cmd_cancel(args) -> None:
    paths, vdir, manifest = _vault(args)
    if not manifest.get("sealed_ts"):
        fail("vault is not sealed yet")
    ts = now()
    text = memo.encode_cancel(crypto.Identity.load(vdir / "principal.key"), manifest["vault_id"], ts)
    txid = _broadcast(args, paths, manifest, text)
    say(f"cancel sent to {manifest['n']} watchers in tx {txid[:16]}...")
    emit({"ok": True, "txid": txid, "ts": ts})


def cmd_status(args) -> None:
    paths, vdir, manifest = _vault(args)
    seq = int((vdir / "seq").read_text() or 0)
    enrolled = sorted(p.stem for p in (vdir / "enroll").glob("*.json"))
    out = {"ok": True, "vault_id": manifest["vault_id"], "name": manifest["name"],
           "sealed": bool(manifest.get("sealed_ts")), "threshold": manifest["threshold"],
           "n": manifest["n"], "enrolled": enrolled, "seq": seq,
           "interval_s": manifest["interval_s"], "grace_s": manifest["grace_s"],
           "release_address": manifest["release_address"]}
    try:
        out["principal_balance_zat"] = open_node(
            args.backend, principal_node(args.backend, args.name), paths).balance_zat()
    except Exception as e:
        say(f"warning: balance unavailable: {e}")
    watchers = {}
    for wid in manifest["watchers"] or enrolled:
        st = load_json(paths.watcher_state(wid, manifest["vault_id"]))
        if st:
            watchers[wid] = {k: st.get(k) for k in ("status", "last_seq", "deadline_ts", "release_txid")}
    out["watchers"] = watchers
    say(f"{manifest['name']}: {'sealed' if out['sealed'] else 'NOT sealed'} "
        f"{manifest['threshold']}-of-{manifest['n']}, last heartbeat seq {seq}")
    emit(out)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="zpulse.principal", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = common_args(sub.add_parser("init"))
    s.add_argument("--name", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--threshold", type=int, default=3)
    s.add_argument("--interval", type=int, default=120)
    s.add_argument("--grace", type=int, default=30)
    s.add_argument("--heartbeat-zat", type=int, default=20000,
                   help="zat paid to each watcher per heartbeat; a release costs 11000 (1000 + fee), so keep >= 20000")
    for c in ("seal", "pulse", "cancel", "status"):
        s = common_args(sub.add_parser(c))
        s.add_argument("--name", required=True)
        if c == "pulse":
            s.add_argument("--duress", action="store_true")
    args = p.parse_args(argv)
    {"init": cmd_init, "seal": cmd_seal, "pulse": cmd_pulse,
     "cancel": cmd_cancel, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
