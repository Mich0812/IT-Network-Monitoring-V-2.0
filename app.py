from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    url_for,
    jsonify,
    make_response,
    abort,
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

import sqlite3
import os
import secrets
import time
import threading
import json
import functools
import hashlib
from datetime import timedelta, datetime, timezone

from config import (
    COMPANIES,
    SPEEDTEST_COMPANY,
    is_valid_company,
    default_company,
    validate_config,
    RETENTION_DAYS as DEFAULT_RETENTION_DAYS,
    AGENT_STALE_SECONDS,
    PUBLIC_STATUS,
    STATUS_STALE_SECONDS,
)


# ============================================================
# FLASK CONFIGURATION
# ============================================================

app = Flask(__name__)

# Re-read templates on every render so edits show up without a server
# restart. Without this, Jinja caches templates forever when debug=False,
# which silently serves stale pages after a template change.
app.config["TEMPLATES_AUTO_RELOAD"] = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# DB_PATH lets tests (and advanced setups) point elsewhere without
# touching the production uptime.db.
DATABASE = os.environ.get(
    "DB_PATH"
) or os.path.join(
    BASE_DIR,
    "uptime.db"
)

# Allowed lookback windows for the latency/speedtest/uptime
# charts: 1 hour, 12 hours, 1 day, 30 days.
ALLOWED_HOURS = [1, 12, 24, 720]

# Dependency order of the check chain: the leftmost failing step in
# a grouped incident is the most likely root cause (a dead gateway
# also breaks internet, DNS and HTTPS).
CHECK_ORDER = ["Gateway", "Internet", "DNS", "HTTPS"]

# Only used for the automatic in-process monitor thread. Set
# AUTO_START_MONITOR=0 in the environment if you'd rather run
# uptime_checker.py as its own separate process/service.
AUTO_START_MONITOR = os.environ.get(
    "AUTO_START_MONITOR",
    "1"
) != "0"

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", DEFAULT_RETENTION_DAYS))

# Bind address/port. Behind a TLS reverse proxy set HOST=127.0.0.1
# so the app is not reachable directly over plain HTTP.
HOST = os.environ.get("HOST", "0.0.0.0")

try:
    PORT = int(os.environ.get("PORT", "5000"))
except ValueError:
    PORT = 5000


# ------------------------------------------------------------
# SECRET KEY
# ------------------------------------------------------------

SECRET_KEY_FILE = os.path.join(
    BASE_DIR,
    "secret_key.txt"
)


def load_or_create_secret_key():

    env_key = os.environ.get("SECRET_KEY")

    if env_key:

        return env_key

    if os.path.exists(SECRET_KEY_FILE):

        with open(SECRET_KEY_FILE, "r") as f:

            key = f.read().strip()

            if key:

                return key

    key = secrets.token_hex(32)

    with open(SECRET_KEY_FILE, "w") as f:

        f.write(key)

    # Restrict file permissions where the OS supports it.
    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except Exception:
        pass

    return key


app.secret_key = load_or_create_secret_key()

app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Set SECURE_COOKIES=1 when serving behind HTTPS.
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("SECURE_COOKIES", "0") == "1"
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)


# Content-Security-Policy: 'self' plus the two CDNs still used for
# Chart.js and Google Fonts (Phase 11 self-hosts them and this list
# shrinks again). 'unsafe-inline' is required by the inline <script>
# blocks and style attributes in the templates. frame-ancestors
# 'none' is the modern clickjacking defence; X-Frame-Options DENY
# below stays for older browsers. Deliberately NO
# upgrade-insecure-requests - the LAN install runs plain HTTP.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


# ============================================================
# BRANDED ERROR PAGES
# Replaces the stock Werkzeug 404/500 pages - they leaked the
# framework name and offered no way back into the app.
# ============================================================

@app.errorhandler(404)
def page_not_found(_error):
    return (
        render_template(
            "error.html",
            code=404,
            title="Page not found",
            message=(
                "That page doesn't exist or has moved. "
                "The dashboard is probably where you want to go."
            ),
        ),
        404,
    )


@app.errorhandler(500)
def internal_server_error(_error):
    # Never echo the exception back to the browser - details go to
    # monitor.log only.
    return (
        render_template(
            "error.html",
            code=500,
            title="Something went wrong",
            message=(
                "The server hit an unexpected error and logged the "
                "details. Try again in a moment."
            ),
        ),
        500,
    )


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db():

    conn = sqlite3.connect(
        DATABASE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass

    return conn


# ============================================================
# INITIALIZE DATABASE
# ============================================================

def initialize_database():

    validate_config()

    conn = get_db()

    # USERS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin'
        )
    """)

    # RBAC migration: databases created before roles get the column.
    try:
        conn.execute(
            "ALTER TABLE users "
            "ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # NETWORK MONITORING
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ping_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT 'Company A',
            target TEXT NOT NULL,
            check_type TEXT NOT NULL,
            status TEXT NOT NULL,
            latency REAL
        )
    """)

    # SPEEDTEST
    conn.execute("""
        CREATE TABLE IF NOT EXISTS speedtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT 'Company A',
            download REAL,
            upload REAL,
            ping REAL,
            server TEXT
        )
    """)

    # Migration: add "company" to databases created before
    # multi-company support existed.
    for table in ("ping_results", "speedtest_results"):

        columns = [
            row["name"]
            for row in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        ]

        if "company" not in columns:

            conn.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN company TEXT "
                "NOT NULL DEFAULT 'Company A'"
            )

    # Migration: "source" distinguishes results polled by this server
    # ('local', the default) from ones pushed in by a remote agent.
    try:
        conn.execute(
            "ALTER TABLE ping_results "
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'local'"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # Remote-agent tokens: one active token per company. Only the
    # SHA-256 hash is stored - the plaintext is shown once at creation.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT UNIQUE NOT NULL,
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT
        )
    """)

    # Indexes for company+time filtered charts.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ping_company_time
        ON ping_results (company, timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ping_type_time
        ON ping_results (check_type, timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_speed_company_time
        ON speedtest_results (company, timestamp)
    """)

    # Persistent login rate-limit state (survives restarts).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_failures (
            ip TEXT PRIMARY KEY,
            attempts TEXT NOT NULL DEFAULT '[]',
            locked_until TEXT
        )
    """)

    # Outages / notifications / hourly rollups - same schema as
    # uptime_checker.py so the app also works with AUTO_START_MONITOR=0.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            check_type TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration REAL,
            cause TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outages_company_start
        ON outages (company, started_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            check_type TEXT NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            channel TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notif_company_time
        ON notifications (company, sent_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rollup_hourly (
            company TEXT NOT NULL,
            check_type TEXT NOT NULL,
            hour_utc TEXT NOT NULL,
            total INTEGER NOT NULL,
            up_count INTEGER NOT NULL,
            avg_latency REAL,
            p95_latency REAL,
            max_latency REAL,
            PRIMARY KEY (company, check_type, hour_utc)
        )
    """)

    # CREATE DEFAULT ADMIN
    any_user = conn.execute(
        "SELECT id FROM users LIMIT 1"
    ).fetchone()

    if any_user is None:

        username = os.environ.get(
            "ADMIN_USERNAME",
            "admin"
        )

        password = os.environ.get("ADMIN_PASSWORD")

        generated = password is None

        if generated:

            password = secrets.token_urlsafe(9)

        password_hash = generate_password_hash(
            password
        )

        conn.execute(
            """
            INSERT INTO users
            (
                username,
                password
            )
            VALUES (?, ?)
            """,
            (
                username,
                password_hash
            )
        )

        if generated:

            print("=" * 60)

            print("Created initial admin account:")

            print(f"  Username: {username}")

            print(f"  Password: {password}")

            print(
                "Save this now - it will not be shown again. "
                "Set ADMIN_USERNAME/ADMIN_PASSWORD env vars to "
                "control this instead."
            )

            print("=" * 60)

    conn.commit()
    conn.close()


