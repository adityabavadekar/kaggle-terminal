#!/usr/bin/env python3
"""
Tiny relay server: Kaggle POSTs its tunnel hostname here, your laptop GETs it.
Protected by a shared secret so randoms hitting your URL can't read/write it.

Supports multiple concurrent kernels identified by a kernel_id (defaults to 'default').
Supports PostgreSQL database persistence (e.g. for Vercel Serverless deployments).
Falls back to in-memory storage if no database URL is provided (e.g. for local testing).
"""

import os
import re
import time
from flask import Flask, request, jsonify, send_from_directory, Response
import psycopg2

# Load local .env file if present
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip().strip("'\""))
    except Exception as e:
        print(f"Notice: Failed to read .env: {e}")

app = Flask(__name__)

# In-memory fallback - only used when DATABASE_URL is not set.
session_data = {}
pubkey_data = None


def get_db_connection():
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        return None
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)


def init_db():
    conn = get_db_connection()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS relay_session (
                        kernel_id TEXT PRIMARY KEY,
                        hostname TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        gpu TEXT,
                        cpu TEXT,
                        ram TEXT,
                        username TEXT,
                        notebook TEXT,
                        run_type TEXT,
                        container_id TEXT,
                        gcp_zone TEXT,
                        container_name TEXT
                    );
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS gpu TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS cpu TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS ram TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS username TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS notebook TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS run_type TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS container_id TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS gcp_zone TEXT;
                    ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS container_name TEXT;
                    CREATE TABLE IF NOT EXISTS relay_pubkey (
                        id INT PRIMARY KEY,
                        pubkey TEXT NOT NULL
                    );
                """)
    except Exception as e:
        print(f"Database initialization failed: {e}")
    finally:
        conn.close()


# Run DB initialization
init_db()


def get_pubkey():
    conn = get_db_connection()
    if conn is None:
        return pubkey_data

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pubkey FROM relay_pubkey WHERE id = 1")
                row = cur.fetchone()
                if row:
                    return row[0]
                return None
    except Exception as e:
        print(f"Error fetching pubkey from database: {e}")
        return pubkey_data
    finally:
        conn.close()


def save_pubkey(pubkey_str):
    global pubkey_data
    pubkey_data = pubkey_str.strip()

    conn = get_db_connection()
    if conn is None:
        return pubkey_data

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO relay_pubkey (id, pubkey)
                    VALUES (1, %s)
                    ON CONFLICT (id)
                    DO UPDATE SET pubkey = EXCLUDED.pubkey;
                """, (pubkey_data,))
        return pubkey_data
    except Exception as e:
        print(f"Error saving pubkey to database: {e}")
        return pubkey_data
    finally:
        conn.close()


SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", 600))


def get_session(kernel_id=None):
    now = time.time()
    conn = get_db_connection()
    if conn is None:
        # Memory cleanup
        expired_keys = [k for k, v in session_data.items() if (now - v.get("created_at", 0)) > SESSION_TTL_SECONDS]
        for k in expired_keys:
            session_data.pop(k, None)

        if not session_data:
            return None
        if kernel_id:
            return session_data.get(kernel_id)
        return max(session_data.values(), key=lambda x: x["created_at"])

    try:
        with conn:
            with conn.cursor() as cur:
                # Cleanup expired in DB
                cur.execute("DELETE FROM relay_session WHERE (%s - created_at) > %s", (now, SESSION_TTL_SECONDS))
                if kernel_id:
                    cur.execute(
                        "SELECT hostname, created_at, gpu, cpu, ram, username, notebook, run_type, container_id, gcp_zone, container_name, kernel_id FROM relay_session WHERE kernel_id = %s",
                        (kernel_id,)
                    )
                else:
                    cur.execute(
                        "SELECT hostname, created_at, gpu, cpu, ram, username, notebook, run_type, container_id, gcp_zone, container_name, kernel_id FROM relay_session ORDER BY created_at DESC LIMIT 1"
                    )
                row = cur.fetchone()
                if row:
                    return {
                        "hostname": row[0],
                        "created_at": row[1],
                        "gpu": row[2],
                        "cpu": row[3],
                        "ram": row[4],
                        "username": row[5],
                        "notebook": row[6],
                        "run_type": row[7],
                        "container_id": row[8],
                        "gcp_zone": row[9],
                        "container_name": row[10],
                        "kernel_id": row[11]
                    }
                return None
    except Exception as e:
        print(f"Error fetching from database: {e}")
        expired_keys = [k for k, v in session_data.items() if (now - v.get("created_at", 0)) > SESSION_TTL_SECONDS]
        for k in expired_keys:
            session_data.pop(k, None)
        if not session_data:
            return None
        if kernel_id:
            return session_data.get(kernel_id)
        return max(session_data.values(), key=lambda x: x["created_at"])
    finally:
        conn.close()


