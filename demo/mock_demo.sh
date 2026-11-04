#!/usr/bin/env bash
# zpulse end-to-end demo on the offline MockChain. Doubles as the e2e test: prints PASS/FAIL.
#   bash demo/mock_demo.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export ZPULSE_BACKEND=mock
PY=python3
D=data
PIDS=()

cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done; wait 2>/dev/null; }
trap cleanup EXIT
die() { echo; echo "FAIL: $*"; exit 1; }
step() { echo; echo "== $*"; }
jget() { $PY -c "import json,sys; d=json.load(open(sys.argv[1])); print(d$2)" "$1"; }
show_deadlines() {  # vault_id
  for w in w1 w2 w3 w4 w5; do
    $PY - "$D/watchers/$w/$1.json" <<'PYEOF'
import json, sys, time
s = json.load(open(sys.argv[1]))
d = s["deadline_ts"]
hms = lambda t: time.strftime("%H:%M:%S", time.gmtime(t))
print(f"   {s['watcher_id']}: {s['status']:9s} last_seq={s['last_seq']} "
      f"deadline {hms(d)} UTC / {hms(d + 19800)} IST (in {d - int(time.time()):>3d}s)")
PYEOF
  done
}
start_watchers() {  # vault name
  for w in w1 w2 w3 w4 w5; do
    $PY -m zpulse.watcher run --id $w --packet $D/vaults/$1/packets/$w.json --poll 2 \
      >"$D/logs/$1-$w.log" 2>&1 &
    PIDS+=($!)
  done
}
T0=$(date +%s)

step "1. clean slate"
rm -rf "$D"; mkdir -p "$D/logs"
SECRET="Dossier #7: the minister's offshore accounts are at Bank X, ledger p.42. -- $(date -u +%FT%TZ)"
printf '%s\n' "$SECRET" > "$D/dossier.txt"

step "2. principal creates vault 'leak' (interval 20s, grace 5s)"
$PY -m zpulse.principal init --name leak --file $D/dossier.txt --threshold 3 --interval 20 --grace 5 \
  >$D/init.json || die "init"
VID=$(jget $D/init.json "['vault_id']")
echo "   vault_id=$VID"
$PY -m zpulse.principal init --name leak --file $D/dossier.txt >/dev/null 2>&1 && die "init overwrote vault"
echo "   (re-init refused, as it should be)"

step "3. five watchers enroll (one fresh address each)"
for w in w1 w2 w3 w4 w5; do
  $PY -m zpulse.watcher enroll --id $w --vault $D/vaults/leak/vault.json >/dev/null || die "enroll $w"
done
ls $D/vaults/leak/enroll

step "4. seal 3-of-5: shares sealed to each watcher, vault key deleted"
$PY -m zpulse.principal seal --name leak >/dev/null || die "seal"
[ -e $D/vaults/leak/.vaultkey ] && die ".vaultkey still present"
echo "   .vaultkey gone; ciphertext sha256 $(jget $D/vaults/leak/vault.json "['ciphertext_sha256'][:16]")..."

step "5. start 5 watcher daemons (poll 2s, logs in $D/logs/)"
start_watchers leak
sleep 1

step "6. heartbeat #1 (one shielded tx paying all 5 watchers)"
$PY -m zpulse.principal pulse --name leak || die "pulse 1"
sleep 3; show_deadlines $VID
sleep 5
step "7. heartbeat #2, 8s later: deadlines move forward"
$PY -m zpulse.principal pulse --name leak || die "pulse 2"
sleep 3; show_deadlines $VID

step "8. principal goes silent... waiting for watchers to release (reveal --watch)"
$PY -m zpulse.reveal --vault $D/vaults/leak/vault.json --watch --poll 2 --timeout 45 >$D/reveal.json \
  || die "reveal did not complete"
show_deadlines $VID
GOT=$(jget $D/reveal.json "['plaintext'].rstrip(chr(10))")
[ "$GOT" == "$SECRET" ] || die "revealed text differs: '$GOT'"
echo "   revealed text matches the original dossier"
echo "   release txs on chain:"
$PY - $D/mockchain.json "release-$VID" <<'PYEOF'
import json, sys
for t in json.load(open(sys.argv[1]))["txs"]:
    for o in t["outputs"]:
        if o["to"].startswith(f"mock:{sys.argv[2]}:"):
            print(f"     h={t['height']:<3} {t['from']:<11} -> release  memo={o['memo'][:60]}...")
PYEOF
cleanup; PIDS=()

step "9. second vault 'coerced': a DURESS heartbeat releases immediately"
printf 'If you read this, I was forced to stop. Check the cloud drive.\n' > $D/dossier2.txt
$PY -m zpulse.principal init --name coerced --file $D/dossier2.txt --threshold 3 --interval 600 --grace 60 \
  >$D/init2.json || die "init 2"
VID2=$(jget $D/init2.json "['vault_id']")
for w in w1 w2 w3 w4 w5; do
  $PY -m zpulse.watcher enroll --id $w --vault $D/vaults/coerced/vault.json >/dev/null || die "enroll2 $w"
done
$PY -m zpulse.principal seal --name coerced >/dev/null || die "seal 2"
start_watchers coerced
sleep 1
$PY -m zpulse.principal pulse --name coerced --duress || die "duress pulse"
TD=$(date +%s)
$PY -m zpulse.reveal --vault $D/vaults/coerced/vault.json --watch --poll 1 --timeout 10 >$D/reveal2.json \
  || die "duress reveal did not complete"
echo "   duress -> revealed in $(( $(date +%s) - TD ))s (interval was 600s)"
[ "$(jget $D/reveal2.json "['plaintext']")" == "$(cat $D/dossier2.txt)" ] || die "duress plaintext differs"
show_deadlines $VID2

echo
echo "PASS  (total $(( $(date +%s) - T0 ))s)"
