ZPULSE - a dead man's switch on Zcash
=====================================

Zcash Presents: Free Society Hackathon, Mumbai, 4 Nov 2026. Privacy track.
Repo: https://github.com/vipenl26/zpulse

WHAT IT IS
----------
A journalist encrypts a dossier and splits the key into 5 shares, held by 5
independent watchers (an NGO, a lawyer, an editor, a friend abroad...).
Every 48 hours she sends ONE shielded Zcash transaction that pays every
watcher a few thousand zatoshi with a signed "I'm alive" memo.

If the heartbeats stop past the grace period, each watcher independently
posts its share as a shielded memo to a public release address. Anyone with
the public manifest can collect 3 shares, rebuild the key and read the
dossier. One share alone reveals nothing. A duress heartbeat (same look,
signed with a second key) releases immediately.

No server. No recipient. No smart contract. Zcash shielded memos are the
only transport. Watchers never talk to each other.

IT RAN ON TESTNET TODAY
-----------------------
Vault d333806ab0c937e7: two heartbeats, then silence, then five independent
share releases, then a reveal from a view-only wallet. All txids with
explorer links are in README.md.

QUICK START (offline, 40 seconds)
---------------------------------
  git clone https://github.com/vipenl26/zpulse && cd zpulse
  pip install -r requirements.txt
  bash demo/mock_demo.sh
  python3 -m pytest tests/            # 21 tests

WHAT IS IN HERE
---------------
  zpulse/          the protocol: principal, watcher, reveal CLIs
  demo/            mock demo script and testnet runbook
  dashboard/       projector view of a live vault (port 8787)
  site/            landing page and 4-scenario animated visualizer
  watcherapp/      web app for watchers (enroll, load packet, run)
  netviz/          live Zcash testnet monitor over lightwalletd gRPC
  wallet/          zcash-devtool wrapper, faucet helper, devtool patch
  tests/           pytest suite
  INSTRUCTIONS.md  complete manual, every command and flag
  SUBMISSION.md    one-page writeup for judges
  THREAT_MODEL.md  what holds and what does not
  SPEC.md          the interface contract the code was built from

HOW TO RUN ON ZCASH TESTNET
---------------------------
  1. Build zcash-devtool at commit 0b3436b with the patch in wallet/
     (see README.md, "Quickstart (Zcash testnet)").
  2. python3 wallet/zwallet.py principal init   -> fund the address (~0.05 TAZ)
  3. export ZPULSE_BACKEND=testnet
  4. principal init -> watchers enroll -> principal seal -> principal pulse
     -> start watchers (after heartbeat 1 is mined) -> go silent -> reveal
  Full steps with flags: INSTRUCTIONS.md section 6.

CREDITS
-------
  Paid Private File (Zcash forum): viewing key as smart escrow, diversified
  address per order. z-canary: threshold keepers, memos as transport.
  Stack: Python 3.12, cryptography, pynacl, zcash-devtool / librustzcash,
  lightwalletd at testnet.zec.rocks. Apache-2.0.

STATUS
------
  Testnet prototype, not audited. Do not use for real reporting yet.
