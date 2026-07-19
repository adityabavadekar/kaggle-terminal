#!/bin/bash
# kaggle_setup.sh - sets up sshd + Cloudflare tunnel inside a Kaggle notebook.

set -euo pipefail

log()  { printf '[ INFO ] %s\n' "$*"; }
ok()   { printf '[  OK  ] %s\n' "$*"; }
warn() { printf '[ WARN ] %s\n' "$*"; }
err()  { printf '[ ERR  ] %s\n' "$*"; }

PUBKEY_URL=""
RELAY_URL="${RELAY_URL:-https://kagglessh.vercel.app}"
RELAY_SECRET="${RELAY_SECRET:-}"
SSH_PASSWORD="${SSH_PASSWORD:-password}"
KERNEL_ID="default"
BLOCK="false"
ACTION="start"

# Check if first positional argument is 'stop' or 'start'
if [[ $# -gt 0 && ( "$1" == "stop" || "$1" == "start" ) ]]; then
  ACTION="$1"
  shift
fi

# Allow passing secret directly as first positional argument (e.g. bash -s secret)
if [[ $# -gt 0 && "$1" != -* ]]; then
  RELAY_SECRET="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
  stop)
    ACTION="stop"
    shift
    ;;
  start)
    ACTION="start"
    shift
    ;;
  -k)
    PUBKEY_URL="$2"
    shift 2
    ;;
  -r)
    RELAY_URL="$2"
    shift 2
    ;;
  -s)
    RELAY_SECRET="$2"
    shift 2
    ;;
  -i)
    KERNEL_ID="$2"
    shift 2
    ;;
  -b)
    BLOCK="true"
    shift
    ;;
  -p)
    SSH_PASSWORD="$2"
    shift 2
    ;;
  -*)
    err "Unknown option: $1"
    exit 1
    ;;
  *)
    if [[ -z "$RELAY_SECRET" ]]; then
      RELAY_SECRET="$1"
      shift
    else
      shift
    fi
    ;;
  esac
done

if [[ -z "$RELAY_SECRET" ]]; then
  err "RELAY_SECRET is required. Export RELAY_SECRET in your environment or pass it as an argument."
  exit 1
fi

if [[ "$ACTION" == "stop" ]]; then
  log "Stopping Kaggle terminal setup for kernel '${KERNEL_ID}'..."
  pkill -f "cloudflared tunnel" 2>/dev/null || true
  service ssh stop 2>/dev/null || pkill sshd 2>/dev/null || true
  
  log "Clearing session on relay server..."
  curl -s -X POST "${RELAY_URL%/}/clear?kernel_id=${KERNEL_ID}" \
    -H "X-Relay-Secret: ${RELAY_SECRET}" >/dev/null || true
  ok "Stopped cloudflared & sshd, and cleared relay session successfully."
  exit 0
fi

log "Installing sshd..."
apt-get update -qq
apt-get install -y -qq openssh-server >/dev/null
mkdir -p /var/run/sshd

log "Configuring SSH..."
sed -i 's/#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/#\?PermitUserEnvironment.*/PermitUserEnvironment yes/' /etc/ssh/sshd_config
echo "root:${SSH_PASSWORD}" | chpasswd

# Preserve Kaggle NVIDIA, CUDA, PATH & LD_LIBRARY_PATH environment variables for SSH sessions
echo "/usr/local/nvidia/lib64" > /etc/ld.so.conf.d/nvidia.conf
echo "/usr/local/nvidia/lib" >> /etc/ld.so.conf.d/nvidia.conf
echo "/usr/local/cuda/lib64" >> /etc/ld.so.conf.d/nvidia.conf
echo "/usr/lib64-nvidia" >> /etc/ld.so.conf.d/nvidia.conf
ldconfig 2>/dev/null || true

