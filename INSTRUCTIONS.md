# zpulse user manual

This manual covers every command in this repository. All commands, flags, defaults and paths come from the code (`zpulse/*.py`, `wallet/*.py`, `dashboard/serve.py`, `demo/mock_demo.sh`). Where the code and the README or SPEC disagree, the code wins, and the manual says so.

Run every command from the repository root (the directory that holds `zpulse/`, `wallet/` and `dashboard/`). The default data directory is `./data`, relative to where you run the command.

Times printed by zpulse are UTC (the `Z` suffix in logs). IST is UTC + 5:30. Example: `11:14:33Z` is 11:14:33 UTC / 16:44:33 IST.

---

## 1. What zpulse is

zpulse is a dead man's switch for whistleblowers, built on Zcash.
The dossier is encrypted, and its key is split into n Shamir shares (t-of-n), one per watcher.
The principal sends a signed heartbeat on a schedule: one shielded transaction that pays every watcher.
If heartbeats stop past the deadline, or a duress heartbeat arrives, each watcher posts its share to a public release address.
Anyone with the public manifest and `vault.bin` can collect t shares and decrypt. There is no server and no recipient.

Roles:

| role | who | what they run | what they hold |
|---|---|---|---|
| principal | the journalist or whistleblower | `zpulse.principal` (init, seal, pulse, cancel, status) | the dossier, `principal.key`, `duress.key`, a funded wallet |
| watcher | an NGO, lawyer, editor, friend abroad | `zpulse.watcher` (enroll, run) | its own `identity.key`, one sealed share (packet file), its own wallet |
| the world | anyone | `zpulse.reveal` | the public manifest `vault.json`, `vault.bin` |

A watcher never talks to the principal or other watchers over any channel other than the chain. It never holds the principal's keys.

---

## 2. Requirements

- macOS or Linux (the mock chain uses `fcntl.flock`, so Windows is not supported).
- Python 3.12.
- Python packages:

```bash
pip install cryptography pynacl pytest
```

- Zcash testnet only: Rust 1.99 or newer, to build `zcash-devtool`. Not needed for the offline mock chain.

---

## 3. Install

```bash
git clone https://github.com/vipenl26/zpulse
cd zpulse
pip install cryptography pynacl pytest
```

Check that the CLIs load:

```bash
python3 -m zpulse.principal --help
python3 -m zpulse.watcher --help
python3 -m zpulse.reveal --help
```

---

## 4. Quick start, offline (mock chain)

The mock chain is a JSON ledger file (`data/mockchain.json`) shared by every process on this machine. Every mock node starts with 10 fake ZEC. Nothing touches the network. The mock backend is the default (`ZPULSE_BACKEND` unset means `mock`).

### 4.1 Run the demo

**Warning:** `demo/mock_demo.sh` starts with `rm -rf data`. It deletes the whole `./data` directory, including any testnet vault keys, watcher identities and revealed files. Move or back up `data/` first if it holds anything you care about. (Testnet wallets live in `wallet/data/` and are not touched.)

```bash
bash demo/mock_demo.sh
```

What it does (interval 20 s, grace 5 s):

1. Cleans `data/` and writes a test dossier.
2. Creates vault `leak` (3-of-n), and checks that a second `init` with the same name is refused.
3. Enrolls watchers `w1` to `w5`.
4. Seals 3-of-5 and checks that `.vaultkey` was deleted.
5. Starts 5 watcher daemons in the background (`--poll 2`, logs in `data/logs/leak-wN.log`).
6. Sends heartbeat #1, then heartbeat #2 eight seconds later. Deadlines move forward.
7. Stops pulsing and runs `reveal --watch` until 3 shares arrive. Checks the revealed text matches.
8. Second vault `coerced` (interval 600 s): sends a duress heartbeat and reveals within seconds.

What you should see at the end:

```
   duress -> revealed in 1s (interval was 600s)
   w1: released  last_seq=1 deadline ... UTC / ... IST (in 658s)
   ...
PASS  (total 40s)
```

Exit code 0 means PASS, 1 means FAIL (the script prints `FAIL: <reason>`). A normal run takes under a minute. If the machine sleeps during the run, it still passes but takes longer.

### 4.2 Run the tests

```bash
python3 -m pytest -q tests/
```

Expected: `21 passed`. The tests use temporary directories and do not touch `./data` or the network.

### 4.3 Start the dashboard

```bash
python3 dashboard/serve.py --data ./data --port 8787
```

Open http://127.0.0.1:8787. The page polls `GET /state` every 2 seconds. It never reads secret key files.

What you should see:

- A badge showing the backend (`mock` or `testnet`).
- A large countdown to the next heartbeat deadline. It turns red near zero, then shows `RELEASED` or `CANCELLED`.
- A t-of-n bar that fills as watchers release shares.
- One card per watcher: status (ARMED, RELEASED, CANCELLED, or ENROLLED if not started yet), last heartbeat age, heartbeats seen, deadline, release txid.
- A chain ticker (last 20 mock transactions, or known txids on testnet).
- The revealed plaintext in a box at the top, once a reveal has run.

Stop the server with Ctrl-C.

---

## 5. Full manual walkthrough (mock chain)

This is the demo, one command at a time. Use a separate terminal for the watchers and the dashboard if you like. Every CLI prints human progress lines to stderr and one JSON object to stdout.

### 5.0 Clean start

```bash
export ZPULSE_BACKEND=mock          # optional: mock is the default
mkdir -p data/logs
echo "Dossier: the minister's offshore accounts are at Bank X." > data/dossier.txt
```