def prune_old_data():
    try:
        conn = get_db()
        for table in ("ping_results", "speedtest_results"):
            conn.execute(
                f"DELETE FROM {table} "
                f"WHERE timestamp < datetime('now', '-{int(RETENTION_DAYS)} days')"
            )
        # Rollups feed the heatmap/SLA reports - same retention window
        # as the raw rows they summarize.
        conn.execute(
            "DELETE FROM rollup_hourly "
            "WHERE substr(hour_utc, 1, 10) < date('now', ?)",
            (f"-{int(RETENTION_DAYS)} days",),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ============================================================
# LOGIN HELPER
# ============================================================

def is_logged_in():

    return "user_id" in session


def current_role():
    """Role of the logged-in user ('admin' or 'viewer')."""
    role = session.get("role")
    if role:
        return role

    if not is_logged_in():
        return None

    # Sessions created before RBAC existed have no role stored.
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT role FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
        conn.close()
    except Exception:
        row = None

    role = (row["role"] if row else None) or "viewer"
    session["role"] = role
    return role


def is_admin():

    return current_role() == "admin"


def require_admin(view=None):
    """Route guard: only the 'admin' role may use this endpoint."""
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            if not is_logged_in():
                if request.path.startswith("/api/"):
                    return {"error": "Unauthorized"}, 401
                return redirect(url_for("login"))
            if not is_admin():
                if request.path.startswith("/api/"):
                    return {"error": "Forbidden"}, 403
                abort(403)
            return f(*args, **kwargs)
        wrapped.__name__ = f.__name__
        return wrapped
    if view is not None:
        return decorator(view)
    return decorator


@app.context_processor
def inject_role():
    """Expose the current user's role to every template."""
    return {"role": current_role()}


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


def check_csrf(form_token):
    expected = session.get("csrf_token", "")
    return bool(expected) and secrets.compare_digest(
        str(form_token or ""), str(expected)
    )


# ============================================================
# COMPANY SELECTION
# ============================================================

def get_selected_company():

    company = request.args.get("company", "")

    if is_valid_company(company):

        return company

    return default_company()


# ============================================================
# LOGIN RATE LIMITING (persistent in SQLite)
# ============================================================

MAX_ATTEMPTS = 5

LOCKOUT_WINDOW = 300  # seconds

# In-memory fallback if DB is momentarily locked.
_failed_attempts_mem = {}


def _load_attempts(ip):
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT attempts FROM login_failures WHERE ip = ?", (ip,)
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row["attempts"] or "[]")
    except Exception:
        pass
    return None


def is_locked_out(ip):

    cutoff = time.time() - LOCKOUT_WINDOW

    attempts = _load_attempts(ip)
    if attempts is None:
        # DB fallback -> memory
        attempts = _failed_attempts_mem.get(ip, [])

    attempts = [t for t in attempts if t > cutoff]

    # Persist the pruned list so old failures age out.
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO login_failures (ip, attempts) VALUES (?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET attempts=excluded.attempts",
            (ip, json.dumps(attempts)),
        )
        conn.commit()
        conn.close()
    except Exception:
        _failed_attempts_mem[ip] = attempts

    return len(attempts) >= MAX_ATTEMPTS


def record_failed_attempt(ip):

    cutoff = time.time() - LOCKOUT_WINDOW
    attempts = _load_attempts(ip)
    if attempts is None:
        attempts = _failed_attempts_mem.get(ip, [])
    attempts = [t for t in attempts if t > cutoff]
    attempts.append(time.time())

    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO login_failures (ip, attempts) VALUES (?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET attempts=excluded.attempts",
            (ip, json.dumps(attempts)),
        )
        conn.commit()
        conn.close()
    except Exception:
        _failed_attempts_mem[ip] = attempts


