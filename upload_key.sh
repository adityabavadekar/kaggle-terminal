#!/bin/bash
# upload_key.sh
#
# Generates dedicated ~/.ssh/kaggle_rsa key if missing and uploads public key to relay server.

set -euo pipefail

log()  { printf '[ INFO ] %s\n' "$*"; }
ok()   { printf '[  OK  ] %s\n' "$*"; }
warn() { printf '[ WARN ] %s\n' "$*"; }
err()  { printf '[ ERR  ] %s\n' "$*"; }

RELAY_URL="${RELAY_URL:-https://kagglessh.vercel.app}"
RELAY_SECRET="${RELAY_SECRET:-}"

# Allow passing secret directly as first positional argument (e.g. bash -s secret)
if [[ $# -gt 0 && "$1" != -* ]]; then
  RELAY_SECRET="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
  -r)
    RELAY_URL="$2"
    shift 2
    ;;
  -s)
    RELAY_SECRET="$2"
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

KEY_PATH="$HOME/.ssh/kaggle_rsa"
mkdir -p "$HOME/.ssh"

if [[ ! -f "${KEY_PATH}" ]]; then
  log "Generating dedicated SSH key pair (${KEY_PATH})..."
  ssh-keygen -t rsa -N "" -f "${KEY_PATH}" -C "kaggle-relay"
else
  log "Using existing SSH key (${KEY_PATH})..."
fi

log "Uploading public key (${KEY_PATH}.pub) to relay server..."
HTTP_STATUS=$(curl -s -w "%{http_code}" -o /tmp/pubkey_resp.json -X POST "${RELAY_URL%/}/pubkey" \
  -H "X-Relay-Secret: ${RELAY_SECRET}" \
  --data-binary "@${KEY_PATH}.pub" || echo "000")

if [[ "$HTTP_STATUS" == "401" ]]; then
  err "Unauthorized (HTTP 401). Invalid RELAY_SECRET provided."
  exit 1
elif [[ "$HTTP_STATUS" != "200" ]]; then
  RESP=$(cat /tmp/pubkey_resp.json 2>/dev/null || echo "")
  err "Public key upload failed (HTTP ${HTTP_STATUS}). ${RESP}"
  exit 1
fi

ok "Public key uploaded successfully!"
