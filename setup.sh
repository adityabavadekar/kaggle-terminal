#!/bin/bash
# setup.sh - sets up sshd + Cloudflare tunnel inside Kaggle & Google Colab environments.

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

PUBKEY_URL=""
RELAY_URL="${RELAY_URL:-https://kagglessh.vercel.app}"
RELAY_SECRET="${RELAY_SECRET:-}"
SSH_PASSWORD="${SSH_PASSWORD:-}"
KERNEL_ID="${KERNEL_ID:-}"
BLOCK="false"
ACTION="start"
PLATFORM_DEFAULT=""
PLATFORM="${PLATFORM_OVERRIDE:-${PLATFORM_DEFAULT:-}}"
WORKING_DIR=""
SSH_PORT="22"
HOSTNAME=""
GPU_INFO=""
CPU_INFO=""
RAM_INFO=""
DISK_INFO=""
SESSION_USER=""
NOTEBOOK_ID=""
RUN_TYPE=""
CONTAINER_ID=""
GCP_ZONE=""
CONTAINER_NAME=""

detect_platform() {
  if [[ -n "${PLATFORM:-}" ]]; then
    : # Keep preset platform from PLATFORM_DEFAULT or -P argument
  elif [[ -n "${COLAB_RELEASE_TAG:-}" || -n "${COLAB_BACKEND_VERSION:-}" || -n "${COLAB_NOTEBOOK_ID:-}" || -n "${COLAB_GPU:-}" || -d "/usr/colab" ]]; then
    PLATFORM="Colab"
  elif [[ -n "${KAGGLE_KERNEL_RUN_TYPE:-}" || -n "${KAGGLE_URL_BASE:-}" || -n "${KAGGLE_DOCKER_IMAGE:-}" || -n "${KAGGLE_CONTAINER_NAME:-}" || -d "/kaggle/input" ]]; then
    PLATFORM="Kaggle"
  elif [[ -d "/content" && ! -d "/kaggle/input" ]]; then
    PLATFORM="Colab"
  elif [[ -d "/kaggle/working" ]]; then
    PLATFORM="Kaggle"
  else
    PLATFORM="Linux"
  fi

  if [[ "$PLATFORM" == "Colab" ]]; then
    WORKING_DIR="/content"
    # Clean up empty /kaggle directory if created accidentally on Colab
    rmdir /kaggle/working 2>/dev/null || true
    rmdir /kaggle 2>/dev/null || true
  elif [[ "$PLATFORM" == "Kaggle" ]]; then
    WORKING_DIR="/kaggle/working"
  else
    WORKING_DIR="$HOME"
  fi
}

