# zpulse on Zcash testnet — runbook

Same CLIs as the mock demo, with `--backend testnet` (or `export ZPULSE_BACKEND=testnet`).
Wallets are zcash-devtool wallets under `wallet/data/<name>/`, server testnet.zec.rocks.
Expect ~75 s per block: use minutes, not seconds, for interval/grace.

Node names on testnet:

| role       | wallet                     | created by                         | spend key? |
|------------|----------------------------|------------------------------------|------------|
| principal  | `principal` (funded)       | already exists                     | yes        |
| watcher wN | `watcher-wN`               | `watcher enroll` (if missing)      | yes        |
| release    | `release-<vault_id>`       | `principal init` (only for UA+UFVK)| yes, unused|
| revealer   | `reveal-<vault_id>`        | `reveal` via `init_fvk(release_ufvk)` | **no** (view-only) |

## 0. Preconditions

```bash
cd /Users/vomni/Documents/zhackathon/zpulse
export ZPULSE_BACKEND=testnet
python3 wallet/zwallet.py principal balance     # needs shielded TAZ: n * heartbeat_zat * pulses + fees
```

## 1. Create the vault (interval 10 min, grace 5 min)

```bash
echo "Dossier: ..." > data/dossier.txt
python3 -m zpulse.principal init --name leak --file data/dossier.txt \
    --threshold 2 --interval 600 --grace 300 --heartbeat-zat 20000
```
Creates `release-<vault_id>`, stores its address + UFVK (+ birthday height) in the public manifest.

## 2. Watchers enroll (each creates its own wallet + a fresh diversified address)

```bash
for w in w1 w2 w3; do python3 -m zpulse.watcher enroll --id $w --vault data/vaults/leak/vault.json; done
python3 -m zpulse.principal seal --name leak
```

## 3. Start watchers (one terminal each, or background)

```bash
for w in w1 w2 w3; do
  python3 -m zpulse.watcher run --id $w --packet data/vaults/leak/packets/$w.json --poll 30 \
     > data/logs/leak-$w.log 2>&1 &
done
```
Each poll runs `zcash-devtool sync` + `list-tx` on its own wallet; heartbeats are matched
by the vault_id inside the memo (devtool does not always print the receiving address).

## 4. Heartbeat (one tx, n shielded outputs, same signed memo)

```bash
python3 -m zpulse.principal pulse --name leak       # -> {"txid": ..., "seq": 1, "ts": ...}
```
Show it: `https://testnet.zecblock.com/tx/<txid>` (amounts/memos are shielded: only the tx is visible).
The first heartbeat also funds each watcher's later release tx (1000 zat + fee). Watcher
wallets need the heartbeat note to be confirmed/spendable before they can release.

## 5. Go silent -> release -> reveal

After `interval + grace` with no new heartbeat (watchers use max(local clock, latest block time)),
each watcher sends `ZP1 SH <vault> <wid> <share> <sig>` to the release address.

```bash
python3 -m zpulse.reveal --vault data/vaults/leak/vault.json --watch --poll 60 --timeout 3600
```
`reveal` imports the release UFVK into a view-only wallet, verifies each share signature
against the manifest, and decrypts once `threshold` distinct watchers have released.

## Duress variant

```bash
python3 -m zpulse.principal pulse --name leak --duress   # looks identical on chain; watchers release at once
```

## Cancel

```bash
python3 -m zpulse.principal cancel --name leak           # watchers mark cancelled and shred their share
```

## Troubleshooting
- `release failed, will retry: ... insufficient` in a watcher log: its heartbeat note is not yet
  spendable (wait 1-2 blocks) or the watcher wallet is unfunded; it retries every poll.
- `reveal` sees 0 shares: the view-only wallet may need a full sync from `release_birthday`;
  rerun, it resumes.
- Wallet state is per machine under `wallet/data/`; vault state under `data/`.

## Lessons from the live run (4 Nov 2026)

- **Memos need `enhance`.** Compact-block sync carries no memo bytes. `ZWallet.received()` now runs `zcash-devtool wallet enhance` before `list-tx`; without it every heartbeat reads as "payment with no memo" and the watchers fire at the deadline.
- **Confirmations.** The devtool's default spend policy wants 10 confirmations on notes received from others. `send`/`pay` are called with `--min-confirmations 1` (env `ZCASH_MIN_CONF`); `pay` needed a 10-line patch in `wallet/zcash-devtool/src/commands/wallet/pay.rs`.
- **Sync before you spend.** `principal pulse` syncs first so the change from the previous heartbeat is visible.
- **Order of operations.** Seal, pulse immediately, then start the watchers once the heartbeat is mined (1-3 min on testnet). The first deadline is `sealed_ts + interval + grace`, so a slow block plus early watchers can fire the switch before heartbeat #1 lands.
- **Load.** Five watchers each running sync + enhance + list-tx every 30 s is heavy on a laptop; `--poll 45` is comfortable. A sleeping laptop stalls every watcher, and they all release within seconds of waking.
- **Timings that worked:** `--interval 300 --grace 120 --heartbeat-zat 20000`. Release txs cost 11,000 zat (1,000 to the release address + 10,000 fee), so one heartbeat funds a release with room to spare.
