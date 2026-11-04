#!/usr/bin/env python3
"""ZWallet: thin Python wrapper over zcash-devtool for Zcash TESTNET.

Interface per INTERFACES.md. All amounts are zatoshi integers.
Wallet data lives in wallet/data/<name>/ (sqlite DBs + age identity key.txt); override with ZCASH_WALLET_DATA.
The devtool binary is expected at wallet/zcash-devtool/target/release/zcash-devtool
(override with env ZCASH_DEVTOOL).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ZCASH_WALLET_DATA", HERE / "data"))  # wallet/data/<name>
BIN = Path(os.environ.get("ZCASH_DEVTOOL", HERE / "zcash-devtool" / "target" / "release" / "zcash-devtool"))
SERVER = os.environ.get("ZCASH_LWD_SERVER", "zecrocks")
MIN_CONF = os.environ.get("ZCASH_MIN_CONF", "1")  # spend notes after this many confirmations (devtool default: 3 trusted / 10 untrusted)  # 'zecrocks' -> testnet.zec.rocks for -n test
TXID_RE = re.compile(r"^[0-9a-f]{64}$")


class ZWalletError(RuntimeError):
    pass


def _run(args: list[str], *, timeout: int = 1800) -> str:
    if not BIN.exists():
        raise ZWalletError(f"zcash-devtool binary not found at {BIN}; build it with "
                           f"`cargo build --release` inside wallet/zcash-devtool")
    cmd = [str(BIN), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise ZWalletError(f"{' '.join(cmd)}\nexit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p.stdout


class ZWallet:
    def __init__(self, name: str):
        self.name = name
        self.dir = DATA / name
        self.identity = self.dir / "key.txt"

    # ---- lifecycle -------------------------------------------------------
    def exists(self) -> bool:
        return (self.dir / "keys.toml").exists()

    def init(self, birthday: int | None = None) -> None:
        if self.exists():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        args = ["wallet", "-w", str(self.dir), "init", "--name", self.name,
                "-i", str(self.identity), "-n", "test", "-s", SERVER]
        if birthday is not None:
            args += ["--birthday", str(birthday)]
        _run(args)

    def _w(self, *rest: str) -> list[str]:
        return ["wallet", "-w", str(self.dir), *rest]

    def sync(self) -> None:
        _run(self._w("sync", "-s", SERVER), timeout=3600)

    # ---- queries ---------------------------------------------------------
    def addresses(self) -> dict:
        out = _run(self._w("list-addresses", "--receiver", "unified", "--receiver", "transparent"))
        ua = re.search(r"Default Address:\s*(\S+)", out)
        ta = re.search(r"Receiver\(transparent\):\s*(\S+)", out)
        if not ua:
            raise ZWalletError(f"could not parse addresses from:\n{out}")
        return {"unified": ua.group(1), "transparent": ta.group(1) if ta else None}

    def balance(self) -> dict:
        out = _run(self._w("balance", "--json"))
        j = json.loads(out.strip().splitlines()[-1])
        shielded = j["sapling_spendable"] + j["orchard_spendable"] + j.get("ironwood_spendable", 0)
        transparent = j["transparent_spendable"]
        return {
            "transparent_zat": transparent,
            "shielded_zat": shielded,
            "pending_zat": max(0, j["total"] - shielded - transparent),
            "chain_tip_height": j["chain_tip_height"],
        }

    def height(self) -> int:
        return self.balance()["chain_tip_height"]

    # ---- spends ----------------------------------------------------------
    def shield(self, amount_zat: int | None = None) -> str:
        """t->z. zcash-devtool shields ALL transparent funds of the account;
        amount_zat is accepted for interface compatibility and ignored."""
        out = _run(self._w("shield", "-i", str(self.identity), "-s", SERVER), timeout=900)
        return _last_txid(out)

    def send_transparent(self, to_taddr: str, amount_zat: int) -> str:
        """z->t unshield of exactly amount_zat to a transparent address."""
        if not to_taddr.startswith("tm"):
            raise ZWalletError("send_transparent expects a testnet transparent address (tm...)")
        return self._send(to_taddr, amount_zat)

    def send_shielded(self, to_ua: str, amount_zat: int) -> str:
        return self._send(to_ua, amount_zat)

    def _send(self, addr: str, amount_zat: int) -> str:
        out = _run(self._w("send", "-i", str(self.identity), "--address", addr,
                           "--value", str(int(amount_zat)), "-s", SERVER,
                           "--min-confirmations", MIN_CONF), timeout=900)
        return _last_txid(out)

    # ---- history ---------------------------------------------------------
    def transactions(self) -> list[dict]:
        """Classify wallet transactions from `list-tx` text output."""
        out = _run(self._w("list-tx"))
        txs = []
        cur = None
        for line in out.splitlines():
            if TXID_RE.match(line.strip()):
                cur = {"txid": line.strip(), "height": None, "time": None, "outputs": []}
                txs.append(cur)
                continue
            if cur is None:
                continue
            s = line.strip()
            m = re.match(r"Mined:\s*(\d+)\s*\((.+)\)", s)
            if m:
                cur["height"] = int(m.group(1))
                cur["time"] = _parse_time(m.group(2))
                continue
            m = re.match(r"Amount:\s*(-?[\d.]+)", s)
            if m:
                cur["delta_zat"] = int(round(float(m.group(1)) * 1e8))
                continue
            m = re.match(r"Output (\d+) \((\w+)\)", s)
            if m:
                cur["outputs"].append({"pool": m.group(2).lower(), "value_zat": 0,
                                       "change": False, "to": None, "sent": False, "recv": False})
                continue
            if cur["outputs"]:
                o = cur["outputs"][-1]
                m = re.match(r"Value:\s*([\d.]+)(.*)", s)
                if m:
                    o["value_zat"] = int(round(float(m.group(1)) * 1e8))
                    o["change"] = "Change" in m.group(2)
                    o["internal"] = "Internal" in m.group(2)
                    continue
                if s.startswith("To:"):
                    o["to"] = s[3:].strip()
                elif s.startswith("Sent from account"):
                    o["sent"] = True
                elif s.startswith("Received by account"):
                    o["recv"] = True
        return [_classify(t) for t in txs]


    # ---- zpulse extensions (additive) -------------------------------------
    def init_fvk(self, ufvk: str, birthday: int | None = None) -> None:
        """Create a VIEW-ONLY wallet from a unified full viewing key (no spend authority)."""
        if self.exists():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        args = ["wallet", "-w", str(self.dir), "init-fvk", "--name", self.name,
                "--fvk", ufvk, "-s", SERVER]
        if birthday is not None:
            args += ["--birthday", str(birthday)]
        _run(args)

    def ufvk(self) -> str:
        """Unified full viewing key of the (first) account, from `list-accounts`."""
        out = _run(self._w("list-accounts"))
        m = re.search(r"UFVK:\s*(\S+)", out)
        if not m or m.group(1) == "None":
            raise ZWalletError(f"no UFVK in list-accounts output:\n{out}")
        return m.group(1)

    def generate_address(self) -> str:
        """A fresh diversified shielded unified address (`generate-address`)."""
        out = _run(self._w("generate-address"))
        m = re.search(r"Address:\s*(\S+)", out)
        if not m:
            raise ZWalletError(f"could not parse generate-address output:\n{out}")
        return m.group(1)

    def send_many(self, outputs: list[tuple[str, int, str | None]]) -> str:
        """ONE transaction paying every (addr, zat, memo) output. Returns txid."""
        if not outputs:
            raise ZWalletError("send_many needs at least one output")
        for _, _, memo in outputs:
            assert memo is None or len(memo.encode()) <= 512, "memo exceeds 512 bytes"
        if len(outputs) == 1:
            addr, zat, memo = outputs[0]
            args = self._w("send", "-i", str(self.identity), "--address", addr,
                           "--value", str(int(zat)), "-s", SERVER, "--min-confirmations", MIN_CONF)
            if memo is not None:
                args += ["--memo", memo]
            return _last_txid(_run(args, timeout=900))
        out = _run(self._w("pay", "-i", str(self.identity), "--payment-uri", zip321_uri(outputs),
                           "--disable-confirmation", "-s", SERVER,
                           "--min-confirmations", MIN_CONF), timeout=900)
        return _last_txid(out)

    def received(self, min_height: int = 0) -> list[dict]:
        """Mined incoming outputs with memos: [{txid, height, time, to, amount_zat, memo}].

        Compact blocks carry no memos, so `enhance` first fetches full transaction
        data from lightwalletd; without it every received memo reads as None."""
        try:
            _run(self._w("enhance", "-s", SERVER), timeout=600)
        except ZWalletError as e:  # network hiccup: fall back to whatever is already stored
            print(f"warning: enhance failed: {str(e)[:200]}", file=sys.stderr)
        rows = parse_received(_run(self._w("list-tx")))
        return [r for r in rows if r["height"] >= min_height]


def _classify(t: dict) -> dict:
    outs = t["outputs"]
    sent = [o for o in outs if o["sent"] and not o["change"]]
    recv = [o for o in outs if o["recv"]]
    t_out = [o for o in sent if o["pool"] == "transparent"]
    z_out = [o for o in sent if o["pool"] != "transparent"]
    internal = [o for o in outs if o.get("internal")]
    delta = t.get("delta_zat", 0)
    if internal and all(o["pool"] != "transparent" for o in internal) and delta < 0 and not sent:
        kind, amount, taddr = "shield", sum(o["value_zat"] for o in internal), None
    elif t_out:
        kind, amount, taddr = "unshield", sum(o["value_zat"] for o in t_out), t_out[0]["to"]
    elif z_out:
        kind, amount, taddr = "z2z", sum(o["value_zat"] for o in z_out), None
    elif recv:
        pool = recv[0]["pool"]
        kind = "recv"
        amount = sum(o["value_zat"] for o in recv)
        taddr = recv[0]["to"] if pool == "transparent" else None
    else:
        kind, amount, taddr = "unknown", abs(delta), None
    return {"txid": t["txid"], "height": t["height"], "time": t["time"],
            "kind": kind, "amount_zat": amount, "taddr": taddr}


def _parse_time(s: str) -> int | None:
    # e.g. "2026-11-04 7:12:23.0 +00:00:00"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{1,2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    import calendar
    y, mo, d, h, mi, se = map(int, m.groups())
    return calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0))


def _last_txid(out: str) -> str:
    for line in reversed(out.strip().splitlines()):
        if TXID_RE.match(line.strip()):
            return line.strip()
    raise ZWalletError(f"no txid in output:\n{out}")


# ---- zpulse helpers: ZIP-321 URIs and list-tx memo parsing ---------------

def _zec(zat: int) -> str:
    """Zatoshi -> ZIP-321 decimal ZEC string (no trailing zeros, max 8 decimals)."""
    zat = int(zat)
    if zat <= 0:
        raise ZWalletError(f"amount must be positive, got {zat}")
    whole, frac = divmod(zat, 100_000_000)
    return f"{whole}.{frac:08d}".rstrip("0").rstrip(".")


def _memo_b64url(memo: str) -> str:
    import base64
    return base64.urlsafe_b64encode(memo.encode()).decode().rstrip("=")


def zip321_uri(outputs: list[tuple[str, int, str | None]]) -> str:
    """ZIP-321 multi-payment URI: first payment unindexed, then .1, .2, ..."""
    first, params = outputs[0][0], []
    for i, (addr, zat, memo) in enumerate(outputs):
        sfx = "" if i == 0 else f".{i}"
        if i:
            params.append(f"address{sfx}={addr}")
        params.append(f"amount{sfx}={_zec(zat)}")
        if memo is not None:
            params.append(f"memo{sfx}={_memo_b64url(memo)}")
    return f"zcash:{first}?" + "&".join(params)


_MEMO_RAW_RE = re.compile(r'^Memo:\s*Memo::Text\("(.*)"\)$', re.S)
_MEMO_DBG_RE = re.compile(r'^Memo:\s*(?:Memo::)?Text\((?:TextMemo\()?"(.*)"\)\)?$', re.S)


def parse_memo_line(s: str) -> str | None:
    """Parse a list-tx `Memo: ...` line (already stripped) into text, or None.

    zcash_protocol prints `Memo::Text("<raw text>")` with NO escaping, so the
    text is taken verbatim between the first `("` and the final `")`. A derived
    Debug form (`Text(TextMemo("..."))`, escaped) is also tolerated and unescaped.
    """
    m = _MEMO_RAW_RE.match(s)
    if m:
        return m.group(1)
    m = _MEMO_DBG_RE.match(s)
    if m:
        return re.sub(r"\\(.)", lambda c: {"n": "\n", "t": "\t"}.get(c.group(1), c.group(1)), m.group(1))
    return None


def _zat(s: str) -> int | None:
    m = re.match(r"(-?)(\d+)\.(\d{8})", s.strip())
    if not m:
        return None
    v = int(m.group(2)) * 100_000_000 + int(m.group(3))
    return -v if m.group(1) else v


def parse_received(text: str) -> list[dict]:
    """Extract mined outputs marked "Received by account" from `list-tx` text output."""
    rows: list[dict] = []
    tx: dict | None = None
    out: dict | None = None

    def flush() -> None:
        if tx and out and out["recv"] and tx["height"] is not None:
            rows.append({"txid": tx["txid"], "height": tx["height"], "time": tx["time"] or 0,
                         "to": out["to"], "amount_zat": out["amount_zat"], "memo": out["memo"]})

    for line in text.splitlines():
        s = line.strip()
        if TXID_RE.match(s):
            flush()
            tx, out = {"txid": s, "height": None, "time": None}, None
            continue
        if tx is None:
            continue
        m = re.match(r"Mined:\s*(\d+)\s*\((.+)\)", s)
        if m:
            tx["height"], tx["time"] = int(m.group(1)), _parse_time(m.group(2))
        elif re.match(r"Output \d+ \(", s):
            flush()
            out = {"recv": False, "to": "", "amount_zat": 0, "memo": None}
        elif out is not None:
            if s.startswith("Value:"):
                out["amount_zat"] = _zat(s[6:]) or 0
            elif s.startswith("Received by account"):
                out["recv"] = True
            elif s.startswith("To:"):
                out["to"] = s[3:].strip()
            elif s.startswith("Memo:"):
                out["memo"] = parse_memo_line(s)
    flush()
    rows.sort(key=lambda r: r["height"])
    return rows


def wait_for_height(w: ZWallet, target: int, poll: int = 20, max_wait: int = 1800) -> int:
    t0 = time.time()
    while True:
        w.sync()
        h = w.height()
        if h >= target:
            return h
        if time.time() - t0 > max_wait:
            raise ZWalletError(f"timed out waiting for height {target} (at {h})")
        time.sleep(poll)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: zwallet.py <name> init [birthday] | sync | addresses | balance | height | "
              "shield | send-t <taddr> <zat> | send-z <ua> <zat> | tx | gen-addr | ufvk | recv [min_height]")
        return 2
    w = ZWallet(argv[0])
    cmd, args = argv[1], argv[2:]
    if cmd == "init":
        w.init(int(args[0]) if args else None); print(json.dumps(w.addresses()))
    elif cmd == "sync":
        w.sync(); print(json.dumps(w.balance()))
    elif cmd == "addresses":
        print(json.dumps(w.addresses()))
    elif cmd == "balance":
        print(json.dumps(w.balance()))
    elif cmd == "height":
        print(w.height())
    elif cmd == "shield":
        print(w.shield())
    elif cmd == "send-t":
        print(w.send_transparent(args[0], int(args[1])))
    elif cmd == "send-z":
        print(w.send_shielded(args[0], int(args[1])))
    elif cmd == "tx":
        print(json.dumps(w.transactions(), indent=1))
    elif cmd == "gen-addr":
        print(w.generate_address())
    elif cmd == "ufvk":
        print(w.ufvk())
    elif cmd == "recv":
        print(json.dumps(w.received(int(args[0]) if args else 0), indent=1))
    else:
        print(f"unknown command {cmd}"); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
