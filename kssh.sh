#!/bin/bash
# kssh.sh - non-interactive exec + file transfer over the relay tunnel.
# Wraps client.sh's raw ssh command for scripting (client.sh itself is interactive).
#
#   export RELAY_SECRET="secret"
#   ./kssh.sh run "nvidia-smi"        # exec a command, capture stdout
#   ./kssh.sh put train.py /kaggle/working/    # upload
#   ./kssh.sh get /kaggle/working/out.csv .    # download
#   ./kssh.sh ssh                     # print the raw ssh command (= client.sh raw)

set -euo pipefail

RELAY_URL="${RELAY_URL:-https://kagglessh.vercel.app}"
RELAY_SECRET="${RELAY_SECRET:-}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/kaggle_rsa}"
KERNEL_ID="${KERNEL_ID:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ -z "$RELAY_SECRET" ]] && { echo "RELAY_SECRET is required." >&2; exit 1; }
[[ $# -lt 1 ]] && { echo "usage: kssh.sh {run|put|get|ssh} ..." >&2; exit 1; }

# Resolve tunnel host from relay (same endpoint client.sh uses).
url="${RELAY_URL%/}/get"; [[ -n "$KERNEL_ID" ]] && url="${url}?kernel_id=${KERNEL_ID}"
HOST=$(curl -fsSL "$url" -H "X-Relay-Secret: ${RELAY_SECRET}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('hostname',''))")
[[ -z "$HOST" ]] && { echo "No active session on relay." >&2; exit 1; }

OPTS=(-o "ProxyCommand=cloudflared access tcp --hostname ${HOST}"
      -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no
      -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15
      -i "$SSH_KEY")

cmd="$1"; shift
case "$cmd" in
  run)  ssh "${OPTS[@]}" root@localhost "$@" ;;
  put)  local="$1"; scp -r "${OPTS[@]}" "$local" root@localhost:"${2:-/kaggle/working/}" ;;
  get)  scp -r "${OPTS[@]}" root@localhost:"$1" "${2:-.}" ;;
  ssh)  echo "ssh ${OPTS[*]} root@localhost" ;;
  *)    echo "usage: kssh.sh {run|put|get|ssh} ..." >&2; exit 1 ;;
esac