def clear_failed_attempts(ip):

    _failed_attempts_mem.pop(ip, None)
    try:
        conn = get_db()
        conn.execute("DELETE FROM login_failures WHERE ip = ?", (ip,))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():

    if is_logged_in():

        return redirect(
            url_for("dashboard")
        )

    return redirect(
        url_for("login")
    )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if is_logged_in():

        return redirect(
            url_for("dashboard")
        )

    if request.method == "POST":

        client_ip = request.remote_addr or "unknown"

        if is_locked_out(client_ip):

            return render_template(
                "login.html",
                error=(
                    "Too many failed attempts. "
                    "Please wait a few minutes and try again."
                ),
                csrf_token=get_csrf_token(),
            )

        if not check_csrf(request.form.get("csrf_token")):
            return render_template(
                "login.html",
                error="Session expired. Please try again.",
                csrf_token=get_csrf_token(),
            ), 400

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        conn = get_db()

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()

        conn.close()

        if (
            user
            and check_password_hash(
                user["password"],
                password
            )
        ):

            clear_failed_attempts(client_ip)

            session.clear()

            session["user_id"] = user["id"]

            session["username"] = user["username"]

            session["role"] = (
                user["role"] if "role" in user.keys() else "admin"
            )

            session.permanent = True

            get_csrf_token()

            return redirect(
                url_for("dashboard")
            )

        record_failed_attempt(client_ip)

        return render_template(
            "login.html",
            error="Invalid username or password.",
            csrf_token=get_csrf_token(),
        )

    return render_template(
        "login.html",
        csrf_token=get_csrf_token(),
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
def dashboard():

    if not is_logged_in():

        return redirect(
            url_for("login")
        )

    return render_template(
        "dashboard.html",
        username=session.get(
            "username",
            "User"
        ),
        companies=list(COMPANIES.keys()),
        default_company=default_company()
    )


# ============================================================
# OVERVIEW (all companies, single page)
# ============================================================

@app.route("/overview")
def overview():

    if not is_logged_in():

        return redirect(
            url_for("login")
        )

    return render_template(
        "overview.html",
        username=session.get(
            "username",
            "User"
        ),
        companies=list(COMPANIES.keys()),
        default_company=default_company()
    )


@app.route("/api/companies/summary")
def companies_summary():
    """One payload for the overview grid: latest + uptime per company."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    hours = _parse_hours(24)

    conn = get_db()
    out = []
    for name in COMPANIES.keys():
        latest = conn.execute(
            """
            SELECT p.check_type, p.target, p.status, p.latency, p.timestamp
            FROM ping_results p
            INNER JOIN (
                SELECT check_type, MAX(id) AS max_id
                FROM ping_results WHERE company = ?
                GROUP BY check_type
            ) latest
            ON p.check_type = latest.check_type AND p.id = latest.max_id
            """,
            (name,),
        ).fetchall()

        uptime_rows = conn.execute(
            """
            SELECT check_type,
                COUNT(*) AS total,
                SUM(CASE WHEN status='UP' THEN 1 ELSE 0 END) AS up_count
            FROM ping_results
            WHERE timestamp >= datetime('now', ?) AND company = ?
            GROUP BY check_type
            """,
            (f"-{hours} hours", name),
        ).fetchall()

        uptime = {}
        for r in uptime_rows:
            total = r["total"] or 0
            up = r["up_count"] or 0
            uptime[r["check_type"]] = round(up / total * 100, 2) if total else None

        checks = {}
        last_check = None
        for r in latest:
            checks[r["check_type"]] = {
                "target": r["target"],
                "status": r["status"],
                "latency": r["latency"],
                "timestamp": r["timestamp"],
                "uptime": uptime.get(r["check_type"]),
            }
            if r["timestamp"] and (last_check is None or r["timestamp"] > last_check):
                last_check = r["timestamp"]

        # Overall mirrors dashboard logic: ok/degraded/down/idle.
        order = ["Gateway", "Internet", "DNS", "HTTPS"]
        present = [t for t in order if t in checks]
        failing = [t for t in present if checks[t]["status"] != "UP"]
        if not present:
            overall = "idle"
        elif not failing:
            overall = "ok"
        elif len(failing) == len(present):
            overall = "down"
        else:
            overall = "degraded"

        out.append({
            "name": name,
            "overall": overall,
            "failing": failing,
            "last_check": last_check,
            "checks": checks,
        })
    conn.close()
    return {"hours": hours, "companies": out}


# ============================================================
# OUTAGES / NOTIFICATIONS / ROLLUPS API
# ============================================================

@app.route("/api/outages")
def outages_api():
    """Incidents for one company (or all when company=ALL)."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    hours = _parse_hours(24)
    company = request.args.get("company", "")
    include_open = request.args.get("open", "0") == "1"

    conn = get_db()

    sql = (
        "SELECT id, company, check_type, started_at, ended_at, "
        "duration, cause FROM outages "
        "WHERE (ended_at IS NULL OR started_at >= datetime('now', ?))"
    )
    params = [f"-{hours} hours"]

    if company and company in COMPANIES:
        sql += " AND company = ?"
        params.append(company)

    if not include_open:
        sql += " AND ended_at IS NOT NULL"

    sql += " ORDER BY started_at DESC LIMIT 200"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = [
        {
            "id": r["id"],
            "company": r["company"],
            "check_type": r["check_type"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration": r["duration"],
            "open": r["ended_at"] is None,
            "cause": r["cause"],
        }
        for r in rows
    ]

    return {"hours": hours, "outages": items, "count": len(items)}


@app.route("/api/notifications")
def notifications_api():
    """Recent alert history (audit trail).

    Optional filters: company (or 'all'/absent = unchanged behaviour)
    and hours (lookback window for sent_at)."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    # limit: default 50, clamp to 200; malformed values are a client
    # error (400), never a 500.
    try:
        limit = int(request.args.get("limit", 50) or 50)
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}, 400
    limit = max(1, min(limit, 200))

    where = ""
    params = []

    company = (request.args.get("company") or "").strip()
    if company and company.lower() != "all":
        if company not in COMPANIES:
            return {"error": "Unknown company"}, 400
        where += " AND company = ?"
        params.append(company)

    hours_raw = request.args.get("hours")
    if hours_raw not in (None, ""):
        try:
            hours = int(hours_raw)
        except (TypeError, ValueError):
            hours = 0
        if hours < 1 or hours > 8760:
            return {"error": "hours must be between 1 and 8760"}, 400
        where += " AND sent_at >= datetime('now', ?)"
        params.append(f"-{hours} hours")

    conn = get_db()
    rows = conn.execute(
        "SELECT id, company, check_type, kind, message, channel, "
        f"sent_at, status FROM notifications WHERE 1=1{where} "
        "ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    return {
        "notifications": [dict(r) for r in rows],
        "count": len(rows),
    }


# ============================================================
# INCIDENT GROUPING
#
# An "incident" is one or more outage rows of the same company
# that overlap or start within INCIDENT_MERGE_GAP seconds of the
# group so far - e.g. gateway dies and takes internet + DNS + HTTPS
# with it. Grouping happens on read (no schema change); the root
# cause hint picks the leftmost failing step of the check chain.
# ============================================================

INCIDENT_MERGE_GAP = 120  # seconds between outages still one incident

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def utc_now_str():
    """UTC 'YYYY-MM-DD HH:MM:SS' matching SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def group_outages(rows, now=None, gap=INCIDENT_MERGE_GAP):
    """
    Group outage rows (dicts or sqlite Rows with company, check_type,
    started_at, ended_at, duration) into incidents.

    Returns a newest-first list of incident dicts:
      company, started_at, ended_at, open, duration (span, seconds),
      downtime (sum of member durations, seconds), checks (chain
      order), root_cause (hint), outage_count, members.
    """
    now = now or datetime.now(timezone.utc)

    def parse(ts):
        return datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)

    ordered = sorted(
        rows,
        key=lambda r: (r["company"], r["started_at"], r["id"]),
    )

    groups = []
    cur = None

    for r in ordered:
        start = parse(r["started_at"])
        end = parse(r["ended_at"]) if r["ended_at"] else None
        # Open outages extend to "now" for gap purposes: anything
        # failing while the incident is ongoing joins the same one.
        eff_end = end or now

        if (
            cur is not None
            and cur["company"] == r["company"]
            and (start - cur["eff_end"]).total_seconds() <= gap
        ):
            cur["members"].append(r)
            cur["eff_end"] = max(cur["eff_end"], eff_end)
            if end is None:
                cur["open"] = True
        else:
            if cur is not None:
                groups.append(cur)
            cur = {
                "company": r["company"],
                "members": [r],
                "eff_end": eff_end,
                "open": end is None,
            }

    if cur is not None:
        groups.append(cur)

    incidents = []
    for g in groups:
        members = g["members"]
        starts = [parse(m["started_at"]) for m in members]
        started = min(starts)

        member_ends = [
            parse(m["ended_at"]) for m in members if m["ended_at"]
        ]
        ended = max(member_ends) if (member_ends and not g["open"]) else None

        span_end = now if g["open"] else (ended or started)
        span = round((span_end - started).total_seconds(), 1)

        downtime = 0.0
        for m in members:
            if m["ended_at"]:
                m_end = parse(m["ended_at"])
            else:
                m_end = now
            downtime += (m_end - parse(m["started_at"])).total_seconds()

        checks = list(dict.fromkeys(m["check_type"] for m in members))
        root_cause = next(
            (c for c in CHECK_ORDER if c in checks), checks[0]
        )

        incidents.append({
            "id": members[0]["id"],
            "company": g["company"],
            "started_at": started.strftime(_TS_FMT),
            "ended_at": ended.strftime(_TS_FMT) if ended else None,
            "open": g["open"],
            "duration": span,
            "downtime": round(downtime, 1),
            "checks": sorted(
                checks,
                key=lambda c: (
                    CHECK_ORDER.index(c) if c in CHECK_ORDER else len(CHECK_ORDER),
                    c,
                ),
            ),
            "root_cause": root_cause,
            "outage_count": len(members),
            "members": [
                {
                    "id": m["id"],
                    "check_type": m["check_type"],
                    "started_at": m["started_at"],
                    "ended_at": m["ended_at"],
                    "duration": m["duration"],
                    "cause": m["cause"],
                }
                for m in members
            ],
        })

    incidents.sort(key=lambda i: (i["started_at"], i["id"]), reverse=True)
    return incidents


@app.route("/api/incidents")
def incidents_api():
    """Outages grouped into incidents (overlapping or near-in-time).

    Query: hours (lookback, same windows as other chart APIs),
    company (single name, 'all' or empty = every company),
    open=1 (only incidents still ongoing).
    """
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    hours = _parse_hours(24)
    company = (request.args.get("company") or "").strip()
    if company.lower() in ("", "all"):
        company = None
    elif company not in COMPANIES:
        return {"error": "Unknown company"}, 400
    open_only = request.args.get("open", "0") == "1"

    conn = get_db()
    sql = (
        "SELECT id, company, check_type, started_at, ended_at, "
        "duration, cause FROM outages "
        "WHERE (ended_at IS NULL OR started_at >= datetime('now', ?))"
    )
    params = [f"-{hours} hours"]
    if company:
        sql += " AND company = ?"
        params.append(company)
    sql += " ORDER BY started_at ASC, id ASC LIMIT 1000"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    incidents = group_outages(rows)
    if open_only:
        incidents = [i for i in incidents if i["open"]]

    return {
        "hours": hours,
        "company": company or "all",
        "gap_seconds": INCIDENT_MERGE_GAP,
        "incidents": incidents[:200],
        "count": len(incidents),
    }


# ============================================================
# PUBLIC STATUS PAGE (no login required)
#
# Deliberately unauthenticated: the login page links to it so
# visitors can check service status without an account. It only
# ever renders company names, check names, status, latency,
# uptime % and incident summaries - never target addresses,
# usernames or anything else internal.
# ============================================================

# state key -> (label, sla-pill CSS class)
_PUBLIC_STATES = {
    "operational": ("Operational", "sla-good"),
    "partial": ("Partial outage", "sla-warn"),
    "major": ("Major outage", "sla-bad"),
    "unknown": ("Unknown", "sla-none"),
}


def _public_companies():
    """Company names published on /status, in config order."""
    return [
        name for name, targets in COMPANIES.items()
        if targets.get("public", True)
    ]


def _age_seconds(ts, now=None):
    """UTC 'YYYY-MM-DD HH:MM:SS' -> seconds elapsed (None if junk)."""
    if not ts:
        return None
    try:
        then = datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - then).total_seconds()))


