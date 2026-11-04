# zpulse — a dead man's switch on Zcash  (SPEC / interface contract)

Hackathon: Zcash Free Society Hackathon, Mumbai, 4 Nov 2026. Submissions close
18:30 UTC / 00:00 IST. Everything here is Python 3.12 + `cryptography` + `pynacl`
+ stdlib. No other deps. Testnet only.

## One-paragraph pitch

A journalist (the **principal**) encrypts a dossier and splits the key into n
Shamir shares held by n independent **watchers** (an NGO, a lawyer, an editor, a
friend abroad...). Every `interval` (48 h in production, 2 min in the demo) the
principal sends ONE shielded Zcash transaction that pays a few thousand zatoshi
to every watcher, with a signed "I'm alive" memo. Watchers detect it with nothing
but their own wallet (borrowed from *Paid Private File*: a unique diversified
address per vault per watcher lets a watcher match a payment 1:1 to a vault, and
a viewing key is enough to see it). If the heartbeats stop and the grace period
passes, each watcher independently publishes its share as a memo to a public
**release address**. Anyone who imports the release viewing key, sees t shares,
and has the public ciphertext can reconstruct the key and read the dossier. One
share leaks nothing; t-of-n must independently agree. A **duress** heartbeat
(same look, signed with a second key) triggers immediate release. The heartbeat
payments also fund the watchers' release transactions.

Zcash is the only transport. Watchers never talk to each other, never run a
server, never hold the principal's keys.

## Layout

```
zpulse/
  SPEC.md                     this file
  README.md                   pitch, how to run, demo script (Agent C)
  THREAT_MODEL.md             (Agent C)
  zpulse/__init__.py
  zpulse/crypto.py            Agent A
  zpulse/shamir.py            Agent A
  zpulse/memo.py              Agent A
  zpulse/chain.py             Agent A   (MockChain + DevtoolChain)
  zpulse/principal.py         Agent B   CLI
  zpulse/watcher.py           Agent B   CLI / daemon
  zpulse/reveal.py            Agent B   CLI
  zpulse/state.py             Agent B   paths + json helpers
  dashboard/index.html        Agent C
  dashboard/serve.py          Agent C
  demo/mock_demo.sh           Agent B   full flow on MockChain in < 1 min
  demo/testnet_demo.md        Agent B/C
  tests/test_crypto.py        Agent A
  tests/test_chain_mock.py    Agent A
  tests/test_flow_mock.py     Agent B
  data/                       runtime state (gitignored):
    vaults/<vault>/           principal side
    watchers/<watcher>/       watcher side
    mockchain.json            MockChain ledger
    revealed/
```

Run everything from `/Users/vomni/Documents/zhackathon/zpulse` as
`python3 -m zpulse.principal ...` etc. `wallet/zwallet.py` lives one level up
(`../wallet/zwallet.py`); chain.py imports it via sys.path.

## Agent A: `zpulse/crypto.py`, `shamir.py`, `memo.py`, `chain.py`

### crypto.py
```python
VAULT_KEY_LEN = 32
def random_key() -> bytes                                   # 32 bytes
def encrypt_vault(plaintext: bytes, key: bytes, vault_id: str) -> bytes
    # AES-256-GCM, 12-byte random nonce, AAD = vault_id.encode(); returns nonce||ct||tag
def decrypt_vault(blob: bytes, key: bytes, vault_id: str) -> bytes   # raises ValueError on tamper
def sha256_hex(b: bytes) -> str

class Identity:            # Ed25519 (pynacl SigningKey)
    @classmethod generate() -> Identity
    @classmethod load(path) -> Identity          # JSON {"sk": b64, "vk": b64}
    def save(path) -> None
    def sign(msg: bytes) -> bytes               # 64-byte detached sig
    @property verify_key_b64 -> str
def verify(vk_b64: str, msg: bytes, sig: bytes) -> bool
# Packet sealing (share -> watcher): pynacl SealedBox on the watcher's Ed25519 vk converted to X25519
def seal_to(vk_b64: str, data: bytes) -> str   # base64 of sealed box
def unseal(identity: Identity, sealed_b64: str) -> bytes
```

### shamir.py
Pure-python Shamir over GF(2^8), byte-wise (like SLIP-39 / `secretsharing`).
```python
def split(secret: bytes, threshold: int, n: int) -> list[bytes]
    # share = bytes([index]) + len(secret) bytes, index in 1..n
def combine(shares: list[bytes]) -> bytes     # any >= threshold distinct shares; order-free
```
Shares must be indistinguishable from random alone (standard Shamir). Test:
any t of n reconstruct; t-1 do not equal secret.

