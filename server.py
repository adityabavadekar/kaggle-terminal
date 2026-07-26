#!/usr/bin/env python3
"""
Tiny relay server: Kaggle POSTs its tunnel hostname here, your laptop GETs it.
Protected by a shared secret so randoms hitting your URL can't read/write it.

Supports multiple concurrent kernels identified by a kernel_id (defaults to 'default').
Supports PostgreSQL database persistence (e.g. for Vercel Serverless deployments).
Falls back to in-memory storage if no database URL is provided (e.g. for local testing).
"""

import datetime
import functools
import hmac
import logging
import os
import re
import time
from urllib.parse import urlparse

import psycopg2
from flask import Flask, Response, jsonify, request

log = logging.getLogger("relay")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(ROOT_DIR, "templates")

# Load local .env file if present
env_path = os.path.join(ROOT_DIR, ".env")
if os.path.exists(env_path):
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip().strip("'\""))
    except Exception as e:
        log.warning("Failed to read .env: %s", e)

app = Flask(__name__)

SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", 600))
IS_DEMO_MODE = os.environ.get("DEMO_MODE", "false").lower() in ("1", "true", "yes")

# Order matters: it defines the SELECT/INSERT column order everywhere below.
SESSION_FIELDS = (
    "kernel_id", "hostname", "created_at", "gpu", "cpu", "ram",
    "username", "notebook", "run_type", "container_id", "gcp_zone",
    "container_name",
)
SESSION_COLUMNS = ", ".join(SESSION_FIELDS)

# In-memory fallback - only used when DATABASE_URL is unset or unreachable.
session_data = {}
pubkey_data = None


def get_db_connection():
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not db_url:
        return None
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)


def with_db(fallback):
    """Run the wrapped body with a cursor, falling back to in-memory on failure.

    The wrapped function receives the cursor as its first argument. `fallback`
    is called with the original arguments when no database is configured or the
    query fails.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                conn = get_db_connection()
            except Exception as e:
                log.warning("%s: connection failed: %s", fn.__name__, e)
                return fallback(*args, **kwargs)
            if conn is None:
                return fallback(*args, **kwargs)
            try:
                with conn:
                    with conn.cursor() as cur:
                        return fn(cur, *args, **kwargs)
            except Exception as e:
                log.warning("%s: query failed: %s", fn.__name__, e)
                return fallback(*args, **kwargs)
            finally:
                conn.close()
        return wrapper
    return decorate


def prune_memory():
    now = time.time()
    for k in [k for k, v in session_data.items()
              if (now - v.get("created_at", 0)) > SESSION_TTL_SECONDS]:
        session_data.pop(k, None)


_db_initialized = False


@with_db(fallback=lambda: None)
def init_db(cur):
    global _db_initialized
    if _db_initialized:
        return
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
        CREATE TABLE IF NOT EXISTS relay_pubkey (
            id INT PRIMARY KEY,
            pubkey TEXT NOT NULL
        );
    """)
    # Backfill metadata columns on tables created by older versions.
    for field in SESSION_FIELDS:
        if field not in ("kernel_id", "hostname", "created_at"):
            cur.execute(f"ALTER TABLE relay_session ADD COLUMN IF NOT EXISTS {field} TEXT")
    _db_initialized = True


init_db()


@with_db(fallback=lambda: pubkey_data)
def get_pubkey(cur):
    cur.execute("SELECT pubkey FROM relay_pubkey WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def save_pubkey(pubkey_str):
    global pubkey_data
    pubkey_data = pubkey_str.strip()
    _save_pubkey_db(pubkey_data)
    return pubkey_data


@with_db(fallback=lambda pubkey: None)
def _save_pubkey_db(cur, pubkey):
    cur.execute("""
        INSERT INTO relay_pubkey (id, pubkey) VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET pubkey = EXCLUDED.pubkey;
    """, (pubkey,))


def _memory_session(kernel_id=None):
    prune_memory()
    if not session_data:
        return None
    if kernel_id:
        return session_data.get(kernel_id)
    return max(session_data.values(), key=lambda x: x["created_at"])


@with_db(fallback=_memory_session)
def get_session(cur, kernel_id=None):
    cur.execute("DELETE FROM relay_session WHERE (%s - created_at) > %s",
                (time.time(), SESSION_TTL_SECONDS))
    if kernel_id:
        cur.execute(f"SELECT {SESSION_COLUMNS} FROM relay_session WHERE kernel_id = %s",
                    (kernel_id,))
    else:
        cur.execute(f"SELECT {SESSION_COLUMNS} FROM relay_session "
                    "ORDER BY created_at DESC LIMIT 1")
    row = cur.fetchone()
    return dict(zip(SESSION_FIELDS, row)) if row else None


def _memory_sessions():
    prune_memory()
    return sorted(session_data.values(), key=lambda x: x["created_at"], reverse=True)


@with_db(fallback=_memory_sessions)
def list_sessions(cur):
    cur.execute("DELETE FROM relay_session WHERE (%s - created_at) > %s",
                (time.time(), SESSION_TTL_SECONDS))
    cur.execute(f"SELECT {SESSION_COLUMNS} FROM relay_session ORDER BY created_at DESC")
    return [dict(zip(SESSION_FIELDS, row)) for row in cur.fetchall()]


def save_session(session):
    session_data[session["kernel_id"]] = session
    _save_session_db(session)
    return session


@with_db(fallback=lambda session: None)
def _save_session_db(cur, session):
    placeholders = ", ".join(["%s"] * len(SESSION_FIELDS))
    updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in SESSION_FIELDS if f != "kernel_id")
    cur.execute(
        f"INSERT INTO relay_session ({SESSION_COLUMNS}) VALUES ({placeholders}) "
        f"ON CONFLICT (kernel_id) DO UPDATE SET {updates};",
        tuple(session[f] for f in SESSION_FIELDS),
    )