env | grep -E '^(PATH|LD_LIBRARY_PATH|NVIDIA_|CUDA_|KAGGLE_)' >> /etc/environment
echo "LD_LIBRARY_PATH=\"/usr/local/nvidia/lib64:/usr/local/cuda/lib64:\${LD_LIBRARY_PATH:-}\"" >> /etc/environment
grep -qF 'export LD_LIBRARY_PATH=' /root/.bashrc 2>/dev/null || echo "export LD_LIBRARY_PATH=\"/usr/local/nvidia/lib64:/usr/local/cuda/lib64:\${LD_LIBRARY_PATH:-}\"" >> /root/.bashrc
grep -qF 'export PATH=' /root/.bashrc 2>/dev/null || echo "export PATH=\"$PATH\"" >> /root/.bashrc

mkdir -p /root/.ssh
chmod 700 /root/.ssh

# Try fetching public key at start
SERVER_PK_URL="${RELAY_URL%/}/pubkey?secret=${RELAY_SECRET}"
if [[ -z "$PUBKEY_URL" ]]; then
  if curl -s -f "$SERVER_PK_URL" -o /tmp/server_pubkey.pub 2>/dev/null && [[ -s /tmp/server_pubkey.pub ]]; then
    PUBKEY_URL="$SERVER_PK_URL"
  fi
fi

if [[ -n "$PUBKEY_URL" ]]; then
  log "Installing public key..."
  curl -fsSL "$PUBKEY_URL" -o /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
else
  warn "No pubkey stored on relay yet. Password auth active (password: ${SSH_PASSWORD})."
fi

# Start background pubkey sync loop (fetches key if uploaded later from laptop)
(
  while true; do
    sleep 10
    if curl -s -f "${SERVER_PK_URL}" -o /tmp/sync_pubkey.pub 2>/dev/null && [[ -s /tmp/sync_pubkey.pub ]]; then
      if ! cmp -s /tmp/sync_pubkey.pub /root/.ssh/authorized_keys 2>/dev/null; then
        cp /tmp/sync_pubkey.pub /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
      fi
    fi
  done
) >/dev/null 2>&1 &

service ssh start
ok "sshd is running."

log "Installing cloudflared..."
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -O /tmp/cloudflared.deb
dpkg -i /tmp/cloudflared.deb >/dev/null

log "Starting Cloudflare quick tunnel to localhost:22..."
LOGFILE="/kaggle/working/cf.log"
: >"$LOGFILE"
nohup cloudflared tunnel --url tcp://localhost:22 --logfile "$LOGFILE" >/dev/null 2>&1 &

log "Waiting for tunnel hostname..."
HOSTNAME=""
for i in $(seq 1 30); do
  HOSTNAME=$(grep -oE '[a-zA-Z0-9-]+\.trycloudflare\.com' "$LOGFILE" | head -n1 || true)
  if [[ -n "$HOSTNAME" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "$HOSTNAME" ]]; then
  err "tunnel hostname never appeared. Check $LOGFILE"
  cat "$LOGFILE" >&2
  exit 1
fi

ok "Tunnel is live: ${HOSTNAME}"
log "Collecting Kaggle environment specs & metadata..."
GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | tr '\n' ' ' | xargs || true)
if [[ -z "$GPU_INFO" ]]; then
  GPU_INFO="None"
fi
CPU_MODEL=$(grep 'model name' /proc/cpuinfo 2>/dev/null | head -n1 | cut -d: -f2 | xargs || uname -m 2>/dev/null || echo "Unknown CPU")
CPU_CORES=$(nproc 2>/dev/null || echo "1")
CPU_INFO="${CPU_MODEL} (${CPU_CORES} cores)"
RAM_INFO=$(free -h 2>/dev/null | awk '/Mem:/ {print $2}' || echo "Unknown")

