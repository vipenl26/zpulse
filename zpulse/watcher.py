"""zpulse.watcher: holds one Shamir share per vault and releases it if the heartbeats stop.

  python3 -m zpulse.watcher enroll --id w1 --vault data/vaults/NAME/vault.json
  python3 -m zpulse.watcher run    --id w1 --packet data/vaults/NAME/packets/w1.json [--poll 5] [--once]

The watcher only ever needs its own wallet and identity key: it never talks to the
principal or other watchers, and never holds the principal's keys.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import crypto, memo
from .state import (Paths, add_log, common_args, emit, fail, load_json, now, open_node,
                    save_json, say, zwallet)

RELEASE_ZAT = 1000


def watcher_node(wid: str) -> str:
    return f"watcher-{wid}"


def load_identity(paths: Paths, wid: str, create: bool) -> crypto.Identity:
    p = paths.watcher_identity(wid)
    if p.exists():
        return crypto.Identity.load(p)
    if not create:
        fail(f"no identity for watcher {wid} at {p}; run enroll first")
    ident = crypto.Identity.generate()
    ident.save(p)
    return ident


# ------------------------------------------------------------------ enroll

def cmd_enroll(args) -> None:
    if not memo.WATCHER_ID_RE.match(args.id):
        fail("watcher id must match [a-z0-9_-]{1,16}")
    paths = Paths(args.data)
    vault_path = Path(args.vault)
    manifest = load_json(vault_path)
    if manifest is None:
        fail(f"no manifest at {vault_path}")
    if manifest.get("sealed_ts"):
        fail("vault is already sealed; enrollment closed")
    ident = load_identity(paths, args.id, create=True)
    node = watcher_node(args.id)
    if args.backend == "testnet":
        w = zwallet(node)
        if not w.exists():
            say(f"creating testnet wallet {node} ...")
            w.init()
    # one fresh diversified address per vault: an incoming payment maps 1:1 to this vault
    address = open_node(args.backend, node, paths).new_address(manifest["vault_id"])
    out = vault_path.parent / "enroll" / f"{args.id}.json"
    save_json(out, {"watcher_id": args.id, "vault_id": manifest["vault_id"],
                    "vk": ident.verify_key_b64, "address": address})
    say(f"watcher {args.id} enrolled in vault {manifest['name']} ({manifest['vault_id']})")
    emit({"ok": True, "watcher_id": args.id, "vault_id": manifest["vault_id"],
          "vk": ident.verify_key_b64, "address": address, "enroll": str(out)})


# ------------------------------------------------------------------ run

class Watcher:
    def __init__(self, args):
        self.args = args
        self.paths = Paths(args.data)
        self.wid = args.id
        self.ident = load_identity(self.paths, self.wid, create=False)
        packet = load_json(args.packet)
        if packet is None:
            fail(f"no packet at {args.packet}")
        if packet["watcher_id"] != self.wid:
            fail(f"packet is for watcher {packet['watcher_id']}, not {self.wid}")
        self.m = packet["manifest"]
        self.vid = packet["vault_id"]
        self.duress_vk = packet.get("duress_vk")
        if self.m["vault_id"] != self.vid or self.m["watchers"][self.wid]["vk"] != self.ident.verify_key_b64:
            fail("packet does not match this watcher's identity")
        self.state_path = self.paths.watcher_state(self.wid, self.vid)
        self.st = load_json(self.state_path)
        if self.st is None:
            try:
                crypto.unseal(self.ident, packet["share_sealed"])  # make sure we can open it
            except Exception:
                fail("cannot unseal share packet with this identity")
            self.st = {
                "vault_id": self.vid, "watcher_id": self.wid, "status": "armed",
                "last_hb_ts": None, "last_seq": 0, "last_kind": None,
                "deadline_ts": self.m["sealed_ts"] + self.m["interval_s"] + self.m["grace_s"],
                "hb_count": 0, "release_txid": None, "release_pending": False,
                "updated_ts": now(), "next_check_ts": now(), "chain_now": 0,
                "share_sealed": packet["share_sealed"], "seen_txids": [], "log": [],
            }
            self.log(f"armed for vault {self.m['name']} ({self.vid}); "
                     f"deadline in {self.st['deadline_ts'] - now()}s")
            self.save()
        self.node = open_node(args.backend, watcher_node(self.wid), self.paths)

    def log(self, msg: str) -> None:
        say(f"[{self.wid}] {add_log(self.st, msg)}")

    def save(self) -> None:
        self.st["updated_ts"] = now()
        save_json(self.state_path, self.st, mode=0o600)  # holds the (sealed) share

    def chain_time(self, txs: list) -> int:
        t = max((tx["time"] for tx in txs), default=0)
        if hasattr(self.node, "all_txs"):  # mock: the whole ledger is visible
            t = max([t] + [x["time"] for x in self.node.all_txs()[-1:]])
        return t

    # -- one poll ---------------------------------------------------------
    def poll(self) -> None:
        st = self.st
        if st["status"] in ("released", "cancelled"):
            return  # terminal: idempotent, nothing more to do
        self.node.sync()
        txs = self.node.received(0)
        seen = set(st["seen_txids"])
        for tx in txs:
            if tx["txid"] in seen:
                continue
            seen.add(tx["txid"])
            st["seen_txids"].append(tx["txid"])
            self.handle(tx)
            if st["status"] == "cancelled":
                break
        st["chain_now"] = max(st.get("chain_now") or 0, self.chain_time(txs))
        t_now = max(now(), st["chain_now"])
        if st["status"] == "armed" and (st["release_pending"] or t_now > st["deadline_ts"]):
            if not st["release_pending"]:
                self.log(f"DEADLINE PASSED ({t_now - st['deadline_ts']}s late): releasing share")
            self.release()
        st["next_check_ts"] = now() + self.args.poll
        self.save()

    def handle(self, tx) -> None:
        st = self.st
        m = memo.parse(tx.get("memo"))
        if not m or m["vault_id"] != self.vid:
            return  # not for this vault (match by vault_id: testnet may not report `to`)
        if m["kind"] == "HB":
            kind = memo.classify_heartbeat(m, self.m["principal_vk"], self.duress_vk)
            if kind is None:
                self.log(f"rejected heartbeat seq {m['seq']}: bad signature")
                return
            if m["seq"] <= st["last_seq"]:
                self.log(f"rejected replayed heartbeat seq {m['seq']} (last {st['last_seq']})")
                return
            st["last_seq"], st["last_kind"] = m["seq"], kind
            st["hb_count"] += 1
            if kind == "duress":
                self.log(f"DURESS heartbeat seq {m['seq']}: releasing now")
                st["release_pending"] = True
                return
            st["last_hb_ts"] = tx["time"]
            st["deadline_ts"] = tx["time"] + self.m["interval_s"] + self.m["grace_s"]
            self.log(f"heartbeat seq {m['seq']} ok; deadline in {st['deadline_ts'] - now()}s")
        elif m["kind"] == "CX":
            if not memo.verify_cancel(m, self.m["principal_vk"]):
                self.log("rejected cancel: bad signature")
                return
            if st["status"] == "armed" and not st["release_pending"]:
                st["status"] = "cancelled"
                st["share_sealed"] = None  # shred our share
                self.log("vault CANCELLED by principal; share shredded")

    def release(self) -> None:
        st = self.st
        st["release_pending"] = True
        try:
            share = crypto.unseal(self.ident, st["share_sealed"])
            text = memo.encode_share(self.ident, self.vid, self.wid, share)
            txid = self.node.send([(self.m["release_address"], RELEASE_ZAT, text)])
        except Exception as e:
            self.log(f"release failed, will retry: {str(e)[:120]}")
            return
        st.update(status="released", release_txid=txid, release_pending=False)
        self.log(f"share RELEASED in tx {txid[:16]}")

    def summary(self) -> dict:
        keys = ("vault_id", "watcher_id", "status", "last_hb_ts", "last_seq", "last_kind",
                "deadline_ts", "hb_count", "release_txid")
        return {"ok": True, **{k: self.st.get(k) for k in keys}}


def cmd_run(args) -> None:
    w = Watcher(args)
    if args.once:
        w.poll()
        emit(w.summary())
        return
    say(f"[{w.wid}] watching vault {w.vid} every {args.poll}s (Ctrl-C to stop)")
    try:
        while True:
            try:
                w.poll()
            except Exception as e:  # keep the daemon alive across transient errors
                w.log(f"poll error: {str(e)[:120]}")
                w.save()
            time.sleep(args.poll)
    except KeyboardInterrupt:
        emit(w.summary())


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="zpulse.watcher", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = common_args(sub.add_parser("enroll"))
    s.add_argument("--id", required=True)
    s.add_argument("--vault", required=True)
    s = common_args(sub.add_parser("run"))
    s.add_argument("--id", required=True)
    s.add_argument("--packet", required=True)
    s.add_argument("--poll", type=float, default=5)
    s.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    {"enroll": cmd_enroll, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
