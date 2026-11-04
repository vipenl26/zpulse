Post for https://forum.zcashcommunity.com/t/what-are-you-building-on-zcash/55172 (reply to the thread):

---

**zpulse: a dead man's switch for whistleblowers, on shielded memos** (Free Society Hack, Mumbai, built today)

A journalist encrypts a dossier and splits the key into 5 Shamir shares held by independent watchers (an NGO, a lawyer, an editor, a friend abroad). Every 48 h she sends ONE shielded transaction that pays every watcher a few thousand zatoshi with a signed "I'm alive" memo. Watchers detect it with only their own wallet, at a diversified address made for that vault (the "viewing key as smart escrow" idea from Paid Private File). If the heartbeats stop past a grace period, or a duress heartbeat (same look, second key) arrives, each watcher independently posts its share as a memo to a release address whose viewing key is public. Anyone collects 3 shares and decrypts. One share reveals nothing; two seized watchers can't stop it; two colluding can't trigger it. No server, no recipient, no smart contract; watchers never talk to each other. The heartbeat payments also fund the watchers' release txs.

It ran end to end on testnet today in the Ironwood pool: two heartbeats, silence, five independent releases, and a reveal from a view-only wallet. Heartbeat tx: 63ddd6ac26d2b9682bfea0c396b431e7946fff4885f18f52581cfbd952105497

Repo (Apache-2.0, Python + zcash-devtool): https://github.com/vipenl26/zpulse
Site + animated explainer: https://zpulse.vipenl26.workers.dev/

Two things for the ecosystem from today:
- compact-block sync carries no memos; a script must run `zcash-devtool wallet enhance` before `list-tx` or every received memo is empty. Cost us a vault that released early.
- `zcash-devtool wallet pay` has no `--min-confirmations` (send does), so scripts can't spend a note received two blocks ago. 14-line patch in the repo; happy to upstream if the maintainers want it.

Credit to z-canary (threshold keepers on memos) and Paid Private File. Questions and poking at the threat model very welcome.
