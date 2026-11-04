#!/usr/bin/env python3
"""Request testnet TAZ from the Valar faucet (0.125 TAZ per IP per 24h).
usage: python3 wallet/faucet.py <testnet address>
"""
import json, sys, time, urllib.request

BASE = "https://faucet.testnet.valargroup.dev"

def _req(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method="POST" if body else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": "zhackathon/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def claim(address: str) -> dict:
    res = _req("/api/claim", {"address": address})
    print("claim:", json.dumps(res))
    cid = res.get("id")
    if not cid:
        return res
    for _ in range(60):
        time.sleep(5)
        st = _req(f"/api/claim/{cid}")
        print("status:", json.dumps(st))
        if st.get("status") in ("sent", "done", "complete", "confirmed", "failed", "error") or st.get("txid"):
            return st
    return st

if __name__ == "__main__":
    print(json.dumps(_req("/api/status"), indent=1))
    if len(sys.argv) > 1:
        claim(sys.argv[1])
