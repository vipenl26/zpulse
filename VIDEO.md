# Recording the 2-minute demo video

Judges watch the first 2 minutes only. One screen recording, your voice, every step real.
Record with QuickTime (File > New Screen Recording, microphone on) or Loom. Upload as an
unlisted YouTube or Loom link and paste it into the Stadium draft, "Demo video link".

## Before you press record

1. Protect today's testnet vaults (the mock demo wipes `data/`):
   ```bash
   cp -r data data-testnet-backup
   ```
2. Start every service and leave them running:
   ```bash
   bash run_all.sh
   ```
3. Open these browser tabs, in this order, and zoom the browser to 125%:
   - https://zpulse.vipenl26.workers.dev/            (landing page)
   - https://zpulse.vipenl26.workers.dev/viz#five-release   (visualizer, press Restart just before recording)
   - http://localhost:8792/                          (network monitor; wait until it shows blocks)
   - https://zexplorer.app/testnet/tx/63ddd6ac26d2b9682bfea0c396b431e7946fff4885f18f52581cfbd952105497
4. Open one terminal, font 16 pt or larger, in the repo, with this typed but not run:
   ```bash
   DATA=./data-video bash -c 'rm -rf data-video; ZPULSE_BACKEND=mock bash demo/mock_demo.sh'
   ```
   (`demo/mock_demo.sh` uses `./data`; if you prefer, just run `bash demo/mock_demo.sh`
   after step 1 and restore `data/` from the backup afterwards.)
5. Close anything that shows a `.key` file, `principal.key`, `duress.key` or a packet's `share_sealed`.

## Shot list and script (2:00)

**0:00 to 0:15, landing page.**
"zpulse is a dead man's switch for whistleblowers on Zcash. If I stop checking in, the world
reads my files. If someone forces me to check in, the world reads them anyway."

**0:15 to 0:45, visualizer, play `five-release`.**
"The dossier key is split into five shares held by independent watchers: an NGO, a lawyer,
an editor, a friend abroad. Every 48 hours the journalist sends one shielded transaction that
pays all five with a signed memo. Each watcher reads the chain with its own wallet; they
never talk to each other. When the pulse stops and the grace period passes, each watcher
posts its share as a memo to a public release address. Three shares and anyone can decrypt."
Let the vault open on screen before you switch.

**0:45 to 1:30, terminal, run the mock demo.**
"This is the real code, end to end, on a mock chain so it fits in 40 seconds: init, five
watchers enroll, seal, two heartbeats, then silence." Point at the deadline moving after
heartbeat 2. "Release. Reveal." Point at the dossier line and PASS. "And a duress heartbeat
looks identical on-chain but releases in one second."

**1:30 to 1:55, network monitor, then explorer.**
"It ran on Zcash testnet today." Point at the zpulse transactions panel: heartbeats and
releases with their confirmation counts. Switch to the explorer tab. "One shielded
transaction, five outputs, Ironwood pool. The principal went silent at 09:32 UTC; at 10:41
five watchers released on their own, and a view-only wallet decrypted the dossier."

**1:55 to 2:00, landing page or repo.**
"No server, no recipient, no smart contract. github.com/vipenl26/zpulse."

## Tips

- One take is fine. If you stumble, keep going. Judges score that it is real, not polished.
- Say "testnet" and "real transaction" out loud while the explorer is on screen.
- Keep the mouse still while you talk; move it only to point.
- After recording, restore the data folder if the demo overwrote it:
  ```bash
  rm -rf data && mv data-testnet-backup data
  ```
