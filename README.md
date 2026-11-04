# zpulse — a dead man's switch on Zcash

> If I stop checking in, the world reads my files. If someone forces me to check in, the world reads them anyway.

Built for **Zcash Presents: Free Society Hackathon** (WebZero x Zcash x Bitrefill), Mumbai during Devcon, 4-5 Nov 2026, Privacy track.

## The problem

A journalist or whistleblower holds a dossier. Their best protection is that hurting them makes the dossier come out. Existing dead man's switches have two weak points: they are a **server** (it can be seized or shut down, and its operator can be leaned on), or they hand the file to **one recipient** (who can be pressured, bribed, or silenced).

## What zpulse does

- The **principal** encrypts the dossier with AES-256-GCM and splits the key into **n Shamir shares (t-of-n)**. Each share is sealed to one **watcher**: an NGO, a lawyer, an editor, a friend abroad.
- Every `interval` (48 h in real life, 2 min in the demo) the principal sends **one shielded Zcash transaction** that pays a few thousand zatoshi to every watcher, with a signed `I'm alive` memo. That is the heartbeat.
- Each watcher spots the heartbeat with **its own wallet only**, on a **diversified address made for this one vault**, so a payment maps 1:1 to a vault.
- If heartbeats stop and the grace period passes, each watcher **on its own** posts its share as a shielded memo to a public **release address**. The release address's **viewing key is in the public manifest**, so anyone can import it, collect t shares, rebuild the key and decrypt the public ciphertext.
- A **duress heartbeat** looks the same on-chain, but it's signed with a second key. Watchers treat it as "release now".

There is no recipient. **The release goes to the world.** No smart contract, no server, no relay: shielded memos on Zcash are the only transport.

```
                     principal (laptop)
                 vault.bin = AES-GCM(dossier, K)
                 K -> Shamir t-of-n -> sealed share per watcher
                            |
          every interval: ONE shielded tx, n outputs,
          memo "ZP1 HB <vault> <seq> <ts> <sig>"  (+ zats to fund releases)
                            |
     +-----------+----------+-----------+-----------+
     v           v                      v           v
  watcher w1  watcher w2   ...       watcher wn   (each: own wallet,
  (NGO)       (lawyer)               (friend)      own diversified addr,
     |           |                      |          no server, no peers)
     |  heartbeat missed + grace, or DURESS heartbeat
     v           v                      v
  "ZP1 SH <vault> <w> <share> <sig>"  -> shielded memo to RELEASE ADDRESS
                            |
                            v
        anyone with release viewing key (in public manifest)
        + vault.bin  ->  t shares  ->  K  ->  dossier
```

## Ideas we borrowed and what is new

