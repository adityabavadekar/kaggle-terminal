#!/bin/bash
# client.sh - connects to a Kaggle SSH session via Cloudflare tunnel + relay.

set -euo pipefail

log()  { printf '[ INFO ] %s\n' "$*"; }
ok()   { printf '[  OK  ] %s\n' "$*"; }
warn() { printf '[ WARN ] %s\n' "$*"; }
err()  { printf '[ ERR  ] %s\n' "$*"; }

RELAY_URL="${RELAY_URL:-https://kagglessh.vercel.app}"
RELAY_SECRET="${RELAY_SECRET:-}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/kaggle_rsa}"
LOCAL_PORT="${LOCAL_PORT:-2222}"
SSH_USER="${SSH_USER:-root}"
KERNEL_ID="${KERNEL_ID:-}"

SHOW_LIST="false"
RAW_MODE="false"

# Allow subcommands ('list', 'ls', 'raw') or secret as first argument
if [[ $# -gt 0 && ( "$1" == "list" || "$1" == "ls" ) ]]; then
  SHOW_LIST="true"
  shift
elif [[ $# -gt 0 && ( "$1" == "raw" ) ]]; then
  RAW_MODE="true"
  shift
elif [[ $# -gt 0 && "$1" != -* ]]; then
  RELAY_SECRET="$1"
  shift
fi

# Parse optional arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
  -l|--list|list|ls)
    SHOW_LIST="true"
    shift
    ;;
  raw|--raw)
    RAW_MODE="true"
    shift
    ;;
  -i)
    KERNEL_ID="$2"
    shift 2
    ;;
  -u|--upload-key)
    KEY_PATH="$HOME/.ssh/kaggle_rsa"
    mkdir -p "$HOME/.ssh"
    if [[ ! -f "${KEY_PATH}" ]]; then
      log "Generating dedicated Kaggle SSH key pair (~/.ssh/kaggle_rsa)..."
      ssh-keygen -t rsa -N "" -f "${KEY_PATH}" -C "kaggle-relay"
    fi
    UPLOAD_KEY="${KEY_PATH}.pub"
    log "Uploading public key (${UPLOAD_KEY}) to relay server..."
    curl -fsSL -X POST "${RELAY_URL%/}/pubkey" \
      -H "X-Relay-Secret: ${RELAY_SECRET}" \
      --data-binary "@${UPLOAD_KEY}"
    ok "Public key uploaded successfully!"
    exit 0
    ;;
  -r|--relay)
    RELAY_URL="$2"
    shift 2
    ;;
  -s|--secret)
    if [[ $# -gt 1 && "$2" == "raw" ]]; then
      RAW_MODE="true"
      shift 2
    elif [[ $# -gt 1 && "$2" != -* ]]; then
      RELAY_SECRET="$2"
      shift 2
    else
      shift
    fi
    ;;
  --)
    shift
    break
    ;;
  -*)
    err "Unknown option: $1"
    exit 1
    ;;
  *)
    break
    ;;
  esac
done

if [[ -z "$RELAY_SECRET" ]]; then
  err "RELAY_SECRET is required. Export RELAY_SECRET in your environment or pass it as an argument."
  exit 1
fi

if [[ "$SHOW_LIST" == "true" ]]; then
  log "Fetching active Kaggle sessions..."
  HTTP_STATUS=$(curl -s -w "%{http_code}" -o /tmp/relay_kernels.json "${RELAY_URL%/}/kernels" -H "X-Relay-Secret: ${RELAY_SECRET}" || echo "000")
  if [[ "$HTTP_STATUS" == "401" ]]; then
    err "Unauthorized (HTTP 401). Invalid RELAY_SECRET provided."
    exit 1
  elif [[ "$HTTP_STATUS" != "200" ]]; then
    err "Failed to fetch sessions (HTTP ${HTTP_STATUS})."
    cat /tmp/relay_kernels.json >&2
    exit 1
  fi

  cat /tmp/relay_kernels.json | python3 -c '
import sys, json, datetime

data = json.load(sys.stdin)
kernels = data.get("kernels", [])
if not kernels:
    print("No active Kaggle sessions found.")
    sys.exit(0)

clean = lambda v: " ".join(str(v or "").split())

print(f"\n  Active Kaggle Sessions ({len(kernels)})\n")
for idx, k in enumerate(kernels, 1):
    kid = clean(k.get("kernel_id", "default"))
    user = clean(k.get("username"))
    nb = clean(k.get("notebook"))
    rtype = clean(k.get("run_type")) or "Interactive"
    gpu = clean(k.get("gpu")) or "None"
    cpu = clean(k.get("cpu"))
    ram = clean(k.get("ram"))
    host = clean(k.get("hostname"))
    zone = clean(k.get("gcp_zone"))

    ts = k.get("created_at")
    time_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "N/A"

    print(f"  ┌─ [{idx}] {kid} ")
    if user and user != "N/A": print(f"  │  User    {user} ({rtype})")
    if nb and nb != "N/A":     print(f"  │  Notebook {nb}")
    print(f"  │  GPU     {gpu}")
    if cpu:  print(f"  │  CPU     {cpu}")
    if ram:  print(f"  │  RAM     {ram}")
    if zone: print(f"  │  Zone    {zone}")
    print(f"  │  Tunnel  {host}")
    print(f"  └─ Since   {time_str}")
    print()
'
  exit 0
fi

if [[ "$RAW_MODE" != "true" ]]; then
  log "Fetching tunnel info from relay..."
fi

URL="${RELAY_URL%/}/get"
if [[ -n "$KERNEL_ID" ]]; then
  URL="${URL}?kernel_id=${KERNEL_ID}"
fi

HTTP_STATUS=$(curl -s -w "%{http_code}" -o /tmp/relay_resp.json "${URL}" -H "X-Relay-Secret: ${RELAY_SECRET}" || echo "000")
RESP=$(cat /tmp/relay_resp.json 2>/dev/null || echo "")

if [[ "$HTTP_STATUS" == "401" ]]; then
  err "Unauthorized (HTTP 401). Invalid RELAY_SECRET provided."
  exit 1
elif [[ "$HTTP_STATUS" == "404" ]]; then
  if [[ -n "$KERNEL_ID" ]]; then
    err "No active session found on relay for kernel '${KERNEL_ID}' (HTTP 404)."
  else
    err "No active session found on relay server (HTTP 404)."
  fi
  exit 1
elif [[ "$HTTP_STATUS" != "200" ]]; then
  err "Request failed (HTTP ${HTTP_STATUS}). ${RESP}"
  exit 1
fi

HOSTNAME=$(echo "$RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('hostname', ''))")

if [[ -z "$HOSTNAME" ]]; then
  err "No tunnel hostname returned in response: ${RESP}"
  exit 1
fi

if [[ "$RAW_MODE" == "true" ]]; then
  if [[ -f "$SSH_KEY" ]]; then
    echo "ssh -o ProxyCommand=\"cloudflared access tcp --hostname ${HOSTNAME}\" -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i \"${SSH_KEY}\" ${SSH_USER}@localhost"
  else
    echo "ssh -o ProxyCommand=\"cloudflared access tcp --hostname ${HOSTNAME}\" -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no ${SSH_USER}@localhost"
  fi
  exit 0
fi

# Print session specs summary
echo "$RESP" | python3 -c '
import sys, json

k = json.load(sys.stdin)
clean = lambda v: " ".join(str(v or "").split())

kid = clean(k.get("kernel_id", "default"))
user = clean(k.get("username"))
nb = clean(k.get("notebook"))
rtype = clean(k.get("run_type")) or "Interactive"
gpu = clean(k.get("gpu")) or "None"
cpu = clean(k.get("cpu"))
ram = clean(k.get("ram"))
host = clean(k.get("hostname"))
zone = clean(k.get("gcp_zone"))

print(f"  ┌─ Kaggle Session [{kid}]")
if nb:   print(f"  │  Notebook  {nb}")
if user: print(f"  │  User      {user} ({rtype})")
print(f"  │  GPU       {gpu}")
if cpu:  print(f"  │  CPU       {cpu}")
if ram:  print(f"  │  RAM       {ram}")
if zone: print(f"  │  Zone      {zone}")
print(f"  └─ Tunnel    {host}")
'

# Ensure local port is free to avoid bind conflicts
if ss -tulpn 2>/dev/null | grep -q ":${LOCAL_PORT} "; then
  LOCAL_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')
fi

log "Connecting via tunnel..."

cloudflared access tcp --hostname "${HOSTNAME}" --url "localhost:${LOCAL_PORT}" >/dev/null 2>&1 &
CF_PID=$!

# Clean up cloudflared on exit
trap 'kill "$CF_PID" 2>/dev/null || true' EXIT

# Give it a moment to bind the local port
sleep 2

log "Connecting..."
SSH_OPTS=("-t" "-o" "RequestTTY=yes" "-o" "UserKnownHostsFile=/dev/null" "-o" "StrictHostKeyChecking=no" "-o" "LogLevel=ERROR" "-o" "PubkeyAuthentication=yes" "-o" "PasswordAuthentication=yes" "-o" "SendEnv=TERM" "-o" "SetEnv=TERM=xterm-256color")

set +e
if [[ -f "$SSH_KEY" ]]; then
  if [[ -t 0 ]]; then
    ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$LOCAL_PORT" "${SSH_USER}@localhost" "$@"
  else
    ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" -p "$LOCAL_PORT" "${SSH_USER}@localhost" "$@" < /dev/tty
  fi
else
  if [[ -t 0 ]]; then
    ssh "${SSH_OPTS[@]}" -p "$LOCAL_PORT" "${SSH_USER}@localhost" "$@"
  else
    ssh "${SSH_OPTS[@]}" -p "$LOCAL_PORT" "${SSH_USER}@localhost" "$@" < /dev/tty
  fi
fi
SSH_EXIT_CODE=$?
set -e

# Only clear relay if SSH itself failed to connect (exit 255), not normal exits
if [[ $SSH_EXIT_CODE -eq 255 ]]; then
  err "SSH connection failed."
  warn "Auto-clearing '${KERNEL_ID:-default}' from relay..."
  curl -s -X POST "${RELAY_URL%/}/clear?kernel_id=${KERNEL_ID:-default}" -H "X-Relay-Secret: ${RELAY_SECRET}" >/dev/null || true
  ok "Cleared."
  exit $SSH_EXIT_CODE
fi
