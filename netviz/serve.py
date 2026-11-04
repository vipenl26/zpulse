#!/usr/bin/env python3
"""zpulse netviz: a live view of the Zcash testnet, read-only, via lightwalletd gRPC.

    python3 netviz/serve.py [--port 8792] [--data ./data] [--server testnet.zec.rocks:443]
                            [--blocks 40] [--txids extra.txt]

GET /                     -> netviz/index.html
GET /api/snapshot         -> JSON snapshot (chain info, last N compact blocks, mempool, tracked txids)
GET /api/track?txid=HEX   -> start tracking a txid (optional &label=...&kind=heartbeat|release|other)

A background thread polls lightwalletd every ~5 s and keeps an in-memory snapshot.
Only read RPCs are used (GetLightdInfo, GetLatestBlock, GetBlockRange, GetBlock,
GetTransaction, GetMempoolTx). Nothing is ever sent; no wallet is touched.
Requires `grpcio` and `protobuf` (stubs are pre-generated in netviz/pb/).
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# grpc's default c-ares resolver fails on some macOS setups ("Could not contact DNS servers").
os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "pb"))  # generated stubs use flat imports

try:
    import grpc  # noqa: E402
    import service_pb2 as S  # noqa: E402
    import service_pb2_grpc as G  # noqa: E402
except ImportError as e:  # pragma: no cover
    sys.exit(f"netviz needs grpcio + protobuf: python3 -m pip install grpcio protobuf  ({e})")

TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
POLL_S = 5.0
RPC_TIMEOUT = 15
ALL_POOLS = [S.TRANSPARENT, S.SAPLING, S.ORCHARD, S.IRONWOOD]
NOT_IN_CHAIN = 0xFFFFFFFFFFFFFFFF


def rev_hex(b: bytes) -> str:
    """lightwalletd returns hashes in internal byte order; display order is reversed."""
    return b[::-1].hex()


# ---------------------------------------------------------------- our txids from data/

def read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def collect_ours(data: Path, txids_file):
    """Return {txid: (label, kind)} from watcher state, reveal metadata and an optional file.

    Same layout dashboard/serve.py reads: data/watchers/<wid>/<vault>.json carries
    release_txid (a share release) and seen_txids (heartbeats it verified);
    data/revealed/<vault>.json carries the share txids the reveal used.
    """
    out = {}

    def put(txid, label, kind, strong=False):
        if not isinstance(txid, str) or not TXID_RE.match(txid):
            return
        txid = txid.lower()
        if strong or txid not in out:
            out[txid] = (label, kind)

    wdir = data / "watchers"
    if wdir.is_dir():
        for d in sorted(p for p in wdir.iterdir() if p.is_dir()):
            for f in sorted(d.glob("*.json")):
                st = read_json(f)
                if not isinstance(st, dict) or "vault_id" not in st:
                    continue
                vid = str(st.get("vault_id"))[:8]
                for t in st.get("seen_txids") or []:
                    put(t, f"heartbeat vault {vid}", "heartbeat")
                if st.get("release_txid"):
                    put(st["release_txid"], f"{st.get('watcher_id', d.name)} share release, vault {vid}",
                        "release", strong=True)
    rdir = data / "revealed"
    if rdir.is_dir():
        for f in sorted(rdir.glob("*.json")):
            meta = read_json(f) or {}
            vid = str(meta.get("vault_id", f.stem))[:8]
            tx = meta.get("txids") or []
            items = tx.items() if isinstance(tx, dict) else ((None, t) for t in tx)
            for wid, t in items:
                put(t, f"{wid or 'share'} share release, vault {vid}", "release")
    if txids_file:
        try:
            for line in Path(txids_file).read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                label = parts[1] if len(parts) > 1 else "tracked"
                low = label.lower()
                kind = "heartbeat" if "heartbeat" in low else "release" if "release" in low else "other"
                put(parts[0], label, kind, strong=True)
        except OSError as e:
            print(f"netviz: cannot read --txids file: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- poller

def summarize_block(b):
    txs = []
    agg = dict(sapling_spends=0, sapling_outputs=0, orchard_actions=0, ironwood_actions=0,
               transparent_only=0, transparent_in=0, transparent_out=0)
    for t in b.vtx:
        row = {
            "txid": rev_hex(t.txid),
            "sapling_spends": len(t.spends),
            "sapling_outputs": len(t.outputs),
            "orchard_actions": len(t.actions),
            "ironwood_actions": len(t.ironwoodActions),
            "t_in": len(t.vin),
            "t_out": len(t.vout),
        }
        shielded = row["sapling_spends"] + row["sapling_outputs"] + row["orchard_actions"] + row["ironwood_actions"]
        row["transparent_only"] = shielded == 0
        for k in ("sapling_spends", "sapling_outputs", "orchard_actions", "ironwood_actions"):
            agg[k] += row[k]
        agg["transparent_in"] += row["t_in"]
        agg["transparent_out"] += row["t_out"]
        agg["transparent_only"] += 1 if shielded == 0 else 0
        txs.append(row)
    return {
        "height": int(b.height),
        "hash": rev_hex(b.hash),
        "time": int(b.time),
        "n_tx": len(b.vtx),
        **agg,
        "other_pool_actions": {"ironwood": agg["ironwood_actions"]},
        "txids": [r["txid"] for r in txs],
        "txs": txs,
    }


class Poller:
    def __init__(self, server, data, txids_file, nblocks):
        self.server = server
        self.data = data
        self.txids_file = txids_file
        self.nblocks = max(5, min(nblocks, 200))
        self.lock = threading.Lock()
        self.blocks = {}        # height -> summary
        self.info = None
        self.mempool = []       # [{txid, ...counts}]
        self.mempool_first = {}  # txid -> first seen ts
        self.manual = {}        # txid -> (label, kind) added via /api/track
        self.resolved = {}      # txid -> {"height": int|None, "checked": ts, "block_time": int|None}
        self.first_seen = {}    # txid -> ts (first time netviz saw it anywhere)
        self.ours = {}          # txid -> (label, kind), refreshed each poll
        self.error = None
        self.last_ok = None
        self.pools = ALL_POOLS
        self.channel = None
        self.stub = None

    # -- grpc plumbing
    def _connect(self):
        host = self.server
        if host.endswith(":443") or ":" not in host:
            creds = grpc.ssl_channel_credentials()
            self.channel = grpc.secure_channel(host if ":" in host else host + ":443", creds)
        else:
            self.channel = grpc.insecure_channel(host)
        self.stub = G.CompactTxStreamerStub(self.channel)

    def _range(self, lo, hi):
        req = S.BlockRange(start=S.BlockID(height=lo), end=S.BlockID(height=hi), poolTypes=self.pools)
        try:
            return list(self.stub.GetBlockRange(req, timeout=RPC_TIMEOUT + 10))
        except grpc.RpcError as e:
            if self.pools and e.code() in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.UNIMPLEMENTED):
                self.pools = []  # older server: legacy shielded-only blocks
                return self._range(lo, hi)
            raise

    def track(self, txid, label=None, kind=None):
        txid = txid.lower()
        if kind not in ("heartbeat", "release", "other"):
            kind = "other"
        with self.lock:
            self.manual[txid] = ((label or "added via /api/track")[:80], kind)
            self.first_seen.setdefault(txid, time.time())
            self.resolved.pop(txid, None)

    def _tracked_set(self):
        ours = collect_ours(self.data, self.txids_file)
        with self.lock:
            ours.update(self.manual)
        return ours

    def _resolve(self, txid, tip, now):
        """Find a tracked tx's block height via GetTransaction (cached once mined deep enough)."""
        r = self.resolved.get(txid)
        if r and r["height"] is not None and tip - r["height"] > 10:
            return r  # deep enough; reorg risk negligible for a demo
        if r and now - r["checked"] < (POLL_S if r["height"] is not None else 15):
            return r
        height = None
        block_time = r.get("block_time") if r else None
        try:
            raw = self.stub.GetTransaction(S.TxFilter(hash=bytes.fromhex(txid)[::-1]), timeout=RPC_TIMEOUT)
            if raw.height and raw.height != NOT_IN_CHAIN:
                height = int(raw.height)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise
        if height is not None:
            b = self.blocks.get(height)
            if b:
                block_time = b["time"]
            elif not r or r.get("height") != height or block_time is None:
                try:
                    block_time = int(self.stub.GetBlock(S.BlockID(height=height), timeout=RPC_TIMEOUT).time)
                except grpc.RpcError:
                    block_time = None
        r = {"height": height, "checked": now, "block_time": block_time}
        self.resolved[txid] = r
        return r

    def poll_once(self):
        if self.stub is None:
            self._connect()
        st = self.stub
        now = time.time()
        info = st.GetLightdInfo(S.Empty(), timeout=RPC_TIMEOUT)
        tip_id = st.GetLatestBlock(S.ChainSpec(), timeout=RPC_TIMEOUT)
        tip = int(tip_id.height)

        lo = tip - self.nblocks + 1
        have = [h for h in self.blocks if h >= lo]
        # refetch the last 3 we hold (cheap reorg guard) plus anything new
        start = max(lo, (max(have) - 2) if have else lo)
        fetched = {int(b.height): summarize_block(b) for b in self._range(start, tip)}

        # mempool: compact txs, all pools, bounded by a timeout (stream closes on its own)
        mp = []
        try:
            req = S.GetMempoolTxRequest(poolTypes=self.pools)
            for t in st.GetMempoolTx(req, timeout=8):
                mp.append({
                    "txid": rev_hex(t.txid),
                    "sapling": len(t.spends) + len(t.outputs),
                    "orchard": len(t.actions),
                    "ironwood": len(t.ironwoodActions),
                    "t_in": len(t.vin), "t_out": len(t.vout),
                })
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.DEADLINE_EXCEEDED:
                raise

        with self.lock:
            for h in [h for h in self.blocks if h < lo or h > tip]:
                del self.blocks[h]
            self.blocks.update(fetched)
            self.info = {
                "chain": info.chainName,
                "height": tip,
                "tip_hash": rev_hex(tip_id.hash),
                "server": self.server,
                "server_version": f"{info.vendor} {info.version}".strip(),
                "node": info.zcashdSubversion,
                "branch_id": info.consensusBranchId or None,
                "estimated_height": int(info.estimatedHeight) or None,
                "upgrade_name": info.upgradeName or None,
                "upgrade_height": int(info.upgradeHeight) or None,
                "protocol_version": info.lightwalletProtocolVersion or None,
            }
            for m in mp:
                self.mempool_first.setdefault(m["txid"], now)
                self.first_seen.setdefault(m["txid"], now)
            live = {m["txid"] for m in mp}
            self.mempool_first = {k: v for k, v in self.mempool_first.items() if k in live}
            for m in mp:
                m["first_seen_ts"] = int(self.mempool_first[m["txid"]])
            self.mempool = mp

        # tracked txids (outside the lock: network calls)
        ours = self._tracked_set()
        mp_ids = {m["txid"] for m in mp}
        in_window = {}
        for b in fetched.values():
            for t in b["txids"]:
                in_window[t] = b["height"]
        for txid in ours:
            if txid in in_window:
                b = self.blocks.get(in_window[txid])
                self.resolved[txid] = {"height": in_window[txid], "checked": now,
                                       "block_time": b["time"] if b else None}
            elif txid in mp_ids:
                self.resolved[txid] = {"height": None, "checked": now, "block_time": None}
            else:
                self._resolve(txid, tip, now)
        with self.lock:
            for txid in ours:
                self.first_seen.setdefault(txid, now)
            self.ours = ours
            self.error = None
            self.last_ok = now

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.poll_once()
            except grpc.RpcError as e:
                with self.lock:
                    self.error = f"gRPC {e.code().name}: {e.details()}"
                if e.code() in (grpc.StatusCode.UNAVAILABLE,):
                    try:
                        self.channel.close()
                    except Exception:
                        pass
                    self.stub = None
            except Exception as e:  # never kill the poller
                with self.lock:
                    self.error = f"{type(e).__name__}: {e}"
            time.sleep(max(0.5, POLL_S - (time.time() - t0)))

    def snapshot(self):
        now = time.time()
        with self.lock:
            blocks = [self.blocks[h] for h in sorted(self.blocks)]
            info = dict(self.info) if self.info else None
            tip = info["height"] if info else None
            if info and blocks:
                info["lag_s"] = int(now - blocks[-1]["time"])
            times = [b["time"] for b in blocks[-21:]]
            interval = round((times[-1] - times[0]) / (len(times) - 1), 1) if len(times) >= 2 else None
            tracked = []
            for txid, (label, kind) in self.ours.items():
                r = self.resolved.get(txid) or {}
                h = r.get("height")
                tracked.append({
                    "txid": txid, "label": label, "kind": kind,
                    "height": h,
                    "confirmations": (tip - h + 1) if (h and tip) else 0,
                    "in_mempool": any(m["txid"] == txid for m in self.mempool),
                    "block_time": r.get("block_time"),
                    "first_seen_ts": int(self.first_seen.get(txid, now)),
                })
            tracked.sort(key=lambda t: (t["height"] is not None, t["height"] or 0), reverse=True)
            tracked.sort(key=lambda t: t["height"] is not None)  # pending first, then newest mined
            return {
                "now": round(now, 3),
                "info": info,
                "blocks": blocks,
                "mempool": [m["txid"] for m in self.mempool],
                "mempool_tx": list(self.mempool),
                "tracked": tracked,
                "block_interval_s": interval,
                "pools": [S.PoolType.Name(p) for p in self.pools] or ["legacy"],
                "error": self.error,
                "last_ok": round(self.last_ok, 3) if self.last_ok else None,
            }