Optional, in another terminal:

```bash
python3 dashboard/serve.py --data ./data --port 8787
```

### 5.1 Principal: create the vault

```bash
python3 -m zpulse.principal init --name leak --file data/dossier.txt \
    --threshold 3 --interval 20 --grace 5
```

Expected output:

```
vault leak (9a99331ff3349e32) created; 57 bytes encrypted.
next: each watcher runs  python3 -m zpulse.watcher enroll --id <w> --vault data/vaults/leak/vault.json
{"ok": true, "vault_id": "9a99331ff3349e32", "vault": "data/vaults/leak/vault.json", "release_address": "mock:release-9a99331ff3349e32:release:bb8d37", "ciphertext_sha256": "8b8c..."}
```

Flags:

| flag | default | meaning |
|---|---|---|
| `--name` | required | Vault name. Must match `[A-Za-z0-9_-]{1,32}`. Becomes `data/vaults/<name>/`. |
| `--file` | required | The dossier to encrypt (any bytes). |
| `--threshold` | `3` | t: how many shares are needed to decrypt. Must be at least 1. Checked against n at `seal`. |
| `--interval` | `120` | Seconds between heartbeats the watchers expect. |
| `--grace` | `30` | Extra seconds after the interval before a watcher releases. Deadline = time of last heartbeat tx + interval + grace. |
| `--heartbeat-zat` | `20000` | Zatoshi paid to each watcher in every heartbeat. On testnet this money pays for the watcher's release tx (see section 6.6). |
| `--backend` | `$ZPULSE_BACKEND` or `mock` | `mock` or `testnet`. |
| `--data` | `data` | Data directory. |

`init` refuses to overwrite an existing vault. It writes the encryption key to `data/vaults/<name>/.vaultkey` until `seal` deletes it. After `init`, securely delete your plaintext dossier from disk if it should not stay there.

### 5.2 Watchers: enroll (five times)

```bash
for w in w1 w2 w3 w4 w5; do
  python3 -m zpulse.watcher enroll --id $w --vault data/vaults/leak/vault.json
done
```

Expected output, per watcher:

```
watcher w1 enrolled in vault leak (9a99331ff3349e32)
{"ok": true, "watcher_id": "w1", "vault_id": "9a99331ff3349e32", "vk": "2P/e...", "address": "mock:watcher-w1:9a99331ff3349e32:01b257", "enroll": "data/vaults/leak/enroll/w1.json"}
```

Flags:

| flag | default | meaning |
|---|---|---|
| `--id` | required | Watcher id. Must match `[a-z0-9_-]{1,16}` (lowercase only). |
| `--vault` | required | Path to the vault's `vault.json`. The enroll file is written to `<dir of vault.json>/enroll/<id>.json`. |
| `--backend`, `--data` | as above | `--data` is where the watcher's `identity.key` lives (`<data>/watchers/<id>/identity.key`). |

Enroll creates the watcher identity if it does not exist yet, and one fresh address for this vault. Enrolling after `seal` fails with `vault is already sealed; enrollment closed`.

On separate machines: the principal sends `vault.json` to the watcher. The watcher saves it anywhere, runs `enroll --vault <that copy>`, and sends back the file `enroll/<id>.json` written next to it. The principal puts that file in `data/vaults/<name>/enroll/`.

### 5.3 Principal: seal

```bash
python3 -m zpulse.principal seal --name leak
```

Expected output:

```
sealed 3-of-5: packets for w1, w2, w3, w4, w5; vault key deleted.
{"ok": true, "vault_id": "9a99331ff3349e32", "threshold": 3, "n": 5, "packets": ["data/vaults/leak/packets/w1.json", ...]}
```

`seal` reads every `enroll/*.json` (n = how many there are), refuses if n < threshold (`only 1 watchers enrolled, threshold is 3`), splits the key into n shares, seals each share to its watcher's key, writes `packets/<id>.json`, adds the watchers to `vault.json`, and deletes `.vaultkey`. After this nobody holds the whole key.

Hand each watcher its own `packets/<id>.json` and nothing else.

### 5.4 Watchers: start the daemons

```bash
for w in w1 w2 w3 w4 w5; do
  python3 -m zpulse.watcher run --id $w --packet data/vaults/leak/packets/$w.json --poll 2 \
    > data/logs/leak-$w.log 2>&1 &
done
```

Each log starts with:

```
[w1] 11:13:58Z armed for vault leak (9a99331ff3349e32); deadline in 25s
[w1] watching vault 9a99331ff3349e32 every 2.0s (Ctrl-C to stop)
```

Flags:

| flag | default | meaning |
|---|---|---|
| `--id` | required | This watcher's id. Must match the packet. |
| `--packet` | required | This watcher's packet file. It must be passed on every start. |
| `--poll` | `5` | Seconds between polls (float). |
| `--once` | off | Poll once, print the state summary as JSON, and exit. Useful for cron or debugging. |
| `--backend`, `--data` | as above | `--data` must hold `watchers/<id>/identity.key` from enroll. |

The first deadline, before any heartbeat, is `sealed_ts + interval + grace`. So pulse soon after sealing.

One-shot check of a single watcher:

```bash
python3 -m zpulse.watcher run --id w1 --packet data/vaults/leak/packets/w1.json --once
```

```
{"ok": true, "vault_id": "9a99331ff3349e32", "watcher_id": "w1", "status": "armed", "last_hb_ts": null, "last_seq": 0, "last_kind": null, "deadline_ts": 1793792996, "hb_count": 0, "release_txid": null}
```

### 5.5 Principal: pulse

