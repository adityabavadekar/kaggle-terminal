# kaggle-terminal

SSH into Kaggle and Google Colab notebooks with a single command using Cloudflare Tunnel and Vercel Relay.

<img src="screenshot.png" width="320" alt="Screenshot">

## Deploy

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fadityabavadekar%2Fkaggle-terminal&project-name=mykagglessh&repository-name=mykagglessh&env=RELAY_SECRET,DATABASE_URL&envDescription=Random+authentication+secret%2CPostgreSQL+connection+string)

Requires `RELAY_SECRET` and `DATABASE_URL`. **Replace `kagglessh.vercel.app` with your deployment URL.**

Endpoints: [`/info`](https://kagglessh.vercel.app/info) (Dashboard) • [`/info.md`](https://kagglessh.vercel.app/info.md?secret=secret) (LLM Docs)

## Usage

1. **Upload public key** (Laptop):

   ```bash
   export RELAY_SECRET="secret"
   curl -fsSL https://kagglessh.vercel.app/upload_key.sh | bash
   ```

2. **Start SSH**:

   - **Kaggle cell**:
     ```bash
     %env RELAY_SECRET=secret
     !curl -fsSL https://kagglessh.vercel.app/kaggle_setup.sh | bash
     ```

   - **Google Colab cell**:
     ```bash
     %env RELAY_SECRET=secret
     !curl -fsSL https://kagglessh.vercel.app/colab_setup.sh | bash
     ```

3. **Connect** (Laptop):

   ```bash
   export RELAY_SECRET="secret"
   curl -fsSL https://kagglessh.vercel.app/client.sh | bash
   ```

## Options

- **List active sessions & specs**:

  ```bash
  export RELAY_SECRET="secret"
  curl -fsSL https://kagglessh.vercel.app/client.sh | bash -s list
  ```

- **Connect to custom kernel ID**:

  ```bash
  # Kaggle or Colab:
  %env RELAY_SECRET=secret
  !curl -fsSL https://kagglessh.vercel.app/colab_setup.sh | bash -s -i my-kernel
  ```

- **Stop tunnel**:

  ```bash
  # Kaggle:
  !curl -fsSL https://kagglessh.vercel.app/kaggle_setup.sh | bash -s stop

  # Colab:
  !curl -fsSL https://kagglessh.vercel.app/colab_setup.sh | bash -s stop
  ```

- **Get raw SSH command**:

  ```bash
  export RELAY_SECRET="secret"
  curl -fsSL https://kagglessh.vercel.app/client.sh | bash -s raw
  # or copy directly to clipboard
  curl -fsSL https://kagglessh.vercel.app/client.sh | bash -s raw | wl-copy
  ```

## Scripting

```bash
export RELAY_SECRET="YOUR_SECRET"
curl -fsSL https://kagglessh.vercel.app/kssh.sh | bash -s run "ls -la"            # exec, capture stdout
curl -fsSL https://kagglessh.vercel.app/kssh.sh | bash -s put train.py                      # upload (to /kaggle/working/ or /content/)
curl -fsSL https://kagglessh.vercel.app/kssh.sh | bash -s get output.csv .                  # download

# Download helper script locally:
curl -fsSL https://kagglessh.vercel.app/kssh.sh?secret=YOUR_SECRET -o kssh.sh && chmod +x kssh.sh
./kssh.sh run "ls -la"
```

## Long Runs & Preventing Timeouts

For running overnight training or unattended agent tasks on Kaggle without hitting the 40-minute inactivity timeout, see [TIMEOUT_PREVENTION.md](TIMEOUT_PREVENTION.md) (covers 12-hour headless batch runs, in-browser anti-idle scripts, and detached tmux workflows).

## License

[Apache 2.0](LICENSE)
