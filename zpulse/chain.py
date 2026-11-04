"""zpulse.chain: the only place zpulse touches a blockchain.

Two backends behind one small `Chain` protocol:

- MockChain: a JSON ledger file shared by every node on this machine, guarded
  by fcntl.flock so many watcher/principal processes can use it concurrently.
  Addresses look like `mock:<node>:<label>:<rand6>`; a node "receives" every
  output whose address starts with `mock:<node>:`. height = number of txs.
- DevtoolChain: Zcash testnet via wallet/zwallet.py (zcash-devtool).
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol, TypedDict

MAX_MEMO_BYTES = 512
MOCK_START_ZAT = 10 * 100_000_000  # every mock node starts with 10 (fake) ZEC
WALLET_DIR = Path(__file__).resolve().parents[1] / "wallet"


class Tx(TypedDict):
    txid: str
    height: int
    time: int
    to: str
    amount_zat: int
    memo: str | None


Output = tuple[str, int, "str | None"]


class Chain(Protocol):
    name: str

    def sync(self) -> None: ...
    def height(self) -> int: ...
    def new_address(self, label: str) -> str: ...
    def send(self, outputs: list[Output]) -> str: ...
    def received(self, min_height: int = 0) -> list[Tx]: ...
    def balance_zat(self) -> int: ...


def _check_outputs(outputs: list[Output]) -> None:
    if not outputs:
        raise ValueError("send() needs at least one output")
    for addr, zat, memo in outputs:
        if int(zat) <= 0:
            raise ValueError(f"non-positive amount {zat} to {addr}")
        if memo is not None:
            assert len(memo.encode()) <= MAX_MEMO_BYTES, "memo exceeds 512 bytes"


# --------------------------------------------------------------------------- mock

def default_ledger_path() -> Path:
    return Path(os.environ.get("ZPULSE_MOCK_LEDGER", "data/mockchain.json"))


class MockChain:
    """Offline, multi-process-safe fake chain. Ledger: {"txs": [{txid, height, time, from, outputs}]}."""

    def __init__(self, node: str, path: str | Path | None = None):
        if ":" in node:
            raise ValueError("node name must not contain ':'")
        self.name = node
        self.path = Path(path) if path else default_ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.prefix = f"mock:{node}:"

    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[dict]:
        with open(self.lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield self._load()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"txs": []}

    def _store(self, ledger: dict) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(ledger, indent=1))
        os.replace(tmp, self.path)  # atomic: readers never see a half-written file

    def sync(self) -> None:
        pass

    def height(self) -> int:
        with self._locked(False) as led:
            return len(led["txs"])

    def new_address(self, label: str) -> str:
        label = "".join(c for c in label if c.isalnum() or c in "_-") or "addr"
        return f"{self.prefix}{label}:{secrets.token_hex(3)}"

    def send(self, outputs: list[Output]) -> str:
        _check_outputs(outputs)
        total = sum(int(z) for _, z, _ in outputs)
        with self._locked(True) as led:
            bal = self._balance(led)
            if total > bal:
                raise RuntimeError(f"{self.name}: insufficient funds ({bal} < {total})")
            txid = secrets.token_hex(32)
            led["txs"].append({
                "txid": txid, "height": len(led["txs"]) + 1, "time": int(time.time()),
                "from": self.name,
                "outputs": [{"to": a, "amount_zat": int(z), "memo": m} for a, z, m in outputs],
            })
            self._store(led)
        return txid

    def received(self, min_height: int = 0) -> list[Tx]:
        with self._locked(False) as led:
            return [Tx(txid=t["txid"], height=t["height"], time=t["time"], to=o["to"],
                       amount_zat=o["amount_zat"], memo=o["memo"])
                    for t in led["txs"] if t["height"] >= min_height
                    for o in t["outputs"] if o["to"].startswith(self.prefix)]

    def _balance(self, led: dict) -> int:
        bal = MOCK_START_ZAT
        for t in led["txs"]:
            for o in t["outputs"]:
                if o["to"].startswith(self.prefix):
                    bal += o["amount_zat"]
                if t["from"] == self.name:
                    bal -= o["amount_zat"]
        return bal

    def balance_zat(self) -> int:
        with self._locked(False) as led:
            return self._balance(led)

    def all_txs(self) -> list[dict]:
        """Raw ledger (for the dashboard)."""
        with self._locked(False) as led:
            return led["txs"]


# ------------------------------------------------------------------------ testnet

def _zwallet_module():
    if str(WALLET_DIR) not in sys.path:
        sys.path.insert(0, str(WALLET_DIR))
    import zwallet  # noqa: E402
    return zwallet


class DevtoolChain:
    """Zcash testnet via zcash-devtool. The wallet must already exist (ZWallet.init / init_fvk)."""

    def __init__(self, node: str):
        self.name = node
        self.wallet = _zwallet_module().ZWallet(node)

    def sync(self) -> None:
        self.wallet.sync()

    def height(self) -> int:
        return self.wallet.height()

    def new_address(self, label: str) -> str:
        return self.wallet.generate_address()  # label is local bookkeeping only

    def send(self, outputs: list[Output]) -> str:
        _check_outputs(outputs)
        return self.wallet.send_many([(a, int(z), m) for a, z, m in outputs])

    def received(self, min_height: int = 0) -> list[Tx]:
        return [Tx(**r) for r in self.wallet.received(min_height)]

    def balance_zat(self) -> int:
        return self.wallet.balance()["shielded_zat"]


def open_chain(backend: str, node: str, ledger: str | Path | None = None) -> Chain:
    """backend: "mock" | "testnet"."""
    if backend == "mock":
        return MockChain(node, ledger)
    if backend == "testnet":
        return DevtoolChain(node)
    raise ValueError(f"unknown backend {backend!r}")