```bash
python3 -m zpulse.principal pulse --name leak
```

Expected output:

```
heartbeat #1 sent to 5 watchers in one tx d0a53c08e2a4beb8...
{"ok": true, "txid": "d0a53c08e2a4beb8...", "seq": 1, "ts": 1793792921}
```

In each watcher log:

```
[w1] 11:14:01Z heartbeat seq 1 ok; deadline in 24s
```

Flags: `--name` (required), `--duress` (see 5.9), `--backend`, `--data`. The vault must be sealed.

### 5.6 Pulse again: the deadline moves

Wait a few seconds, then:

```bash
python3 -m zpulse.principal pulse --name leak
python3 -m zpulse.principal status --name leak
```

Watcher logs show `heartbeat seq 2 ok; deadline in 24s`. The deadline is always measured from the chain time of the latest heartbeat tx. `status` prints:

```
leak: sealed 3-of-5, last heartbeat seq 2
{"ok": true, "vault_id": "...", "name": "leak", "sealed": true, "threshold": 3, "n": 5, "enrolled": ["w1", ...], "seq": 2, "interval_s": 20, "grace_s": 5, "release_address": "...", "principal_balance_zat": 999940000, "watchers": {"w1": {"status": "armed", "last_seq": 2, "deadline_ts": ..., "release_txid": null}, ...}}
```

`status` shows watcher state only for watchers whose state files are in the same `--data` directory (that is, on the same machine).

### 5.7 Go silent and watch the release

Stop pulsing. After interval + grace (25 s here), each watcher logs:

```
[w1] 11:14:35Z DEADLINE PASSED (2s late): releasing share
[w1] 11:14:35Z share RELEASED in tx ae08b4138926b2ef
```

A watcher that released or was cancelled stays in that state. Further polls do nothing.

### 5.8 Reveal (anyone can do this)

```bash
python3 -m zpulse.reveal --vault data/vaults/leak/vault.json --watch --poll 2 --timeout 60
```

Expected output:

```
0 of 3 needed shares seen (none)
...
5 of 3 needed shares seen (w1, w2, w3, w4, w5)
============================================================
REVEALED vault leak (9a99331ff3349e32):
Dossier: the minister's offshore accounts are at Bank X.
============================================================
{"ok": true, "vault_id": "...", "name": "leak", "shares_seen": [...], "txids": {...}, "revealed_ts": ..., "plaintext": "...", "plaintext_path": "data/revealed/9a99331ff3349e32.txt"}
```

Flags:

| flag | default | meaning |
|---|---|---|
| `--vault` | required | The public manifest `vault.json`. `vault.bin` must be in the same directory. Its SHA-256 is checked against the manifest. |
| `--watch` | off | Keep polling until revealed. Without it, reveal checks once and exits with code 2 if there are not enough shares. |
| `--poll` | `5` | Seconds between checks with `--watch`. |
| `--timeout` | `0` | With `--watch`: give up after this many seconds (exit code 3). `0` means never. |
| `--backend`, `--data` | as above | On mock, `--data` must be the same directory the chain lives in. The plaintext is written to `<data>/revealed/<vault_id>.txt` with metadata in `<vault_id>.json`. |

Reveal ignores share memos whose signature does not match the watcher key in the manifest. It only decrypts once `threshold` distinct watchers have released.

Stop the background watchers:

```bash
kill %1 %2 %3 %4 %5      # or: pkill -f "zpulse.watcher run"
```

### 5.9 Duress

A duress heartbeat is signed with `duress.key` instead of `principal.key`. On chain it looks the same. Watchers release at once.

```bash
echo "If you read this, I was forced to stop." > data/dossier2.txt
python3 -m zpulse.principal init --name coerced --file data/dossier2.txt --threshold 3 --interval 600 --grace 60
for w in w1 w2 w3 w4 w5; do python3 -m zpulse.watcher enroll --id $w --vault data/vaults/coerced/vault.json; done
python3 -m zpulse.principal seal --name coerced
for w in w1 w2 w3 w4 w5; do
  python3 -m zpulse.watcher run --id $w --packet data/vaults/coerced/packets/$w.json --poll 2 \
    > data/logs/coerced-$w.log 2>&1 &
done
python3 -m zpulse.principal pulse --name coerced --duress
python3 -m zpulse.reveal --vault data/vaults/coerced/vault.json --watch --poll 1 --timeout 10
```

`pulse --duress` prints `heartbeat #1 sent to 5 watchers in one tx ...  [DURESS]`. Watcher logs show `DURESS heartbeat seq 1: releasing now` and `share RELEASED in tx ...`. Reveal succeeds in about a second, although the interval is 600 s.

Once a watcher has seen a duress heartbeat, a later normal heartbeat or cancel does not stop it.

### 5.10 Cancel

Cancel tells every watcher to shred its share. It is signed with `principal.key`.

```bash
python3 -m zpulse.principal cancel --name leak
```

```
cancel sent to 5 watchers in tx f4a2682341406de1...
{"ok": true, "txid": "f4a2682341406de1...", "ts": 1793792922}
```

Watcher logs show `vault CANCELLED by principal; share shredded`. The watcher sets its share to null in its state file, and ignores everything after (including duress).

Cancel only works on a watcher that is still armed and has not started releasing. It cannot undo a release that already happened. It is not reversible: there is no "uncancel". To protect something again, create a new vault.

Note: the principal CLI does not record the cancel locally. `pulse` still works after `cancel`, but cancelled watchers ignore it.

---

## 6. Zcash testnet