### memo.py   (all memos are UTF-8 text <= 512 bytes)
```python
PREFIX = "ZP1"
# Heartbeat (principal -> every watcher, same memo in every output):
#   "ZP1 HB <vault_id> <seq> <ts> <sig_b64>"
#   sig over f"zpulse|HB|{vault_id}|{seq}|{ts}".encode()
#   signed by principal key  => ALIVE
#   signed by duress key     => DURESS   (memo text identical in shape)
# Cancel (principal -> every watcher): "ZP1 CX <vault_id> <ts> <sig_b64>", sig over f"zpulse|CX|{vault_id}|{ts}"
# Share release (watcher -> release address):
#   "ZP1 SH <vault_id> <watcher_id> <share_b64> <sig_b64>"
#   sig over f"zpulse|SH|{vault_id}|{watcher_id}|".encode() + share_bytes, by watcher key
def encode_heartbeat(identity, vault_id, seq: int, ts: int) -> str
def encode_cancel(identity, vault_id, ts: int) -> str
def encode_share(identity, vault_id, watcher_id, share: bytes) -> str
def parse(text: str) -> dict | None
    # {"kind": "HB"|"CX"|"SH", "vault_id":..., "seq"/"ts"/"watcher_id"/"share": ..., "sig": bytes}
def classify_heartbeat(m: dict, principal_vk_b64: str, duress_vk_b64: str | None) -> str | None
    # "alive" | "duress" | None (bad sig)
def verify_cancel(m, principal_vk_b64) -> bool
def verify_share(m, watcher_vk_b64) -> bool
```
vault_id = 16 hex chars. watcher_id = short slug `[a-z0-9_-]{1,16}`.

### chain.py
```python
class Tx(TypedDict): txid: str; height: int; time: int; to: str; amount_zat: int; memo: str | None

class Chain(Protocol):
    name: str                                    # node name
    def sync(self) -> None
    def height(self) -> int
    def new_address(self, label: str) -> str     # fresh diversified receiving address for this node
    def send(self, outputs: list[tuple[str, int, str | None]]) -> str   # [(addr, zat, memo)] -> txid, ONE tx
    def received(self, min_height: int = 0) -> list[Tx]  # incoming outputs to this node, with memos, ascending height
    def balance_zat(self) -> int

def open_chain(backend: str, node: str) -> Chain   # backend "mock" | "testnet"
```
`MockChain(node, path=data/mockchain.json)`: one JSON ledger file shared by all
nodes on this machine (use fcntl.flock). Addresses are
`mock:<node>:<label>:<random6>`; `received()` returns outputs whose address
starts with `mock:<node>:`. `height` = number of txs in the ledger. `time` =
int(time.time()) at send. Env `ZPULSE_MOCK_LEDGER` overrides path.

`DevtoolChain(node)`: wraps `ZWallet(node)` from `../wallet/zwallet.py`. Extend
`wallet/zwallet.py` (allowed, additive only) with:
- `send_many(outputs: list[(addr, zat, memo|None)]) -> txid` using
  `zcash-devtool wallet pay --payment-uri <ZIP-321 URI> --disable-confirmation`.
  ZIP-321: `zcash:<addr>?amount=<ZEC decimal>&memo=<base64url no pad>&address.1=<addr>&amount.1=...&memo.1=...`
  (first recipient has no index; later ones `.1`, `.2`...). Single recipient: `send --memo`.
- `generate_address() -> str` via `wallet generate-address` (parse the UA).
- `received()` -> parse `list-tx` text, including `Memo: Text("...")` lines
  (Rust Debug formatting: unescape `\"` and `\\`), returning outputs
  "Received by account" with `to` = address if printed else "".
- `init_fvk(ufvk, birthday)` for view-only wallets (`wallet init-fvk`).
- `ufvk() -> str` from `list-accounts` ("UFVK: ...").
ZWallet already has `init/sync/addresses/balance/height/send_shielded`. Server
is testnet.zec.rocks (`-s zecrocks`). The binary is already built at
`wallet/zcash-devtool/target/release/zcash-devtool`. Don't run testnet commands
in tests; test DevtoolChain parsing with captured text fixtures.

Tests: `python3 -m pytest tests/` (pytest may need `pip install pytest`; if not
available, make tests plain `python3 tests/test_x.py` runnable too).

## Agent B: `principal.py`, `watcher.py`, `reveal.py`, `state.py`, `demo/mock_demo.sh`

All CLIs: `argparse`, `--backend mock|testnet` (default from env `ZPULSE_BACKEND`,
default `mock`), `--data DIR` (default `./data`). Print JSON on stdout for
machine-readable results, human lines to stderr.

### Files
- `data/vaults/<vault>/vault.json`   PUBLIC manifest:
  `{vault_id, name, threshold, n, interval_s, grace_s, created_ts, principal_vk,
    release_address, release_ufvk (testnet only, else null), ciphertext_sha256,
    watchers: {watcher_id: {address, vk}}, heartbeat_zat}`
