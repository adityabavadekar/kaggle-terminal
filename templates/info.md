---
name: kaggle-terminal
description: Run shell commands and transfer files on a Kaggle notebook's container over an SSH-through-Cloudflare-Tunnel relay. Use for training runs, GPU inspection, and moving files in or out of /kaggle/working.
---

# Kaggle Terminal

This relay brokers SSH access to a Kaggle notebook container. The notebook
registers its Cloudflare Tunnel hostname here; clients look it up and connect.

## Configuration

- **Relay server**: `{{ RELAY_URL }}`
- **Secret**: `{{ RELAY_SECRET }}`

Export the secret once per shell; every script below reads it from the
environment:

```bash
export RELAY_SECRET="{{ RELAY_SECRET }}"
```

Prefer the environment variable over `?secret=` in URLs — query strings end up
in server access logs and shell history.

## Prerequisites

- `cloudflared` on the client machine ([install docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)).
  The client scripts fail early with a clear message if it is missing.
- A Kaggle notebook that has run step 2 below. Sessions expire on the relay
  after {{ SESSION_TTL }} seconds without a heartbeat; a stopped notebook
  disappears on its own.

## Which command do I want?

| Goal | Use |
| --- | --- |
| One-off command, capture stdout | `kssh.sh run "..."` |
| Copy a file to the notebook | `kssh.sh put <local> <remote>` |
| Copy a file back | `kssh.sh get <remote> <local>` |
| Interactive shell | `client.sh` |
| See what sessions exist | `client.sh list` |

## Setup

### 1. Upload your SSH public key (client machine)

Generates `~/.ssh/kaggle_rsa` if absent and stores the public half on the relay.
Do this before step 2 — the notebook then uses key auth and disables password
login entirely.

```bash
curl -fsSL {{ RELAY_URL }}/upload_key.sh | bash
```

### 2. Start the tunnel (Kaggle notebook cell)

```bash
%env RELAY_SECRET={{ RELAY_SECRET }}
!curl -fsSL {{ RELAY_URL }}/kaggle_setup.sh | bash
```

Runs `sshd` plus a Cloudflare quick tunnel in the background and posts the
hostname here. The cell returns immediately; add `-s -b` to keep it blocking.
If no public key is on the relay yet, it falls back to a random one-time root
password (printed in the cell output) and switches to key-only auth within
~10 s of a key being uploaded.

Multiple notebooks: give each one `-s -i <kernel_id>`, then pass the matching
`KERNEL_ID=<kernel_id>` to the client commands.

## Usage

### Run commands non-interactively

```bash
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s run "nvidia-smi"
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s run "cd /kaggle/working && python train.py"
```

Exit status and stdout/stderr pass through, so this composes with normal shell
error handling.

### Transfer files

```bash
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s put train.py /kaggle/working/
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s get /kaggle/working/out.csv .
```

Only `/kaggle/working` persists across notebook restarts — write outputs there.

### Interactive shell

```bash
curl -fsSL {{ RELAY_URL }}/client.sh | bash
```

### Install the helper locally

Faster than re-fetching over the network for every call:

```bash
curl -fsSL "{{ RELAY_URL }}/kssh.sh?secret={{ RELAY_SECRET }}" -o kssh.sh && chmod +x kssh.sh
export RELAY_SECRET="{{ RELAY_SECRET }}"
./kssh.sh run "ls -la"
```

### Stop a session

```bash
%env RELAY_SECRET={{ RELAY_SECRET }}
!curl -fsSL {{ RELAY_URL }}/kaggle_setup.sh | bash -s stop
```

## Troubleshooting

- **`No active session on relay`** — the notebook has not run step 2, or the
  session aged out. Re-run the setup cell.
- **HTTP 401** — `RELAY_SECRET` does not match the relay's.
- **`Permission denied (publickey)`** — run step 1, then wait ~10 s for the
  notebook to pick the key up.
- **`cloudflared not found`** — install it on the client machine.
- **Commands work but CUDA does not** — the setup script propagates Kaggle's
  `PATH`/`LD_LIBRARY_PATH` into SSH sessions; a login shell (`bash -lc "..."`)
  picks them up most reliably.

## Notes for automation

- `GET {{ RELAY_URL }}/kernels` returns registered sessions as JSON, including
  GPU/CPU/RAM. Authenticate with the `X-Relay-Secret` header.
- The relay stores only a hostname and metadata; it never proxies SSH traffic
  and never holds a private key.

---

## Active sessions

{{ SESSIONS_BLOCK }}