Same CLIs, with `--backend testnet` or `export ZPULSE_BACKEND=testnet`. Wallets are `zcash-devtool` wallets under `wallet/data/<name>/`. The default lightwalletd server is `testnet.zec.rocks`. Testnet blocks come about every 75 seconds, so use minutes, not seconds, for timings.

Wallet names used on testnet:

| role | wallet name | created by | can spend |
|---|---|---|---|
| principal | `principal` (one wallet for all vaults) | you: `python3 wallet/zwallet.py principal init` | yes |
| watcher wN | `watcher-wN` (one wallet per watcher, all vaults) | `zpulse.watcher enroll` if missing | yes |
| release | `release-<vault_id>` | `zpulse.principal init` | yes, but only holds the share dust |
| revealer | `reveal-<vault_id>` | `zpulse.reveal` from the manifest's viewing key | no (view-only) |

On mock the principal node is `principal-<name>` per vault. On testnet it is always `principal`.

### 6.1 Build zcash-devtool

zpulse uses zcash-devtool at commit `0b3436b` plus a 14-line patch that adds `--min-confirmations` to `wallet pay`:

```bash
git clone https://github.com/zcash/zcash-devtool wallet/zcash-devtool
git -C wallet/zcash-devtool checkout 0b3436b
git -C wallet/zcash-devtool apply ../zcash-devtool-pay-min-confirmations.patch
cargo build --release --manifest-path wallet/zcash-devtool/Cargo.toml
```

zpulse looks for the binary at `wallet/zcash-devtool/target/release/zcash-devtool`. To use an existing binary instead:

```bash
export ZCASH_DEVTOOL=/path/to/zcash-devtool
```

If the binary is missing, every wallet call fails with `zcash-devtool binary not found at ...`.

### 6.2 Create the principal wallet

```bash
python3 wallet/zwallet.py principal init
```

This prints `{"unified": "<unified address>", "transparent": "tm..."}`. Fund the unified address.

Note: `demo/testnet_demo.md` writes `python3 wallet/zwallet.py balance principal`. That order is wrong. The code takes the wallet name first: `python3 wallet/zwallet.py <name> <command>`.

Useful wallet commands:

```bash
python3 wallet/zwallet.py principal addresses   # print addresses
python3 wallet/zwallet.py principal sync        # sync, then print balance
python3 wallet/zwallet.py principal balance     # balance from last sync (no sync)
python3 wallet/zwallet.py principal shield      # move ALL transparent funds to shielded
```

### 6.3 Fund it

About 0.05 TAZ is enough for many heartbeats (see 6.6 for costs). Options:

- Valar faucet, 0.125 TAZ per IP per 24 h:

  ```bash
  python3 wallet/faucet.py <unified address>
  ```

- Jino Labs faucet (browser proof-of-work, 0.1 TAZ per address per day): https://zcashfaucet.jinolabs.xyz
- Ask a mentor or another Zcash developer to send you about 0.05 TAZ. On 4 Nov 2026 both public faucets were unreliable, and this was the fastest route.

zpulse spends only shielded funds. If you receive transparent funds, shield them:

```bash
python3 wallet/zwallet.py principal shield
python3 wallet/zwallet.py principal sync
```

`sync` prints `{"transparent_zat": ..., "shielded_zat": ..., "pending_zat": ..., "chain_tip_height": ...}`. You need `shielded_zat` above zero, and the funds must have at least `ZCASH_MIN_CONF` (default 1) confirmation.

### 6.4 Recommended timings

For a testnet demo:

- `principal init ... --interval 300 --grace 120 --heartbeat-zat 20000`
- `watcher run ... --poll 45`

Five watchers each running sync + enhance + list-tx every 30 s is heavy on one laptop. 45 s is comfortable.

Note: the README quick start suggests `--interval 120 --grace 60` for testnet. The live run showed that is too tight for 75 s blocks. Use 300/120.

### 6.5 Order of operations (important)

The first deadline is `sealed_ts + interval + grace`. If watchers start polling before heartbeat #1 is mined, a slow block can make them fire. So:

1. `seal`.
2. `pulse` immediately.
3. Wait until heartbeat #1 is mined (1 to 3 minutes). Check the txid on the explorer.
4. Only then start the watchers.

Watchers read the whole wallet history on start, so a watcher started late still sees heartbeat #1.

Full sequence:

```bash
export ZPULSE_BACKEND=testnet
mkdir -p data/logs
echo "Dossier: ..." > data/dossier.txt

python3 -m zpulse.principal init --name leak --file data/dossier.txt \
    --threshold 3 --interval 300 --grace 120 --heartbeat-zat 20000

for w in w1 w2 w3 w4 w5; do
  python3 -m zpulse.watcher enroll --id $w --vault data/vaults/leak/vault.json
done

python3 -m zpulse.principal seal --name leak
python3 -m zpulse.principal pulse --name leak        # note the txid; wait until it is mined

for w in w1 w2 w3 w4 w5; do
  python3 -m zpulse.watcher run --id $w --packet data/vaults/leak/packets/$w.json --poll 45 \
    > data/logs/leak-$w.log 2>&1 &
done

# keep pulsing at least every 5 minutes to stay alive:
python3 -m zpulse.principal pulse --name leak
```

`principal init` on testnet creates the wallet `release-<vault_id>` and stores its address, its unified full viewing key (`release_ufvk`) and a birthday height (`release_birthday`) in `vault.json`. `watcher enroll` creates `watcher-<id>` if it does not exist (this takes a moment) and makes one fresh diversified address for the vault. `principal pulse` syncs the principal wallet first, then sends one transaction with one output per watcher.