parse_args() {
  if [[ $# -gt 0 && ( "$1" == "stop" || "$1" == "start" ) ]]; then
    ACTION="$1"
    shift
  fi

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
    -P|--platform)
      PLATFORM="$2"
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
}

stop_session() {
  log "Stopping ${PLATFORM} terminal setup for kernel '${KERNEL_ID}'..."
  pkill -f "\.relay_heartbeat\.sh" 2>/dev/null || true
  pkill -f "cloudflared tunnel" 2>/dev/null || true
  service ssh stop 2>/dev/null || /etc/init.d/ssh stop 2>/dev/null || pkill sshd 2>/dev/null || true

  local SESSION_TOKEN=""
  if [[ -f "${WORKING_DIR}/.relay_token" ]]; then
    SESSION_TOKEN="$(cat "${WORKING_DIR}/.relay_token" 2>/dev/null || echo "")"
  fi

  log "Clearing session on relay server..."
  curl -s -X POST "${RELAY_URL%/}/clear?kernel_id=${KERNEL_ID}" \
    -H "X-Relay-Secret: ${RELAY_SECRET}" \
    -H "X-Session-Token: ${SESSION_TOKEN}" >/dev/null || true
  rm -f "${WORKING_DIR}/.relay_token" "${WORKING_DIR}/.relay_payload.json" 2>/dev/null || true
  ok "Stopped cloudflared, sshd & heartbeat daemon, and cleared relay session."
  exit 0
}

install_packages() {
  log "Checking sshd..."
  if ! command -v sshd >/dev/null 2>&1; then
    log "Installing sshd..."
    local APT_LOG
    APT_LOG="$(mktemp)"
    if ! apt-get update -qq >"$APT_LOG" 2>&1 ||
       ! DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server >>"$APT_LOG" 2>&1; then
      err "Failed to install openssh-server:"
      cat "$APT_LOG" >&2
      rm -f "$APT_LOG"
      exit 1
    fi

    rm -f "$APT_LOG"
    ok "sshd installed."
  else
    ok "sshd is already installed."
  fi

  log "Checking cloudflared..."
  if ! command -v cloudflared >/dev/null 2>&1; then
    log "Installing cloudflared..."
    local CLOUDFLARED_DEB="/tmp/cloudflared.deb"
    local DPKG_LOG
    DPKG_LOG="$(mktemp)"
    if ! wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -O "$CLOUDFLARED_DEB"; then
      err "Failed to download cloudflared."
      rm -f "$DPKG_LOG" "$CLOUDFLARED_DEB"
      exit 1
    fi

    if ! dpkg -i "$CLOUDFLARED_DEB" >"$DPKG_LOG" 2>&1; then
      err "Failed to install cloudflared:"
      cat "$DPKG_LOG" >&2
      rm -f "$DPKG_LOG" "$CLOUDFLARED_DEB"
      exit 1
    fi
    rm -f "$DPKG_LOG" "$CLOUDFLARED_DEB"
    ok "cloudflared installed."
  else
    ok "cloudflared is already installed."
  fi
}

configure_environment() {
  echo "/usr/local/nvidia/lib64" > /etc/ld.so.conf.d/nvidia.conf
  echo "/usr/local/nvidia/lib" >> /etc/ld.so.conf.d/nvidia.conf
  echo "/usr/local/cuda/lib64" >> /etc/ld.so.conf.d/nvidia.conf
  echo "/usr/lib64-nvidia" >> /etc/ld.so.conf.d/nvidia.conf
  ldconfig 2>/dev/null || true

  sed -i '/^# relay-terminal env passthrough$/,/^# relay-terminal env end$/d' /etc/environment 2>/dev/null || true
  {
    echo "# relay-terminal env passthrough"
    env | grep -E '^(PATH|PYTHONPATH|NVIDIA_|CUDA_|KAGGLE_|COLAB_)=' || true
    echo "LD_LIBRARY_PATH=\"/usr/local/nvidia/lib64:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}\""
    echo "# relay-terminal env end"
  } >> /etc/environment
  grep -qF 'export LD_LIBRARY_PATH=' /root/.bashrc 2>/dev/null || echo "export LD_LIBRARY_PATH=\"/usr/local/nvidia/lib64:/usr/local/cuda/lib64:\${LD_LIBRARY_PATH:-}\"" >> /root/.bashrc
  grep -qF 'export PATH=' /root/.bashrc 2>/dev/null || echo "export PATH=\"$PATH\"" >> /root/.bashrc
}

configure_sshd() {
  log "Configuring SSH..."
  sed -i 's/#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || true
  sed -i 's/#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true
  sed -i 's/#\?PermitUserEnvironment.*/PermitUserEnvironment yes/' /etc/ssh/sshd_config 2>/dev/null || true

  mkdir -p /etc/ssh/sshd_config.d
  cat <<'EOF' > /etc/ssh/sshd_config.d/99-relay.conf
PermitRootLogin yes
PubkeyAuthentication yes
PermitUserEnvironment yes
StrictModes no
PubkeyAcceptedKeyTypes +ssh-rsa
PubkeyAcceptedAlgorithms +ssh-rsa
AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2
EOF
}

setup_auth() {
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh

  local SERVER_PK_URL="${RELAY_URL%/}/pubkey?secret=${RELAY_SECRET}"
  if [[ -z "$PUBKEY_URL" ]]; then
    if curl -s -f "$SERVER_PK_URL" -o /tmp/server_pubkey.pub 2>/dev/null && [[ -s /tmp/server_pubkey.pub ]]; then
      PUBKEY_URL="$SERVER_PK_URL"
    fi
  fi

  if [[ -n "$PUBKEY_URL" ]]; then
    log "Installing public key..."
    curl -fsSL "$PUBKEY_URL" -o /root/.ssh/authorized_keys
    echo "" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    sed -i 's/#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config 2>/dev/null || true
    echo "root:$(head -c 24 /dev/urandom | base64)" | chpasswd
    ok "Public key installed. Password authentication disabled."
  else
    if [[ -z "$SSH_PASSWORD" ]]; then
      SSH_PASSWORD=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
    fi
    sed -i 's/#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true
    echo "root:${SSH_PASSWORD}" | chpasswd
    warn "No pubkey stored on relay yet. Password auth active (password: ${SSH_PASSWORD})."
    warn "Upload a key with upload_key.sh instead - it will be picked up automatically."

    # Background sync loop: runs ONLY while waiting for the first key upload
    (
      while true; do
        sleep 15
        if curl -s -f "${SERVER_PK_URL}" -o /tmp/sync_pubkey.pub 2>/dev/null && [[ -s /tmp/sync_pubkey.pub ]]; then
          cp /tmp/sync_pubkey.pub /root/.ssh/authorized_keys
          echo "" >> /root/.ssh/authorized_keys
          chmod 600 /root/.ssh/authorized_keys
          sed -i 's/#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config 2>/dev/null || true
          echo "root:$(head -c 24 /dev/urandom | base64)" | chpasswd
          service ssh reload >/dev/null 2>&1 || pkill -HUP sshd 2>/dev/null || true
          rm -f /tmp/sync_pubkey.pub
          break # Key installed - stop polling!
        fi
      done
    ) >/dev/null 2>&1 &
  fi
}

check_ssh_port() {
  python3 -c "import socket; s = socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', $1)); s.close()" 2>/dev/null
}

start_sshd() {
  log "Starting SSH..."

  pkill -9 sshd 2>/dev/null || true

  rm -f /etc/ssh/sshd_not_to_be_run
  rm -f /var/run/sshd.pid /run/sshd.pid

  mkdir -p /var/run/sshd /run/sshd
  chmod 0755 /var/run/sshd /run/sshd

  ssh-keygen -A >/dev/null 2>&1 || true

  if ! /usr/sbin/sshd >/dev/null 2>&1; then
    err "Failed to start sshd."
    /usr/sbin/sshd -t || true
    exit 1
  fi

  local SSHD_OK=false
  for attempt in 1 2 3 4 5; do
    if check_ssh_port 2222; then
      SSH_PORT=2222
      SSHD_OK=true
      break
    elif check_ssh_port 22; then
      SSH_PORT=22
      SSHD_OK=true
      break
    fi

    sleep 1
  done

  if [[ "$SSHD_OK" != "true" ]]; then
    err "sshd started but is not listening on port 22 or 2222."
    /usr/sbin/sshd -t || true
    ss -lntp 2>/dev/null || true
    exit 1
  fi
  ok "sshd is running and listening on 127.0.0.1:${SSH_PORT}."
}

start_tunnel() {
  log "Starting Cloudflare quick tunnel to 127.0.0.1:${SSH_PORT}..."
  local LOGFILE="${WORKING_DIR}/cf.log"
  mkdir -p "$(dirname "$LOGFILE")"
  : >"$LOGFILE"
  nohup cloudflared tunnel --url "tcp://127.0.0.1:${SSH_PORT}" --logfile "$LOGFILE" >/dev/null 2>&1 &

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
}

collect_specs() {
  log "Collecting ${PLATFORM} environment specs & metadata..."
  local raw_gpu
  raw_gpu=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true)
  if [[ -z "$raw_gpu" ]]; then
    GPU_INFO="None"
  else
    local gpu_count uniq_count
    gpu_count=$(echo "$raw_gpu" | grep -c . || true)
    uniq_count=$(echo "$raw_gpu" | sort -u | grep -c . || true)
    if [[ "$gpu_count" -gt 1 && "$uniq_count" -eq 1 ]]; then
      local first_gpu
      first_gpu=$(echo "$raw_gpu" | head -n1)
      GPU_INFO="${gpu_count}x ${first_gpu}"
    elif [[ "$gpu_count" -gt 1 ]]; then
      GPU_INFO=$(echo "$raw_gpu" | paste -sd ";" - | sed 's/;/; /g')
    else
      GPU_INFO="$raw_gpu"
    fi
  fi
  local CPU_MODEL
  CPU_MODEL=$(grep 'model name' /proc/cpuinfo 2>/dev/null | head -n1 | cut -d: -f2 | xargs || uname -m 2>/dev/null || echo "Unknown CPU")
  local CPU_CORES
  CPU_CORES=$(nproc 2>/dev/null || echo "1")
  CPU_INFO="${CPU_MODEL} (${CPU_CORES} cores)"
  RAM_INFO=$(free -h 2>/dev/null | awk '/Mem:/ {print $2}' || echo "Unknown")
  DISK_INFO=$(df -h "$WORKING_DIR" 2>/dev/null | awk 'NR==2 {print $4 " free / " $2 " (" $5 " used)"}' || df -h / 2>/dev/null | awk 'NR==2 {print $4 " free / " $2 " (" $5 " used)"}' || echo "Unknown")

  if [[ "$PLATFORM" == "Colab" ]]; then
    SESSION_USER="${COLAB_USER:-$(whoami 2>/dev/null || echo "root")}"
    NOTEBOOK_ID="${COLAB_NOTEBOOK_ID:-${COLAB_NOTEBOOK:-colab-notebook}}"
    RUN_TYPE="Interactive"
    CONTAINER_ID="$(hostname 2>/dev/null || echo "unknown")"
    GCP_ZONE="${COLAB_ZONE:-}"
    CONTAINER_NAME="colab"
  else
    SESSION_USER="${KAGGLE_USERNAME:-$(whoami 2>/dev/null || echo "root")}"
    NOTEBOOK_ID="${KAGGLE_KERNEL_SLUG:-${KAGGLE_CONTAINER_NAME:-$(basename "$(pwd 2>/dev/null || echo "notebook")")}}"
    RUN_TYPE="${KAGGLE_KERNEL_RUN_TYPE:-Interactive}"
    CONTAINER_ID="$(hostname 2>/dev/null || echo "unknown")"
    GCP_ZONE="${KAGGLE_GCP_ZONE:-}"
    CONTAINER_NAME="${KAGGLE_CONTAINER_NAME:-}"
  fi

  if [[ -z "${KERNEL_ID:-}" || "${KERNEL_ID}" == "default" ]]; then
    if [[ "$PLATFORM" == "Colab" ]]; then
      if [[ -n "${COLAB_NOTEBOOK_ID:-}" ]]; then
        KERNEL_ID="colab-${COLAB_NOTEBOOK_ID:0:8}"
      else
        KERNEL_ID="colab-${CONTAINER_ID:0:8}"
      fi
    elif [[ "$PLATFORM" == "Kaggle" ]]; then
      if [[ -n "${KAGGLE_CONTAINER_NAME:-}" ]]; then
        local SHORT_CN="${KAGGLE_CONTAINER_NAME#kaggle_}"
        KERNEL_ID="kaggle-${SHORT_CN:0:8}"
      elif [[ -n "${KAGGLE_KERNEL_SLUG:-}" ]]; then
        KERNEL_ID="${KAGGLE_KERNEL_SLUG:0:20}"
      else
        KERNEL_ID="kaggle-${CONTAINER_ID:0:8}"
      fi
    else
      KERNEL_ID="linux-${CONTAINER_ID:0:8}"
    fi
    KERNEL_ID="$(echo "$KERNEL_ID" | tr -cs 'a-zA-Z0-9_-' '-' | sed 's/^-//;s/-$//')"
    if [[ -z "$KERNEL_ID" ]]; then
      KERNEL_ID="default"
    fi
  fi
}

