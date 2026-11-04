#!/usr/bin/env python3
"""zpulse dashboard server (stdlib only).

    python3 dashboard/serve.py --data ./data --port 8787

GET /       -> dashboard/index.html
GET /state  -> JSON snapshot of everything under --data (read-only).
Tolerates missing / half-written files: anything unreadable is skipped.
"""
import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET_NAMES = {"principal.key", "duress.key", "identity.key", ".vaultkey"}  # never read


def read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def collect_vaults(data: Path):
    out = []
    vdir = data / "vaults"
    if not vdir.is_dir():
        return out
    for d in sorted(p for p in vdir.iterdir() if p.is_dir()):
        man = read_json(d / "vault.json")
        if not isinstance(man, dict):
            continue
        man = dict(man)
        man["dir_name"] = d.name
        seq = (read_text(d / "seq") or "").strip()
        man["seq"] = int(seq) if seq.isdigit() else None
        enroll = d / "enroll"
        man["enrolled"] = sorted(p.stem for p in enroll.glob("*.json")) if enroll.is_dir() else []
        man["sealed"] = bool(man.get("watchers"))
        out.append(man)
    return out


def collect_watchers(data: Path):
    out = []
    wdir = data / "watchers"
    if not wdir.is_dir():
        return out
    for d in sorted(p for p in wdir.iterdir() if p.is_dir()):
        for f in sorted(d.glob("*.json")):
            if f.name in SECRET_NAMES:
                continue
            st = read_json(f)
            if not isinstance(st, dict) or "vault_id" not in st:
                continue
            st = dict(st)
            st.setdefault("watcher_id", d.name)
            log = st.get("log")
            if isinstance(log, list):
                st["log"] = log[-5:]
            out.append(st)
    return out


def collect_revealed(data: Path):
    out = []
    rdir = data / "revealed"
    if not rdir.is_dir():
        return out
    for f in sorted(rdir.glob("*.txt")):
        meta = read_json(f.with_suffix(".json")) or {}
        text = read_text(f) or ""
        out.append({
            "vault_id": meta.get("vault_id", f.stem),
            "text": text[:20000],
            "shares_seen": meta.get("shares_seen"),
            "txids": meta.get("txids") or [],
            "revealed_ts": meta.get("revealed_ts") or int(f.stat().st_mtime),
        })
    return out


def collect_ledger(data: Path):
    path = Path(os.environ.get("ZPULSE_MOCK_LEDGER") or data / "mockchain.json")
    led = read_json(path)
    if led is None:
        return None
    txs = led if isinstance(led, list) else (led.get("txs") or led.get("ledger") or []) if isinstance(led, dict) else []
    tail = []
    for i, tx in enumerate(txs):
        if not isinstance(tx, dict):
            continue
        outs = tx.get("outputs")
        if not isinstance(outs, list):  # flat Tx rows
            outs = [{"to": tx.get("to"), "amount_zat": tx.get("amount_zat"), "memo": tx.get("memo")}]
        tail.append({
            "txid": tx.get("txid", ""),
            "height": tx.get("height", i + 1),
            "time": tx.get("time"),
            "from": tx.get("from") or tx.get("sender"),
            "outputs": [{"to": o.get("to") or o.get("address"), "amount_zat": o.get("amount_zat") or o.get("zat"),
                         "memo": (o.get("memo") or "")[:120]} for o in outs if isinstance(o, dict)],
        })
    return tail[-20:]


def snapshot(data: Path):
    vaults = collect_vaults(data)
    watchers = collect_watchers(data)
    revealed = collect_revealed(data)
    ledger = collect_ledger(data)
    backend = "mock" if ledger is not None else ("testnet" if any(v.get("release_ufvk") for v in vaults) else "unknown")
    # testnet: txids we know about (watcher releases + reveal) for the chain ticker
    known = []
    for w in watchers:
        if w.get("release_txid"):
            known.append({"txid": w["release_txid"], "time": w.get("updated_ts"),
                          "label": f"SH {w.get('watcher_id')} -> release"})
    for r in revealed:
        for t in r["txids"]:
            if t not in {k["txid"] for k in known}:
                known.append({"txid": t, "time": r["revealed_ts"], "label": "share seen by reveal"})
    known.sort(key=lambda k: k.get("time") or 0)
    return {
        "now": int(time.time()),
        "backend": backend,
        "vaults": vaults,
        "watchers": watchers,
        "revealed": revealed,
        "mock_ledger_tail": ledger or [],
        "known_txids": known[-20:],
    }


def make_handler(data: Path):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body = (HERE / "index.html").read_bytes()
                self._send(200, body, "text/html; charset=utf-8")
            elif path == "/state":
                try:
                    body = json.dumps(snapshot(data)).encode()
                    self._send(200, body, "application/json")
                except Exception as e:  # never crash the projector
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser(description="zpulse dashboard")
    ap.add_argument("--data", default="./data")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    data = Path(a.data).resolve()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(data))
    print(f"zpulse dashboard: http://{a.host}:{a.port}/  (data={data})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