def _fmt_age(seconds):
    """Human label for an age: '12s', '7m', '3h', '2d', 'never'."""
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 129600:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _fmt_duration(seconds):
    """Human label for a duration in seconds."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 129600:
        return f"{seconds // 3600}h"
    return f"{round(seconds / 86400, 1)}d"


_CHECK_VIEW_CLASSES = {
    "up": "sla-good",
    "down": "sla-bad",
    "unknown": "sla-none",
}


def _check_view(check):
    """(view_state, label, pill css class) for one check dict."""
    if check.get("fresh") and check.get("status"):
        if check["status"] == "UP":
            return "up", "Up", _CHECK_VIEW_CLASSES["up"]
        return "down", "Down", _CHECK_VIEW_CLASSES["down"]
    return "unknown", "Unknown", _CHECK_VIEW_CLASSES["unknown"]


def _company_public_state(checks):
    """Aggregate a company's checks into one status pill state.

    Any fresh DOWN wins (partial, or major when all four are down).
    No DOWN but some stale/missing -> Unknown (the monitor itself
    may be dead; never claim "Operational" without fresh data).
    """
    known = [c for c in checks if c.get("fresh") and c.get("status")]
    down = [c for c in known if c["status"] != "UP"]
    if not known:
        return "unknown"
    if down:
        return "major" if len(down) == len(CHECK_ORDER) else "partial"
    if len(known) < len(CHECK_ORDER):
        return "unknown"
    return "operational"


def _latest_public_checks(companies):
    """Latest row per (company, check_type), every CHECK_ORDER slot.

    Returns {company: [check dict, ...]} where a check dict is
    name/status/latency/timestamp/age_seconds/fresh. Checks with no
    data at all come back as fresh=False, status=None (=> Unknown).
    """
    result = {}
    if not companies:
        return result

    conn = get_db()
    placeholders = ",".join("?" * len(companies))
    rows = conn.execute(
        f"""
        SELECT p.company, p.check_type, p.status, p.latency, p.timestamp
        FROM ping_results p
        JOIN (SELECT company, check_type, MAX(id) AS mid
              FROM ping_results
              WHERE company IN ({placeholders})
              GROUP BY company, check_type) m
          ON p.id = m.mid
        """,
        list(companies),
    ).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    latest = {}
    for r in rows:
        age = _age_seconds(r["timestamp"], now)
        latest[(r["company"], r["check_type"])] = {
            "name": r["check_type"],
            "status": r["status"],
            "latency": r["latency"],
            "timestamp": r["timestamp"],
            "age_seconds": age,
            "fresh": age is not None and age <= STATUS_STALE_SECONDS,
        }

    empty = {
        "name": None, "status": None, "latency": None,
        "timestamp": None, "age_seconds": None, "fresh": False,
    }
    for company in companies:
        checks = []
        for name in CHECK_ORDER:
            check = dict(latest.get((company, name)) or empty)
            check["name"] = name
            checks.append(check)
        result[company] = checks
    return result


def _public_incidents(company=None, hours=720):
    """Outages grouped into incidents (same query as /api/incidents,
    minus the auth check) - default: last 30 days, all companies."""
    conn = get_db()
    sql = (
        "SELECT id, company, check_type, started_at, ended_at, "
        "duration, cause FROM outages "
        "WHERE (ended_at IS NULL OR started_at >= datetime('now', ?))"
    )
    params = [f"-{hours} hours"]
    if company:
        sql += " AND company = ?"
        params.append(company)
    sql += " ORDER BY started_at ASC, id ASC LIMIT 1000"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return group_outages(rows)


def _public_uptime(company):
    """{check_type: {1: pct|None, 7: ..., 30: ...}} via _sla_rows."""
    out = {}
    for days in (1, 7, 30):
        for row in _sla_rows(days, company):
            slot = out.setdefault(
                row["check_type"], {1: None, 7: None, 30: None}
            )
            slot[days] = row["uptime_pct"]
    return out


@app.route("/status")
def public_status():
    """Public status page - intentionally NO login check.

    /status                -> every published company
    /status?company=X      -> one company's detail page
    Unknown/private X      -> friendly 404
    PUBLIC_STATUS=False    -> redirect to login
    """
    if not PUBLIC_STATUS:
        return redirect(url_for("login"))

    companies = _public_companies()
    company = (request.args.get("company") or "").strip()
    now_label = utc_now_str()

    def render(context, status=200):
        resp = make_response(
            render_template("public_status.html", **context), status
        )
        # Status data must never be served stale by a proxy.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    if company and company not in companies:
        return render({
            "error": f"Unknown company: {company}",
            "companies": companies,
            "now_label": now_label,
        }, status=404)

    checks_map = _latest_public_checks(companies)
    incidents_all = _public_incidents()

    # ---- overview ----
    if not company:
        open_counts = {}
        for inc in incidents_all:
            if inc["open"]:
                open_counts[inc["company"]] = (
                    open_counts.get(inc["company"], 0) + 1
                )

        cards = []
        any_stale = False
        oldest_stale = None
        for name in companies:
            checks = checks_map.get(name, [])
            state = _company_public_state(checks)
            state_label, state_class = _PUBLIC_STATES[state]

            for c in checks:
                view, label, view_class = _check_view(c)
                c["view"], c["view_label"] = view, label
                c["view_class"] = view_class
                if view == "unknown":
                    any_stale = True
                    age = c["age_seconds"]
                    if age is not None and (
                        oldest_stale is None or age > oldest_stale
                    ):
                        oldest_stale = age

            ages = [c["age_seconds"] for c in checks
                    if c["age_seconds"] is not None]
            last_age = min(ages) if ages else None
            cards.append({
                "name": name,
                "state": state,
                "state_label": state_label,
                "state_class": state_class,
                "checks": checks,
                "down_count": sum(
                    1 for c in checks
                    if c["view"] == "down"
                ),
                "last_age": last_age,
                "last_label": _fmt_age(last_age),
                "open_incidents": open_counts.get(name, 0),
            })

        stale_banner = None
        if any_stale:
            stale_banner = (
                f"Monitoring has not reported for "
                f"{_fmt_age(oldest_stale)} - statuses may be out of date."
                if oldest_stale is not None else
                "Monitoring has not reported any checks yet."
            )

        return render({
            "companies": companies,
            "cards": cards,
            "stale_banner": stale_banner,
            "now_label": now_label,
        })

    # ---- detail ----
    checks = checks_map.get(company, [])
    state = _company_public_state(checks)
    state_label, state_class = _PUBLIC_STATES[state]
    for c in checks:
        view, label, view_class = _check_view(c)
        c["view"], c["view_label"] = view, label
        c["view_class"] = view_class
        c["age_label"] = _fmt_age(c["age_seconds"])

    ages = [c["age_seconds"] for c in checks if c["age_seconds"] is not None]
    last_age = min(ages) if ages else None

    any_stale = any(c["view"] == "unknown" for c in checks)
    stale_banner = (
        f"Monitoring has not reported for {_fmt_age(last_age)} - "
        f"statuses may be out of date."
        if any_stale and last_age is not None else
        "Monitoring has not reported any checks yet."
        if any_stale else None
    )

    uptime = _public_uptime(company)
    for c in checks:
        c["uptime"] = uptime.get(c["name"], {1: None, 7: None, 30: None})

    incidents = _public_incidents(company=company)[:10]
    for inc in incidents:
        inc["duration_label"] = _fmt_duration(inc["duration"])
    ongoing = next((i for i in incidents if i["open"]), None)

    return render({
        "companies": companies,
        "company": company,
        "checks": checks,
        "state": state,
        "state_label": state_label,
        "state_class": state_class,
        "last_label": _fmt_age(last_age),
        "stale_banner": stale_banner,
        "uptime": uptime,
        "incidents": incidents,
        "ongoing": ongoing,
        "now_label": now_label,
    })


@app.route("/api/rollups")
def rollups_api():
    """Pre-aggregated hourly availability (fast 30d queries)."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    company = request.args.get("company", "")
    if not company or company not in COMPANIES:
        company = default_company()

    try:
        hours = int(request.args.get("hours", 720))
    except ValueError:
        hours = 720

    conn = get_db()
    rows = conn.execute(
        """
        SELECT check_type, hour_utc, total, up_count,
               avg_latency, p95_latency, max_latency
        FROM rollup_hourly
        WHERE company = ?
          AND hour_utc >= datetime('now', ?)
        ORDER BY hour_utc ASC
        """,
        (company, f"-{hours} hours"),
    ).fetchall()
    conn.close()

    return {
        "company": company,
        "hours": hours,
        "rollups": [dict(r) for r in rows],
        "count": len(rows),
    }