KAGGLE_USER="${KAGGLE_USERNAME:-$(whoami 2>/dev/null || echo "root")}"
NOTEBOOK_ID="${KAGGLE_KERNEL_SLUG:-${KAGGLE_CONTAINER_NAME:-$(basename "$(pwd 2>/dev/null || echo "notebook")")}}"
RUN_TYPE="${KAGGLE_KERNEL_RUN_TYPE:-Interactive}"
CONTAINER_ID="$(hostname 2>/dev/null || echo "unknown")"
GCP_ZONE="${KAGGLE_GCP_ZONE:-}"
CONTAINER_NAME="${KAGGLE_CONTAINER_NAME:-}"

POST_PAYLOAD=$(HOSTNAME_VAL="$HOSTNAME" KERNEL_ID_VAL="$KERNEL_ID" GPU_VAL="$GPU_INFO" CPU_VAL="$CPU_INFO" RAM_VAL="$RAM_INFO" USER_VAL="$KAGGLE_USER" NOTEBOOK_VAL="$NOTEBOOK_ID" RUN_TYPE_VAL="$RUN_TYPE" CONTAINER_ID_VAL="$CONTAINER_ID" GCP_ZONE_VAL="$GCP_ZONE" CONTAINER_NAME_VAL="$CONTAINER_NAME" python3 -c '
import json, os
data = {
    "hostname": os.environ.get("HOSTNAME_VAL", ""),
    "kernel_id": os.environ.get("KERNEL_ID_VAL", "default"),
    "gpu": os.environ.get("GPU_VAL", ""),
    "cpu": os.environ.get("CPU_VAL", ""),
    "ram": os.environ.get("RAM_VAL", ""),
    "username": os.environ.get("USER_VAL", ""),
    "notebook": os.environ.get("NOTEBOOK_VAL", ""),
    "run_type": os.environ.get("RUN_TYPE_VAL", ""),
    "container_id": os.environ.get("CONTAINER_ID_VAL", ""),
    "gcp_zone": os.environ.get("GCP_ZONE_VAL", ""),
    "container_name": os.environ.get("CONTAINER_NAME_VAL", "")
}
print(json.dumps(data))
')

log "Posting to relay server..."
HTTP_CODE=$(curl -s -o /tmp/relay_resp.json -w "%{http_code}" \
  -X POST "${RELAY_URL%/}/post" \
  -H "Content-Type: application/json" \
  -H "X-Relay-Secret: ${RELAY_SECRET}" \
  -d "${POST_PAYLOAD}")

if [[ "$HTTP_CODE" == "401" ]]; then
  err "Unauthorized (HTTP 401). Invalid RELAY_SECRET provided."
  exit 1
elif [[ "$HTTP_CODE" != "200" ]]; then
  warn "Relay POST failed (HTTP ${HTTP_CODE}). Response:"
  cat /tmp/relay_resp.json >&2
  echo "" >&2
  log "Tunnel is still live locally — connect manually with cloudflared access."
else
  ok "Relay updated successfully."
fi

# Background heartbeat loop to keep timestamp fresh on relay while cloudflared runs
(
  while true; do
    sleep 30
    if pgrep -f "cloudflared tunnel" >/dev/null; then
      curl -s -X POST "${RELAY_URL%/}/post" \
        -H "Content-Type: application/json" \
        -H "X-Relay-Secret: ${RELAY_SECRET}" \
        -d "${POST_PAYLOAD}" >/dev/null || true
    else
      break
    fi
  done
) >/dev/null 2>&1 &

echo ""
echo "=================================================="
echo " Tunnel hostname : ${HOSTNAME}"
echo " SSH auth        : $([[ -n "$PUBKEY_URL" ]] && echo "public key" || echo "password (${SSH_PASSWORD})")"
echo "=================================================="
echo ""
if [[ "$BLOCK" == "true" ]]; then
  log "Blocking to keep the notebook cell running (tailing logs)..."
  tail -f "$LOGFILE"
else
  ok "Script finished successfully. Cloudflare tunnel and sshd are running in the background."
fi
