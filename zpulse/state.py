"""zpulse.state: data-dir layout, atomic JSON helpers, shared CLI plumbing."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import chain

LOG_KEEP = 50  # log lines kept per watcher state file


# ------------------------------------------------------------------ paths

class Paths:
    def __init__(self, data: str | Path = "data"):
        self.data = Path(data)

    @property
    def ledger(self) -> Path:
        env = os.environ.get("ZPULSE_MOCK_LEDGER")  # same override the dashboard honours
        return Path(env) if env else self.data / "mockchain.json"

    def vault_dir(self, name: str) -> Path:
        return self.data / "vaults" / name

    def watcher_dir(self, wid: str) -> Path:
        return self.data / "watchers" / wid

    def watcher_identity(self, wid: str) -> Path:
        return self.watcher_dir(wid) / "identity.key"

    def watcher_state(self, wid: str, vault_id: str) -> Path:
        return self.watcher_dir(wid) / f"{vault_id}.json"

    @property
    def revealed(self) -> Path:
        return self.data / "revealed"


# ------------------------------------------------------------------ json / time

def load_json(path: str | Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save_json(path: str | Path, obj, mode: int = 0o644) -> None:
    """Atomic write (tmp + rename) so the dashboard never reads half a file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, p)


def now() -> int:
    return int(time.time())


def utc(ts: int | None = None) -> str:
    return time.strftime("%H:%M:%SZ", time.gmtime(now() if ts is None else ts))


def add_log(state: dict, msg: str) -> str:
    line = f"{utc()} {msg}"
    state.setdefault("log", []).append(line)
    del state["log"][:-LOG_KEEP]
    return line


# ------------------------------------------------------------------ CLI plumbing

def common_args(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--backend", choices=["mock", "testnet"],
                   default=os.environ.get("ZPULSE_BACKEND", "mock"))
    p.add_argument("--data", default="data", help="data directory (default ./data)")
    return p


def say(msg: str) -> None:
    """Human progress line -> stderr."""
    print(msg, file=sys.stderr, flush=True)


def emit(obj: dict) -> None:
    """Machine-readable result -> stdout (one JSON object)."""
    print(json.dumps(obj), flush=True)


def fail(msg: str, code: int = 1):
    say(f"error: {msg}")
    emit({"ok": False, "error": msg})
    sys.exit(code)


def open_node(backend: str, node: str, paths: Paths) -> chain.Chain:
    return chain.open_chain(backend, node, ledger=paths.ledger if backend == "mock" else None)


def zwallet(node: str):
    """testnet only: the ZWallet object for a node (wallet may not exist yet)."""
    return chain._zwallet_module().ZWallet(node)