def list_sessions():
    now = time.time()
    conn = get_db_connection()
    if conn is None:
        expired_keys = [k for k, v in session_data.items() if (now - v.get("created_at", 0)) > SESSION_TTL_SECONDS]
        for k in expired_keys:
            session_data.pop(k, None)
        return sorted(list(session_data.values()), key=lambda x: x["created_at"], reverse=True)

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM relay_session WHERE (%s - created_at) > %s", (now, SESSION_TTL_SECONDS))
                cur.execute(
                    "SELECT kernel_id, hostname, created_at, gpu, cpu, ram, username, notebook, run_type, container_id, gcp_zone, container_name FROM relay_session ORDER BY created_at DESC"
                )
                rows = cur.fetchall()
                return [
                    {
                        "kernel_id": row[0],
                        "hostname": row[1],
                        "created_at": row[2],
                        "gpu": row[3],
                        "cpu": row[4],
                        "ram": row[5],
                        "username": row[6],
                        "notebook": row[7],
                        "run_type": row[8],
                        "container_id": row[9],
                        "gcp_zone": row[10],
                        "container_name": row[11]
                    }
                    for row in rows
                ]
    except Exception as e:
        print(f"Error listing sessions from database: {e}")
        expired_keys = [k for k, v in session_data.items() if (now - v.get("created_at", 0)) > SESSION_TTL_SECONDS]
        for k in expired_keys:
            session_data.pop(k, None)
        return sorted(list(session_data.values()), key=lambda x: x["created_at"], reverse=True)
    finally:
        conn.close()