### 6.6 Costs

- Heartbeat: n x `heartbeat_zat` plus the fee. The fee is ZIP-317: 5,000 zat per logical action, at least 10,000. The live 5-watcher heartbeat cost 100,000 zat to watchers + 30,000 zat fee.
- Release: each watcher pays 1,000 zat to the release address plus a 10,000 zat fee, so 11,000 zat. This comes from the heartbeat money it received.

The default `--heartbeat-zat 20000` covers one release (11,000 zat) from a single heartbeat. Do not lower it below 11,000 on testnet, or a watcher that has received only one heartbeat (for example, a duress heartbeat as the first pulse) cannot release. (On mock this does not matter: every mock node starts with 10 fake ZEC.)

### 6.7 Check a transaction

Every heartbeat, cancel and release prints a txid. Look it up:

```
https://testnet.zecblock.com/tx/<txid>
```

Amounts, recipients and memos are shielded. You only see that the tx exists and when it was mined. (The dashboard links to `https://testnet.cipherscan.app/tx/<txid>`, another testnet explorer.)

### 6.8 Reveal with a view-only wallet

```bash
python3 -m zpulse.reveal --backend testnet --vault data/vaults/leak/vault.json --watch --poll 60 --timeout 3600
```

On first run, reveal prints `importing release viewing key into view-only wallet reveal-<vault_id> ...` and creates that wallet from `release_ufvk` with `zcash-devtool wallet init-fvk`, starting at `release_birthday`. This wallet has no spend key. It then syncs, fetches memos, checks each share's signature against the manifest, and decrypts once t distinct watchers have released. Reruns reuse the same wallet and resume.

A third party needs: `vault.json`, `vault.bin` next to it, Python with the packages above, and a built `zcash-devtool`. They do not need any key from the principal or the watchers. A manifest made on the mock backend has no `release_ufvk` and cannot be revealed on testnet.

### 6.9 Environment variables

| variable | default | used by | meaning |
|---|---|---|---|
| `ZPULSE_BACKEND` | `mock` | all zpulse CLIs | Default for `--backend`: `mock` or `testnet`. |
| `ZPULSE_MOCK_LEDGER` | `data/mockchain.json` | dashboard (and `MockChain` when no path is given) | Path of the mock ledger. See note below. |
| `ZCASH_DEVTOOL` | `wallet/zcash-devtool/target/release/zcash-devtool` | `wallet/zwallet.py` | Path to the zcash-devtool binary. |
| `ZCASH_LWD_SERVER` | `zecrocks` (testnet.zec.rocks) | `wallet/zwallet.py` | Passed as `-s` to every devtool call. |
| `ZCASH_MIN_CONF` | `1` | `wallet/zwallet.py` | `--min-confirmations` for `send` and `pay`. devtool's own default is 3 trusted / 10 untrusted. |
| `ZCASH_WALLET_DATA` | `wallet/data` | `wallet/zwallet.py` | Directory that holds one subdirectory per wallet. |

Note on `ZPULSE_MOCK_LEDGER`: the zpulse CLIs always pass `<--data>/mockchain.json` explicitly, so they ignore this variable. Only the dashboard honours it. If you set it, the dashboard will read a different ledger from the one the CLIs write. Leave it unset and use `--data` instead.

---

## 7. Files and directories

### Principal side: `data/vaults/<name>/`

| path | secret? | contents |
|---|---|---|
| `vault.json` | public | Manifest: `vault_id`, `name`, `threshold`, `n`, `interval_s`, `grace_s`, `created_ts`, `sealed_ts`, `principal_vk`, `release_address`, `release_ufvk` (testnet only), `release_birthday`, `ciphertext_sha256`, `watchers` (id -> address, public key), `heartbeat_zat`. |
| `vault.bin` | public | AES-256-GCM ciphertext of the dossier, bound to the vault id. |
| `principal.key` | SECRET | Ed25519 key that signs heartbeats and cancels. File mode 0600. |
| `duress.key` | SECRET | Ed25519 key that signs duress heartbeats. File mode 0600. |
| `.vaultkey` | SECRET | The vault key in plaintext. Exists only between `init` and `seal`. |
| `seq` | private | Last heartbeat sequence number sent. Must never go backwards. |
| `enroll/<id>.json` | public | Written by `watcher enroll`: watcher id, vault id, watcher public key, address. |
| `packets/<id>.json` | for that watcher only | Written by `seal`: the share sealed to that watcher's key, the duress public key, a copy of the manifest. |

### Watcher side: `data/watchers/<id>/`

| path | secret? | contents |
|---|---|---|
| `identity.key` | SECRET | The watcher's Ed25519 key. Opens its sealed shares and signs its release memo. |
| `<vault_id>.json` | private | State per vault: status (`armed`, `released`, `cancelled`), last seq, deadline, heartbeat count, release txid, seen txids, the sealed share, last 50 log lines. Mode 0600. |

### Other

| path | contents |
|---|---|
| `data/mockchain.json` (+ `.lock`) | Mock ledger. Mock backend only. |
| `data/revealed/<vault_id>.txt` | Decrypted dossier, written by `reveal`. |
| `data/revealed/<vault_id>.json` | Which watchers' shares were used, their txids, reveal time. |
| `data/logs/` | Where the demo and these instructions send watcher logs. Not created by the CLIs. |
| `wallet/data/<name>/` | Testnet wallets: sqlite databases, `keys.toml`, and the age identity `key.txt`. SECRET (spend keys), except `reveal-*` which is view-only. |

