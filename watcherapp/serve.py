#!/usr/bin/env python3
"""zpulse watcher app: a local web UI for ONE watcher (stdlib only).

    python3 watcherapp/serve.py --id w1 [--data ./data] [--backend mock|testnet] [--port 8791]

Binds 127.0.0.1 only. Endpoints:
  GET  /                      the page
  GET  /api/state             identity, wallet, vaults, run status (no secrets)
  GET  /api/log?vault_id=...  tail of data/watchers/<id>/logs/<vault_id>.log
  POST /api/enroll  {vault_json}
  POST /api/packet  {packet_json}
  POST /api/run     {vault_id, action: start|stop|once, poll}
POSTs need header `X-Zpulse: 1` and `Content-Type: application/json`.
Errors are always {"ok": false, "error": "..."}.

Never served: identity.key's secret, any *.key file, a packet's share_sealed,
the state file's share_sealed. Watcher daemons are started in their own session,
so they keep running if this server stops; their pids live in logs/<vault_id>.pid
and are re-adopted on the next start.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
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
MAX_BODY = 1 << 20          # 1 MiB is plenty for a vault.json or packet
LOG_TAIL = 200
STATE_DROP = {"share_sealed", "seen_txids"}  # never leave the machine over HTTP

try:  # in-process zpulse (preferred); the CLI is the fallback
    from zpulse import watcher as zw
    from zpulse.state import Paths, open_node, save_json as zp_save_json
    HAVE_ZPULSE = True
except Exception:  # e.g. pynacl missing in this interpreter
    HAVE_ZPULSE = False


class ApiError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------ small helpers

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


def tail_lines(path: Path, n: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def parse_maybe_json(v, what: str) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError as e:
            raise ApiError(f"{what} is not valid JSON: {e}")
    if not isinstance(v, dict):
        raise ApiError(f"{what} must be a JSON object")
    return v


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_cmdline(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


# ------------------------------------------------------------------ the app

class App:
    def __init__(self, wid: str, data: Path, backend: str):
        self.wid = wid
        self.data = data
        self.backend = backend
        self.node = f"watcher-{wid}"
        self.wdir = data / "watchers" / wid
        self.lock = threading.Lock()          # guards procs / file writes from handlers
        self.procs: dict[str, subprocess.Popen] = {}
        self.adopted: dict[str, int] = {}     # vault_id -> pid from a previous server run
        self.polls: dict[str, float] = {}
        self.wallet = {"state": "pending", "balance_zat": None, "address": None,
                       "error": None, "updated_ts": None}
        for d in ("vaults", "packets", "logs"):
            (self.wdir / d).mkdir(parents=True, exist_ok=True)
        self._adopt()
        threading.Thread(target=self._wallet_loop, daemon=True).start()

    # -- paths ------------------------------------------------------------
    def vault_path(self, vid): return self.wdir / "vaults" / vid / "vault.json"
    def enroll_path(self, vid): return self.wdir / "vaults" / vid / "enroll" / f"{self.wid}.json"
    def packet_path(self, vid): return self.wdir / "packets" / f"{vid}.json"
    def state_path(self, vid): return self.wdir / f"{vid}.json"
    def log_path(self, vid): return self.wdir / "logs" / f"{vid}.log"
    def pid_path(self, vid): return self.wdir / "logs" / f"{vid}.pid"

    def env(self) -> dict:
        e = dict(os.environ)
        e["ZPULSE_BACKEND"] = self.backend
        e["PYTHONUNBUFFERED"] = "1"
        e["PYTHONPATH"] = str(REPO) + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
        return e

    def cli(self, *rest: str) -> list[str]:
        return [sys.executable, "-m", "zpulse.watcher", *rest,
                "--backend", self.backend, "--data", str(self.data)]

    # -- identity / wallet --------------------------------------------------
    def verify_key(self) -> str | None:
        d = read_json(self.wdir / "identity.key")  # only the public half leaves this function
        return d.get("vk") if isinstance(d, dict) and isinstance(d.get("vk"), str) else None

    def _wallet_loop(self) -> None:
        while True:
            try:
                self._wallet_refresh()
            except Exception as e:
                self.wallet.update(state="error", error=str(e)[:200], updated_ts=int(time.time()))
            time.sleep(15 if self.backend == "mock" else 60)

    def _wallet_refresh(self) -> None:
        if not HAVE_ZPULSE:
            self.wallet.update(state="unavailable", error="zpulse not importable in this Python")
            return
        if self.backend == "mock":
            n = open_node("mock", self.node, Paths(self.data))
            self.wallet.update(state="ok", balance_zat=n.balance_zat(), address=f"mock:{self.node}:*",
                               error=None, updated_ts=int(time.time()))
            return
        # testnet: local wallet db only (balance / addresses do not touch the network)
        from zpulse.state import zwallet
        w = zwallet(self.node)
        if not w.exists():
            self.wallet.update(state="missing", error="no testnet wallet yet; enroll creates it",
                               updated_ts=int(time.time()))
            return
        addr = w.addresses().get("unified")
        bal = w.balance()
        self.wallet.update(state="ok", address=addr, balance_zat=bal.get("shielded_zat"),
                           transparent_zat=bal.get("transparent_zat"),
                           pending_zat=bal.get("pending_zat"),
                           chain_tip=bal.get("chain_tip_height"), error=None,
                           updated_ts=int(time.time()))

    # -- processes ------------------------------------------------------------
    def _adopt(self) -> None:
        for pf in (self.wdir / "logs").glob("*.pid"):
            vid = pf.stem
            try:
                pid = int(pf.read_text().strip())
            except (OSError, ValueError):
                pf.unlink(missing_ok=True)
                continue
            cmd = pid_cmdline(pid) if pid_alive(pid) else ""
            if "zpulse.watcher" in cmd and "run" in cmd and str(self.packet_path(vid)) in cmd:
                self.adopted[vid] = pid
            else:
                pf.unlink(missing_ok=True)

    def running_pid(self, vid: str) -> int | None:
        p = self.procs.get(vid)
        if p is not None:
            if p.poll() is None:
                return p.pid
            self.procs.pop(vid, None)
            self.pid_path(vid).unlink(missing_ok=True)
            with open(self.log_path(vid), "a") as f:
                f.write(f"[app] watcher process exited with code {p.returncode}\n")
        pid = self.adopted.get(vid)
        if pid is not None:
            if pid_alive(pid):
                return pid
            self.adopted.pop(vid, None)
            self.pid_path(vid).unlink(missing_ok=True)
        return None

    def start(self, vid: str, poll: float) -> int:
        if self.running_pid(vid):
            raise ApiError("watcher is already running for this vault")
        if not self.packet_path(vid).exists():
            raise ApiError("no packet loaded for this vault yet")
        if self.verify_key() is None:
            raise ApiError("no identity yet; enroll first")
        log = open(self.log_path(vid), "a", buffering=1)
        log.write(f"[app] {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} start poll={poll}s\n")
        argv = self.cli("run", "--id", self.wid, "--packet", str(self.packet_path(vid)),
                        "--poll", f"{poll:g}")
        p = subprocess.Popen(argv, cwd=str(REPO), env=self.env(), stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()  # the child holds its own fd
        self.procs[vid] = p
        self.polls[vid] = poll
        self.pid_path(vid).write_text(str(p.pid))
        time.sleep(0.4)  # catch an immediate failure (bad packet, wrong identity)
        if p.poll() is not None:
            self.procs.pop(vid, None)
            self.pid_path(vid).unlink(missing_ok=True)
            err = next((ln for ln in reversed(tail_lines(self.log_path(vid), 20))
                        if ln.startswith("error:")), f"exited with code {p.returncode}")
            raise ApiError(f"watcher failed to start: {err}")
        return p.pid

    def stop(self, vid: str) -> None:
        pid = self.running_pid(vid)
        if not pid:
            raise ApiError("watcher is not running")
        p = self.procs.get(vid)
        try:
            os.kill(pid, signal.SIGINT)  # clean exit: the watcher prints its summary
        except ProcessLookupError:
            pass
        deadline = time.time() + 5
        while time.time() < deadline:
            if (p.poll() is not None) if p else not pid_alive(pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if p:
                p.wait(5)
        self.procs.pop(vid, None)
        self.adopted.pop(vid, None)
        self.pid_path(vid).unlink(missing_ok=True)
        with open(self.log_path(vid), "a") as f:
            f.write(f"[app] {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} stopped\n")

    def once(self, vid: str) -> dict:
        if self.running_pid(vid):
            raise ApiError("the watcher is running; it already checks every poll. Stop it to run a single check.")
        if not self.packet_path(vid).exists():
            raise ApiError("no packet loaded for this vault yet")
        argv = self.cli("run", "--id", self.wid, "--packet", str(self.packet_path(vid)), "--once")
        try:
            r = subprocess.run(argv, cwd=str(REPO), env=self.env(), capture_output=True,
                               text=True, timeout=900)
        except subprocess.TimeoutExpired:
            raise ApiError("check timed out", 504)
        with open(self.log_path(vid), "a") as f:
            f.write(f"[app] {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} check once\n")
            f.write(r.stderr)
            f.write(r.stdout)
        out = None
        for ln in reversed(r.stdout.strip().splitlines()):
            try:
                out = json.loads(ln)
                break
            except ValueError:
                continue
        if not isinstance(out, dict):
            raise ApiError(f"check failed: {(r.stderr or r.stdout).strip()[-300:]}", 500)
        if not out.get("ok"):
            raise ApiError(str(out.get("error") or "check failed"))
        return {k: v for k, v in out.items() if k not in STATE_DROP}

    # -- enroll ---------------------------------------------------------------
    def enroll(self, body: dict) -> dict:
        m = parse_maybe_json(body.get("vault_json"), "vault_json")
        vid = m.get("vault_id")
        if not isinstance(vid, str) or not VAULT_ID_RE.match(vid):
            raise ApiError("this does not look like a zpulse vault.json (vault_id missing or malformed)")
        for k in ("name", "threshold", "interval_s", "grace_s", "principal_vk", "release_address"):
            if k not in m:
                raise ApiError(f"vault.json is missing {k!r}")
        existing = read_json(self.enroll_path(vid))
        if isinstance(existing, dict) and existing.get("vk") == self.verify_key():
            return {"ok": True, "already": True, "enroll": existing, "vault_id": vid,
                    "name": m.get("name")}
        if m.get("sealed_ts"):
            raise ApiError("vault is already sealed; enrollment closed")
        with self.lock:
            write_json(self.vault_path(vid), m)
            if HAVE_ZPULSE:
                enroll = self._enroll_inproc(vid, m)
            else:
                enroll = self._enroll_cli(vid)
        return {"ok": True, "already": False, "enroll": enroll, "vault_id": vid, "name": m.get("name")}

    def _enroll_inproc(self, vid: str, m: dict) -> dict:
        paths = Paths(self.data)
        try:
            ident = zw.load_identity(paths, self.wid, create=True)
        except SystemExit:
            raise ApiError("could not load or create the watcher identity", 500)
        if self.backend == "testnet":
            from zpulse.state import zwallet
            w = zwallet(self.node)
            if not w.exists():
                w.init()  # network; this request blocks until the wallet exists
        address = open_node(self.backend, self.node, paths).new_address(vid)
        enroll = {"watcher_id": self.wid, "vault_id": vid, "vk": ident.verify_key_b64,
                  "address": address}
        zp_save_json(self.enroll_path(vid), enroll)
        return enroll

    def _enroll_cli(self, vid: str) -> dict:
        r = subprocess.run(self.cli("enroll", "--id", self.wid, "--vault", str(self.vault_path(vid))),
                           cwd=str(REPO), env=self.env(), capture_output=True, text=True, timeout=1800)
        enroll = read_json(self.enroll_path(vid))
        if r.returncode != 0 or not isinstance(enroll, dict):
            raise ApiError(f"enroll failed: {(r.stderr or r.stdout).strip()[-300:]}", 500)
        return enroll

    # -- packet ---------------------------------------------------------------
    def packet(self, body: dict) -> dict:
        p = parse_maybe_json(body.get("packet_json"), "packet_json")
        vid = p.get("vault_id")
        if not isinstance(vid, str) or not VAULT_ID_RE.match(vid):
            raise ApiError("this does not look like a zpulse packet (vault_id missing or malformed)")
        if not isinstance(p.get("share_sealed"), str) or not isinstance(p.get("manifest"), dict):
            raise ApiError("packet is missing share_sealed or manifest")
        if p.get("watcher_id") != self.wid:
            raise ApiError(f"this packet is for watcher {p.get('watcher_id')!r}, not {self.wid!r}")
        man = p["manifest"]
        if man.get("vault_id") != vid:
            raise ApiError("packet manifest is for a different vault")
        vk = self.verify_key()
        if vk is None:
            raise ApiError("no identity yet; enroll in this vault first")
        mine = (man.get("watchers") or {}).get(self.wid) or {}
        if mine.get("vk") != vk:
            raise ApiError("packet was sealed to a different key than this watcher's identity")
        with self.lock:
            write_json(self.packet_path(vid), p, mode=0o600)  # holds the sealed share
            if not self.vault_path(vid).exists():
                write_json(self.vault_path(vid), man)
        return {"ok": True, "vault_id": vid, "name": man.get("name"),
                "threshold": man.get("threshold"), "n": man.get("n")}

    # -- state ------------------------------------------------------------------
    def vault_ids(self) -> list[str]:
        ids = set()
        for d in (self.wdir / "vaults").iterdir():
            if d.is_dir() and VAULT_ID_RE.match(d.name):
                ids.add(d.name)
        for f in (self.wdir / "packets").glob("*.json"):
            if VAULT_ID_RE.match(f.stem):
                ids.add(f.stem)
        for f in self.wdir.glob("*.json"):
            if VAULT_ID_RE.match(f.stem):
                ids.add(f.stem)
        return sorted(ids)

    def vault_info(self, vid: str) -> dict:
        packet = read_json(self.packet_path(vid))
        man = (packet or {}).get("manifest") if isinstance(packet, dict) else None
        if not isinstance(man, dict):
            man = read_json(self.vault_path(vid)) or {}
        st = read_json(self.state_path(vid))
        if isinstance(st, dict):
            st = {k: v for k, v in st.items() if k not in STATE_DROP}
            st["log"] = (st.get("log") or [])[-20:]
        else:
            st = None
        enroll = read_json(self.enroll_path(vid))
        pid = self.running_pid(vid)
        return {
            "vault_id": vid,
            "name": man.get("name"),
            "threshold": man.get("threshold"),
            "n": man.get("n") or len(man.get("watchers") or {}) or None,
            "interval_s": man.get("interval_s"),
            "grace_s": man.get("grace_s"),
            "sealed_ts": man.get("sealed_ts"),
            "release_address": man.get("release_address"),
            "watchers": sorted((man.get("watchers") or {}).keys()),
            "enroll": enroll if isinstance(enroll, dict) else None,
            "has_packet": isinstance(packet, dict),
            "state": st,
            "running": bool(pid),
            "pid": pid,
            "poll": self.polls.get(vid),
        }

    def snapshot(self) -> dict:
        with self.lock:
            vaults = [self.vault_info(v) for v in self.vault_ids()]
        return {
            "ok": True,
            "now": int(time.time()),
            "identity": {"watcher_id": self.wid, "vk": self.verify_key(), "node": self.node,
                         "backend": self.backend, "data": str(self.data),
                         "inproc": HAVE_ZPULSE},
            "wallet": dict(self.wallet),
            "vaults": vaults,
        }

    def log(self, vid: str) -> dict:
        if not isinstance(vid, str) or not VAULT_ID_RE.match(vid):
            raise ApiError("bad vault_id")
        lines = tail_lines(self.log_path(vid), LOG_TAIL)
        source = "process"
        if not lines:
            st = read_json(self.state_path(vid))
            lines = (st or {}).get("log") or [] if isinstance(st, dict) else []
            source = "state"
        return {"ok": True, "vault_id": vid, "lines": lines, "source": source}

    def run(self, body: dict) -> dict:
        vid = body.get("vault_id")
        if not isinstance(vid, str) or not VAULT_ID_RE.match(vid):
            raise ApiError("bad vault_id")
        action = body.get("action")
        if action == "start":
            try:
                poll = float(body.get("poll") or 5)
            except (TypeError, ValueError):
                raise ApiError("poll must be a number")
            if not 0.5 <= poll <= 3600:
                raise ApiError("poll must be between 0.5 and 3600 seconds")
            with self.lock:
                pid = self.start(vid, poll)
            return {"ok": True, "vault_id": vid, "running": True, "pid": pid}
        if action == "stop":
            with self.lock:
                self.stop(vid)
            return {"ok": True, "vault_id": vid, "running": False}
        if action == "once":
            return {"ok": True, "vault_id": vid, "summary": self.once(vid)}
        raise ApiError("action must be start, stop or once")


# ------------------------------------------------------------------ HTTP

def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    page = HERE / "index.html"

    class H(BaseHTTPRequestHandler):
        server_version = "zpulse-watcher"
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
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/state":
                    return self._json(200, app.snapshot())
                if u.path == "/api/log":
                    vid = (parse_qs(u.query).get("vault_id") or [""])[0]
                    return self._json(200, app.log(vid))
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
                route = {"/api/enroll": app.enroll, "/api/packet": app.packet,
                         "/api/run": app.run}.get(urlparse(self.path).path)
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
    ap = argparse.ArgumentParser(description="zpulse watcher app")
    ap.add_argument("--id", required=True, help="watcher id, [a-z0-9_-]{1,16}")
    ap.add_argument("--data", default="./data")
    ap.add_argument("--backend", choices=["mock", "testnet"],
                    default=os.environ.get("ZPULSE_BACKEND", "mock"))
    ap.add_argument("--port", type=int, default=8791)
    a = ap.parse_args()
    if not WATCHER_ID_RE.match(a.id):
        ap.error("--id must match [a-z0-9_-]{1,16}")
    data = Path(a.data).resolve()
    # A shell that backgrounds us sets SIGINT to SIG_IGN, and children inherit that, so the
    # watcher would ignore our clean-stop SIGINT. Installing a handler resets it on exec.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    app = App(a.id, data, a.backend)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app, a.port))
    srv.daemon_threads = True
    print(f"zpulse watcher {a.id}: http://127.0.0.1:{a.port}/  (backend={a.backend}, data={data})",
          flush=True)
    if not HAVE_ZPULSE:
        print("warning: zpulse not importable here; enroll falls back to the CLI", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        running = [v for v in app.vault_ids() if app.running_pid(v)]
        if running:
            print(f"note: watcher daemons still running for {', '.join(running)} "
                  f"(they are re-adopted on next start; stop them from the app)", flush=True)


if __name__ == "__main__":
    main()