def clear_session(kernel_id=None):
    if kernel_id:
        session_data.pop(kernel_id, None)
    else:
        session_data.clear()
    _clear_session_db(kernel_id)


@with_db(fallback=lambda kernel_id=None: None)
def _clear_session_db(cur, kernel_id=None):
    if kernel_id:
        cur.execute("DELETE FROM relay_session WHERE kernel_id = %s", (kernel_id,))
    else:
        cur.execute("DELETE FROM relay_session")


def secrets_match(provided):
    """Constant-time comparison of a caller-supplied secret to RELAY_SECRET."""
    expected = os.environ.get("RELAY_SECRET")
    if not expected:
        log.error("RELAY_SECRET environment variable is not set!")
        return False
    if not provided:
        return False
    return hmac.compare_digest(str(provided), expected)


def require_auth(allow_query=False, messages=None):
    """Accept the X-Relay-Secret header, and optionally a ?secret= parameter.

    Returns None when authorized, or a Flask error response.
    """
    provided = request.headers.get("X-Relay-Secret")
    from_query = False
    if allow_query and not provided:
        provided = request.args.get("secret")
        from_query = provided is not None
    if secrets_match(provided):
        return None
    body = {"error": "unauthorized"}
    if messages:
        body["message"] = messages["invalid"] if from_query else messages["missing"]
    return jsonify(body), 401


# Interpolated into served shell scripts - allowlist only.
SECRET_RE = re.compile(r"[A-Za-z0-9._:\-]{1,256}")

TUNNEL_HOSTNAME_RE = re.compile(
    r"[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+"
    r"(:[0-9]{1,5})?"
)


def is_valid_url(url_str):
    """Validate that the URL is well-formed and uses http/https."""
    try:
        parsed = urlparse(url_str)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    # Interpolated into a shell script - keep it inert.
    return bool(re.fullmatch(r"https?://[A-Za-z0-9._:\-/]{1,253}", url_str))


def is_valid_secret(secret_str):
    """Validate secret parameter against an allowlist of safe characters."""
    return bool(SECRET_RE.fullmatch(secret_str))


def is_valid_hostname(hostname_str):
    """Clients place this inside an ssh ProxyCommand, run via /bin/sh."""
    if not isinstance(hostname_str, str):
        return False
    return bool(TUNNEL_HOSTNAME_RE.fullmatch(hostname_str.strip().lower()))


def relay_url():
    url = request.url_root.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https" and url.startswith("http://"):
        url = "https://" + url[7:]
    return url


def read_template(name):
    with open(os.path.join(TEMPLATE_DIR, name), "r") as f:
        return f.read()


DEMO_ALLOWED_ENDPOINTS = {"index", "health", "info_page", "info_markdown"}


@app.before_request
def before_request():
    if IS_DEMO_MODE and request.endpoint not in DEMO_ALLOWED_ENDPOINTS:
        return jsonify({"error": "This relay server is running in demo mode. Only /info, /info.md, /, and /health endpoints are accessible. For more information on running your own server, please refer to the README."}), 403


@app.route("/post", methods=["POST"])
def post_tunnel_info():
    if (unauthorized := require_auth()):
        return unauthorized

    data = request.get_json(force=True, silent=True) or {}
    hostname = data.get("hostname")
    if not hostname:
        return jsonify({"error": "missing 'hostname'"}), 400

    hostname = str(hostname).strip()
    if not is_valid_hostname(hostname):
        # Clients feed this into an ssh ProxyCommand.
        return jsonify({"error": "invalid 'hostname' - must be a plain DNS hostname"}), 400

    session = {f: data.get(f) for f in SESSION_FIELDS}
    session["hostname"] = hostname
    session["created_at"] = time.time()
    session["kernel_id"] = request.args.get("kernel_id") or data.get("kernel_id") or "default"

    return jsonify({"ok": True, "stored": save_session(session)}), 200


@app.route("/get", methods=["GET"])
def get_tunnel_info():
    if (unauthorized := require_auth()):
        return unauthorized

    current = get_session(request.args.get("kernel_id"))
    if current is None:
        return jsonify({"error": "no active session"}), 404
    return jsonify(current), 200


@app.route("/kernels", methods=["GET"])
@app.route("/list", methods=["GET"])
def list_kernels():
    if (unauthorized := require_auth(allow_query=True)):
        return unauthorized

    kernels = list_sessions()
    return jsonify({"kernels": kernels, "count": len(kernels)}), 200


