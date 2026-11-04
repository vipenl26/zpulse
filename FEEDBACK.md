# Feedback

## Faucet
- Valar (`POST /api/claim`): every claim returned `{"status":"failed","message":"the wallet cannot pay this address"}` for transparent, unified and Sapling addresses alike (4 Nov, 09:00 UTC). Its status page showed no payouts for ~12 days. Expected a payout or a clear reason.
- Jino Labs: up, but zero drips in 30 days and browser proof-of-work only. The hack guide says it is paused for NU7; a note on the faucet page itself would save time.
- We did not know about Fauzec until the agent guide arrived mid-afternoon. Put the Fauzec curl in the first message of the hacker chat.

## Node or indexer (lightwalletd, Zebra, Zaino)
- `testnet.zec.rocks` was reliable all day. Block times varied 25 s to several minutes; one heartbeat sat ~4 minutes in the mempool, which matters for deadline-based protocols.
- Python gRPC against it needs `GRPC_DNS_RESOLVER=native` on macOS or DNS resolution fails.

## SDK or wallet tool (zcash-devtool, commit 0b3436b)
- Compact-block sync carries no memos; `list-tx` shows `Memo: Memo::Empty` / 0 memos until `wallet enhance` is run. Not documented anywhere we found; cost us a vault that released early. Suggest `list-tx` print a hint when memos are not yet fetched, or `sync` run enhance automatically.
- `pay` has no `--min-confirmations` while `send` does; with the default policy (10 confirmations on untrusted notes) a script cannot spend a note received 2 blocks ago. We patched it (14 lines) and will upstream.
- The explorer link in zcash-devtool docs points to cipherscan, which now redirects to zecblock; zecblock blocks scripted requests.

## Wallet
- A fresh testnet wallet `init` took from a few seconds to over 5 minutes at different times of day with no progress output.

## Docs
- The Luma page gives no submission platform; Stadium was only discoverable from the agent guide. Link Stadium from Luma.

## Other
- A laptop going to sleep stalls every watcher process; a protocol with deadlines should document this. We did.