- `data/vaults/<vault>/vault.bin`    ciphertext (public)
- `data/vaults/<vault>/principal.key`, `duress.key`  (SECRET)
- `data/vaults/<vault>/seq`          last heartbeat seq
- `data/vaults/<vault>/enroll/<watcher_id>.json`   `{watcher_id, vk, address}` written by watcher enroll
- `data/vaults/<vault>/packets/<watcher_id>.json`  `{vault_id, watcher_id, share_sealed, duress_vk, manifest: <vault.json copy>}`
- `data/watchers/<watcher_id>/identity.key`        watcher Ed25519 (SECRET)
- `data/watchers/<watcher_id>/<vault_id>.json`     watcher state:
  `{vault_id, status: "armed"|"released"|"cancelled", last_hb_ts, last_seq, last_kind,
    deadline_ts, hb_count, release_txid, updated_ts, next_check_ts, log: [str]}`
- `data/revealed/<vault_id>.txt`  + `.json`  `{vault_id, shares_seen, txids, revealed_ts}`

### principal.py
```
python3 -m zpulse.principal init    --name NAME --file PATH --threshold 3 --interval 120 --grace 30 [--heartbeat-zat 10000]
    # creates keys, vault_id, encrypts file (key kept in memory only -> written to data/vaults/NAME/.vaultkey until seal, then deleted)
    # creates release node: mock -> MockChain("release-<vault_id>") address; testnet -> ZWallet("release-<vault_id>") init + ufvk
python3 -m zpulse.principal seal    --name NAME
    # reads enroll/*.json (n = count), checks n >= threshold, splits key, writes packets/, fills watchers in vault.json, deletes .vaultkey
python3 -m zpulse.principal pulse   --name NAME [--duress]
    # seq+1, ts=now, one tx paying heartbeat_zat to every watcher address with HB memo; prints {"txid","seq","ts"}
python3 -m zpulse.principal cancel  --name NAME
python3 -m zpulse.principal status  --name NAME
```
Principal's chain node name = `principal-<name>` (mock) / `principal` (testnet, one funded wallet).

### watcher.py
```
python3 -m zpulse.watcher enroll --id w1 --vault data/vaults/NAME/vault.json
    # generates identity if missing, new_address(label=vault_id) on chain node "watcher-w1", writes enroll json next to vault
python3 -m zpulse.watcher run    --id w1 --packet data/vaults/NAME/packets/w1.json [--poll 5] [--once]
    # loop: sync; scan received() for HB/CX memos to MY vault address with valid sig
    #   alive  -> last_hb_ts = tx.time, deadline = last_hb_ts + interval + grace
    #   duress -> release NOW
    #   cancel -> status cancelled, shred share
    #   now() > deadline (and hb_count>0 or armed since seal_ts) -> release
    # release: unseal share, send (release_address, 1000 zat, SH memo) from my node; record txid; status released
    # now() = max(local clock, latest chain tx time)
```
The watcher must never need the principal's keys. It must keep working across
restarts (state file). Use block/tx `time` from the chain for `last_hb_ts`.

### reveal.py
```
python3 -m zpulse.reveal --vault data/vaults/NAME/vault.json [--watch] [--poll 5]
    # opens chain node "release-<vault_id>" (mock) / view-only wallet from release_ufvk (testnet)
    # collects SH memos, verify_share against manifest watcher vks, dedupe by watcher_id
    # when >= threshold: combine -> key -> decrypt vault.bin (check sha256) -> write data/revealed/, print plaintext
```

### demo/mock_demo.sh
Non-interactive, < 60 s, `--interval 20 --grace 5`: init, enroll w1..w5, seal,
start 5 watchers in background (`--poll 2`), pulse twice 8 s apart (show
deadline moving), stop pulsing, wait, show 3+ release memos, reveal prints the
secret. Then a second run with `--duress` showing instant release. Clean
`data/` at start. Exit non-zero on any failure (this doubles as the e2e test).

## Agent C: `dashboard/`, `README.md`, `THREAT_MODEL.md`

`dashboard/serve.py --data ./data --port 8787`: stdlib http.server; `GET /`
serves index.html, `GET /state` returns
`{now, vaults:[manifest + principal seq], watchers:[watcher state files],
revealed:[...], mock_ledger_tail:[last 20 txs if mock]}`.
`index.html`: single file, no build step, polls /state every 2 s. Big countdown
to the earliest deadline, one card per watcher (armed / released / cancelled,
last heartbeat age, release txid linking to https://testnet.cipherscan.app/tx/<txid>
when not mock), a t-of-n progress bar of released shares, and the revealed
plaintext when present. Dark theme, readable from across a room on a projector.

README: pitch, diagram (ASCII), how it borrows from Paid Private File (viewing
key as smart escrow, diversified address per vault, light-client detection with
no spend authority) and z-canary (threshold keepers, shielded memos as
transport), quickstart for mock, quickstart for testnet, demo script, limits.
THREAT_MODEL: who is trusted, coercion, early collusion (t shares), seizure of
watchers, replay, clock skew, chain reorgs, metadata (shielded, but timing),
what is NOT solved.

## Conventions
- Amounts in zatoshi ints. Timestamps unix seconds ints.
- All randomness from `secrets` / `os.urandom`.
- No network in unit tests. Mock backend must run the whole flow offline.
- Keep it small and readable: this is judged on a projector.