**From [Paid Private File](https://forum.zcashcommunity.com/t/paid-private-file-sell-a-file-for-shielded-zec-delivered-over-nym/56322)** (sells a file for shielded ZEC, delivered over Nym):
- **Viewing key as smart escrow.** No contract decides that a payment happened. A light client with the right key sees it. Our watchers detect heartbeats with only their own wallet, and the public reveals shares with only a viewing key. Nobody holds spend authority over anyone else's funds.
- **One diversified address per vault per watcher**, so a payment matches exactly one vault and the watcher needs nothing else to tell vaults apart.
- **Light-client detection** through lightwalletd (`testnet.zec.rocks`). No full node needed.

**From [z-canary](https://github.com/unspecifiedcoder/z-canary):**
- **Threshold keepers** (Shamir t-of-n) and **shielded 512-byte memos as the only transport**, with no smart contract.

**New in zpulse:**
- **No recipient.** Shares go to a public release address whose viewing key is published, so the whole world can reconstruct.
- **Heartbeats are payments.** Each heartbeat pays the watchers, and that money funds their release transactions. A watcher never has to top up a wallet in order to act.
- **Duress heartbeat.** Under coercion the principal "checks in" and the switch fires.
- **Watchers never talk to each other and run no server.** Each one decides alone, from chain data.
- Pure Python, testnet, runs on a laptop.

## Website and visualizer

Live (Cloudflare Workers static assets, free plan): **https://zpulse.vipenl26.workers.dev/** (landing page) and **https://zpulse.vipenl26.workers.dev/viz** (four-scenario visualizer). Deploy with `npx wrangler deploy` using `wrangler.jsonc`.

- `site/index.html`: the landing page (what it is, how it works, quotes, full instructions, testnet evidence).
- `site/viz.html`: an animated visualizer with four scenarios: heartbeat reaches every watcher; two watchers down, nothing lost; pulse stops, 5 of 5 release; pulse stops, only 3 of 5 are up.
- `INSTRUCTIONS.md`: the complete user manual, every command and flag, derived from the code.
- `watcherapp/`: a local web app for watchers. Enroll by pasting the principal's `vault.json`, load the sealed packet, start or stop the watcher daemon, and watch the countdown, heartbeats and release status live. `python3 watcherapp/serve.py --id w1 --backend testnet` then open http://127.0.0.1:8791/.
- `netviz/`: a live view of the Zcash testnet read straight from lightwalletd over gRPC: block stream with per-pool shielded activity, mempool, and zpulse's own heartbeat and release transactions with confirmations ticking up. Needs `pip install grpcio protobuf`. `python3 netviz/serve.py` then open http://127.0.0.1:8792/.

Serve them locally with `python3 -m http.server 8790 --directory site` and open http://localhost:8790/.

## Stack

Python 3.12, `cryptography` (AES-256-GCM), `pynacl` (Ed25519 signatures, SealedBox share delivery), pure-Python Shamir over GF(2^8), [`zcash-devtool`](https://github.com/zcash/zcash-devtool) (Rust, librustzcash light client) driven by subprocess, lightwalletd at `testnet.zec.rocks`, explorer `https://testnet.zecblock.com/tx/<txid>`.

Memo formats (UTF-8, at most 512 bytes):

| kind | memo | signed by |
|---|---|---|
| heartbeat | `ZP1 HB <vault_id> <seq> <ts> <sig>` | principal key (alive) or duress key (duress) |
| cancel | `ZP1 CX <vault_id> <ts> <sig>` | principal key |
| share | `ZP1 SH <vault_id> <watcher_id> <share_b64> <sig>` | watcher key |

## Quickstart (mock chain, offline, < 1 min)

```bash
cd zpulse
pip install cryptography pynacl
demo/mock_demo.sh                                     # full flow + duress run, exits non-zero on failure
python3 dashboard/serve.py --data ./data --port 8787  # open http://127.0.0.1:8787
```

Step by step, as the demo script runs it (`ZPULSE_BACKEND=mock` is the default):

```bash
python3 -m zpulse.principal init --name leak --file dossier.txt --threshold 3 --interval 20 --grace 5
for w in w1 w2 w3 w4 w5; do python3 -m zpulse.watcher enroll --id $w --vault data/vaults/leak/vault.json; done
python3 -m zpulse.principal seal --name leak
for w in w1 w2 w3 w4 w5; do python3 -m zpulse.watcher run --id $w --packet data/vaults/leak/packets/$w.json --poll 2 & done
python3 -m zpulse.principal pulse --name leak            # heartbeat (repeat to stay alive)
python3 -m zpulse.principal pulse --name leak --duress   # ...or fire it right now
python3 -m zpulse.principal status --name leak
python3 -m zpulse.reveal --vault data/vaults/leak/vault.json --watch   # anyone can run this
```

## Quickstart (Zcash testnet)

1. Build the light client (Rust 1.99+). zpulse uses [zcash-devtool](https://github.com/zcash/zcash-devtool) at commit `0b3436b` plus a 14-line patch that adds `--min-confirmations` to `pay`:
   ```bash
   git clone https://github.com/zcash/zcash-devtool wallet/zcash-devtool
   git -C wallet/zcash-devtool checkout 0b3436b
   git -C wallet/zcash-devtool apply ../zcash-devtool-pay-min-confirmations.patch
   cargo build --release --manifest-path wallet/zcash-devtool/Cargo.toml
   ```
   (or set `ZCASH_DEVTOOL=/path/to/zcash-devtool` to an existing binary).
2. Create the `principal` wallet and get its unified address:
   ```bash
   python3 wallet/zwallet.py principal init
   ```
3. Fund it with testnet ZEC (TAZ). Any of these works; on 4 Nov 2026 both public faucets were flaky, so a Zcash mentor sending ~0.05 TAZ is the fastest route:
   - Valar faucet, 0.125 TAZ per IP per day: `python3 wallet/faucet.py <unified address>`
   - Jino Labs faucet (browser proof-of-work, 0.1 TAZ per address per day): https://zcashfaucet.jinolabs.xyz
   Faucet payouts arrive shielded; if you receive transparent funds instead, shield them with `python3 wallet/zwallet.py principal shield`. Check with `python3 wallet/zwallet.py principal sync`.
4. Run the same flow as above with `ZPULSE_BACKEND=testnet` and timings that worked live: `--interval 300 --grace 120 --heartbeat-zat 20000`, watchers with `--poll 45`. Every heartbeat and share tx shows up at `https://testnet.cipherscan.app/tx/<txid>`, and the dashboard links to them.

Testnet blocks come about every 75 s, so allow a block or two after each pulse before watchers see it.

## Submission summary

- **One-line pitch:** If I stop checking in, the world reads my files. If someone forces me to check in, the world reads them anyway. A threshold dead man's switch for whistleblowers, on Zcash shielded memos.
- **Testnet transaction (shielded, utest1 addresses, public testnet):** heartbeat #2 of vault `d333806ab0c937e7`: `63ddd6ac26d2b9682bfea0c396b431e7946fff4885f18f52581cfbd952105497` — https://zexplorer.app/testnet/tx/63ddd6ac26d2b9682bfea0c396b431e7946fff4885f18f52581cfbd952105497
- **Release wallet viewing key (optional, lets judges see the five share memos arrive):** `uviewtest10kcem476j43my50effdc2s0d8yulqnfmegyf4uhhtzdaq5yccejjqgwacw080sf359es0gxew3j00620v78nga8yakh24numzefy4ac2ukgzuzn22uswtser0nqhg6zg6ds3q9g70jprms9uld3w5hen5hh6ll0rkm3cgs6j5cpzcfax7uaedqtdts42f43xlkn2kvk20rggwuw3mn7hrfp9grekqrttktdhs7k96re67q97ckzzsanpdcqsccesplf6c0s4mlhp2mdx0u03rlugqw5uh0tq4dj2yamz7cm0jg7usw22ctj7kv9rxyspj00r0k0nveg89k6r52e4q89ddrlg7w8z803dgh2yg29udgjdsxazykwwxdzvhfvu0rg4la8q6t5vfltqhzjavzhrjtqggx82vqetz338jhvp6e9tdrkrw2udyltycfh53cjmvsqaz9cghruv94hmz00mh5jenl376w0m64wud0vx7js9gyhvk8lx`, birth height 4581595. This key reveals only the release address's history (five 1,000-zat share memos); it cannot spend.
- **Theme:** 3, privacy beyond the transaction (encrypted memos as transport, viewing keys as the only read path).

## Privacy and trust note

1. **What stays private:** sender, receiver and amount of every heartbeat and share release are shielded on-chain; every memo (signed heartbeat, Shamir share) is encrypted. The dossier is AES-256-GCM ciphertext until t shares are public. The principal's spending key and signing keys never leave the principal's machine; a watcher never sees them.
2. **What the app reveals, to whom:**
   - the light-wallet server (`testnet.zec.rocks`) sees each watcher's and the principal's IP and sync timing, and which compact blocks they fetch;
   - each watcher sees the heartbeat timing for its vault (not the amounts to other watchers, not the dossier), and holds one share that reveals nothing alone;
   - anyone with the published release viewing key sees the share memos once released (by design: that is the release);
   - our software stores vault state as local files only; there is no server, no analytics, no model provider in the loop;
   - the explorer, if you click a link, sees that you looked up a txid.
3. **Who users must trust:** the principal trusts that fewer than t watchers collude early and that at least t stay reachable; watchers trust nobody; the world trusts the Zcash chain and the published manifest. There is no operator.
4. **What we'd fix next:** route watcher sync and broadcast through Nym; a second co-signer or delay for cancel so a seized principal key cannot silently disarm; a hardware-key heartbeat.

## Next 30 days

- Upstream the `pay --min-confirmations` option to zcash-devtool (the patch in `wallet/zcash-devtool-pay-min-confirmations.patch`, 14 lines, needed by anyone spending freshly received notes from a script).
- Post zpulse in the forum thread "What are you building on Zcash?" and ask z-canary's author about a shared memo format for shares.
- Publish `zpulse/memo.py` + `zpulse/shamir.py` as a small library for memo-signed threshold release, usable by other apps.
- Replace the devtool subprocess with zkool_graphql or librustzcash directly, so a watcher runs on a phone.

## Testnet evidence (4 Nov 2026)

Everything below happened on Zcash testnet, in the Ironwood shielded pool (NU6.3), via `testnet.zec.rocks`. Explorer: `https://zexplorer.app/testnet/tx/<txid>` (or `https://testnet.zecblock.com/tx/<txid>`).

Vault `78d34ee1f3122a6e` (3-of-5, 5 watchers, each with its own wallet and a fresh diversified address):

| step | txid |
|---|---|
| heartbeat #1: ONE tx, 5 shielded outputs of 20,000 zat + signed memo, fee 30,000 zat, block 4581588 | `3b8edb23d74567682d3b27cc8f02ae2f84156263b8beb812a26110a2fb3b6829` |
| w1 releases its share (memo `ZP1 SH ...`) to the release address | `3311672af1f79dc29435fed8e0937d1d0c2b9b03e7f1d8c0fa75c6f44d9f9db0` |
| w2 | `64b34426de15a19d4dc1bc4eb659741ca2016076090344d767a52e104ac4074e` |
| w3 | `09c419abfac51bc1e1fd786c2b00b45588c50c643bab140e92e0149b4d1e6260` |
| w4 | `5ac4745079605d80578e579e2f60645e155bf14400b41db4a4548637d9940de6` |
| w5 | `b3b33bb13763a629979aeae47b58d7483c29b153af47b622af94fc3e4e824694` |

Each release tx was paid for with the 20,000 zat the heartbeat delivered (1,000 zat to the release address + 10,000 zat fee). `python3 -m zpulse.reveal` then created a brand-new **view-only** wallet from the release viewing key in the public manifest, scanned the chain, verified the five watcher signatures, combined three shares and decrypted `vault.bin` in 12 s. No spending key of anyone was involved in the reveal.

Vault `d333806ab0c937e7` (`leak2`, the clean run: two heartbeats, then silence):

| step | txid |
|---|---|
| heartbeat #1 (seq 1), 5 outputs, 09:28 UTC | `449f7b65f4edcce1ab31b68e7068240ff35c46a7168f29693ee053a330c23520` |
| heartbeat #2 (seq 2), 09:31 UTC: every watcher moved its deadline forward | `63ddd6ac26d2b9682bfea0c396b431e7946fff4885f18f52581cfbd952105497` |
| principal goes silent; deadline (last heartbeat + 5 min + 2 min grace) passes | |
| w1 releases | `22db443dd3cb81e6318e6229a40d1a0334eb1f39c9ef5c14261f86e974c7a325` |
| w2 releases | `eea13b5bb1f7cbf7f5853a1f142b8fd9a8dd8b290ba902ec8a3ce8ee3a9ac8cb` |
| w3 releases | `12fb1feea54d5aa8f288c9b7e00a076f65f5b49e176d3c8f08a81379fe68271f` |
| w4 releases | `162fff83aa8e30613f83e82a666c253026f02d4e7b24f4765ddc91a278981694` |
| w5 releases | `1e16f15a98b6e1913fb5d359981a1fc5d2afc6fb037486c9d12583ae14585b76` |

Each watcher decided alone, from its own wallet's view of the chain, and the reveal below used only the public manifest.

Honest note: this vault released because of a bug we found live, not because the principal went silent. Compact-block sync does not carry memos, so watchers saw the payment but a null memo, treated it as "no valid heartbeat", and fired at the deadline. The fix (run `zcash-devtool wallet enhance` before reading memos) is in `wallet/zwallet.py`; the switch did exactly what it should when it could not verify a heartbeat.

## Demo script (3 minutes, projector)

1. **Pitch (20 s).** "If I go quiet, the world reads my files. No server, no recipient, only Zcash."
2. **Set up (40 s).** `init` with a short dossier, enroll 5 watchers, `seal` as 3-of-5. On the dashboard, five watcher cards turn ARMED.
3. **Heartbeat (30 s).** `pulse`. The dashboard flashes, the countdown resets, and the chain ticker shows one tx with 5 outputs. Point out that watchers saw it with their own wallets only.
4. **Silence (60 s).** Stop pulsing. The countdown goes red under 30 s and hits zero. Watcher cards flip to RELEASED one by one, and the t-of-n bar fills.
5. **Reveal (20 s).** At 3 shares, `reveal` (which anyone could run with only the public manifest) prints the dossier. The dashboard shows it in the red box.
6. **Duress (20 s).** Second vault: `pulse --duress`. Instant RELEASED. "A gun to my head is a heartbeat that kills the switch."

## Limits (honest list)

- Hackathon code, testnet only, not audited. Do not protect a real life with it yet.
- Watchers need to be online (or come online) to post shares. If fewer than t watchers are alive, honest and online, the dossier never comes out.
- Any t watchers who collude can release early. Choose them across jurisdictions and interests.
- Heartbeat timing is visible as "some shielded tx at roughly regular intervals". Amounts, recipients and memos are not visible. See [THREAT_MODEL.md](THREAT_MODEL.md).
- The ciphertext (`vault.bin`) has to be published or mirrored somewhere (IPFS, torrent, a few friends). zpulse only moves the key on-chain.
- Testnet timings (2 min) are for the demo. Real use would be 24-72 h with hours of grace.

## Layout

`zpulse/` crypto, Shamir, memo codec, chain backends (mock + zcash-devtool) and the `principal` / `watcher` / `reveal` CLIs · `dashboard/` stdlib HTTP server + single-file live view · `demo/` scripts · `tests/` · `data/` runtime state (contains secret keys: keep it out of git).
