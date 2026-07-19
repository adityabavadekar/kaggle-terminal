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
    return jsonify({"status": "running"}), 200


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
