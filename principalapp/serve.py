#!/usr/bin/env python3
"""zpulse principal app: a local web UI for the PRINCIPAL (the journalist). Stdlib only.

    python3 principalapp/serve.py [--data ./data] [--backend mock|testnet] [--port 8793]

Binds 127.0.0.1 only. Endpoints:
  GET  /                                  the page
  GET  /api/state                         all vaults: manifest summary, enrolls, pulses, watcher states
  GET  /api/vault?name=N                  the public vault.json (to send to watchers)
  GET  /api/packet?name=N&watcher_id=W    one sealed packet (send to that watcher only)
  POST /api/init    {name, secret_text | secret_b64, threshold, interval, grace, heartbeat_zat}
  POST /api/enroll  {name, enroll_json}
  POST /api/seal    {name}
  POST /api/pulse   {name, duress?: bool}
  POST /api/cancel  {name}
POSTs need header `X-Zpulse: 1` and `Content-Type: application/json`.
Errors are always {"ok": false, "error": "..."}.

Never served: principal.key, duress.key, .vaultkey, vault.bin, the plaintext secret.
Calls zpulse.principal in-process (stdout captured); falls back to the CLI if zpulse
cannot be imported in this interpreter.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

VAULT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
WATCHER_ID_RE = re.compile(r"^[a-z0-9_-]{1,16}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_BODY = 12 << 20          # an uploaded dossier arrives base64 in JSON
MAX_SECRET = 8 << 20
PULSE_KEEP = 10
WATCHER_KEYS = ("status", "last_seq", "last_kind", "last_hb_ts", "deadline_ts", "hb_count",
                "release_txid", "release_pending", "updated_ts")

try:  # in-process zpulse (preferred); the CLI is the fallback
    from zpulse import principal as zp
    HAVE_ZPULSE = True
except Exception:  # e.g. pynacl missing in this interpreter
    HAVE_ZPULSE = False


class ApiError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def parse_maybe_json(v, what: str) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError as e:
            raise ApiError(f"{what} is not valid JSON: {e}")
    if not isinstance(v, dict):
        raise ApiError(f"{what} must be a JSON object")
    return v


def last_json(text: str):
    for ln in reversed((text or "").strip().splitlines()):
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        if isinstance(j, dict):
            return j
    return None


def int_field(body: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = body.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be an integer")
    if not lo <= v <= hi:
        raise ApiError(f"{key} must be between {lo} and {hi}")
    return v


# ------------------------------------------------------------------ the app

class App:
    def __init__(self, data: Path, backend: str):
        self.data = data
        self.backend = backend
        self.lock = threading.Lock()   # one principal command at a time (stdout capture is global)
        (data / "vaults").mkdir(parents=True, exist_ok=True)

    def vdir(self, name) -> Path:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ApiError("vault name must match [A-Za-z0-9_-]{1,32}")
        return self.data / "vaults" / name

    def manifest(self, name) -> tuple[Path, dict]:
        d = self.vdir(name)
        m = read_json(d / "vault.json")
        if not isinstance(m, dict):
            raise ApiError(f"no vault named {name!r}", 404)
        return d, m

    # -- run a principal command ---------------------------------------------
    def run(self, cmd: str, **kw) -> dict:
        with self.lock:
            if HAVE_ZPULSE:
                out = self._inproc(cmd, kw)
            else:
                out = self._cli(cmd, kw)
        if not isinstance(out, dict):
            raise ApiError(f"{cmd} produced no result", 500)
        if not out.get("ok"):
            raise ApiError(str(out.get("error") or f"{cmd} failed"))
        return out

    def _inproc(self, cmd: str, kw: dict):
        ns = argparse.Namespace(backend=self.backend, data=str(self.data), **kw)
        fn = {"init": zp.cmd_init, "seal": zp.cmd_seal, "pulse": zp.cmd_pulse,
              "cancel": zp.cmd_cancel}[cmd]
        so, se = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                fn(ns)
        except SystemExit:
            pass
        except Exception as e:
            return {"ok": False, "error": f"{cmd}: {str(e)[:300]}"}
        return last_json(so.getvalue()) or {"ok": False, "error": se.getvalue().strip()[-300:]}

    def _cli(self, cmd: str, kw: dict):
        argv = [sys.executable, "-m", "zpulse.principal", cmd, "--name", kw["name"],
                "--backend", self.backend, "--data", str(self.data)]
        if cmd == "init":
            argv += ["--file", kw["file"], "--threshold", str(kw["threshold"]),
                     "--interval", str(kw["interval"]), "--grace", str(kw["grace"]),
                     "--heartbeat-zat", str(kw["heartbeat_zat"])]
        if cmd == "pulse" and kw.get("duress"):
            argv.append("--duress")
        env = dict(os.environ, PYTHONPATH=str(REPO))
        try:
            r = subprocess.run(argv, cwd=str(REPO), env=env, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            raise ApiError(f"{cmd} timed out", 504)
        return last_json(r.stdout) or {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}

    # -- pulses.json (append-only log of what this app sent) -------------------
    def log_pulse(self, d: Path, entry: dict) -> None:
        p = d / "pulses.json"
        lst = read_json(p)
        lst = lst if isinstance(lst, list) else []
        lst.append(entry)
        write_json(p, lst)

    # -- POST handlers -----------------------------------------------------------
    def init(self, body: dict) -> dict:
        name = body.get("name")
        d = self.vdir(name)
        if (d / "vault.json").exists():
            raise ApiError(f"vault {name!r} already exists; refusing to overwrite")
        threshold = int_field(body, "threshold", 2, 1, 255)
        interval = int_field(body, "interval", 120, 5, 365 * 86400)
        grace = int_field(body, "grace", 30, 0, 365 * 86400)
        hb = int_field(body, "heartbeat_zat", 20000, 1, 10 ** 9)
        if isinstance(body.get("secret_b64"), str) and body["secret_b64"]:
            try:
                secret = base64.b64decode(body["secret_b64"], validate=True)
            except ValueError:
                raise ApiError("secret_b64 is not valid base64")
        elif isinstance(body.get("secret_text"), str) and body["secret_text"].strip():
            secret = body["secret_text"].encode("utf-8")
        else:
            raise ApiError("give the secret as text or a file")
        if len(secret) > MAX_SECRET:
            raise ApiError("secret is larger than 8 MiB")
        fd, tmp = tempfile.mkstemp(prefix=".zpulse-secret-", dir=str(self.data))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(secret)
            out = self.run("init", name=name, file=tmp, threshold=threshold, interval=interval,
                           grace=grace, heartbeat_zat=hb)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        _, m = self.manifest(name)
        return {"ok": True, "name": name, "vault_id": out.get("vault_id"),
                "vault_path": str(d / "vault.json"), "vault": m}

    def enroll(self, body: dict) -> dict:
        d, m = self.manifest(body.get("name"))
        e = parse_maybe_json(body.get("enroll_json"), "enroll_json")
        wid = e.get("watcher_id")
        if not isinstance(wid, str) or not WATCHER_ID_RE.match(wid):
            raise ApiError("watcher_id missing or not [a-z0-9_-]{1,16}")
        if e.get("vault_id") != m["vault_id"]:
            raise ApiError(f"this enroll file is for vault {e.get('vault_id')!r}, not {m['vault_id']}")
        if not isinstance(e.get("vk"), str) or not isinstance(e.get("address"), str):
            raise ApiError("enroll file is missing vk or address")
        clean = {"watcher_id": wid, "vault_id": m["vault_id"], "vk": e["vk"], "address": e["address"]}
        path = d / "enroll" / f"{wid}.json"
        with self.lock:
            old = read_json(path)
            if isinstance(old, dict) and old.get("vk") == clean["vk"] and old.get("address") == clean["address"]:
                return {"ok": True, "already": True, "watcher_id": wid}
            if m.get("sealed_ts"):
                raise ApiError("vault is already sealed; enrollment closed")
            write_json(path, clean)
        return {"ok": True, "already": False, "watcher_id": wid}

    def seal(self, body: dict) -> dict:
        name = body.get("name")
        self.manifest(name)
        out = self.run("seal", name=name)
        return {"ok": True, "name": name, "threshold": out.get("threshold"), "n": out.get("n"),
                "packets": out.get("packets")}

    def pulse(self, body: dict) -> dict:
        name = body.get("name")
        d, _ = self.manifest(name)
        duress = body.get("duress") is True
        out = self.run("pulse", name=name, duress=duress)
        entry = {"kind": "duress" if duress else "pulse", "txid": out["txid"], "seq": out["seq"],
                 "ts": out["ts"], "duress": duress}
        self.log_pulse(d, entry)
        return {"ok": True, "name": name, **entry}

    def cancel(self, body: dict) -> dict:
        name = body.get("name")
        d, _ = self.manifest(name)
        out = self.run("cancel", name=name)
        entry = {"kind": "cancel", "txid": out["txid"], "seq": None, "ts": out["ts"], "duress": False}
        self.log_pulse(d, entry)
        return {"ok": True, "name": name, **entry}

    # -- GET handlers ---------------------------------------------------------------
    def vault_json(self, name) -> dict:
        d, m = self.manifest(name)
        return {"ok": True, "name": name, "path": str(d / "vault.json"), "vault": m}

    def packet(self, name, wid) -> dict:
        d, m = self.manifest(name)
        if not isinstance(wid, str) or not WATCHER_ID_RE.match(wid):
            raise ApiError("bad watcher_id")
        p = read_json(d / "packets" / f"{wid}.json")
        if not isinstance(p, dict):
            raise ApiError(f"no packet for {wid}", 404)
        return {"ok": True, "watcher_id": wid, "path": str(d / "packets" / f"{wid}.json"), "packet": p}

    def vault_info(self, d: Path) -> dict | None:
        m = read_json(d / "vault.json")
        if not isinstance(m, dict) or not VAULT_ID_RE.match(str(m.get("vault_id", ""))):
            return None
        vid = m["vault_id"]
        try:
            seq = int((d / "seq").read_text().strip() or 0)
        except (OSError, ValueError):
            seq = 0
        enrolls = []
        for f in sorted((d / "enroll").glob("*.json")):
            e = read_json(f)
            if isinstance(e, dict) and WATCHER_ID_RE.match(str(e.get("watcher_id", ""))):
                enrolls.append({"watcher_id": e["watcher_id"], "address": e.get("address"),
                                "vk": e.get("vk"), "ok": e.get("vault_id", vid) == vid})
        packets = sorted(f.stem for f in (d / "packets").glob("*.json") if WATCHER_ID_RE.match(f.stem))
        pulses = read_json(d / "pulses.json")
        pulses = pulses if isinstance(pulses, list) else []
        last_pulse = next((p for p in reversed(pulses) if p.get("kind") in ("pulse", "duress")), None)
        cancelled = next((p for p in reversed(pulses) if p.get("kind") == "cancel"), None)
        watchers = {}
        for wid in sorted(set((m.get("watchers") or {}).keys()) | {e["watcher_id"] for e in enrolls}):
            st = read_json(self.data / "watchers" / wid / f"{vid}.json")
            if isinstance(st, dict):
                watchers[wid] = {k: st.get(k) for k in WATCHER_KEYS}
        return {
            "name": m.get("name") or d.name, "dir": d.name, "vault_id": vid,
            "threshold": m.get("threshold"), "n": m.get("n"),
            "interval_s": m.get("interval_s"), "grace_s": m.get("grace_s"),
            "heartbeat_zat": m.get("heartbeat_zat"), "created_ts": m.get("created_ts"),
            "sealed_ts": m.get("sealed_ts"), "release_address": m.get("release_address"),
            "principal_vk": m.get("principal_vk"), "seq": seq,
            "enrolls": enrolls, "packets": packets,
            "pulses": pulses[-PULSE_KEEP:], "last_pulse": last_pulse,
            "cancelled": cancelled, "watchers": watchers,
            "vault_path": str(d / "vault.json"),
        }

    def snapshot(self) -> dict:
        vaults = []
        for d in sorted((self.data / "vaults").iterdir()):
            if d.is_dir() and NAME_RE.match(d.name):
                v = self.vault_info(d)
                if v:
                    vaults.append(v)
        vaults.sort(key=lambda v: v.get("created_ts") or 0, reverse=True)
        return {"ok": True, "now": int(time.time()), "backend": self.backend,
                "data": str(self.data), "inproc": HAVE_ZPULSE, "vaults": vaults}


# ------------------------------------------------------------------ HTTP

def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    page = HERE / "index.html"

    class H(BaseHTTPRequestHandler):
        server_version = "zpulse-principal"
        sys_version = ""

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:  # DNS-rebinding guard
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "bad Host header"})
            u = urlparse(self.path)
            q = parse_qs(u.query)
            g = lambda k: (q.get(k) or [""])[0]
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/state":
                    return self._json(200, app.snapshot())
                if u.path == "/api/vault":
                    return self._json(200, app.vault_json(g("name")))
                if u.path == "/api/packet":
                    return self._json(200, app.packet(g("name"), g("watcher_id")))
                return self._json(404, {"ok": False, "error": "not found"})
            except ApiError as e:
                return self._json(e.code, {"ok": False, "error": str(e)})
            except Exception as e:
                return self._json(500, {"ok": False, "error": f"internal error: {str(e)[:200]}"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "bad Host header"})
            if self.headers.get("X-Zpulse") != "1":
                return self._json(403, {"ok": False, "error": "missing X-Zpulse: 1 header"})
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._json(415, {"ok": False, "error": "Content-Type must be application/json"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._json(413, {"ok": False, "error": "body too large or bad Content-Length"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ApiError("body must be a JSON object")
                route = {"/api/init": app.init, "/api/enroll": app.enroll, "/api/seal": app.seal,
                         "/api/pulse": app.pulse, "/api/cancel": app.cancel}.get(urlparse(self.path).path)
                if route is None:
                    return self._json(404, {"ok": False, "error": "not found"})
                return self._json(200, route(body))
            except ApiError as e:
                return self._json(e.code, {"ok": False, "error": str(e)})
            except ValueError as e:
                return self._json(400, {"ok": False, "error": f"bad JSON body: {e}"})
            except Exception as e:
                return self._json(500, {"ok": False, "error": f"internal error: {str(e)[:200]}"})

        def do_PUT(self):
            self._json(405, {"ok": False, "error": "method not allowed"})

        do_DELETE = do_PATCH = do_PUT

        def log_message(self, *a):
            pass

    return H


def main() -> None:
    ap = argparse.ArgumentParser(description="zpulse principal app")
    ap.add_argument("--data", default="./data")
    ap.add_argument("--backend", choices=["mock", "testnet"],
                    default=os.environ.get("ZPULSE_BACKEND", "mock"))
    ap.add_argument("--port", type=int, default=8793)
    a = ap.parse_args()
    data = Path(a.data).resolve()
    app = App(data, a.backend)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app, a.port))
    srv.daemon_threads = True
    print(f"zpulse principal: http://127.0.0.1:{a.port}/  (backend={a.backend}, data={data})", flush=True)
    if not HAVE_ZPULSE:
        print("warning: zpulse not importable here; falling back to the CLI", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