@app.route("/api/alerts/test", methods=["POST"])
@require_admin
def alerts_test():
    """Fire a test alert through the configured channels."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    try:
        import uptime_checker as uc
    except Exception as error:
        return {"error": f"checker unavailable: {error}"}, 500

    company = default_company()
    delivered = uc.dispatch_alert(
        company,
        "Test",
        "test",
        "This is a test alert from Uptime Monitor.",
        subject="[UptimeMonitor] Test alert",
    )
    return {
        "ok": True,
        "delivered": bool(delivered),
        "channels": {
            "email": bool(uc.ALERTS.get("email", {}).get("enabled")),
            "webhook": bool(uc.ALERTS.get("webhook", {}).get("enabled")),
        },
    }


# ============================================================
# REMOTE AGENT INGEST (POST /api/ingest)
#
# A remote site runs agent.py, which pushes its check results here.
# Auth: "Authorization: Bearer <token>" - one token per company,
# stored only as a SHA-256 hash (manage on the /agents page).
# The company comes from the TOKEN, never from the payload, so one
# site cannot write into another company's data.
# ============================================================

# One push carries one check cycle (Gateway/Internet/DNS/HTTPS).
MAX_INGEST_RESULTS = 20


def _agent_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.route("/api/ingest", methods=["POST"])
def ingest_api():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return {"error": "Missing Bearer token"}, 401
    token = auth[7:].strip()
    if not token:
        return {"error": "Empty Bearer token"}, 401

    conn = get_db()
    row = conn.execute(
        "SELECT id, company FROM agent_tokens WHERE token_hash = ?",
        (_agent_token_hash(token),),
    ).fetchone()
    if row is None:
        conn.close()
        return {"error": "Invalid token"}, 401

    company = row["company"]

    # The company must still be configured AND agent-managed;
    # otherwise fall back to the local poller instead of double-counting.
    targets = COMPANIES.get(company)
    if targets is None:
        conn.close()
        return {"error": f"Company {company!r} is no longer configured"}, 409
    if targets.get("managed_by", "local") != "agent":
        conn.close()
        return {
            "error": f"Company {company!r} is not agent-managed "
                     "(set managed_by: 'agent' in config.py)"
        }, 409

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(
        payload.get("results"), list
    ):
        conn.close()
        return {"error": "Body must be an object with a 'results' list"}, 400
    results = payload["results"]
    if not results:
        conn.close()
        return {"error": "'results' must not be empty"}, 400
    if len(results) > MAX_INGEST_RESULTS:
        conn.close()
        return {"error": f"Too many results (max {MAX_INGEST_RESULTS})"}, 400

    # ---- validate every result BEFORE writing anything ----
    now = utc_now_str()
    clean = []
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            conn.close()
            return {"error": f"results[{i}] must be an object"}, 400

        check_type = item.get("check_type")
        if check_type not in CHECK_ORDER:
            conn.close()
            return {
                "error": f"results[{i}].check_type must be one of "
                         f"{CHECK_ORDER}"
            }, 400

        status = item.get("status")
        if status not in ("UP", "DOWN", "ERROR"):
            conn.close()
            return {
                "error": f"results[{i}].status must be UP, DOWN or ERROR"
            }, 400

        latency = item.get("latency")
        if latency is not None and not isinstance(latency, (int, float)):
            conn.close()
            return {"error": f"results[{i}].latency must be a number"}, 400

        target = item.get("target")
        if not isinstance(target, str) or not target.strip():
            conn.close()
            return {"error": f"results[{i}].target must be a string"}, 400

        timestamp = item.get("timestamp")
        if timestamp is not None:
            if not isinstance(timestamp, str) or not _valid_ts(timestamp):
                conn.close()
                return {
                    "error": f"results[{i}].timestamp must be "
                             "'YYYY-MM-DD HH:MM:SS' (UTC)"
                }, 400
        else:
            timestamp = now

        clean.append(
            (timestamp, company, check_type, target.strip(), status, latency)
        )

    # ---- write results + feed the state machine ----
    try:
        for ts, comp, check_type, target, status, latency in clean:
            conn.execute(
                "INSERT INTO ping_results "
                "(timestamp, company, target, check_type, status, latency, "
                " source) VALUES (?, ?, ?, ?, ?, ?, 'agent')",
                (ts, comp, target, check_type, status, latency),
            )
        conn.execute(
            "UPDATE agent_tokens SET last_seen = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()
    except sqlite3.Error as error:
        conn.close()
        return {"error": f"database error: {error}"}, 500
    conn.close()

    # Same pipeline as the local poller: outage state machine +
    # alert rules, so agent results open/close outages and alert.
    try:
        import uptime_checker as uc
        for ts, comp, check_type, target, status, latency in clean:
            info = uc.process_check_result(comp, check_type, status, ts)
            uc.evaluate_alerts(comp, check_type, status, latency, info)
    except Exception as error:
        app.logger.warning(f"Ingest alert pipeline error: {error}")

    return {
        "ok": True,
        "company": company,
        "accepted": len(clean),
        "server_time": now,
    }


def _valid_ts(value):
    """'YYYY-MM-DD HH:MM:SS' that sqlite datetime() will accept."""
    try:
        datetime.strptime(value, _TS_FMT)
        return True
    except (ValueError, TypeError):
        return False


# ============================================================
# HEALTH (for monitoring the monitor)
# ============================================================

@app.route("/api/health")
def health():
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT MAX(timestamp) AS last_ts, COUNT(*) AS n "
            "FROM ping_results "
            "WHERE timestamp >= datetime('now', '-1 day')"
        ).fetchone()
        conn.close()
        return {
            "ok": True,
            "db": "up",
            "last_check": row["last_ts"] if row else None,
            "checks_24h": row["n"] if row else 0,
            "companies": list(COMPANIES.keys()),
            "retention_days": RETENTION_DAYS,
        }
    except Exception as error:
        return {"ok": False, "error": str(error)}, 500


def _parse_hours(default=24):
    try:
        hours = int(request.args.get("hours", default))
    except ValueError:
        hours = default
    if hours not in ALLOWED_HOURS:
        hours = default
    return hours


# ============================================================
# NETWORK LATENCY API
# ============================================================

@app.route("/api/latency")
def latency_data():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    hours = _parse_hours(24)

    company = get_selected_company()

    conn = get_db()

    results = conn.execute(
        """
        SELECT
            timestamp,
            check_type,
            target,
            status,
            latency
        FROM ping_results
        WHERE timestamp >= datetime(
            'now',
            ?
        )
        AND company = ?
        ORDER BY timestamp ASC
        """,
        (
            f"-{hours} hours",
            company
        )
    ).fetchall()

    conn.close()

    timestamp_map = {}

    for result in results:

        timestamp = result["timestamp"]

        if timestamp not in timestamp_map:

            timestamp_map[timestamp] = {
                "Gateway": None,
                "Internet": None,
                "DNS": None,
                "HTTPS": None
            }

        check_type = result["check_type"]

        if check_type in timestamp_map[timestamp]:

            timestamp_map[timestamp][check_type] = (
                result["latency"]
            )

    labels = list(
        timestamp_map.keys()
    )

    gateway = []
    internet = []
    dns = []
    https = []

    for timestamp in labels:

        gateway.append(
            timestamp_map[timestamp]["Gateway"]
        )

        internet.append(
            timestamp_map[timestamp]["Internet"]
        )

        dns.append(
            timestamp_map[timestamp]["DNS"]
        )

        https.append(
            timestamp_map[timestamp]["HTTPS"]
        )

    return {
        "labels": labels,
        "gateway": gateway,
        "internet": internet,
        "dns": dns,
        "https": https,
        "company": company
    }


# ============================================================
# CURRENT NETWORK STATUS
# ============================================================

@app.route("/api/status")
def current_status():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    company = get_selected_company()

    conn = get_db()

    results = conn.execute(
        """
        SELECT
            p.check_type,
            p.target,
            p.status,
            p.latency,
            p.timestamp

        FROM ping_results p

        INNER JOIN (

            SELECT
                check_type,
                MAX(id) AS max_id

            FROM ping_results

            WHERE company = ?

            GROUP BY check_type

        ) latest

        ON p.check_type = latest.check_type

        AND p.id = latest.max_id

        ORDER BY p.id
        """,
        (
            company,
        )
    ).fetchall()

    conn.close()

    data = []

    for result in results:

        data.append({
            "check_type": result["check_type"],
            "target": result["target"],
            "status": result["status"],
            "latency": result["latency"],
            "timestamp": result["timestamp"]
        })

    response = {
        "results": data,
        "company": company
    }

    # Agent-managed companies: is the remote agent still pushing?
    # The UI shows a staleness badge when data stops arriving.
    if COMPANIES.get(company, {}).get("managed_by") == "agent":
        conn = get_db()
        row = conn.execute(
            "SELECT MAX(timestamp) AS last_ts FROM ping_results "
            "WHERE company = ? AND source = 'agent'",
            (company,),
        ).fetchone()
        conn.close()
        last_ts = row["last_ts"] if row else None
        stale = True
        age_seconds = None
        if last_ts:
            try:
                seen = datetime.strptime(last_ts, _TS_FMT).replace(
                    tzinfo=timezone.utc
                )
                age_seconds = max(
                    0, int((datetime.now(timezone.utc) - seen).total_seconds())
                )
                stale = age_seconds > AGENT_STALE_SECONDS
            except ValueError:
                pass
        response["agent"] = {
            "managed_by": "agent",
            "last_seen": last_ts,
            "age_seconds": age_seconds,
            "stale": stale,
            "stale_after_seconds": AGENT_STALE_SECONDS,
        }

    return response


# ============================================================
# UPTIME PERCENTAGE
# ============================================================

@app.route("/api/uptime")
def uptime_data():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    hours = _parse_hours(24)

    company = get_selected_company()

    conn = get_db()

    results = conn.execute(
        """
        SELECT
            check_type,
            COUNT(*) AS total,
            SUM(
                CASE
                    WHEN status = 'UP' THEN 1
                    ELSE 0
                END
            ) AS up_count

        FROM ping_results

        WHERE timestamp >= datetime(
            'now',
            ?
        )

        AND company = ?

        GROUP BY check_type
        """,
        (
            f"-{hours} hours",
            company
        )
    ).fetchall()

    conn.close()

    uptime = {}

    for result in results:

        total = result["total"]

        up_count = result["up_count"] or 0

        if total > 0:

            percent = round(
                (up_count / total) * 100,
                2
            )

        else:

            percent = None

        uptime[result["check_type"]] = percent

    return {
        "uptime": uptime,
        "hours": hours,
        "company": company
    }


# ============================================================
# SPEEDTEST API
# ============================================================

@app.route("/api/speedtest")
def speedtest_data():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    hours = _parse_hours(24)

    # Speedtest only ever measures the connection of the machine
    # actually running speedtest.exe, so it's always tagged with
    # SPEEDTEST_COMPANY (see config.py) rather than whatever
    # company happens to be selected on the dashboard.
    conn = get_db()

    results = conn.execute(
        """
        SELECT
            timestamp,
            download,
            upload,
            ping,
            server

        FROM speedtest_results

        WHERE timestamp >= datetime(
            'now',
            ?
        )

        AND company = ?

        ORDER BY timestamp ASC
        """,
        (
            f"-{hours} hours",
            SPEEDTEST_COMPANY
        )
    ).fetchall()

    conn.close()

    labels = []
    download = []
    upload = []
    ping = []

    for result in results:

        labels.append(
            result["timestamp"]
        )

        download.append(
            result["download"]
        )

        upload.append(
            result["upload"]
        )

        ping.append(
            result["ping"]
        )

    return {
        "labels": labels,
        "download": download,
        "upload": upload,
        "ping": ping,
        "company": SPEEDTEST_COMPANY
    }


# ============================================================
# RECENT SPEEDTEST
# ============================================================

@app.route("/api/speedtest/recent")
def recent_speedtest():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    conn = get_db()

    results = conn.execute(
        """
        SELECT
            timestamp,
            download,
            upload,
            ping,
            server

        FROM speedtest_results

        WHERE company = ?

        ORDER BY id DESC

        LIMIT 10
        """,
        (
            SPEEDTEST_COMPANY,
        )
    ).fetchall()

    conn.close()

    data = []

    for result in results:

        data.append({
            "timestamp": result["timestamp"],
            "download": result["download"],
            "upload": result["upload"],
            "ping": result["ping"],
            "server": result["server"]
        })

    return {
        "results": data,
        "company": SPEEDTEST_COMPANY
    }


# ============================================================
# REPORTS (SLA table + availability heatmap)
# ============================================================

def _clamp_days(default=30, minimum=1, maximum=365):
    try:
        days = int(request.args.get("days", default))
    except (TypeError, ValueError):
        days = default
    return max(minimum, min(maximum, days))


def _report_company():
    """Company filter: '' or 'all' -> None (aggregate mode)."""
    company = (request.args.get("company") or "").strip()
    if not company or company.lower() == "all":
        return None, None
    if company not in COMPANIES:
        return None, ({"error": "Unknown company"}, 400)
    return company, None


def _sla_rows(days, company):
    """
    One row per (company, check) with uptime % from rollup_hourly and
    outage stats from the outages table, both bounded by `days`.
    """
    cutoff = f"-{days} days"
    conn = get_db()

    params = [cutoff]
    where = "WHERE hour_utc >= datetime('now', ?)"
    if company:
        where += " AND company = ?"
        params.append(company)

    rollups = conn.execute(
        f"""
        SELECT company, check_type,
               SUM(total) AS checks,
               SUM(up_count) AS up_checks,
               CASE WHEN SUM(CASE WHEN avg_latency IS NULL
                                  THEN 0 ELSE total END) > 0
                    THEN SUM(COALESCE(avg_latency, 0) * total) * 1.0
                         / SUM(CASE WHEN avg_latency IS NULL
                                    THEN 0 ELSE total END)
                    ELSE NULL END AS avg_latency,
               MAX(p95_latency) AS worst_p95
        FROM rollup_hourly
        {where}
        GROUP BY company, check_type
        """,
        params,
    ).fetchall()

    oparams = [cutoff]
    owhere = "WHERE started_at >= datetime('now', ?)"
    if company:
        owhere += " AND company = ?"
        oparams.append(company)

    outages = conn.execute(
        f"""
        SELECT company, check_type,
               COUNT(*) AS outage_count,
               SUM(COALESCE(duration,
                    (julianday('now') - julianday(started_at)) * 86400)
               ) AS downtime_sec,
               MAX(COALESCE(duration,
                    (julianday('now') - julianday(started_at)) * 86400)
               ) AS longest_sec
        FROM outages
        {owhere}
        GROUP BY company, check_type
        """,
        oparams,
    ).fetchall()
    conn.close()

    merged = {}

    def _empty(key):
        return {
            "company": key[0],
            "check_type": key[1],
            "checks": 0,
            "up_checks": 0,
            "uptime_pct": None,
            "avg_latency": None,
            "p95_latency": None,
            "outages": 0,
            "downtime_minutes": 0.0,
            "longest_minutes": 0.0,
        }

    for r in rollups:
        row = _empty((r["company"], r["check_type"]))
        row["checks"] = r["checks"] or 0
        row["up_checks"] = r["up_checks"] or 0
        if row["checks"]:
            row["uptime_pct"] = round(
                row["up_checks"] * 100.0 / row["checks"], 3
            )
        row["avg_latency"] = (
            round(r["avg_latency"], 2) if r["avg_latency"] is not None else None
        )
        row["p95_latency"] = (
            round(r["worst_p95"], 2) if r["worst_p95"] is not None else None
        )
        merged[(row["company"], row["check_type"])] = row

    for r in outages:
        key = (r["company"], r["check_type"])
        row = merged.get(key) or _empty(key)
        row["outages"] = r["outage_count"] or 0
        row["downtime_minutes"] = round((r["downtime_sec"] or 0) / 60, 1)
        row["longest_minutes"] = round((r["longest_sec"] or 0) / 60, 1)
        merged[key] = row

    return sorted(
        merged.values(),
        key=lambda x: (
            x["company"],
            CHECK_ORDER.index(x["check_type"])
            if x["check_type"] in CHECK_ORDER
            else len(CHECK_ORDER),
            x["check_type"],
        ),
    )


@app.route("/reports")
def reports_page():
    if not is_logged_in():
        return redirect(url_for("login"))
    return render_template(
        "reports.html",
        username=session.get("username", "User"),
        companies=list(COMPANIES.keys()),
        default_company=default_company(),
        csrf_token=get_csrf_token(),
    )


@app.route("/incidents")
def incidents_page():
    if not is_logged_in():
        return redirect(url_for("login"))
    return render_template(
        "incidents.html",
        username=session.get("username", "User"),
        companies=list(COMPANIES.keys()),
        default_company=default_company(),
    )


@app.route("/api/sla")
def sla_api():
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    days = _clamp_days()
    company, err = _report_company()
    if err:
        return err
    return {
        "days": days,
        "company": company or "all",
        "rows": _sla_rows(days, company),
    }


@app.route("/api/heatmap")
def heatmap_api():
    """Daily availability per check (specific company) or per company."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    days = _clamp_days(default=30, minimum=7, maximum=120)
    company, err = _report_company()
    if err:
        return err

    # Per-check rows for one company, per-company rows for aggregate mode.
    name_col = "check_type" if company else "company"

    conn = get_db()
    params = [f"-{days} days"]
    where = "WHERE hour_utc >= datetime('now', ?)"
    if company:
        where += " AND company = ?"
        params.append(company)

    rows = conn.execute(
        f"""
        SELECT substr(hour_utc, 1, 10) AS day,
               {name_col} AS name,
               SUM(total) AS checks,
               SUM(up_count) AS up_checks
        FROM rollup_hourly
        {where}
        GROUP BY day, name
        """,
        params,
    ).fetchall()
    conn.close()

    today = datetime.now(timezone.utc).date()
    days_list = [
        (today - timedelta(days=i)).isoformat()
        for i in range(days - 1, -1, -1)
    ]

    grid = {}
    names = set()
    for r in rows:
        grid.setdefault(r["name"], {})[r["day"]] = (
            r["checks"],
            r["up_checks"],
        )
        names.add(r["name"])

    if company:
        preferred = CHECK_ORDER
    else:
        preferred = list(COMPANIES.keys())
    ordered = [n for n in preferred if n in names]
    ordered += sorted(names - set(preferred))

    series = []
    for name in ordered:
        cells = []
        for day in days_list:
            vals = grid.get(name, {}).get(day)
            if vals and vals[0]:
                cells.append({
                    "day": day,
                    "checks": vals[0],
                    "uptime_pct": round(vals[1] * 100.0 / vals[0], 3),
                })
            else:
                cells.append({"day": day, "checks": 0, "uptime_pct": None})
        series.append({"name": name, "cells": cells})

    return {
        "days": days,
        "company": company or "all",
        "days_list": days_list,
        "series": series,
    }


