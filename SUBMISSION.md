# zpulse — hackathon submission

**Event:** Zcash Presents: Free Society Hackathon (WebZero x Zcash x Bitrefill), Mumbai, 4-5 Nov 2026. **Track:** Privacy (build on Zcash).
**Repo:** https://github.com/vipenl26/zpulse

## One line

A dead man's switch for whistleblowers where Zcash shielded memos are the only transport: if the journalist stops checking in, or checks in under duress, t-of-n independent watchers each post a Shamir share on-chain and the world can decrypt the dossier.

## What we built in one day

- **Protocol + CLIs** (`zpulse/`): principal (init, seal, pulse, duress, cancel), watcher (enroll, run), reveal. AES-256-GCM vault, Shamir 3-of-5 over GF(2^8), Ed25519-signed memos, shares sealed per watcher.
- **Heartbeat = payment.** One shielded tx pays every watcher a few thousand zatoshi at a vault-specific diversified address, memo `ZP1 HB <vault> <seq> <ts> <sig>`. Watchers detect it with only their own wallet. The payments fund their release transactions.
- **Release to the world.** Each watcher independently posts `ZP1 SH <vault> <id> <share> <sig>` to a release address whose viewing key is in the public manifest. Anyone with a view-only wallet collects t shares and decrypts. One share alone reveals nothing; two seized watchers cannot stop it; two colluding watchers cannot trigger it.
- **Duress heartbeat:** identical on-chain, signed with a second key, releases immediately.
- **Watcher web app** (`watcherapp/`): enroll, load packet, run the daemon, live countdown and release status.
- **Live network monitor** (`netviz/`): reads lightwalletd over gRPC; block stream, per-pool shielded activity, mempool, our txids with confirmations.
- **Projector dashboard** (`dashboard/`), **landing site** and **four-scenario animated visualizer** (`site/`), **manual** (`INSTRUCTIONS.md`), **threat model**.

## Proof it works on Zcash testnet (4 Nov 2026)

Vault `d333806ab0c937e7`: heartbeat #1 `449f7b65…c23520`, heartbeat #2 `63ddd6ac…105497` (every watcher moved its deadline), principal went silent, five independent releases (`22db443d…`, `eea13b5b…`, `12fb1fee…`, `162fff83…`, `1e16f15a…`), then `reveal` rebuilt the key from a fresh view-only wallet and printed the dossier. Full txid tables with explorer links are in the README. All transactions are in the Ironwood shielded pool via `testnet.zec.rocks`.

Offline: `bash demo/mock_demo.sh` runs the whole flow, including duress, in 40 seconds. `python3 -m pytest tests/` → 21 passed.

## What we borrowed, honestly

- From **Paid Private File** (Zcash forum): the viewing key as "smart escrow" and a unique diversified address per vault per watcher, detected by a light client with no spend authority.
- From **z-canary**: threshold keepers and 512-byte shielded memos as the only transport, no smart contract.

New here: no recipient (release goes to the world via a published viewing key), heartbeats that pay for the release, duress, watchers that never talk to each other and run no server, and the whole thing in pure Python on a laptop.

## Limits we know about

Testnet prototype, not audited. t colluding watchers can release early. A seized principal key can forge heartbeats. Timing of heartbeats is visible to a watcher even though amounts and memos are shielded. See `THREAT_MODEL.md`.

## Run it

```bash
git clone https://github.com/vipenl26/zpulse && cd zpulse
pip install -r requirements.txt
bash demo/mock_demo.sh            # offline, 40 s
python3 dashboard/serve.py        # http://127.0.0.1:8787
python3 -m http.server 8790 --directory site   # landing + visualizer
```

Testnet steps, every flag, and troubleshooting: `INSTRUCTIONS.md`.
