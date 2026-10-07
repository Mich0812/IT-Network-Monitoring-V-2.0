"""Shared helpers and constants - extracted from app.py in Phase 13.

Blueprints import these directly; app.py re-exports the same names
(app.get_db, app.group_outages, ...) so the existing test-suite and
any app.<name> access keep working unchanged.
"""

import functools
import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from flask import abort, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

from config import (
    COMPANIES,
    default_company,
    is_valid_company,
    RETENTION_DAYS as DEFAULT_RETENTION_DAYS,
    validate_config,
)


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


RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", DEFAULT_RETENTION_DAYS))


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
            server TEXT,
            status TEXT NOT NULL DEFAULT 'ok'
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

    # Migration: "status" marks failed speedtest runs ('ok' for every
    # pre-existing row; failed rows keep NULL speeds + reason in
    # server). Charts filter on it, so it must exist before serving.
    try:
        conn.execute(
            "ALTER TABLE speedtest_results "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

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
                return redirect(url_for("auth.login"))
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
# RUNTIME-READ CONFIG FLAGS (Phase 13)
# ============================================================

def live_flags():
    """The app module, read at request time.

    Tests patch PUBLIC_STATUS / MAINTENANCE on the *app module*, so
    blueprints must not import those names - a plain import would
    freeze the value from import time. Routes that moved out of
    app.py read the flags through this accessor instead, so the
    patched value stays visible.
    """
    from flask import current_app

    return current_app.extensions["app_module"]


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


def _agent_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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