@app.route("/api/sla/export")
def sla_export():
    """CSV download of the SLA table."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    import csv
    import io

    days = _clamp_days()
    company, err = _report_company()
    if err:
        return err
    rows = _sla_rows(days, company)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "company", "check", "uptime_pct", "checks", "outages",
        "downtime_minutes", "longest_outage_minutes",
        "avg_latency_ms", "worst_hourly_p95_ms",
    ])
    for r in rows:
        writer.writerow([
            r["company"], r["check_type"], r["uptime_pct"], r["checks"],
            r["outages"], r["downtime_minutes"], r["longest_minutes"],
            r["avg_latency"], r["p95_latency"],
        ])

    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="sla_{company or "all"}_{days}d.csv"'
    )
    return resp


# ============================================================
# BACKUPS
# ============================================================

@app.route("/api/backups")
def backups_list_api():
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    import backup as backup_mod
    return {
        "backups": backup_mod.list_backups(),
        "keep": backup_mod.backup_keep(),
        "interval_seconds": backup_mod.backup_interval(),
        "dir": backup_mod.backup_dir(),
    }


@app.route("/api/backup", methods=["POST"])
@require_admin
def backup_create_api():
    """Snapshot the live database now (admin only, CSRF-protected)."""
    import backup as backup_mod

    token = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
    )
    if not check_csrf(token):
        return {"error": "CSRF token missing or expired"}, 400

    try:
        path = backup_mod.create_backup()
    except Exception as error:
        return {"error": str(error)}, 500

    return {
        "ok": True,
        "file": os.path.basename(path),
        "size": os.path.getsize(path),
    }


# ============================================================
# USER MANAGEMENT (admin only)
# ============================================================

def _users_redirect(msg=None, err=None):
    """Back to the user list carrying a status banner."""
    return redirect(url_for("users_page", msg=msg, err=err))


def _valid_password(password):
    return isinstance(password, str) and len(password) >= 8


@app.route("/users")
@require_admin
def users_page():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, role FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return render_template(
        "users.html",
        username=session.get("username", "User"),
        users=[dict(row) for row in rows],
        csrf_token=get_csrf_token(),
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.route("/users/create", methods=["POST"])
@require_admin
def user_create():
    if not check_csrf(request.form.get("csrf_token")):
        return _users_redirect(err="Session expired. Please try again.")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role") or "viewer"

    if not (3 <= len(username) <= 48):
        return _users_redirect(err="Username must be 3-48 characters.")
    if not _valid_password(password):
        return _users_redirect(err="Password must be at least 8 characters.")
    if role not in ("admin", "viewer"):
        return _users_redirect(err="Unknown role.")

    conn = get_db()
    clash = conn.execute(
        "SELECT id FROM users WHERE lower(username) = lower(?)",
        (username,),
    ).fetchone()
    if clash:
        conn.close()
        return _users_redirect(err="That username already exists.")

    conn.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), role),
    )
    conn.commit()
    conn.close()
    return _users_redirect(msg=f"User '{username}' created.")


@app.route("/users/<int:user_id>/password", methods=["POST"])
@require_admin
def user_set_password(user_id):
    if not check_csrf(request.form.get("csrf_token")):
        return _users_redirect(err="Session expired. Please try again.")

    password = request.form.get("password") or ""
    if not _valid_password(password):
        return _users_redirect(err="Password must be at least 8 characters.")

    conn = get_db()
    row = conn.execute(
        "SELECT id, username FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        conn.close()
        return _users_redirect(err="User not found.")

    conn.execute(
        "UPDATE users SET password = ? WHERE id = ?",
        (generate_password_hash(password), user_id),
    )
    conn.commit()
    conn.close()
    return _users_redirect(msg=f"Password updated for '{row['username']}'.")


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@require_admin
def user_delete(user_id):
    if not check_csrf(request.form.get("csrf_token")):
        return _users_redirect(err="Session expired. Please try again.")

    if user_id == session.get("user_id"):
        return _users_redirect(err="You cannot delete your own account.")

    conn = get_db()
    row = conn.execute(
        "SELECT id, username, role FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        conn.close()
        return _users_redirect(err="User not found.")

    if row["role"] == "admin":
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"
        ).fetchone()["n"]
        if admins <= 1:
            conn.close()
            return _users_redirect(err="Cannot delete the last admin.")

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return _users_redirect(msg=f"User '{row['username']}' deleted.")


# ============================================================
# REMOTE AGENTS (admin only): per-company ingest tokens
# ============================================================

def _agents_redirect(msg=None, err=None):
    return redirect(url_for("agents_page", msg=msg, err=err))


@app.route("/agents")
@require_admin
def agents_page():
    conn = get_db()
    tokens = {
        row["company"]: dict(row)
        for row in conn.execute(
            "SELECT company, created_at, last_seen FROM agent_tokens"
        ).fetchall()
    }
    conn.close()

    companies = []
    for name, targets in COMPANIES.items():
        managed_by = targets.get("managed_by", "local")
        tok = tokens.get(name)
        companies.append({
            "name": name,
            "managed_by": managed_by,
            "has_token": tok is not None,
            "created_at": tok["created_at"] if tok else None,
            "last_seen": tok["last_seen"] if tok else None,
        })

    # Plaintext token shown exactly once, right after creation.
    # It travels via the session (not a URL) so it never lands in
    # browser history or server access logs.
    new_token = None
    new_token_company = None
    pending = session.pop("pending_agent_token", None)
    if pending:
        new_token, new_token_company = pending

    return render_template(
        "agents.html",
        username=session.get("username", "User"),
        companies=companies,
        csrf_token=get_csrf_token(),
        new_token=new_token,
        new_token_company=new_token_company,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.route("/agents/<company>/token", methods=["POST"])
@require_admin
def agents_create_token(company):
    if not check_csrf(request.form.get("csrf_token")):
        return _agents_redirect(err="Session expired. Please try again.")
    if not is_valid_company(company):
        return _agents_redirect(err="Unknown company.")

    token = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "DELETE FROM agent_tokens WHERE company = ?",
        (company,),
    )
    conn.execute(
        "INSERT INTO agent_tokens (company, token_hash, created_at) "
        "VALUES (?, ?, ?)",
        (company, _agent_token_hash(token), utc_now_str()),
    )
    conn.commit()
    conn.close()

    # One-shot display via the session (popped on the next render).
    session["pending_agent_token"] = (token, company)
    return _agents_redirect(
        msg=f"Token created for {company}. Copy it now - it is shown "
            "only once.",
    )


@app.route("/agents/<company>/revoke", methods=["POST"])
@require_admin
def agents_revoke_token(company):
    if not check_csrf(request.form.get("csrf_token")):
        return _agents_redirect(err="Session expired. Please try again.")
    if not is_valid_company(company):
        return _agents_redirect(err="Unknown company.")

    conn = get_db()
    cur = conn.execute(
        "DELETE FROM agent_tokens WHERE company = ?",
        (company,),
    )
    conn.commit()
    conn.close()

    if cur.rowcount:
        return _agents_redirect(msg=f"Token for {company} revoked.")
    return _agents_redirect(err=f"No token exists for {company}.")


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# START APPLICATION
# ============================================================

def start_monitor_thread():
    """
    Optionally runs the network/speedtest monitor loop
    (uptime_checker.py) in a background thread inside this same
    process, so `python app.py` alone is enough to get both the
    dashboard and the data collection running.

    Set AUTO_START_MONITOR=0 in the environment if you'd rather
    run uptime_checker.py as its own separate process/service
    instead (e.g. via Task Scheduler) - that also works fine and
    writes to the same database.
    """

    try:

        import uptime_checker

    except Exception as error:

        print(
            f"Could not import uptime_checker.py: {error}"
        )

        print(
            "The dashboard will still run, but no new data "
            "will be collected. Run uptime_checker.py "
            "separately, or fix the import error above."
        )

        return

    thread = threading.Thread(
        target=uptime_checker.monitor,
        daemon=True
    )

    thread.start()


def start_backup_thread():
    """
    Daily SQLite backups via backup.py (one at startup, then every
    BACKUP_INTERVAL seconds; newest BACKUP_KEEP kept). Set
    BACKUP_AUTO=0 to disable the in-process thread and schedule
    `python backup.py` yourself instead (Task Scheduler / cron).
    """

    if os.environ.get("BACKUP_AUTO", "1") == "0":

        print(
            "BACKUP_AUTO=0 - automatic backups disabled; run "
            "`python backup.py` on a schedule instead."
        )

        return

    import logging

    logging.getLogger("backup").setLevel(logging.INFO)

    import backup as backup_mod

    threading.Thread(
        target=backup_mod.backup_loop,
        daemon=True
    ).start()


if __name__ == "__main__":

    initialize_database()
    prune_old_data()

    print("=" * 60)

    print(
        "       INTERNET UPTIME MONITOR DASHBOARD"
    )

    print("=" * 60)

    shown_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST

    print(
        f"Dashboard: http://{shown_host}:{PORT}"
    )

    print("=" * 60)

    if AUTO_START_MONITOR:

        start_monitor_thread()

    else:

        print(
            "AUTO_START_MONITOR=0 - remember to run "
            "uptime_checker.py separately to collect data."
        )

    start_backup_thread()

    # Production WSGI server when available; Flask dev server otherwise.
    try:
        from waitress import serve
        print("Serving with Waitress (production).")
        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        print("Waitress not installed - using Flask dev server. "
              "pip install -r requirements.txt for production.")
        app.run(
            host=HOST,
            port=PORT,
            debug=False,
            threaded=True
        )