@app.route("/clear", methods=["POST"])
def clear_tunnel_info():
    if (unauthorized := require_auth()):
        return unauthorized

    clear_session(request.args.get("kernel_id"))
    return jsonify({"ok": True}), 200


@app.route("/pubkey", methods=["GET", "POST"])
def pubkey_handler():
    if request.method == "POST":
        if (unauthorized := require_auth()):
            return unauthorized

        data = request.get_json(force=True, silent=True) or {}
        pubkey_val = data.get("pubkey") if isinstance(data, dict) else None
        if not pubkey_val:
            pubkey_val = request.get_data(as_text=True)
        if not pubkey_val or not pubkey_val.strip():
            return jsonify({"error": "missing public key content"}), 400

        return jsonify({"ok": True, "pubkey": save_pubkey(pubkey_val)}), 200

    if (unauthorized := require_auth(allow_query=True)):
        return unauthorized

    pk = get_pubkey()
    if not pk:
        return jsonify({"error": "no public key uploaded"}), 404
    return pk, 200, {"Content-Type": "text/plain"}


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "running",
        "info": "For info refer /info",
        "info_md": "For LLM context refer /info.md?secret=...",
        "public_key_uploaded": bool(get_pubkey()),
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "up"}), 200


@app.route("/<script_name>.sh", methods=["GET"])
def serve_script(script_name):
    file_path = os.path.join(ROOT_DIR, f"{script_name}.sh")
    if not os.path.isfile(file_path):
        return jsonify({"error": "script not found"}), 404

    with open(file_path, "r") as f:
        content = f.read()

    url = relay_url()
    if not is_valid_url(url):
        return jsonify({"error": "invalid relay_url"}), 400
    # Already inside a double-quoted bash default - do not re-quote.
    content = re.sub(r'RELAY_URL="\${RELAY_URL:-[^"]*}"',
                     f'RELAY_URL="${{RELAY_URL:-{url}}}"', content)

    secret = request.args.get("secret")
    if secret:
        if not is_valid_secret(secret):
            return jsonify({
                "error": "invalid secret parameter",
                "message": "Secret may only contain letters, digits, and the characters . _ : -"
            }), 400
        content = re.sub(r'RELAY_SECRET="\${RELAY_SECRET:-[^"]*}"',
                         f'RELAY_SECRET="${{RELAY_SECRET:-{secret}}}"', content)

    return Response(content, mimetype="text/x-shellscript")


@app.route("/info", methods=["GET"])
@app.route("/info.html", methods=["GET"])
def info_page():
    html = read_template("info.html").replace("{{ SESSION_TTL }}", str(SESSION_TTL_SECONDS))
    return Response(html, mimetype="text/html")


def format_session_md(s):
    created = s.get("created_at")
    try:
        age = max(0, int(time.time() - float(created)))
        stamp = datetime.datetime.fromtimestamp(
            float(created), datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC") + f" ({age}s ago)"
    except (TypeError, ValueError):
        stamp = "unknown"

    kid = s.get("kernel_id") or "default"
    return (
        f"### `{kid}`\n"
        f"- **Tunnel host**: `{s.get('hostname', '')}`\n"
        f"- **Last heartbeat**: {stamp}\n"
        f"- **GPU**: {s.get('gpu') or 'None'}\n"
        f"- **CPU**: {s.get('cpu') or 'N/A'}\n"
        f"- **RAM**: {s.get('ram') or 'N/A'}\n"
        f"- **User / notebook**: {s.get('username') or 'N/A'} / {s.get('notebook') or 'N/A'}\n"
        f"- **Connect**: `KERNEL_ID={kid} ./kssh.sh run \"nvidia-smi\"`\n"
    )


INFO_MD_AUTH_MESSAGES = {
    "invalid": "Invalid secret provided. Please check your RELAY_SECRET and try again.",
    "missing": "Authentication required. Please provide a valid secret via ?secret=... or the X-Relay-Secret header.",
}


@app.route("/info.md", methods=["GET"])
def info_markdown():
    if (unauthorized := require_auth(allow_query=True, messages=INFO_MD_AUTH_MESSAGES)):
        return unauthorized

    sessions = list_sessions()
    if sessions:
        blocks = [f"{len(sessions)} session(s) registered.\n"]
        blocks += [format_session_md(s) for s in sessions]
        sess_block = "\n".join(blocks)
    else:
        sess_block = ("*No sessions registered.* Run step 2 in a Kaggle notebook cell to "
                      "register one.")

    secret = (request.args.get("secret") or request.headers.get("X-Relay-Secret")
              or os.environ.get("RELAY_SECRET", ""))
    rendered = (
        read_template("info.md")
        .replace("{{ RELAY_URL }}", relay_url())
        .replace("{{ RELAY_SECRET }}", secret)
        .replace("{{ SESSION_TTL }}", str(SESSION_TTL_SECONDS))
        .replace("{{ SESSIONS_BLOCK }}", sess_block)
    )
    return Response(rendered, mimetype="text/markdown")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
