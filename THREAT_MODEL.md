# zpulse threat model

zpulse aims for one property. **The dossier becomes public if, and only if, the principal stops sending valid heartbeats (or sends a duress heartbeat), and at least t of n watchers act on it.** Before that, no one, including any t-1 watchers, can read it.

## Actors and what is trusted

| actor | holds | trusted for |
|---|---|---|
| principal | principal + duress signing keys, the vault key until `seal` | sending heartbeats. The key is deleted after sealing, so seizing the principal's laptop does not decrypt the vault. |
| watcher (x n) | own Ed25519 identity, one sealed share, own wallet | **liveness**: publish its share when the rule says so. **Restraint**: do not publish early. |
| release address | viewing key public in the manifest | nothing. It is a public bulletin board on Zcash. Anyone can read it. Its spend key protects only a few zatoshi. |
| lightwalletd / chain | blocks | availability and ordering. Not secrecy: memos are shielded and every message is signed. |
| the public | manifest, `vault.bin`, release viewing key | reconstructing once t shares are posted. |

No party ever holds spend authority over another party's funds. Watchers detect payments with only their own keys ("viewing key as smart escrow", from Paid Private File).

## Threats

### Coercion of the principal
- *"Keep checking in or else."* The **duress key** signs a heartbeat that looks the same as a normal one in shape, size and amount, but it tells watchers to release now. The attacker sees a successful pulse. The principal must keep the two keys apart in their head, for example as two passphrases.
- *"Cancel the vault."* `cancel` exists, and a coerced cancel cannot be told apart from a real one. If you fear this, don't use cancel: drop the principal key and let the switch run out. (Future work: cancel needs a co-signature from k watchers.)
- *Seizing the laptop.* That gets the signing keys, so the attacker can **forge heartbeats** and keep the switch alive forever. This is the main gap that remains open. Mitigations: keep keys only on encrypted, hardware-backed storage; the duress key is still useful if the principal is held but the key is not; watchers can also be told off-chain to release when a heartbeat arrives together with credible news of arrest.

### Early collusion (t watchers)
Any t watchers can combine their shares and decrypt before the deadline. This is how threshold systems work and can't be avoided. It is handled by choice of **who**, not by code: pick watchers who don't know each other, sit in different jurisdictions and have different incentives, and keep the roster private (the manifest lists only addresses and public keys). t-1 shares reveal nothing (Shamir, information-theoretic).

### Seizure or silencing of watchers
- Seizing **fewer than n-t+1** watchers does not stop release: the other t still fire.
- Seizing **t or more** gives the attacker the key. Same mitigation as collusion: spread them out.
- Shares are sealed (SealedBox) to each watcher's own key, so a stolen packet file is useless without that watcher's `identity.key`.
- A watcher whose wallet is drained still has enough to release, because every heartbeat tops it up.

### Watchers offline
If fewer than t watchers are online when the deadline passes, release waits until enough come back. Watchers keep their state across restarts. Choose n comfortably above t (e.g. 3-of-5).

### Forged or replayed heartbeats
- Every heartbeat is signed over `zpulse|HB|vault_id|seq|ts`. Without the principal key it can't be forged.
- **Replay**: a heartbeat only counts if its `seq` is greater than the last one seen. The watcher also uses the **chain time of the tx carrying it**, not the `ts` inside, so an old memo sent again in a new tx cannot push the deadline forward.
- The vault id is in the signature, so a heartbeat for vault A can't be replayed against vault B. The diversified address per vault already keeps them apart on the wire.
- Share memos are signed by the watcher key listed in the manifest, so junk posted to the release address is ignored by `reveal`.

### Clock skew
Watchers use `now = max(local clock, latest chain tx time)`, and the deadline comes from the chain time of the heartbeat tx. A watcher with a fast clock might release a little early, and one with a slow clock a little late. Neither can be pushed by an attacker beyond what the chain timestamps allow. Grace periods should be much longer than block time and expected skew (hours in production, 30-60 s in the demo).

### Chain reorgs
A heartbeat in a block that gets reorged away might briefly count and then disappear. The harm is a deadline that is too late by one interval at most, and the principal's wallet rebroadcasts. A release in a reorged block simply lands again. Shares are idempotent and `reveal` dedupes by watcher. On Zcash, reorgs deeper than a few blocks are rare. Production should wait for a few confirmations before acting.

### Metadata
- Shielded transactions hide sender, receivers, amounts and memos. An observer can't tell which shielded txs are zpulse heartbeats or who the watchers are.
- **Timing remains**: "this wallet makes a shielded tx every 48 h" could stand out to a powerful observer of the principal's network. Mitigations: add jitter to the schedule, send through Tor/Nym, and use a light client that doesn't leak which addresses it watches.
- lightwalletd learns the IP and sync pattern of each client. Use Tor, or run your own lightwalletd.
- The release address is public **by design**. Once shares land there, the world sees them, which is the point.
- The manifest contains the watchers' shielded addresses. Publish it only when you choose to (it is needed for public reconstruction). If you are worried, share it with your watchers ahead of time and post it publicly only at release.

### Ciphertext availability
The key goes on-chain. The ciphertext is not (it would be far too big for memos). If every copy of `vault.bin` disappears, release is useless. Mirror it widely ahead of time: it is safe to publish, since it is AES-256-GCM, authenticated, and bound to the vault id.

## Not solved
- A seized principal key lets the attacker keep the switch alive forever with forged heartbeats.
- Coerced `cancel` (if you enable it).
- t colluding or captured watchers can release early.
- Watcher liveness relies on people. There is no on-chain enforcement and no slashing.
- Timing metadata at the network layer.
- Long-term ciphertext hosting.
- Legal pressure on watchers to *not* act, which is why they should be spread across jurisdictions.
- This is hackathon code on testnet. No audit, no side-channel hardening, no secure deletion guarantees on the OS.
