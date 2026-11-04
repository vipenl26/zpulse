#!/usr/bin/env bash
# zpulse: start (or stop) every local service in one go.
#
#   bash run_all.sh            start everything (testnet backend by default)
#   bash run_all.sh mock       start everything against the offline mock chain
#   bash run_all.sh stop       stop everything started by this script
#   bash run_all.sh status     show what is running and the URLs
#
# Services and ports:
#   8790  site/            landing page + visualizer   http://localhost:8790/
#   8787  dashboard/       live vault dashboard        http://localhost:8787/
#   8792  netviz/          live Zcash network monitor  http://localhost:8792/   (testnet only)
#   8791  watcherapp/      watcher app for WATCHER_ID  http://localhost:8791/
#   8793  principalapp/    principal app (init, seal, pulse, duress, cancel)  http://localhost:8793/
#
# Env: WATCHER_ID (default w1), DATA (default ./data), ZPULSE_BACKEND (set by the mode).
set -u
cd "$(dirname "$0")"
MODE="${1:-testnet}"
WATCHER_ID="${WATCHER_ID:-w1}"
DATA="${DATA:-./data}"
LOGS="$DATA/logs"; PIDS="$DATA/run_all.pids"
mkdir -p "$LOGS"

start() {  # name, port, command...
  local name="$1" port="$2"; shift 2
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  $name: already listening on :$port (left as is)"; return
  fi
  nohup "$@" > "$LOGS/$name.log" 2>&1 &
  echo "$name $! $port" >> "$PIDS"
  echo "  $name: started on http://localhost:$port/  (log: $LOGS/$name.log)"
}

case "$MODE" in
  stop)
    [ -f "$PIDS" ] || { echo "nothing recorded in $PIDS"; exit 0; }
    while read -r name pid port; do
      kill "$pid" 2>/dev/null && echo "  stopped $name (pid $pid)" || echo "  $name (pid $pid) was not running"
    done < "$PIDS"
    rm -f "$PIDS"; exit 0;;
  status)
    for p in 8790 8787 8792 8791 8793; do
      if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then echo "  :$p up   http://localhost:$p/"; else echo "  :$p down"; fi
    done; exit 0;;
  mock|testnet) export ZPULSE_BACKEND="$MODE";;
  *) echo "usage: $0 [testnet|mock|stop|status]"; exit 2;;
esac

echo "zpulse services (backend=$ZPULSE_BACKEND, data=$DATA, watcher=$WATCHER_ID)"
: > "$PIDS"
start site       8790 python3 -m http.server 8790 --directory site
start dashboard  8787 python3 dashboard/serve.py --data "$DATA" --port 8787
start watcherapp 8791 python3 watcherapp/serve.py --id "$WATCHER_ID" --data "$DATA" --backend "$ZPULSE_BACKEND" --port 8791
start principalapp 8793 python3 principalapp/serve.py --data "$DATA" --backend "$ZPULSE_BACKEND" --port 8793
if [ "$ZPULSE_BACKEND" = testnet ]; then
  if python3 -c "import grpc" 2>/dev/null; then
    start netviz   8792 python3 netviz/serve.py --port 8792 --data "$DATA"
  else
    echo "  netviz: skipped (pip install grpcio protobuf to enable)"
  fi
else
  echo "  netviz: skipped on mock (it reads the real testnet)"
fi
echo
echo "open:  http://localhost:8790/        landing page (+ /viz.html)"
echo "       http://localhost:8787/        dashboard"
echo "       http://localhost:8791/        watcher app ($WATCHER_ID)"
echo "       http://localhost:8793/        principal app"
[ "$ZPULSE_BACKEND" = testnet ] && echo "       http://localhost:8792/        network monitor"
echo "stop:  bash run_all.sh stop"
