"""End-to-end zpulse flow on MockChain, in-process (interval 3 s, grace 1 s). No network."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zpulse import chain, crypto, memo, principal, reveal, watcher  # noqa: E402
from zpulse.state import load_json  # noqa: E402

WATCHERS = ["w1", "w2", "w3", "w4", "w5"]


def run(mod, *argv: str, expect_exit: int = 0) -> dict:
    """Run a CLI main() in-process; return its stdout JSON."""
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            mod.main(list(argv))
        except SystemExit as e:
            code = e.code or 0
    assert code == expect_exit, f"{mod.__name__} {argv}: exit {code}\n{out.getvalue()}"
    return json.loads(out.getvalue().strip().splitlines()[-1])


class Env:
    def __init__(self, d: str):
        self.d = d
        self.common = ["--backend", "mock", "--data", d]

    def make_vault(self, name: str, text: bytes, interval=3, grace=1, t=3) -> str:
        f = os.path.join(self.d, f"{name}.txt")
        open(f, "wb").write(text)
        vid = run(principal, "init", *self.common, "--name", name, "--file", f, "--threshold", str(t),
                  "--interval", str(interval), "--grace", str(grace))["vault_id"]
        for w in WATCHERS:
            run(watcher, "enroll", *self.common, "--id", w, "--vault", self.manifest_path(name))
        run(principal, "seal", *self.common, "--name", name)
        return vid

    def manifest_path(self, name):
        return os.path.join(self.d, "vaults", name, "vault.json")

    def tick(self, name: str, who=WATCHERS) -> dict:
        return {w: run(watcher, "run", *self.common, "--id", w, "--once",
                       "--packet", os.path.join(self.d, "vaults", name, "packets", f"{w}.json"))
                for w in who}

    def pulse(self, name, *extra) -> dict:
        return run(principal, "pulse", *self.common, "--name", name, *extra)

    def state(self, w, vid):
        return load_json(os.path.join(self.d, "watchers", w, f"{vid}.json"))


def test_full_flow_heartbeat_silence_release_reveal():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        secret = b"the minister's offshore ledger, p.42\n"
        vid = e.make_vault("leak", secret)
        vdir = os.path.join(d, "vaults", "leak")
        assert not os.path.exists(os.path.join(vdir, ".vaultkey"))
        # re-init must refuse to overwrite
        run(principal, "init", *e.common, "--name", "leak", "--file", os.path.join(d, "leak.txt"),
            expect_exit=1)

        e.pulse("leak")
        s1 = e.tick("leak")
        assert all(s["status"] == "armed" and s["last_seq"] == 1 for s in s1.values())
        time.sleep(1.2)
        e.pulse("leak")
        s2 = e.tick("leak")
        assert s2["w1"]["last_seq"] == 2 and s2["w1"]["deadline_ts"] > s1["w1"]["deadline_ts"]

        # replay of an old heartbeat and a forged one are both rejected
        m = load_json(os.path.join(vdir, "vault.json"))
        pk = crypto.Identity.load(os.path.join(vdir, "principal.key"))
        p_node = chain.MockChain("principal-leak", os.path.join(d, "mockchain.json"))
        addrs = [w["address"] for w in m["watchers"].values()]
        old = memo.encode_heartbeat(pk, vid, 1, int(time.time()))
        forged = memo.encode_heartbeat(crypto.Identity.generate(), vid, 99, int(time.time()))
        p_node.send([(a, 1, old) for a in addrs])
        p_node.send([(a, 1, forged) for a in addrs])
        e.tick("leak", ["w1"])
        st = e.state("w1", vid)
        assert st["last_seq"] == 2 and st["hb_count"] == 2
        assert any("replayed" in line for line in st["log"])
        assert any("bad signature" in line for line in st["log"])

        # not enough shares yet -> reveal exits 2
        run(reveal, *e.common, "--vault", e.manifest_path("leak"), expect_exit=2)

        # silence: loop --once until the deadline passes and shares go out
        deadline = time.time() + 15
        while time.time() < deadline:
            states = e.tick("leak")
            if all(s["status"] == "released" for s in states.values()):
                break
            time.sleep(0.5)
        assert all(s["status"] == "released" and s["release_txid"] for s in states.values())
        # idempotent: another tick does not send again
        h = p_node.height()
        e.tick("leak")
        assert p_node.height() == h

        res = run(reveal, *e.common, "--vault", e.manifest_path("leak"))
        assert res["ok"] and res["plaintext"].encode() == secret
        assert open(os.path.join(d, "revealed", f"{vid}.txt"), "rb").read() == secret
        meta = load_json(os.path.join(d, "revealed", f"{vid}.json"))
        assert len(meta["shares_seen"]) == 5 and set(meta["txids"]) == set(WATCHERS)


def test_duress_releases_immediately():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        vid = e.make_vault("coerced", b"forced\n", interval=600, grace=60)
        e.pulse("coerced", "--duress")
        states = e.tick("coerced", WATCHERS[:3])
        assert all(s["status"] == "released" and s["last_kind"] == "duress" for s in states.values())
        res = run(reveal, *e.common, "--vault", e.manifest_path("coerced"), "--watch", "--timeout", "2")
        assert res["plaintext"] == "forced\n" and res["vault_id"] == vid


def test_cancel_shreds_share_and_never_releases():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        vid = e.make_vault("oops", b"nevermind\n", interval=1, grace=0)
        run(principal, "cancel", *e.common, "--name", "oops")
        e.tick("oops")
        time.sleep(1.5)
        states = e.tick("oops")
        assert all(s["status"] == "cancelled" and s["release_txid"] is None for s in states.values())
        assert e.state("w1", vid)["share_sealed"] is None
        assert os.path.exists(os.path.join(d, "vaults", "oops", "packets", "w1.json"))
        st = run(principal, "status", *e.common, "--name", "oops")
        assert st["watchers"]["w1"]["status"] == "cancelled"


def test_release_retries_when_watcher_is_broke():
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        e.make_vault("broke", b"x\n", interval=1, grace=0, t=1)
        ledger = os.path.join(d, "mockchain.json")
        w1 = chain.MockChain("watcher-w1", ledger)
        sink = chain.MockChain("sink", ledger).new_address("x")
        w1.send([(sink, w1.balance_zat(), None)])  # drain w1
        time.sleep(2.2)  # deadline = sealed_ts + 1 (whole seconds, strict >)
        s = e.tick("broke", ["w1"])["w1"]
        assert s["status"] == "armed"
        assert any("release failed" in line for line in e.state("w1", s["vault_id"])["log"])
        chain.MockChain("sink", ledger).send([(w1.new_address("topup"), 5000, None)])
        s = e.tick("broke", ["w1"])["w1"]
        assert s["status"] == "released"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