`data/` and `wallet/data` are in `.gitignore`. Never commit them.

### What to publish, keep, and hand out

- **Publish** (or give to people who should be able to reveal): `vault.json` and `vault.bin`. Mirror `vault.bin` widely: if every copy is lost, release is useless. Note that `vault.json` lists every watcher's address. If that worries you, give it to your watchers ahead of time and publish it only when needed (see THREAT_MODEL.md).
- **Keep secret**: `principal.key`, `duress.key`, `seq`, `.vaultkey` (gone after seal), your original dossier, and `wallet/data/principal/`.
- **Give each watcher**: its own `packets/<id>.json` only. A packet is useless without that watcher's `identity.key`.

---

## 8. Operating it for real (48 h interval)

zpulse is hackathon code, tested on testnet only and not audited. Read THREAT_MODEL.md before relying on it.

### 8.1 Vault settings

48 h interval, 12 h grace:

```bash
export ZPULSE_BACKEND=testnet
python3 -m zpulse.principal init --name dossier --file /path/to/dossier.pdf \
    --threshold 3 --interval 172800 --grace 43200 --heartbeat-zat 20000
```

Choose n comfortably above t (for example 3-of-5), with watchers in different places and with different interests.

### 8.2 Pulsing on a schedule

Pulse well inside the interval, for example once every 24 h for a 48 h interval.

Important: a fully automatic pulse defeats the purpose. If cron pulses for you, the switch only fires when the machine stops, not when you stop. An attacker who takes your running laptop keeps you "alive". Prefer pulsing by hand, and use cron only for a reminder. If you do automate it, keep the machine encrypted and make sure someone notices if you disappear.

Cron reminder (macOS notification at 09:00 local time every day; 09:00 IST = 03:30 UTC):

```bash
0 9 * * * osascript -e 'display notification "send zpulse heartbeat" with title "zpulse"'
```

Automatic pulse, if you accept the risk above (`crontab -e`; cron has a minimal environment, so use full paths):

```bash
0 9 * * * cd /path/to/zpulse && ZPULSE_BACKEND=testnet /usr/bin/env python3 -m zpulse.principal pulse --name dossier >> data/logs/pulse.log 2>&1
```