register_relay() {
  local POST_PAYLOAD
  POST_PAYLOAD=$(HOSTNAME_VAL="$HOSTNAME" KERNEL_ID_VAL="$KERNEL_ID" GPU_VAL="$GPU_INFO" CPU_VAL="$CPU_INFO" RAM_VAL="$RAM_INFO" USER_VAL="$SESSION_USER" NOTEBOOK_VAL="$NOTEBOOK_ID" RUN_TYPE_VAL="$RUN_TYPE" CONTAINER_ID_VAL="$CONTAINER_ID" GCP_ZONE_VAL="$GCP_ZONE" CONTAINER_NAME_VAL="$CONTAINER_NAME" python3 -c '
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
  local HTTP_CODE
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
  else
    ok "Relay updated successfully."
  fi

  local PAYLOAD_FILE="${WORKING_DIR}/.relay_payload.json"
  echo "$POST_PAYLOAD" > "$PAYLOAD_FILE"
  chmod 600 "$PAYLOAD_FILE" 2>/dev/null || true

  local SESSION_TOKEN
  SESSION_TOKEN=$(python3 -c '
import json
try:
    with open("/tmp/relay_resp.json") as f:
        data = json.load(f)
    print(data.get("session_token") or "")
except Exception:
    print("")
' 2>/dev/null || echo "")

  local TOKEN_FILE="${WORKING_DIR}/.relay_token"
  if [[ -n "$SESSION_TOKEN" ]]; then
    echo "$SESSION_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE" 2>/dev/null || true
  fi

  # Start detached self-healing heartbeat loop
  local HB_SCRIPT="${WORKING_DIR}/.relay_heartbeat.sh"
  cat <<EOF > "$HB_SCRIPT"
#!/bin/bash
while true; do
  SESSION_TOKEN=""
  if [[ -f "${TOKEN_FILE}" ]]; then
    SESSION_TOKEN="\$(cat "${TOKEN_FILE}" 2>/dev/null || echo "")"
  fi

  HB_CODE=\$(curl -s -o /dev/null -w "%{http_code}" -X POST "${RELAY_URL%/}/heartbeat?kernel_id=${KERNEL_ID}" \\
    -H "X-Relay-Secret: ${RELAY_SECRET}" \\
    -H "X-Session-Token: \${SESSION_TOKEN}" || echo "000")

  if [[ "\$HB_CODE" == "404" && -f "${PAYLOAD_FILE}" ]]; then
    # Session lost on server (e.g. server restart/cache wipe). Auto-restore!
    NEW_RESP=\$(curl -s -X POST "${RELAY_URL%/}/post" \\
      -H "Content-Type: application/json" \\
      -H "X-Relay-Secret: ${RELAY_SECRET}" \\
      --data-binary @"${PAYLOAD_FILE}" || echo "")
    NEW_TOKEN=\$(echo "\$NEW_RESP" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get("session_token") or "")
except Exception:
    print("")
' 2>/dev/null || echo "")
    if [[ -n "\$NEW_TOKEN" ]]; then
      echo "\$NEW_TOKEN" > "${TOKEN_FILE}"
      chmod 600 "${TOKEN_FILE}" 2>/dev/null || true
    fi
  fi
  sleep 30
done
EOF
  chmod 700 "$HB_SCRIPT"

  pkill -f "\.relay_heartbeat\.sh" 2>/dev/null || true
  curl -s -X POST "${RELAY_URL%/}/heartbeat?kernel_id=${KERNEL_ID}" \
    -H "X-Relay-Secret: ${RELAY_SECRET}" \
    -H "X-Session-Token: ${SESSION_TOKEN}" >/dev/null 2>&1 || true

  nohup "$HB_SCRIPT" >/dev/null 2>&1 &
  disown $! 2>/dev/null || true
  ok "Heartbeat daemon active (30s interval, authenticated & self-healing)."
}

print_banner() {
  echo ""
  echo "=================================================="
  echo " Platform        : ${PLATFORM}"
  echo " Kernel ID       : ${KERNEL_ID}"
  echo " Tunnel hostname : ${HOSTNAME}"
  echo " SSH auth        : $([[ -n "$PUBKEY_URL" ]] && echo "public key (password auth disabled)" || echo "password (${SSH_PASSWORD})")"
  echo " GPU             : ${GPU_INFO}"
  echo " CPU             : ${CPU_INFO}"
  echo " RAM             : ${RAM_INFO}"
  echo " Disk            : ${DISK_INFO}"
  echo " Working dir     : ${WORKING_DIR}"
  echo "=================================================="
  echo ""
}

block_if_requested() {
  if [[ "$BLOCK" == "true" ]]; then
    log "Blocking to keep the notebook cell running (tailing logs)..."
    local LOGFILE="${WORKING_DIR}/cf.log"
    exec tail -f "$LOGFILE"
  fi
}

main() {
  detect_platform
  parse_args "$@"

  if [[ "$ACTION" == "stop" ]]; then
    stop_session
  fi

  install_packages
  configure_environment
  configure_sshd
  setup_auth
  start_sshd
  start_tunnel
  collect_specs
  register_relay
  print_banner
  block_if_requested
}

main "$@"
