# Preventing Timeouts on Kaggle & Google Colab

When running long agentic workflows, model training runs, or heavy evaluations, cloud notebook platforms enforce strict idle timeouts that can kill your session prematurely.

This guide outlines strategies to prevent timeouts and keep sessions running for hours—even while your local machine is asleep.

---

## Understanding Timeout Limits

| Platform | Interactive Idle Timeout | Max Session Duration | Browser Required? |
|---|---|---|---|
| **Kaggle (Interactive)** | **40 minutes** of no UI / kernel interaction | 12 hours (GPU) / 9 hours (CPU) | Yes (disconnect triggers 40m timer) |
| **Kaggle (Save & Run All / Batch)** | **None** | **12 hours** | **No** (runs headless on cloud) |
| **Google Colab (Free)** | **90 minutes** of idle | 12 hours | Yes |
| **Google Colab (Pro/Pro+)** | Extended | Up to 24 hours | Background execution supported on Pro+ |

---

## Strategy 1: Headless 12-Hour Run (Recommended for Overnight Jobs)

Kaggle allows you to run notebooks in **Batch Mode** ("Save & Run All / Commit"). In this mode:
- The notebook runs entirely on Kaggle's backend for up to **12 continuous hours**.
- **You do not need to keep your browser open or your computer turned on.**
- The VM retains its dedicated GPU (e.g. Nvidia Tesla T4 or P100) for the full 12 hours.

### Step-by-Step Instructions:

1. In your Kaggle notebook, add a single cell with the blocking flag (`-b`):
   ```bash
   %env RELAY_SECRET=YOUR_SECRET
   !curl -fsSL https://kagglessh.vercel.app/setup.sh | bash -s -b
   ```
   *(The `-b` flag instructs `setup.sh` to block indefinitely by tailing tunnel logs, preventing the cell from completing prematurely).*

2. Click **Save Version** (top right) $\rightarrow$ select **"Save & Run All (Commit)"** $\rightarrow$ click **Save**.

3. **Close your browser and turn off your laptop.**

4. The headless notebook boots on Kaggle, registers its tunnel hostname with your relay server, and activates self-healing heartbeats.

5. Your local agent or scripts can SSH in, transfer files, and monitor execution at any time:
   ```bash
   export RELAY_SECRET="YOUR_SECRET"
   ./kssh.sh run "nvidia-smi"
   ./kssh.sh run "python train.py"
   ```

---

## Strategy 2: In-Browser Anti-Idle Auto-Pinger (Interactive Mode)

If you prefer using the interactive notebook editor, Kaggle's 40-minute timeout occurs because the browser tab stops sending DOM and WebSocket keepalive events when left untouched.

Run this cell in your notebook to inject a background JavaScript keepalive loop into the page:

```python
from IPython.display import display, HTML

display(HTML("""
<script>
console.log("Anti-Timeout Keepalive Activated!");
setInterval(() => {
    // 1. Dispatch synthetic activity to reset idle counters
    window.dispatchEvent(new MouseEvent('mousemove'));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Shift' }));

    // 2. Ping the Jupyter kernel if active
    if (window.IPython && window.IPython.notebook) {
        window.IPython.notebook.kernel.execute('pass');
    }
}, 60000); // Triggers every 60 seconds
</script>
"""))
```

> [!NOTE]
> Keep the browser tab open (can be minimized or running on a locked screen). It will continuously reset Kaggle's 40-minute inactivity timer.

---

## Strategy 3: Client-Side Agent Heartbeat Loop

If your agent runs commands remotely from a local terminal or automation pipeline, running a periodic lightweight command keeps the relay session and connection active:

```bash
# Run this in background on your local machine:
while true; do
  ./kssh.sh run "date" >/dev/null 2>&1
  sleep 300 # Every 5 minutes
done &
```

---

## Best Practices for Long-Running Training Jobs

1. **Use `tmux` or `screen` for Training Runs**:
   SSH connections through Cloudflare Tunnels can occasionally renegotiate. To ensure long training jobs don't terminate if an SSH session drops:
   ```bash
   ./kssh.sh run "tmux new-session -d -s train 'python train.py > train.log 2>&1'"
   ```
   Inspect progress anytime with:
   ```bash
   ./kssh.sh run "tail -n 20 train.log"
   ```

2. **Save Checkpoints to Persistent Storage**:
   - **Kaggle**: Write checkpoints to `/kaggle/working/` (survives cell runs; exported on batch runs).
   - **Google Colab**: Mount Google Drive (`from google.colab import drive; drive.mount('/content/drive')`) or sync models locally via `kssh.sh get`.