macOS launchd equivalent (`~/Library/LaunchAgents/zpulse.pulse.plist`, load with `launchctl load ~/Library/LaunchAgents/zpulse.pulse.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>zpulse.pulse</string>
  <key>WorkingDirectory</key><string>/path/to/zpulse</string>
  <key>EnvironmentVariables</key><dict><key>ZPULSE_BACKEND</key><string>testnet</string></dict>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/env</string><string>python3</string><string>-m</string>
    <string>zpulse.principal</string><string>pulse</string><string>--name</string><string>dossier</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/path/to/zpulse/data/logs/pulse.log</string>
  <key>StandardErrorPath</key><string>/path/to/zpulse/data/logs/pulse.log</string>
</dict></plist>
```

Never pulse the same vault from two machines. Each machine has its own `seq` file. If a heartbeat arrives with a seq not above the last one a watcher saw, the watcher rejects it as a replay, and the switch may fire.

### 8.3 Keeping watchers running

Each watcher runs on its own always-on machine. Use a long poll; minutes are fine for a 48 h interval.

Simple, with nohup:

```bash
cd /path/to/zpulse
mkdir -p data/logs
ZPULSE_BACKEND=testnet nohup python3 -m zpulse.watcher run --id w1 \
    --packet /path/to/w1.json --poll 300 >> data/logs/w1.log 2>&1 &
```

Linux systemd (`/etc/systemd/system/zpulse-w1.service`, then `sudo systemctl enable --now zpulse-w1`):

```ini
[Unit]
Description=zpulse watcher w1
After=network-online.target
Wants=network-online.target

[Service]
User=zpulse
WorkingDirectory=/path/to/zpulse
Environment=ZPULSE_BACKEND=testnet
ExecStart=/usr/bin/python3 -m zpulse.watcher run --id w1 --packet /path/to/w1.json --poll 300
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Check on a watcher at any time:

```bash
tail -n 20 data/logs/w1.log
python3 -m zpulse.watcher run --id w1 --packet /path/to/w1.json --once
```

One `run` process watches one vault. A watcher in several vaults runs one process per packet (same `--id`, same identity, same wallet).

### 8.4 What happens on restart

- The watcher's state is saved after every poll to `data/watchers/<id>/<vault_id>.json`. On restart it carries on from there. It skips txids it has already seen.
- If the deadline passed while the watcher was down and no newer heartbeat is on chain, it releases on its first poll.
- If a release fails (for example the wallet has no spendable note yet), it logs `release failed, will retry: ...` and retries every poll.
- Errors during a poll are logged as `poll error: ...` and the loop keeps going.
- The packet file is read on every start. Keep it.

### 8.5 Backups

Principal:

- `data/vaults/<name>/principal.key`, `duress.key`, `seq`, `vault.json`, `vault.bin`.
- `wallet/data/principal/` (wallet keys and the age identity `key.txt`).
- Restore `seq` with its latest value. A seq that goes backwards makes watchers reject your heartbeats.

Watcher:

- `data/watchers/<id>/identity.key` (without it the share cannot be opened).
- The packet file.
- `data/watchers/<id>/<vault_id>.json` (state; if lost, the watcher re-arms from the packet and re-reads the chain history).
- `wallet/data/watcher-<id>/` on testnet.

Store backups encrypted. Anyone with `principal.key` can forge heartbeats and keep the switch alive forever.

### 8.6 Duress: how it works and its limits

- `pulse --duress` sends a heartbeat signed with `duress.key`. It has the same shape, size and amount as a normal one. An observer, or the person forcing you, sees a successful pulse.
- Every watcher that sees it releases immediately. A later normal heartbeat or cancel does not stop that.
- Limits:
  - `duress.key` sits next to `principal.key` in the same directory. Someone who takes your laptop and knows how zpulse works can use the normal key. Keep the two keys apart (for example under different passphrases on encrypted storage).
  - Release still needs t watchers to be online and to have funds (see 6.6).
  - On testnet, release waits for the duress heartbeat to be mined and for the watchers' next poll.

### 8.7 How to cancel

```bash
ZPULSE_BACKEND=testnet python3 -m zpulse.principal cancel --name dossier
```

Watchers that are still armed shred their share. Watchers that already released are not affected. A coerced cancel looks the same as a real one. If you fear being forced to cancel, consider deleting `principal.key` once watchers are running, so cancel is impossible (but then you can no longer pulse either, and the switch will fire at the deadline).

---

## 9. Troubleshooting

**`send failed: ... insufficient balance` (principal pulse or cancel)**
The principal wallet has too little spendable shielded money. Run `python3 wallet/zwallet.py principal sync` and check `shielded_zat`. Funds just received need `ZCASH_MIN_CONF` confirmations (default 1). Transparent funds must be shielded first (`python3 wallet/zwallet.py principal shield`). Each pulse needs n x heartbeat_zat plus the fee. On mock this means the principal spent its 10 fake ZEC.

**`release failed, will retry: ... insufficient` (watcher log)**
The watcher's heartbeat note is not spendable yet (wait 1 or 2 blocks), or the watcher received less than 11,000 zat in total (use `--heartbeat-zat 20000`). It retries every poll by itself.

**Watchers see heartbeats with no memo ("memo is None") and release at the deadline**
Compact-block sync carries no memos. `ZWallet.received()` runs `zcash-devtool wallet enhance` before `list-tx` to fetch them. If enhance fails, the watcher log shows `warning: enhance failed: ...` and that poll sees no memos. Usually a network problem; it retries next poll. Make sure you have the current `wallet/zwallet.py`, and that the deadline has enough grace to survive a few failed polls.

**Watchers released early**
- They were started before heartbeat #1 was mined, and the first deadline (`sealed_ts + interval + grace`) passed. Follow 6.5.
- interval + grace was too short for block times. Use at least 300 + 120 on testnet.
- The machine slept (see below).
- The principal's `seq` went backwards (restored from an old backup, or pulses from two machines). Watchers log `rejected replayed heartbeat seq N (last M)`.
- A heartbeat failed signature checks: `rejected heartbeat seq N: bad signature`.
A release cannot be undone. Make a new vault.

**Faucet errors**
`wallet/faucet.py` prints the faucet status, then the claim result. Public faucets limit by IP and day and were unreliable during the hackathon. Try the other faucet, try later, or ask a mentor for about 0.05 TAZ.

**Sync is slow**
The first sync of a new wallet scans from its birthday. Each watcher poll on testnet runs sync, enhance and list-tx. Use a longer `--poll` (45 s for a demo, minutes in production). A wallet created long ago scans more blocks. Reveal's view-only wallet starts at `release_birthday`; if it was missing, it scans from the wallet default, which takes longer. Rerun reveal; it resumes.

**Laptop sleep**
A sleeping machine stops every watcher and the principal. When it wakes, watchers whose deadline passed release within seconds. A principal that sleeps through its pulse time misses it. Run watchers on always-on machines, keep the grace period long (hours in real use), and do not run a demo on a laptop that may sleep.

**`no vault named 'X' in data` / `no manifest at ...` / `no identity for watcher wN ... run enroll first`**
You are in the wrong directory, or using a different `--data`. Commands use `./data` by default, relative to the current directory.

**`zcash-devtool binary not found at ...`**
Build it (6.1) or set `ZCASH_DEVTOOL`.

**`manifest has no release_ufvk (was it created on the mock backend?)`**
You ran reveal with `--backend testnet` on a mock vault. Use `--backend mock`.

**`vault.bin does not match ciphertext_sha256 in the manifest`**
The `vault.bin` next to `vault.json` is the wrong file or damaged. Get the published copy.

---

## 10. Command reference

All zpulse CLIs accept `--backend {mock,testnet}` (default `$ZPULSE_BACKEND`, else `mock`) and `--data DIR` (default `data`). They print human lines to stderr and one JSON object to stdout.

### `python3 -m zpulse.principal init`

| flag | type | default | required |
|---|---|---|---|
| `--name` | string `[A-Za-z0-9_-]{1,32}` | | yes |
| `--file` | path | | yes |
| `--threshold` | int >= 1 | 3 | no |
| `--interval` | int seconds | 120 | no |
| `--grace` | int seconds | 30 | no |
| `--heartbeat-zat` | int zatoshi | 20000 | no |

### `python3 -m zpulse.principal seal`

| flag | default | required |
|---|---|---|
| `--name` | | yes |

### `python3 -m zpulse.principal pulse`

| flag | default | required |
|---|---|---|
| `--name` | | yes |
| `--duress` | off | no |

### `python3 -m zpulse.principal cancel`

| flag | default | required |
|---|---|---|
| `--name` | | yes |

### `python3 -m zpulse.principal status`

| flag | default | required |
|---|---|---|
| `--name` | | yes |

### `python3 -m zpulse.watcher enroll`

| flag | type | default | required |
|---|---|---|---|
| `--id` | string `[a-z0-9_-]{1,16}` | | yes |
| `--vault` | path to `vault.json` | | yes |

### `python3 -m zpulse.watcher run`

| flag | type | default | required |
|---|---|---|---|
| `--id` | string | | yes |
| `--packet` | path to `packets/<id>.json` | | yes |
| `--poll` | float seconds | 5 | no |
| `--once` | flag | off | no |

### `python3 -m zpulse.reveal`

| flag | type | default | required |
|---|---|---|---|
| `--vault` | path to `vault.json` | | yes |
| `--watch` | flag | off | no |
| `--poll` | float seconds | 5 | no |
| `--timeout` | float seconds, 0 = never | 0 | no |

### `python3 dashboard/serve.py`

| flag | default |
|---|---|
| `--data` | `./data` |
| `--port` | `8787` |
| `--host` | `127.0.0.1` |

Endpoints: `GET /` (the page), `GET /state` (JSON snapshot).

### `python3 wallet/zwallet.py <name> <command>`

| command | does |
|---|---|
| `init [birthday]` | Create a testnet wallet (if missing) and print its addresses. |
| `sync` | Sync with lightwalletd, then print the balance. |
| `addresses` | Print unified and transparent addresses. |
| `balance` | Print the balance from the last sync. |
| `height` | Print the chain tip height known to the wallet. |
| `shield` | Shield all transparent funds. |
| `send-t <taddr> <zat>` | Send to a transparent `tm...` address. |
| `send-z <ua> <zat>` | Send to a shielded or unified address. |
| `tx` | List wallet transactions (JSON). |
| `gen-addr` | Make a new diversified address. |
| `ufvk` | Print the unified full viewing key. |
| `recv [min_height]` | Run enhance, then list received outputs with memos (JSON). |

All of these except `balance`, `addresses`, `height`, `tx`, `gen-addr` and `ufvk` use the network, and `shield`, `send-t` and `send-z` spend funds.

### `python3 wallet/faucet.py [address]`

Prints the Valar faucet status. With an address, requests TAZ and polls the claim status for up to 5 minutes.

### `bash demo/mock_demo.sh`

No flags. Deletes and recreates `./data`. Runs the full mock flow and the duress flow.

### Exit codes

| program | code | meaning |
|---|---|---|
| principal, watcher, reveal | 0 | Success. |
| principal, watcher, reveal | 1 | Error reported by zpulse (`error: ...` on stderr, `{"ok": false, "error": ...}` on stdout), or an uncaught exception such as a missing `--file`. |
| principal, watcher, reveal | 2 | Bad command-line arguments (argparse). |
| reveal | 2 | Not enough shares yet (without `--watch`). |
| reveal | 3 | `--watch --timeout N` expired. |
| watcher run | 0 | Stopped with Ctrl-C (prints the state summary). Killed with a signal: no summary, but state is saved after every poll. |
| zwallet.py | 0 / 2 | Success / usage or unknown command. A devtool failure raises an exception (exit 1). |
| mock_demo.sh | 0 / 1 | PASS / FAIL. |

## 11. Web apps

### 11.1 Watcher app (for the people holding shares)

```bash
python3 watcherapp/serve.py --id w1 --backend mock --data ./data --port 8791
```

Open http://127.0.0.1:8791/. The page has three steps:

1. **Enroll.** Paste the `vault.json` the principal sent you and press Enroll. The app creates your identity (if new), derives a fresh address in your own wallet, and shows an enroll JSON. Copy it and send it back to the principal. Nothing secret leaves your machine.
2. **Load packet.** After the principal seals the vault, paste the packet JSON they send you. The app checks it is really addressed to you (watcher id, vault id and your verify key must match) and stores it with mode 0600.
3. **Run.** Press Start. The app launches `python3 -m zpulse.watcher run` as a background daemon and shows its state every 2 seconds: ARMED / RELEASED / CANCELLED, countdown to the deadline (amber under 60 s, red when passed), heartbeats seen, last sequence number, release txid (linked to the explorer on testnet) and the last log lines. "Check once" runs a single poll. Stop asks for an inline confirmation.

The daemon keeps running if you close the page or stop the server; the next server start picks it up again. All POST requests need the `X-Zpulse: 1` header and a localhost Host, so another website cannot drive the app from your browser. The app never sends your secret key, your sealed share, or any `*.key` file over HTTP.

On testnet (`--backend testnet`) the identity panel also shows your wallet address and shielded balance. Enrolling creates the wallet on first use, which takes a few seconds.

### 11.2 Network monitor (what is happening on Zcash)

```bash
python3 -m pip install grpcio protobuf
python3 netviz/serve.py --port 8792 --data ./data [--server testnet.zec.rocks:443] [--blocks 40] [--txids FILE]
```

Open http://127.0.0.1:8792/. The server polls lightwalletd every 5 s over gRPC (read-only calls: GetLightdInfo, GetLatestBlock, GetBlockRange, GetTransaction, GetMempoolTx) and the page polls the server every 3 s. You see the tip height, seconds since the last block, the average block interval, a stream of the last 40 blocks with stacked bars for Ironwood / Orchard / Sapling / transparent activity, the mempool, and a "zpulse transactions" list: every heartbeat and release txid found in `data/` (and any you add via the input or `--txids`), with its block and a confirmation counter. Block times are shown in UTC and IST.

If the page says "connecting to lightwalletd", the server could not reach `testnet.zec.rocks:443`; the last good snapshot stays on screen. On macOS the server sets `GRPC_DNS_RESOLVER=native` for you because gRPC's own resolver fails there.
