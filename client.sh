#!/bin/bash
# client.sh - connects to a Kaggle SSH session via Cloudflare tunnel + relay.

set -euo pipefail

if [[ -t 1 ]]; then
  C_INFO=$'\033[34m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi

log()  { printf '%s[ INFO ]%s %s\n' "$C_INFO" "$C_OFF" "$*"; }
ok()   { printf '%s[  OK  ]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[ WARN ]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
err()  { printf '%s[ ERR  ]%s %s\n' "$C_ERR" "$C_OFF" "$*"; }

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
  -k|--key)
    SSH_KEY="$2"
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

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT
RESP_FILE="${TMPDIR_RUN}/relay_resp.json"
KERNELS_FILE="${TMPDIR_RUN}/relay_kernels.json"

if [[ "$SHOW_LIST" == "true" ]]; then
  log "Fetching active Kaggle sessions..."
  HTTP_STATUS=$(curl -s -w "%{http_code}" -o "$KERNELS_FILE" "${RELAY_URL%/}/kernels" -H "X-Relay-Secret: ${RELAY_SECRET}" || echo "000")
  if [[ "$HTTP_STATUS" == "401" ]]; then
    err "Unauthorized (HTTP 401). Invalid RELAY_SECRET provided."
    exit 1
  elif [[ "$HTTP_STATUS" != "200" ]]; then
    err "Failed to fetch sessions (HTTP ${HTTP_STATUS})."
    cat "$KERNELS_FILE" >&2
    exit 1
  fi

  cat "$KERNELS_FILE" | python3 -c '
import sys, json, datetime

data = json.load(sys.stdin)
kernels = data.get("kernels", [])
if not kernels:
    print("No active sessions found.")
    sys.exit(0)

clean = lambda v: " ".join(str(v or "").split())

print(f"\n  Active Sessions ({len(kernels)})\n")
for idx, k in enumerate(kernels, 1):
    kid = clean(k.get("kernel_id", "default"))
    cn = clean(k.get("container_name")).lower()
    rt = clean(k.get("run_type")).lower()
    nb = clean(k.get("notebook")).lower()
    plat = "Colab" if ("colab" in cn or "colab" in rt or "colab" in nb) else "Kaggle"
    user = clean(k.get("username"))
    nb = clean(k.get("notebook"))
    rtype = clean(k.get("run_type")) or "Interactive"
    def format_gpu(g):
        if not g or g.lower() in ("none", "no gpu", "—", "n/a"): return "None"
        parts = [p.strip() for p in g.split(",") if p.strip()]
        if len(parts) >= 4 and len(parts) % 2 == 0:
            devs = [f"{parts[i]}, {parts[i+1]}" for i in range(0, len(parts), 2)]
            if len(set(devs)) == 1: return f"{len(devs)}x {devs[0]}"
        return g
    gpu = format_gpu(clean(k.get("gpu")))
    cpu = clean(k.get("cpu"))
    ram = clean(k.get("ram"))
    host = clean(k.get("hostname"))
    zone = clean(k.get("gcp_zone"))

    ts_start = k.get("created_at")
    start_str = datetime.datetime.fromtimestamp(ts_start).strftime("%Y-%m-%d %H:%M:%S") if ts_start else "N/A"
    hb_age = k.get("heartbeat_age_s")
    if hb_age is None:
        last_seen = k.get("updated_at") or ts_start
        if last_seen:
            hb_age = max(0, int(datetime.datetime.now().timestamp() - float(last_seen)))
    status = (k.get("status") or ("alive" if hb_age is not None and hb_age <= 75 else "stale")).upper()
    hb_str = f"{hb_age}s ago" if hb_age is not None else "unknown"

    print(f"  ┌─ [{idx}] {kid} ({plat}) [{status}]")
    if user and user != "N/A": print(f"  │  User      {user} ({rtype})")
    if nb and nb != "N/A":     print(f"  │  Notebook  {nb}")
    print(f"  │  Heartbeat {hb_str}")
    print(f"  │  GPU       {gpu}")
    if cpu:  print(f"  │  CPU       {cpu}")
    if ram:  print(f"  │  RAM       {ram}")
    if zone: print(f"  │  Zone      {zone}")
    print(f"  │  Tunnel    {host}")
    print(f"  └─ Started   {start_str}")
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

HTTP_STATUS=$(curl -s -w "%{http_code}" -o "$RESP_FILE" "${URL}" -H "X-Relay-Secret: ${RELAY_SECRET}" || echo "000")
RESP=$(cat "$RESP_FILE" 2>/dev/null || echo "")

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

# Hostname lands in an ssh ProxyCommand, which runs via /bin/sh.
if ! [[ "$HOSTNAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$ ]]; then
  err "Refusing to use malformed tunnel hostname from relay: ${HOSTNAME}"
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

if ! command -v cloudflared >/dev/null 2>&1; then
  err "cloudflared not found in PATH."
  log "Install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  exit 1
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
def format_gpu(g):
    if not g or g.lower() in ("none", "no gpu", "—", "n/a"): return "None"
    parts = [p.strip() for p in g.split(",") if p.strip()]
    if len(parts) >= 4 and len(parts) % 2 == 0:
        devs = [f"{parts[i]}, {parts[i+1]}" for i in range(0, len(parts), 2)]
        if len(set(devs)) == 1: return f"{len(devs)}x {devs[0]}"
    return g
gpu = format_gpu(clean(k.get("gpu")))
cpu = clean(k.get("cpu"))
ram = clean(k.get("ram"))
host = clean(k.get("hostname"))
cn = clean(k.get("container_name")).lower()
plat = "Colab" if ("colab" in cn or "colab" in rtype.lower() or "colab" in nb.lower()) else "Kaggle"

print(f"  ┌─ {plat} Session [{kid}]")
if nb:   print(f"  │  Notebook  {nb}")
if user: print(f"  │  User      {user} ({rtype})")
print(f"  │  GPU       {gpu}")
if cpu:  print(f"  │  CPU       {cpu}")
if ram:  print(f"  │  RAM       {ram}")
if zone: print(f"  │  Zone      {zone}")
print(f"  └─ Tunnel    {host}")
'

# Ensure local port is free to avoid bind conflicts (ss is Linux-only)
if ! python3 -c "
import socket, sys
s = socket.socket()
try:
    s.bind(('127.0.0.1', ${LOCAL_PORT}))
except OSError:
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null; then
  LOCAL_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')
fi

log "Connecting via tunnel..."

cloudflared access tcp --hostname "${HOSTNAME}" --url "localhost:${LOCAL_PORT}" >/dev/null 2>&1 &
CF_PID=$!

# Clean up cloudflared on exit
trap 'kill "$CF_PID" 2>/dev/null || true; rm -rf "$TMPDIR_RUN"' EXIT

# Give it a moment to bind the local port
sleep 2

log "Connecting..."
SSH_OPTS=("-t" "-o" "RequestTTY=yes" "-o" "UserKnownHostsFile=/dev/null" "-o" "StrictHostKeyChecking=no" "-o" "LogLevel=ERROR" "-o" "PubkeyAuthentication=yes" "-o" "SendEnv=TERM" "-o" "SetEnv=TERM=xterm-256color")

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