def save_session(hostname, kernel_id="default", gpu=None, cpu=None, ram=None, username=None, notebook=None, run_type=None, container_id=None, gcp_zone=None, container_name=None):
    global session_data
    created_at = time.time()
    session_data[kernel_id] = {
        "hostname": hostname,
        "created_at": created_at,
        "kernel_id": kernel_id,
        "gpu": gpu,
        "cpu": cpu,
        "ram": ram,
        "username": username,
        "notebook": notebook,
        "run_type": run_type,
        "container_id": container_id,
        "gcp_zone": gcp_zone,
        "container_name": container_name
    }

    conn = get_db_connection()
    if conn is None:
        return session_data[kernel_id]

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO relay_session (kernel_id, hostname, created_at, gpu, cpu, ram, username, notebook, run_type, container_id, gcp_zone, container_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (kernel_id)
                    DO UPDATE SET hostname = EXCLUDED.hostname, created_at = EXCLUDED.created_at,
                                  gpu = EXCLUDED.gpu, cpu = EXCLUDED.cpu, ram = EXCLUDED.ram,
                                  username = EXCLUDED.username, notebook = EXCLUDED.notebook,
                                  run_type = EXCLUDED.run_type, container_id = EXCLUDED.container_id,
                                  gcp_zone = EXCLUDED.gcp_zone, container_name = EXCLUDED.container_name;
                """, (kernel_id, hostname, created_at, gpu, cpu, ram, username, notebook, run_type, container_id, gcp_zone, container_name))
        return session_data[kernel_id]
    except Exception as e:
        print(f"Error saving to database: {e}")
        return session_data[kernel_id]
    finally:
        conn.close()


def clear_session(kernel_id=None):
    global session_data
    if kernel_id:
        session_data.pop(kernel_id, None)
    else:
        session_data.clear()

    conn = get_db_connection()
    if conn is None:
        return

    try:
        with conn:
            with conn.cursor() as cur:
                if kernel_id:
                    cur.execute("DELETE FROM relay_session WHERE kernel_id = %s", (kernel_id,))
                else:
                    cur.execute("DELETE FROM relay_session")
    except Exception as e:
        print(f"Error clearing database: {e}")
    finally:
        conn.close()


def check_auth(req):
    secret = os.environ.get("RELAY_SECRET")
    if not secret:
        print("ERROR: RELAY_SECRET environment variable is not set!")
        return False
    return req.headers.get("X-Relay-Secret") == secret


@app.route("/post", methods=["POST"])
def post_tunnel_info():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    hostname = data.get("hostname")
    if not hostname:
        return jsonify({"error": "missing 'hostname'"}), 400

    kernel_id = request.args.get("kernel_id") or data.get("kernel_id") or "default"
    gpu = data.get("gpu")
    cpu = data.get("cpu")
    ram = data.get("ram")
    username = data.get("username")
    notebook = data.get("notebook")
    run_type = data.get("run_type")
    container_id = data.get("container_id")
    gcp_zone = data.get("gcp_zone")
    container_name = data.get("container_name")

    stored = save_session(
        hostname, kernel_id, gpu=gpu, cpu=cpu, ram=ram,
        username=username, notebook=notebook, run_type=run_type,
        container_id=container_id, gcp_zone=gcp_zone, container_name=container_name
    )
    return jsonify({"ok": True, "stored": stored}), 200


@app.route("/get", methods=["GET"])
def get_tunnel_info():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    kernel_id = request.args.get("kernel_id")
    current = get_session(kernel_id)
    if current is None:
        return jsonify({"error": "no active session"}), 404

    return jsonify(current), 200


@app.route("/kernels", methods=["GET"])
@app.route("/list", methods=["GET"])
def list_kernels():
    secret = request.args.get("secret")
    if secret:
        expected_secret = os.environ.get("RELAY_SECRET")
        if secret != expected_secret:
            return jsonify({"error": "unauthorized"}), 401
    elif not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    kernels = list_sessions()
    return jsonify({"kernels": kernels, "count": len(kernels)}), 200


@app.route("/clear", methods=["POST"])
def clear_tunnel_info():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    kernel_id = request.args.get("kernel_id")
    clear_session(kernel_id)
    return jsonify({"ok": True}), 200


@app.route("/pubkey", methods=["GET", "POST"])
def pubkey_handler():
    if request.method == "POST":
        if not check_auth(request):
            return jsonify({"error": "unauthorized"}), 401

        data = request.get_json(force=True, silent=True) or {}
        pubkey_val = data.get("pubkey") if isinstance(data, dict) else None
        if not pubkey_val:
            pubkey_val = request.get_data(as_text=True)

        if not pubkey_val or not pubkey_val.strip():
            return jsonify({"error": "missing public key content"}), 400

        saved = save_pubkey(pubkey_val)
        return jsonify({"ok": True, "pubkey": saved}), 200

    secret = request.args.get("secret")
    if secret:
        expected_secret = os.environ.get("RELAY_SECRET")
        if secret != expected_secret:
            return jsonify({"error": "unauthorized"}), 401
    elif not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    pk = get_pubkey()
    if not pk:
        return jsonify({"error": "no public key uploaded"}), 404
    return pk, 200, {"Content-Type": "text/plain"}


@app.route("/", methods=["GET"])
def index():
    has_pk = bool(get_pubkey())
    return jsonify({
        "status": "running",
        "info": "For info refer /info",
        "info_md": "For LLM context refer /info.md?secret=...",
        "public_key_uploaded": has_pk
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "up"}), 200


@app.route("/<script_name>.sh", methods=["GET"])
def serve_script(script_name):
    filename = f"{script_name}.sh"
    root_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(root_dir, filename)
    if not os.path.isfile(file_path):
        return jsonify({"error": "script not found"}), 404

    with open(file_path, "r") as f:
        content = f.read()

    relay_url = request.url_root.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https" and relay_url.startswith("http://"):
        relay_url = "https://" + relay_url[7:]

    secret = request.args.get("secret")

    if relay_url:
        content = re.sub(
            r'RELAY_URL="\${RELAY_URL:-[^"]*}"',
            f'RELAY_URL="${{RELAY_URL:-{relay_url}}}"',
            content
        )
    if secret:
        content = re.sub(
            r'RELAY_SECRET="\${RELAY_SECRET:-[^"]*}"',
            f'RELAY_SECRET="${{RELAY_SECRET:-{secret}}}"',
            content
        )

    return Response(content, mimetype="text/x-shellscript")


INFO_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kaggle SSH Tunnel Relay</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #000000;
      --card-bg: #0a0a0a;
      --card-border: #1f1f1f;
      --accent: #2563eb;
      --accent-hover: #1d4ed8;
      --accent-glow: rgba(37, 99, 235, 0.2);
      --text: #ffffff;
      --text-muted: #888888;
      --success: #10b981;
      --code-bg: #050505;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 2rem 1rem;
      min-height: 100vh;
    }
    .container { max-width: 900px; margin: 0 auto; }
    
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--card-border);
    }
    h1 { font-size: 1.5rem; font-weight: 700; background: linear-gradient(90deg, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    
    .secret-bar {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      padding: 0.5rem 0.75rem;
      border-radius: 8px;
    }
    .secret-bar input {
      background: transparent;
      border: none;
      color: var(--text);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.875rem;
      outline: none;
      width: 160px;
    }
    .btn-sm {
      background: var(--accent);
      color: white;
      border: none;
      padding: 0.35rem 0.75rem;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.2s;
    }
    .btn-sm:hover { background: var(--accent-hover); }

    .modal-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 100;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s;
    }
    .modal-overlay.active { opacity: 1; pointer-events: auto; }
    .modal {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 2rem;
      width: 100%;
      max-width: 400px;
      box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5), 0 0 30px var(--accent-glow);
    }
    .modal h2 { font-size: 1.25rem; margin-bottom: 0.5rem; }
    .modal p { color: var(--text-muted); font-size: 0.875rem; margin-bottom: 1.25rem; }
    .modal input {
      width: 100%;
      padding: 0.75rem;
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      color: var(--text);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.9rem;
      margin-bottom: 1rem;
      outline: none;
    }
    .modal input:focus { border-color: var(--accent); }
    .modal button {
      width: 100%;
      padding: 0.75rem;
      background: var(--accent);
      color: white;
      border: none;
      border-radius: 8px;
      font-weight: 600;
      cursor: pointer;
    }

    .section {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
    }
    .section h2 { font-size: 1.1rem; margin-bottom: 0.5rem; color: #60a5fa; }
    .section p { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1rem; }

    .code-block {
      position: relative;
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 1rem;
      margin-bottom: 1rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85rem;
      color: #e5e7eb;
      overflow-x: auto;
    }
    .code-block code {
      display: block;
      white-space: pre-wrap;
    }
    .code-block:last-child { margin-bottom: 0; }
    .code-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.5rem;
      color: var(--text-muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .copy-btn {
      position: absolute;
      top: 0.75rem;
      right: 0.75rem;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 0.35rem 0.65rem;
      border-radius: 6px;
      font-size: 0.75rem;
      cursor: pointer;
      transition: all 0.2s;
    }
    .copy-btn:hover { background: var(--accent); border-color: var(--accent); }
    .copy-btn.copied { background: var(--success); border-color: var(--success); }

    .session-card {
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 1rem;
      margin-top: 1rem;
    }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-top: 0.5rem; }
    .stat-item { font-size: 0.85rem; }
    .stat-label { color: var(--text-muted); font-size: 0.75rem; display: block; }
    .stat-val { font-family: 'JetBrains Mono', monospace; font-weight: 500; }
  </style>
</head>
<body>

  <div class="modal-overlay" id="authModal">
    <div class="modal">
      <h2>Authentication</h2>
      <p>Enter your <code>RELAY_SECRET</code> to unlock one-click scripts & session status.</p>
      <input type="password" id="secretInput" placeholder="RELAY_SECRET" autocomplete="off" onkeydown="if(event.key==='Enter') saveSecret()">
      <button onclick="saveSecret()">Unlock Scripts</button>
    </div>
  </div>

  <div class="container">
    <header>
      <h1>Kaggle SSH Tunnel Relay</h1>
      <div style="display:flex; align-items:center; gap:0.75rem;">
        <a id="llmContextBtn" href="#" target="_blank" class="btn-sm" style="text-decoration:none; background:rgba(255,255,255,0.06); border:1px solid var(--card-border); color:var(--text); display:inline-flex; align-items:center; gap:0.35rem;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
          Open info.md
        </a>
        <div class="secret-bar">
          <span style="font-size:0.8rem; color:var(--text-muted);">SECRET:</span>
          <input type="password" id="activeSecret" readonly placeholder="••••••••">
          <button class="btn-sm" onclick="promptSecret()">Change</button>
        </div>
      </div>
    </header>

    <div style="display:flex; align-items:center; justify-content:space-between; background:var(--card-bg); border:1px solid var(--card-border); padding:0.75rem 1rem; border-radius:8px; margin-bottom:1.5rem;">
      <span style="font-size:0.85rem; font-weight:500; color:var(--text);">Script Format:</span>
      <div style="display:flex; gap:1rem; font-size:0.85rem;">
        <label style="cursor:pointer; display:flex; align-items:center; gap:0.35rem;">
          <input type="radio" name="formatMode" value="export" checked onchange="toggleFormatMode()">
          Use <code>export</code> (RELAY_SECRET & RELAY_URL)
        </label>
        <label style="cursor:pointer; display:flex; align-items:center; gap:0.35rem;">
          <input type="radio" name="formatMode" value="query" onchange="toggleFormatMode()">
          Use <code>?secret=</code> query param
        </label>
      </div>
    </div>

    <div class="section">
      <h2>1. Upload SSH Public Key (Laptop)</h2>
      <p>Generates <code>~/.ssh/kaggle_rsa</code> locally (if missing) and uploads the public key to this relay server.</p>
      <div class="code-block">
        <button class="copy-btn" onclick="copyCode(this, 'code-upload')">Copy</button>
        <code id="code-upload"></code>
      </div>
    </div>

    <div class="section">
      <h2>2. Start SSH Tunnel (Kaggle Cell)</h2>
      <p>Run inside a Kaggle notebook cell to launch <code>sshd</code> and Cloudflare Tunnel.</p>
      <div class="code-block">
        <button class="copy-btn" onclick="copyCode(this, 'code-kaggle')">Copy</button>
        <code id="code-kaggle"></code>
      </div>
    </div>

    <div class="section">
      <h2>3. Connect to Shell (Laptop)</h2>
      <p>Fetches active tunnel endpoint and launches interactive SSH session.</p>
      <div class="code-block">
        <button class="copy-btn" onclick="copyCode(this, 'code-client')">Copy</button>
        <code id="code-client"></code>
      </div>
    </div>

    <div class="section">
      <h2>4. Non-Interactive Scripting & Transfer (<code>kssh.sh</code>)</h2>
      <p>Run commands non-interactively or transfer files to/from Kaggle without opening interactive SSH.</p>

      <div class="sub-group">
        <h3 style="font-size:0.95rem; margin-bottom:0.75rem; color:#60a5fa;">Direct Command Execution</h3>
        
        <div class="code-block">
          <div class="code-header">Run "ls -la"</div>
          <button class="copy-btn" onclick="copyCode(this, 'code-kssh-ls')">Copy</button>
          <code id="code-kssh-ls"></code>
        </div>

        <div class="code-block" style="margin-top: 0.75rem;">
          <div class="code-header">Run "pwd"</div>
          <button class="copy-btn" onclick="copyCode(this, 'code-kssh-pwd')">Copy</button>
          <code id="code-kssh-pwd"></code>
        </div>

        <div class="code-block" style="margin-top: 0.75rem;">
          <div class="code-header">Run "nvidia-smi"</div>
          <button class="copy-btn" onclick="copyCode(this, 'code-kssh-gpu')">Copy</button>
          <code id="code-kssh-gpu"></code>
        </div>
      </div>

      <div class="sub-group" style="margin-top: 1.25rem;">
        <h3 style="font-size:0.95rem; margin-bottom:0.75rem; color:#60a5fa;">Direct File Transfers</h3>
        
        <div class="code-block">
          <div class="code-header">Upload File (put)</div>
          <button class="copy-btn" onclick="copyCode(this, 'code-kssh-put')">Copy</button>
          <code id="code-kssh-put"></code>
        </div>

        <div class="code-block" style="margin-top: 0.75rem;">
          <div class="code-header">Download File (get)</div>
          <button class="copy-btn" onclick="copyCode(this, 'code-kssh-get')">Copy</button>
          <code id="code-kssh-get"></code>
        </div>
      </div>

      <div class="sub-group" style="margin-top: 1.25rem;">
        <h3 style="font-size:0.95rem; margin-bottom:0.75rem; color:#60a5fa;">Local Helper Download</h3>

        <div class="code-block">
          <div class="code-header">Download & Run Locally</div>
          <button class="copy-btn" onclick="copyCode(this, 'code-kssh-dl')">Copy</button>
          <code id="code-kssh-dl"></code>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Active Kernel Sessions</h2>
      <p>Sessions currently active on this relay server.</p>
      <div id="sessionsContainer">
        <p style="color:var(--text-muted); font-size:0.85rem;">Loading active sessions...</p>
      </div>
    </div>
  </div>

  <script>
    const baseUrl = window.location.origin;

    function getSecret() {
      return localStorage.getItem('RELAY_SECRET') || '';
    }

    function promptSecret() {
      document.getElementById('authModal').classList.add('active');
      document.getElementById('secretInput').focus();
    }

    function saveSecret() {
      const val = document.getElementById('secretInput').value.trim();
      if (val) {
        localStorage.setItem('RELAY_SECRET', val);
        document.getElementById('authModal').classList.remove('active');
        updateUI();
      }
    }

    function toggleFormatMode() {
      updateUI();
    }

    function copyCode(btn, elementId) {
      const el = document.getElementById(elementId);
      if (!el) return;
      navigator.clipboard.writeText(el.innerText).then(() => {
        const orig = btn.innerText;
        btn.innerText = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.innerText = orig;
          btn.classList.remove('copied');
        }, 2000);
      });
    }

    function updateUI() {
      const secret = getSecret();
      if (!secret) {
        promptSecret();
        return;
      }
      document.getElementById('activeSecret').value = secret;
      const llmBtn = document.getElementById('llmContextBtn');
      if (llmBtn) {
        llmBtn.href = `${baseUrl}/info.md?secret=${encodeURIComponent(secret)}`;
      }

      const mode = document.querySelector('input[name="formatMode"]:checked')?.value || 'export';

      const snippets = {};
      if (mode === 'export') {
        const expHeader = `export RELAY_SECRET="${secret}"\n`;
        snippets['code-upload'] = `${expHeader}curl -fsSL ${baseUrl}/upload_key.sh | bash`;
        snippets['code-kaggle'] = `!export RELAY_SECRET="${secret}"\n!curl -fsSL ${baseUrl}/kaggle_setup.sh | bash`;
        snippets['code-client'] = `${expHeader}curl -fsSL ${baseUrl}/client.sh | bash`;
        snippets['code-kssh-ls'] = `${expHeader}curl -fsSL ${baseUrl}/kssh.sh | bash -s run "ls -la"`;
        snippets['code-kssh-pwd'] = `${expHeader}curl -fsSL ${baseUrl}/kssh.sh | bash -s run "pwd"`;
        snippets['code-kssh-gpu'] = `${expHeader}curl -fsSL ${baseUrl}/kssh.sh | bash -s run "nvidia-smi"`;
        snippets['code-kssh-put'] = `${expHeader}curl -fsSL ${baseUrl}/kssh.sh | bash -s put train.py /kaggle/working/`;
        snippets['code-kssh-get'] = `${expHeader}curl -fsSL ${baseUrl}/kssh.sh | bash -s get /kaggle/working/out.csv .`;
        snippets['code-kssh-dl'] = `${expHeader}curl -fsSL ${baseUrl}/kssh.sh -o kssh.sh && chmod +x kssh.sh\n./kssh.sh run "ls -la"`;
      } else {
        snippets['code-upload'] = `curl -fsSL ${baseUrl}/upload_key.sh?secret=${secret} | bash`;
        snippets['code-kaggle'] = `!curl -fsSL ${baseUrl}/kaggle_setup.sh?secret=${secret} | bash`;
        snippets['code-client'] = `curl -fsSL ${baseUrl}/client.sh?secret=${secret} | bash`;
        snippets['code-kssh-ls'] = `export RELAY_SECRET="${secret}"\ncurl -fsSL ${baseUrl}/kssh.sh | bash -s run "ls -la"`;
        snippets['code-kssh-pwd'] = `export RELAY_SECRET="${secret}"\ncurl -fsSL ${baseUrl}/kssh.sh | bash -s run "pwd"`;
        snippets['code-kssh-gpu'] = `export RELAY_SECRET="${secret}"\ncurl -fsSL ${baseUrl}/kssh.sh | bash -s run "nvidia-smi"`;
        snippets['code-kssh-put'] = `export RELAY_SECRET="${secret}"\ncurl -fsSL ${baseUrl}/kssh.sh | bash -s put train.py /kaggle/working/`;
        snippets['code-kssh-get'] = `export RELAY_SECRET="${secret}"\ncurl -fsSL ${baseUrl}/kssh.sh | bash -s get /kaggle/working/out.csv .`;
        snippets['code-kssh-dl'] = `curl -fsSL ${baseUrl}/kssh.sh?secret=${secret} -o kssh.sh && chmod +x kssh.sh\nexport RELAY_SECRET="${secret}"\n./kssh.sh run "ls -la"`;
      }

      Object.keys(snippets).forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerText = snippets[id];
      });

      fetchSessions(secret);
    }

    async function fetchSessions(secret) {
      const container = document.getElementById('sessionsContainer');
      try {
        const res = await fetch(`${baseUrl}/kernels?secret=${encodeURIComponent(secret)}`);
        if (res.status === 401) {
          container.innerHTML = '<p style="color:#ef4444; font-size:0.85rem;">Unauthorized: Invalid secret.</p>';
          return;
        }
        const data = await res.json();
        if (!data.kernels || data.kernels.length === 0) {
          container.innerHTML = '<p style="color:var(--text-muted); font-size:0.85rem;">No active Kaggle sessions found.</p>';
          return;
        }

        container.innerHTML = data.kernels.map(k => `
          <div class="session-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem;">
              <strong style="color:#60a5fa;">${k.kernel_id || 'default'}</strong>
              <span style="font-size:0.75rem; color:var(--text-muted);">${new Date(k.created_at * 1000).toLocaleString()}</span>
            </div>
            <div class="grid">
              <div class="stat-item"><span class="stat-label">GPU</span><span class="stat-val">${k.gpu || 'None'}</span></div>
              <div class="stat-item"><span class="stat-label">CPU</span><span class="stat-val">${k.cpu || 'N/A'}</span></div>
              <div class="stat-item"><span class="stat-label">RAM</span><span class="stat-val">${k.ram || 'N/A'}</span></div>
              <div class="stat-item"><span class="stat-label">Tunnel Host</span><span class="stat-val">${k.hostname}</span></div>
            </div>
          </div>
        `).join('');
      } catch (err) {
        container.innerHTML = '<p style="color:#ef4444; font-size:0.85rem;">Failed to load sessions.</p>';
      }
    }

    window.addEventListener('DOMContentLoaded', () => {
      if (!getSecret()) {
        promptSecret();
      } else {
        updateUI();
      }
    });
  </script>
</body>
</html>"""


@app.route("/info", methods=["GET"])
@app.route("/info.html", methods=["GET"])
def info_page():
    return Response(INFO_HTML_TEMPLATE, mimetype="text/html")


INFO_MD_TEMPLATE = """---
name: kaggle-terminal
description: Manage and execute non-interactive SSH commands and file transfers to Kaggle notebook sessions over a Cloudflare Tunnel relay.
---

# Kaggle Terminal Skill

Use this skill when performing tasks requiring execution of commands or transferring files to active Kaggle kernel containers.

## Configuration & Environment

- **Relay Server**: `{{ RELAY_URL }}`
- **Secret**: `{{ RELAY_SECRET }}`

Always set `RELAY_SECRET="{{ RELAY_SECRET }}"` in environment when running commands.

---

## Operations & Workflow

### 1. Upload SSH Public Key (Laptop)
Ensure public key is uploaded to relay server prior to starting tunnel:

```bash
export RELAY_SECRET="{{ RELAY_SECRET }}"
curl -fsSL {{ RELAY_URL }}/upload_key.sh | bash
```

### 2. Start SSH Tunnel (Kaggle Notebook Cell)
Execute inside Kaggle notebook cell to launch `sshd` and Cloudflare Tunnel:

```bash
!export RELAY_SECRET="{{ RELAY_SECRET }}"
!curl -fsSL {{ RELAY_URL }}/kaggle_setup.sh | bash
```

### 3. Interactive Shell Connection
Launch interactive terminal session:

```bash
export RELAY_SECRET="{{ RELAY_SECRET }}"
curl -fsSL {{ RELAY_URL }}/client.sh | bash
```

### 4. Non-Interactive Command Execution (`kssh.sh`)

#### Run Shell Commands Directly
```bash
export RELAY_SECRET="{{ RELAY_SECRET }}"
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s run "ls -la"
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s run "pwd"
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s run "nvidia-smi"
```

#### Transfer Files (Upload / Download)
```bash
export RELAY_SECRET="{{ RELAY_SECRET }}"
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s put train.py /kaggle/working/
curl -fsSL {{ RELAY_URL }}/kssh.sh | bash -s get /kaggle/working/out.csv .
```

#### Download Local Helper Script
```bash
curl -fsSL {{ RELAY_URL }}/kssh.sh?secret={{ RELAY_SECRET }} -o kssh.sh && chmod +x kssh.sh
export RELAY_SECRET="{{ RELAY_SECRET }}"
./kssh.sh run "ls -la"
./kssh.sh put train.py /kaggle/working/
./kssh.sh get /kaggle/working/out.csv .
```

---

## 5. Active Sessions Status

{{ SESSIONS_BLOCK }}
"""


@app.route("/info.md", methods=["GET"])
def info_markdown():
    secret = request.args.get("secret")
    if secret:
        expected_secret = os.environ.get("RELAY_SECRET")
        if secret != expected_secret:
            return jsonify({
                "error": "unauthorized",
                "message": "Invalid secret provided. Please check your RELAY_SECRET and try again."
            }), 401
    elif not check_auth(request):
        return jsonify({
            "error": "unauthorized",
            "message": "Authentication required. Please provide a valid secret via ?secret=... or the X-Relay-Secret header."
        }), 401

    relay_url = request.url_root.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https" and relay_url.startswith("http://"):
        relay_url = "https://" + relay_url[7:]

    req_secret = secret or request.headers.get("X-Relay-Secret") or os.environ.get("RELAY_SECRET", "")
    sessions = list_sessions()

    sess_lines = []
    if sessions:
        sess_lines.append(f"Total Active Sessions: {len(sessions)}\n")
        for s in sessions:
            kid = s.get("kernel_id") or "default"
            host = s.get("hostname", "")
            created = s.get("created_at", "")
            gpu = s.get("gpu") or "None"
            cpu = s.get("cpu") or "N/A"
            ram = s.get("ram") or "N/A"
            user = s.get("username") or "N/A"
            nb = s.get("notebook") or "N/A"
            sess_lines.append(
                f"### Kernel ID: `{kid}`\n"
                f"- **Tunnel Host**: `{host}`\n"
                f"- **Created At**: {created}\n"
                f"- **GPU**: {gpu}\n"
                f"- **CPU**: {cpu}\n"
                f"- **RAM**: {ram}\n"
                f"- **User / Notebook**: {user} / {nb}\n"
            )
        sess_block = "\n".join(sess_lines)
    else:
        sess_block = "*No active Kaggle sessions currently registered on relay.*"

    rendered = (
        INFO_MD_TEMPLATE
        .replace("{{ RELAY_URL }}", relay_url)
        .replace("{{ RELAY_SECRET }}", req_secret)
        .replace("{{ SESSIONS_BLOCK }}", sess_block)
    )

    return Response(rendered, mimetype="text/markdown")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