# ---------------------------------------------------------------- http

def make_handler(poller: Poller):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/snapshot":
                try:
                    self._json(200, poller.snapshot())
                except Exception as e:
                    self._json(500, {"error": str(e)})
            elif u.path == "/api/track":
                q = parse_qs(u.query)
                txid = (q.get("txid") or [""])[0].strip()
                if not TXID_RE.match(txid):
                    self._json(400, {"ok": False, "error": "txid must be 64 hex characters"})
                    return
                poller.track(txid, (q.get("label") or [None])[0], (q.get("kind") or [None])[0])
                self._json(200, {"ok": True, "txid": txid.lower()})
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser(description="zpulse netviz: live Zcash testnet view (read-only)")
    ap.add_argument("--port", type=int, default=8792)
    ap.add_argument("--data", default="./data")
    ap.add_argument("--server", default="testnet.zec.rocks:443")
    ap.add_argument("--blocks", type=int, default=40)
    ap.add_argument("--txids", default=None, help="file with one txid per line, optional label after it")
    a = ap.parse_args()
    poller = Poller(a.server, Path(a.data).resolve(), a.txids, a.blocks)
    threading.Thread(target=poller.run, daemon=True, name="lwd-poller").start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(poller))
    print(f"zpulse netviz: http://127.0.0.1:{a.port}/  (lightwalletd={a.server}, data={poller.data})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
