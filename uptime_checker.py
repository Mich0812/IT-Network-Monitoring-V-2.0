import subprocess
import platform
import time
import sqlite3
import socket
import urllib.request
import json
import os
import re
import logging
import threading

from concurrent.futures import ThreadPoolExecutor

from datetime import datetime, timezone

from config import (
    COMPANIES,
    SPEEDTEST_COMPANY,
    CHECK_INTERVAL as DEFAULT_CHECK_INTERVAL,
    SPEEDTEST_INTERVAL as DEFAULT_SPEEDTEST_INTERVAL,
    RETENTION_DAYS as DEFAULT_RETENTION_DAYS,
    validate_config,
)


# ============================================================
# LOGGING
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.environ.get(
    "LOG_PATH"
) or os.path.join(
    BASE_DIR,
    "monitor.log"
)

from logging.handlers import RotatingFileHandler

# Rotate at 5 MB, keep 5 backups - monitor.log would otherwise grow
# without bound since the checker logs every check every cycle.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)

log = logging.getLogger("uptime_checker")


# ============================================================
# CONFIGURATION
# ============================================================

# DB_PATH lets tests (and advanced setups) point elsewhere without
# touching the production uptime.db.
DATABASE = os.environ.get(
    "DB_PATH"
) or os.path.join(
    BASE_DIR,
    "uptime.db"
)

# Your Speedtest executable (Ookla Speedtest CLI - speedtest.exe,
# NOT the old open-source "speedtest-cli" pip package - the JSON
# output format is different and this file parses Ookla's format).
SPEEDTEST_EXE = os.environ.get(
    "SPEEDTEST_PATH",
    os.path.join(BASE_DIR, "speedtest.exe")
    if platform.system().lower() == "windows"
    else os.environ.get("SPEEDTEST_PATH", "speedtest"),
)


def _int_env(name, default, minimum):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(value, minimum)


# Env overrides win over config.py defaults.
CHECK_INTERVAL = _int_env(
    "CHECK_INTERVAL", DEFAULT_CHECK_INTERVAL, 10
)
SPEEDTEST_INTERVAL = _int_env(
    "SPEEDTEST_INTERVAL", DEFAULT_SPEEDTEST_INTERVAL, 300
)
RETENTION_DAYS = _int_env(
    "RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 7
)

# How many companies are probed at the same time. Results are still
# saved and evaluated in the main thread, in config order.
POLL_WORKERS = _int_env(
    "POLL_WORKERS", len(COMPANIES), 1
)


def utc_now_str():
    """UTC 'YYYY-MM-DD HH:MM:SS' matching SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# DATABASE
# ============================================================

def get_db():

    conn = sqlite3.connect(
        DATABASE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    # WAL allows the Flask reader + checker writer to coexist
    # without frequent "database is locked" errors.
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass

    return conn


def initialize_database():

    conn = get_db()

    # --------------------------------------------------------
    # Network monitoring results
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Speedtest results
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Migration: older databases created before multi-company
    # support won't have the "company" column yet.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Migration: "status" marks failed speedtest runs ('ok' for
    # every pre-existing row; failed rows keep NULL speeds).
    # --------------------------------------------------------

    try:
        conn.execute(
            "ALTER TABLE speedtest_results "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # --------------------------------------------------------
    # OUTAGES - closed incidents (UP->DOWN ... DOWN->UP).
    # ended_at is NULL while the outage is still open.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # NOTIFICATIONS - alert audit + dedup/cooldown state.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # HOURLY ROLLUPS - pre-aggregated availability so 30-day
    # charts/reports don't scan ping_results row by row.
    # One row per (company, check_type, hour_utc).
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Indexes: the dashboard always filters by company + time.
    # Without these every chart is a full table scan.
    # --------------------------------------------------------

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

    # Persistent login rate-limit (survives restarts,unlike in-memory).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_failures (
            ip TEXT PRIMARY KEY,
            attempts TEXT NOT NULL DEFAULT '[]',
            locked_until TEXT
        )
    """)

    conn.commit()

    conn.close()


def prune_old_data(retention_days=None):
    """Delete rows older than retention window. Runs once per day."""
    days = retention_days or RETENTION_DAYS
    try:
        conn = get_db()
        for table in ("ping_results", "speedtest_results"):
            cur = conn.execute(
                f"DELETE FROM {table} "
                f"WHERE timestamp < datetime('now', '-{int(days)} days')"
            )
            if cur.rowcount:
                log.info(f"Pruned {cur.rowcount} rows from {table}.")
        cur = conn.execute(
            "DELETE FROM rollup_hourly "
            "WHERE substr(hour_utc, 1, 10) < date('now', ?)",
            (f"-{int(days)} days",),
        )
        if cur.rowcount:
            log.info(f"Pruned {cur.rowcount} rows from rollup_hourly.")
        conn.commit()
        conn.close()
    except Exception as error:
        log.warning(f"Retention prune failed: {error}")


# ============================================================
# FIND DEFAULT GATEWAY
# ============================================================

def get_gateway():

    # Set GATEWAY_IP (environment variable) to skip auto-detection,
    # e.g.  set GATEWAY_IP=192.168.1.1
    manual = os.environ.get("GATEWAY_IP")

    if manual:

        return manual.strip()

    system = platform.system().lower()

    try:

        # ----------------------------------------------------
        # WINDOWS
        # ----------------------------------------------------

        if system == "windows":

            result = subprocess.run(
                ["ipconfig"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )

            # The IPv4 gateway often sits on the line AFTER
            # "Default Gateway" (when an IPv6 one is listed
            # first), so scan every IPv4-looking address that
            # follows that label, not just the label's own line.
            in_gateway_block = False

            for line in result.stdout.splitlines():

                if "Default Gateway" in line:

                    in_gateway_block = True

                elif ":" in line and line.strip():

                    in_gateway_block = False

                if in_gateway_block:

                    match = re.search(
                        r"\b(\d{1,3}(?:\.\d{1,3}){3})\b",
                        line
                    )

                    if match:

                        return match.group(1)

        # ----------------------------------------------------
        # macOS
        # ----------------------------------------------------

        elif system == "darwin":

            result = subprocess.run(
                ["route", "-n", "get", "default"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )

            match = re.search(
                r"gateway:\s*(\S+)",
                result.stdout,
            )

            if match:

                return match.group(1).strip()

        # ----------------------------------------------------
        # LINUX (and fallback)
        # ----------------------------------------------------

        else:

            result = subprocess.run(
                ["ip", "route"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )

            for line in result.stdout.splitlines():

                if line.startswith("default"):

                    parts = line.split()

                    if "via" in parts:

                        return parts[
                            parts.index("via") + 1
                        ]

    except Exception as error:

        log.debug(f"Gateway detection failed: {error}")

    return None


# ============================================================
# PING
# ============================================================

# Matches "time=12ms", "time<1ms", "time=12.3 ms" on Win/Linux/macOS.
PING_TIME_RE = re.compile(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)


def parse_ping_latency(output):
    """Extract true ICMP time from ping stdout. None if not found."""
    if not output:
        return None
    matches = PING_TIME_RE.findall(output)
    if not matches:
        return None
    try:
        # Windows -n 1 prints one reply; Linux may print one too.
        # Take the last match (the reply, not a summary line).
        value = float(matches[-1])
        # "time<1ms" reports as 1 -> treat as ~0.5ms, still UP.
        if "time<" in output.lower() and value <= 1:
            return round(value / 2, 2) or 0.5
        return round(value, 2)
    except ValueError:
        return None


def ping(target):

    system = platform.system().lower()

    if system == "windows":

        command = [
            "ping",
            "-n",
            "1",
            "-w",
            "2000",
            target
        ]

    else:

        command = [
            "ping",
            "-c",
            "1",
            "-W",
            "2",
            target
        ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            # Prefer the ICMP-reported time over wall-clock, which
            # includes process-spawn overhead (10-30ms on Windows).
            latency = parse_ping_latency(result.stdout)
            if latency is None:
                # Fallback: shouldn't happen, but keeps UP/DOWN correct.
                latency = parse_ping_latency(result.stderr)
            return (
                target,
                "UP",
                latency if latency is not None else 1.0,
            )

        return (
            target,
            "DOWN",
            None
        )

    except Exception:

        return (
            target,
            "ERROR",
            None
        )


# ============================================================
# DNS CHECK
# ============================================================

def dns_check(domain):

    start_time = time.perf_counter()

    try:

        socket.gethostbyname(domain)

        end_time = time.perf_counter()

        latency = (
            end_time - start_time
        ) * 1000

        return (
            domain,
            "UP",
            round(latency, 2)
        )

    except socket.gaierror:

        return (
            domain,
            "DOWN",
            None
        )

    except Exception:

        return (
            domain,
            "ERROR",
            None
        )


# ============================================================
# HTTPS CHECK
# ============================================================

def https_check(url):

    start_time = time.perf_counter()

    try:

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                    "UptimeMonitor/1.0"
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=5
        ) as response:

            response.read(1)

        end_time = time.perf_counter()

        latency = (
            end_time - start_time
        ) * 1000

        return (
            url,
            "UP",
            round(latency, 2)
        )

    except Exception:

        return (
            url,
            "DOWN",
            None
        )


# ============================================================
# SAVE NETWORK RESULT
# ============================================================

def save_result(
    timestamp,
    company,
    check_type,
    target,
    status,
    latency
):

    try:

        conn = get_db()

        conn.execute(
            """
            INSERT INTO ping_results
            (
                timestamp,
                company,
                target,
                check_type,
                status,
                latency
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                company,
                target,
                check_type,
                status,
                latency
            )
        )

        conn.commit()

        conn.close()

    except Exception as error:

        log.warning(
            f"Database error: {error}"
        )


# ============================================================
# SAVE SPEEDTEST
# ============================================================

def save_speedtest(
    timestamp,
    company,
    download,
    upload,
    ping_value,
    server,
    status="ok"
):

    try:

        conn = get_db()

        conn.execute(
            """
            INSERT INTO speedtest_results
            (
                timestamp,
                company,
                download,
                upload,
                ping,
                server,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                company,
                download,
                upload,
                ping_value,
                server,
                status
            )
        )

        conn.commit()

        conn.close()

    except Exception as error:

        log.warning(
            f"Database error: {error}"
        )


def save_speedtest_failure(reason):
    """Record a failed speedtest attempt.

    Failed runs used to vanish into the log, leaving an unexplained
    gap in the chart. A row with status='failed', NULL speeds and the
    reason in `server` keeps the history honest without polluting the
    charts (they filter status='ok').
    """
    save_speedtest(
        utc_now_str(),
        SPEEDTEST_COMPANY,
        None,
        None,
        None,
        "FAILED: " + str(reason)[:160],
        status="failed",
    )


# ============================================================
# RUN SPEEDTEST
# ============================================================

def run_speedtest():

    log.info("Running Speedtest...")

    # --------------------------------------------------------
    # Check if speedtest binary exists
    # --------------------------------------------------------

    exe = SPEEDTEST_EXE
    exe_exists = os.path.exists(exe) if os.path.isabs(exe or "") else False

    # Allow bare "speedtest" on PATH (Linux/macOS).
    if not exe_exists and not os.path.isabs(exe or ""):
        exe_exists = True  # let subprocess resolve via PATH

    if not exe_exists and os.path.isabs(exe or ""):

        log.error(
            "speedtest binary was not found at %s. "
            "Set SPEEDTEST_PATH env var.",
            SPEEDTEST_EXE,
        )

        save_speedtest_failure(f"binary not found: {SPEEDTEST_EXE}")

        return False

    result = None

    try:

        # ----------------------------------------------------
        # Run the Ookla Speedtest CLI.
        #
        # --accept-license / --accept-gdpr are required the
        # first time the CLI runs on a machine - without them
        # it blocks waiting for an interactive "y/n" prompt
        # that never comes when launched from this script,
        # which looks like a silent hang.
        # ----------------------------------------------------

        result = subprocess.run(

            [
                exe or SPEEDTEST_EXE,
                "--accept-license",
                "--accept-gdpr",
                "-f",
                "json"
            ],

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            text=True,

            timeout=180
        )

        # ----------------------------------------------------
        # Check execution
        # --------------------------------------------------------

        if result.returncode != 0:

            log.warning(
                "Speedtest failed: %s",
                (result.stderr or "")[:500],
            )

            save_speedtest_failure(
                f"exit {result.returncode}: "
                f"{(result.stderr or result.stdout or '')[:120]}"
            )

            return False

        # ----------------------------------------------------
        # Convert JSON result (Ookla format: bandwidth bytes/sec)
        # --------------------------------------------------------

        data = json.loads(
            result.stdout
        )

        if data.get("type") != "result":

            log.warning(
                "Unexpected Speedtest output (no result object)."
            )

            save_speedtest_failure("unexpected output (no result object)")

            return False

        # ----------------------------------------------------
        # Download / Upload (bytes/sec -> Mbps)
        # --------------------------------------------------------

        download_bytes_per_sec = data.get(
            "download",
            {}
        ).get(
            "bandwidth",
            0
        )

        download_mbps = (
            download_bytes_per_sec * 8 / 1_000_000
        )

        upload_bytes_per_sec = data.get(
            "upload",
            {}
        ).get(
            "bandwidth",
            0
        )

        upload_mbps = (
            upload_bytes_per_sec * 8 / 1_000_000
        )

        # ----------------------------------------------------
        # Ping (already in ms)
        # --------------------------------------------------------

        ping_value = data.get(
            "ping",
            {}
        ).get(
            "latency",
            0
        )

        # ----------------------------------------------------
        # Server
        # --------------------------------------------------------

        server = data.get(
            "server",
            {}
        )

        server_name = server.get(
            "name",
            "Unknown"
        )

        server_location = server.get(
            "location",
            ""
        )

        server_country = server.get(
            "country",
            ""
        )

        if server_location:

            server_display = (
                f"{server_name} "
                f"({server_location}, "
                f"{server_country})"
            )

        else:

            server_display = server_name

        # ----------------------------------------------------
        # Round values
        # --------------------------------------------------------

        download_mbps = round(
            download_mbps,
            2
        )

        upload_mbps = round(
            upload_mbps,
            2
        )

        ping_value = round(
            ping_value,
            2
        )

        # ----------------------------------------------------
        # Timestamp (UTC - must match SQLite datetime('now'))
        # --------------------------------------------------------

        timestamp = utc_now_str()

        # ----------------------------------------------------
        # Display result
        # --------------------------------------------------------

        log.info(
            f"Speedtest: down={download_mbps} Mbps up={upload_mbps} "
            f"Mbps ping={ping_value} ms server={server_display} "
            f"company={SPEEDTEST_COMPANY}"
        )

        # ----------------------------------------------------
        # Save to database
        # --------------------------------------------------------

        save_speedtest(
            timestamp,
            SPEEDTEST_COMPANY,
            download_mbps,
            upload_mbps,
            ping_value,
            server_display
        )

        return True

    except subprocess.TimeoutExpired:

        log.warning(
            "Speedtest timed out."
        )

        save_speedtest_failure("timed out after 180s")

        return False

    except json.JSONDecodeError:

        log.warning(
            "Could not read Speedtest JSON."
        )

        save_speedtest_failure("could not parse JSON output")

        return False

    except Exception as error:

        log.warning(
            f"Speedtest error: {error}"
        )

        save_speedtest_failure(f"error: {error}")

        return False


def run_speedtest_async():
    """Non-blocking wrapper - speedtest takes 10-30s, never stall checks."""
    thread = threading.Thread(target=run_speedtest, daemon=True)
    thread.start()
    return thread


# ============================================================
# RUN ALL NETWORK CHECKS
# ============================================================

def check_all(company_name, targets):

    results = []

    timestamp = utc_now_str()

    # --------------------------------------------------------
    # GATEWAY ("auto" = this machine's own default gateway)
    # --------------------------------------------------------

    gateway_config = targets.get("gateway", "auto")

    if gateway_config == "auto":

        gateway = get_gateway()

    else:

        gateway = gateway_config

    if gateway:

        target, status, latency = ping(
            gateway
        )

        results.append(
            (
                timestamp,
                company_name,
                "Gateway",
                target,
                status,
                latency
            )
        )

    else:

        results.append(
            (
                timestamp,
                company_name,
                "Gateway",
                "Unknown",
                "DOWN",
                None
            )
        )

    # --------------------------------------------------------
    # INTERNET
    # --------------------------------------------------------

    target, status, latency = ping(
        targets["internet"]
    )

    results.append(
        (
            timestamp,
            company_name,
            "Internet",
            target,
            status,
            latency
        )
    )

    # --------------------------------------------------------
    # DNS
    # --------------------------------------------------------

    target, status, latency = dns_check(
        targets["dns"]
    )

    results.append(
        (
            timestamp,
            company_name,
            "DNS",
            target,
            status,
            latency
        )
    )

    # --------------------------------------------------------
    # HTTPS
    # --------------------------------------------------------

    target, status, latency = https_check(
        targets["https"]
    )

    results.append(
        (
            timestamp,
            company_name,
            "HTTPS",
            target,
            status,
            latency
        )
    )

    return results


# ============================================================
# OUTAGE DETECTION
#
# State machine over (company, check_type):
#   UP   -> DOWN : open an outage row (ended_at NULL)
#   DOWN -> DOWN : keep it open (flap guard counts separately)
#   DOWN -> UP   : close the row, record duration
# State survives restarts by seeding from the last row per key.
# ============================================================

_state_lock = threading.Lock()
_check_state = {}          # (company, check_type) -> {"status", "fails"}
_open_outage_id = {}       # (company, check_type) -> outages.id


def seed_check_state():
    """Load last known status + open outage IDs at startup."""
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT p.company, p.check_type, p.status, p.id
            FROM ping_results p
            INNER JOIN (
                SELECT company, check_type, MAX(id) AS max_id
                FROM ping_results
                GROUP BY company, check_type
            ) latest
            ON p.company = latest.company
            AND p.check_type = latest.check_type
            AND p.id = latest.max_id
        """).fetchall()
        for r in rows:
            key = (r["company"], r["check_type"])
            _check_state[key] = {
                "status": r["status"],
                "fails": 0 if r["status"] == "UP" else 1,
            }

        open_rows = conn.execute(
            "SELECT id, company, check_type FROM outages "
            "WHERE ended_at IS NULL"
        ).fetchall()
        for r in open_rows:
            _open_outage_id[(r["company"], r["check_type"])] = r["id"]
        conn.close()
        log.info(
            f"Seeded check state: {len(_check_state)} series, "
            f"{len(_open_outage_id)} open outage(s)."
        )
    except Exception as error:
        log.warning(f"State seeding failed: {error}")


def close_stale_outages():
    """Any open outage whose check is now UP gets closed at startup."""
    try:
        conn = get_db()
        open_rows = conn.execute(
            "SELECT id, company, check_type, started_at FROM outages "
            "WHERE ended_at IS NULL"
        ).fetchall()
        now = utc_now_str()
        for r in open_rows:
            key = (r["company"], r["check_type"])
            state = _check_state.get(key, {})
            if state.get("status") == "UP":
                duration = _diff_seconds(r["started_at"], now)
                conn.execute(
                    "UPDATE outages SET ended_at=?, duration=?, "
                    "cause=? WHERE id=?",
                    (now, duration, "closed_at_restart", r["id"]),
                )
                _open_outage_id.pop(key, None)
                log.info(
                    f"Closed stale outage #{r['id']} "
                    f"({r['company']}/{r['check_type']})"
                )
        conn.commit()
        conn.close()
    except Exception as error:
        log.warning(f"Stale outage cleanup failed: {error}")


def _diff_seconds(start_ts, end_ts):
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        from datetime import datetime as _dt
        a = _dt.strptime(start_ts, fmt)
        b = _dt.strptime(end_ts, fmt)
        return round((b - a).total_seconds(), 1)
    except Exception:
        return None


def process_check_result(company, check_type, status, timestamp):
    """
    Feed one result into the state machine. Returns a dict
    describing what happened, used by the alert engine:
      {"event": "down|up|still_down|still_up", "open_outage_id": id}
    """
    key = (company, check_type)
    with _state_lock:
        prev = _check_state.get(key, {"status": None, "fails": 0})
        prev_status = prev["status"]

        if status == "DOWN" or status == "ERROR":
            fails = prev["fails"] + 1 if prev_status != "UP" else 1
        else:  # UP
            fails = 0

        _check_state[key] = {"status": status, "fails": fails}

        event = "still_up"
        outage_id = _open_outage_id.get(key)

        if prev_status is None:
            # First observation ever: don't invent an outage start,
            # but respect an outage row left open from a prior run.
            event = "still_up" if status == "UP" else "down"
            if status != "UP" and outage_id is None:
                outage_id = _open_outage(key, timestamp)
            if status == "UP" and outage_id:
                _close_outage(key, outage_id, timestamp)
                outage_id = None
            # Remember (or clear) the open-outage id here too, or an
            # outage opened on the very first observation would never
            # be closed by a later UP in the same run.
            _open_outage_id[key] = outage_id if outage_id else None
            if _open_outage_id[key] is None:
                _open_outage_id.pop(key, None)
            return {"event": event, "open_outage_id": outage_id,
                    "fails": fails}

        if prev_status == "UP" and status != "UP":
            # UP -> DOWN: open outage.
            outage_id = _open_outage(key, timestamp)
            event = "down"

        elif prev_status != "UP" and status == "UP":
            # DOWN -> UP: close outage.
            if outage_id:
                _close_outage(key, outage_id, timestamp)
            outage_id = None
            event = "up"

        elif prev_status != "UP" and status != "UP":
            event = "still_down"

        _open_outage_id[key] = outage_id if outage_id else None
        if _open_outage_id[key] is None:
            _open_outage_id.pop(key, None)

    return {"event": event, "open_outage_id": outage_id, "fails": fails}


def _open_outage(key, started_at):
    company, check_type = key
    try:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO outages (company, check_type, started_at) "
            "VALUES (?, ?, ?)",
            (company, check_type, started_at),
        )
        conn.commit()
        conn.close()
        return cur.lastrowid
    except Exception as error:
        log.warning(f"Open outage failed: {error}")
        return None


def _close_outage(key, outage_id, ended_at):
    company, check_type = key
    try:
        conn = get_db()
        duration = None
        row = conn.execute(
            "SELECT started_at FROM outages WHERE id=?", (outage_id,)
        ).fetchone()
        if row:
            duration = _diff_seconds(row["started_at"], ended_at)
        conn.execute(
            "UPDATE outages SET ended_at=?, duration=? WHERE id=?",
            (ended_at, duration, outage_id),
        )
        conn.commit()
        conn.close()
        mins = (
            f"{duration / 60:.1f}m" if duration is not None else "?"
        )
        log.info(
            f"Outage closed: {company}/{check_type} "
            f"#{outage_id} duration={mins}"
        )
    except Exception as error:
        log.warning(f"Close outage failed: {error}")


# ============================================================
# HOURLY ROLLUPS
#
# Aggregates completed UTC hours from ping_results into
# rollup_hourly (total, up%, avg/p95/max latency). Rebuilt for
# any hour that changed; safe to re-run (UPSERT).
# ============================================================

def build_rollups(since_hours=48):
    """Aggregate the last `since_hours` completed UTC hours."""
    import statistics
    try:
        conn = get_db()
        rows = conn.execute(
            """
            SELECT company, check_type,
                   strftime('%Y-%m-%dT%H:00:00', timestamp) AS hour_utc,
                   status, latency
            FROM ping_results
            WHERE timestamp >= datetime('now', ?)
            """,
            (f"-{int(since_hours)} hours",),
        ).fetchall()

        buckets = {}
        for r in rows:
            k = (r["company"], r["check_type"], r["hour_utc"])
            b = buckets.setdefault(
                k, {"total": 0, "up": 0, "lat": []}
            )
            b["total"] += 1
            if r["status"] == "UP":
                b["up"] += 1
            if r["latency"] is not None:
                b["lat"].append(r["latency"])

        for (company, check_type, hour_utc), b in buckets.items():
            lat = sorted(b["lat"])
            avg = round(statistics.fmean(lat), 2) if lat else None
            p95 = (
                round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 2)
                if lat
                else None
            )
            maxv = round(max(lat), 2) if lat else None
            conn.execute(
                """
                INSERT INTO rollup_hourly
                    (company, check_type, hour_utc, total, up_count,
                     avg_latency, p95_latency, max_latency)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (company, check_type, hour_utc)
                DO UPDATE SET
                    total=excluded.total,
                    up_count=excluded.up_count,
                    avg_latency=excluded.avg_latency,
                    p95_latency=excluded.p95_latency,
                    max_latency=excluded.max_latency
                """,
                (
                    company, check_type, hour_utc,
                    b["total"], b["up"], avg, p95, maxv,
                ),
            )

        conn.commit()
        conn.close()
        if buckets:
            log.info(f"Rollups rebuilt: {len(buckets)} bucket(s).")
    except Exception as error:
        log.warning(f"Rollup build failed: {error}")


# ============================================================
# ALERT ENGINE
#
# Conditions (config.ALERTS):
#   - down: `down_failures` consecutive non-UP results
#   - latency: > latency_ms for `latency_failures` checks
# Suppressed inside MAINTENANCE windows; deduped by cooldown.
# Channels: SMTP email + Slack/Teams webhook, always logged.
# ============================================================

from config import ALERTS, MAINTENANCE   # noqa: E402

_latency_state = {}   # (company, check_type) -> consecutive over-threshold
_last_alert_ts = {}   # (company, check_type, kind) -> epoch


def _in_maintenance(company, now_dt=None):
    """True if `company` (or any company when window has no company)
    is inside an active maintenance window."""
    if not MAINTENANCE:
        return False

    now_dt = now_dt or datetime.now()
    day = now_dt.weekday()          # 0=Mon
    hhmm = now_dt.strftime("%H:%M")

    def in_window(window):
        days = window.get("days", "all")
        if days != "all" and day not in days:
            return False
        start = window.get("start", "")
        end = window.get("end", "")
        if not start or not end:
            return False
        # Handles windows crossing midnight (e.g. 23:00 -> 02:00).
        if start <= end:
            return start <= hhmm < end
        return hhmm >= start or hhmm < end

    for window in MAINTENANCE:
        wc = window.get("company")
        if wc is not None and wc != company:
            continue
        if in_window(window):
            return True
    return False


def _cooldown_ok(company, check_type, kind):
    key = (company, check_type, kind)
    last = _last_alert_ts.get(key, 0)
    cooldown = ALERTS.get("cooldown_seconds", 1800)
    if time.time() - last < cooldown:
        return False

    # Also honor cooldown across restarts via notifications table.
    try:
        conn = get_db()
        row = conn.execute(
            """
            SELECT sent_at FROM notifications
            WHERE company=? AND check_type=? AND kind=?
            ORDER BY id DESC LIMIT 1
            """,
            (company, check_type, kind),
        ).fetchone()
        conn.close()
        if row:
            fmt = "%Y-%m-%d %H:%M:%S"
            from datetime import datetime as _dt
            # sent_at is UTC (utc_now_str) - attach the tz explicitly,
            # otherwise .timestamp() reads it as LOCAL time and the
            # cooldown window shifts by the machine's UTC offset.
            sent = _dt.strptime(row["sent_at"], fmt).replace(
                tzinfo=timezone.utc
            ).timestamp()
            if time.time() - sent < cooldown:
                _last_alert_ts[key] = sent
                return False
    except Exception:
        pass

    return True


def _record_notification(company, check_type, kind, message, channel, status):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO notifications "
            "(company, check_type, kind, message, channel, sent_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                company, check_type, kind, message,
                channel, utc_now_str(), status,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as error:
        log.warning(f"Notification record failed: {error}")


def _send_email(subject, body):
    cfg = ALERTS.get("email", {})
    if not cfg.get("enabled"):
        return False, "disabled"
    try:
        import smtplib
        from email.mime.text import MIMEText

        password = os.environ.get("SMTP_PASSWORD") or cfg.get("password", "")

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = cfg.get("from", "uptime-monitor@localhost")
        msg["To"] = ", ".join(cfg.get("to", []))

        with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587)),
                          timeout=15) as server:
            if cfg.get("starttls", True):
                server.starttls()
            if cfg.get("username"):
                server.login(cfg["username"], password)
            server.sendmail(
                msg["From"], cfg.get("to", []), msg.as_string()
            )
        return True, "sent"
    except Exception as error:
        return False, f"error: {error}"


def _send_webhook(payload):
    cfg = ALERTS.get("webhook", {})
    if not cfg.get("enabled"):
        return False, "disabled"
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            cfg["url"],
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, f"http {resp.status}"
    except Exception as error:
        return False, f"error: {error}"


def dispatch_alert(company, check_type, kind, message, subject=None):
    """
    Send one alert through every enabled channel, always log it,
    and record it for audit + cooldown. Returns True if delivered.
    """
    if not ALERTS.get("enabled", False):
        return False

    if _in_maintenance(company):
        log.info(
            f"Alert suppressed (maintenance): "
            f"{company}/{check_type} - {message}"
        )
        _record_notification(
            company, check_type, kind, message, "suppressed", "maintenance"
        )
        return False

    if not _cooldown_ok(company, check_type, kind):
        log.debug(
            f"Alert in cooldown: {company}/{check_type} ({kind})"
        )
        return False

    subject = subject or f"[UptimeMonitor] {company}: {check_type} {kind}"
    delivered = False

    ok, status = _send_email(subject, message)
    if ok:
        delivered = True
    _record_notification(company, check_type, kind, message, "email",
                         "sent" if ok else status)

    ok2, status2 = _send_webhook({
        "text": subject,
        "body": message,
        "company": company,
        "check_type": check_type,
        "kind": kind,
        "timestamp": utc_now_str(),
    })
    if ok2:
        delivered = True
    _record_notification(company, check_type, kind, message, "webhook",
                         "sent" if ok2 else status2)

    _last_alert_ts[(company, check_type, kind)] = time.time()
    log.info(f"ALERT ({kind}) {company}/{check_type}: {message}")
    return delivered


def evaluate_alerts(company, check_type, status, latency, result_info):
    """Called per check result. Applies flap guard + latency rules."""
    if not ALERTS.get("enabled", False):
        return

    if _in_maintenance(company):
        return

    fails = result_info.get("fails", 0)
    event = result_info.get("event")

    # --- Down rule (flap guard) ---
    down_needed = int(ALERTS.get("down_failures", 2))
    if (status == "DOWN" or status == "ERROR") and fails >= down_needed:
        if event in ("down", "still_down"):
            duration_note = ""
            oid = result_info.get("open_outage_id")
            if oid:
                duration_note = f" (outage #{oid})"
            dispatch_alert(
                company,
                check_type,
                "down",
                f"{check_type} is {status} for {fails} consecutive "
                f"checks{duration_note}. Target: see dashboard.",
            )
    elif event == "up":
        # Recovery notice only if we previously alerted down.
        if (company, check_type, "down") in _last_alert_ts:
            dispatch_alert(
                company,
                check_type,
                "recovery",
                f"{check_type} recovered and is UP again.",
            )
            _last_alert_ts.pop((company, check_type, "down"), None)

    # --- Latency rule ---
    threshold = float(ALERTS.get("latency_ms", 0) or 0)
    if threshold > 0 and latency is not None:
        key = (company, check_type)
        if latency > threshold:
            over = _latency_state.get(key, 0) + 1
            _latency_state[key] = over
            needed = int(ALERTS.get("latency_failures", 3))
            if over >= needed:
                dispatch_alert(
                    company,
                    check_type,
                    "latency",
                    f"{check_type} latency {latency:.0f} ms exceeds "
                    f"threshold {threshold:.0f} ms "
                    f"({over} consecutive checks).",
                )
        else:
            _latency_state[key] = 0


# ============================================================
# MONITOR
# ============================================================

def local_companies():
    """Companies THIS machine polls.

    Entries with "managed_by": "agent" push their results in via
    POST /api/ingest instead - polling them here would double-count
    every check.
    """
    return {
        name: targets
        for name, targets in COMPANIES.items()
        if targets.get("managed_by", "local") != "agent"
    }


def monitor():

    validate_config()
    initialize_database()
    prune_old_data()
    seed_check_state()
    close_stale_outages()
    build_rollups(since_hours=48)

    log.info("=" * 80)
    log.info("INTERNET UPTIME MONITOR")
    log.info("=" * 80)

    log.info(
        f"Monitoring {len(COMPANIES)} "
        "compan"
        + ("y" if len(COMPANIES) == 1 else "ies")
        + ":"
    )

    for company_name, targets in COMPANIES.items():

        gateway_config = targets.get(
            "gateway",
            "auto"
        )

        gateway_display = (
            get_gateway()
            if gateway_config == "auto"
            else gateway_config
        )

        managed = targets.get("managed_by", "local")

        suffix = (
            "  (remote agent pushes results)"
            if managed == "agent"
            else ""
        )

        log.info(
            f"  [{company_name}] "
            f"Gateway={gateway_display}  "
            f"Internet={targets['internet']}  "
            f"DNS={targets['dns']}  "
            f"HTTPS={targets['https']}"
            f"{suffix}"
        )

    log.info(
        f"Speedtest runs against: {SPEEDTEST_COMPANY}"
    )

    log.info(
        f"Network checks: Every {CHECK_INTERVAL} seconds, "
        "per company"
    )

    log.info(
        f"Speedtest: Every {SPEEDTEST_INTERVAL} seconds (background thread)"
    )

    log.info(f"Retention: {RETENTION_DAYS} days")
    log.info("=" * 80)

    # --------------------------------------------------------
    # Run speedtest immediately on startup (background)
    # --------------------------------------------------------

    last_speedtest = 0
    last_prune = time.time()
    last_rollup = time.time()

    # Log alerting status once at startup.
    _channels = []
    if ALERTS.get("enabled"):
        if ALERTS.get("email", {}).get("enabled"):
            _channels.append("email")
        if ALERTS.get("webhook", {}).get("enabled"):
            _channels.append("webhook")
        log.info(
            "Alerting: ON "
            f"(down_failures={ALERTS.get('down_failures')}, "
            f"cooldown={ALERTS.get('cooldown_seconds')}s, "
            f"channels={_channels or ['log-only']}, "
            f"maintenance_windows={len(MAINTENANCE)})"
        )
    else:
        log.info("Alerting: OFF (ALERTS['enabled']=False)")

    # --------------------------------------------------------
    # PARALLEL POLLING
    # All companies are probed at the same time; rows are still
    # saved and the outage/alert state machine still runs in
    # this thread, in config order.
    # --------------------------------------------------------

    # Companies with "managed_by": "agent" push their results in via
    # POST /api/ingest - never poll them from here, or every check
    # would be double-counted.
    local_companies_map = local_companies()

    poll_workers = max(
        1,
        min(POLL_WORKERS, len(local_companies_map) or 1),
    )

    poll_pool = ThreadPoolExecutor(
        max_workers=poll_workers,
        thread_name_prefix="poll",
    )

    log.info(
        f"Parallel polling: {poll_workers} worker(s) "
        f"for {len(local_companies_map)} local company/companies "
        f"({len(COMPANIES) - len(local_companies_map)} agent-managed)"
    )

    while True:

        try:

            cycle_start = time.time()

            # ------------------------------------------------
            # NETWORK CHECKS (locally managed companies only)
            # ------------------------------------------------

            futures = [
                (
                    company_name,
                    poll_pool.submit(
                        check_all,
                        company_name,
                        targets,
                    ),
                )
                for company_name, targets in local_companies_map.items()
            ]

            for company_name, future in futures:

                try:
                    results = future.result()
                except Exception as check_error:
                    log.warning(
                        f"Check error [{company_name}]: {check_error}"
                    )
                    continue

                if not results:
                    continue

                log.info(
                    f"[{results[0][0]}] {company_name}"
                )

                for (
                    timestamp,
                    company,
                    check_type,
                    target,
                    status,
                    latency
                ) in results:

                    save_result(
                        timestamp,
                        company,
                        check_type,
                        target,
                        status,
                        latency
                    )

                    # Outage state machine + alert rules.
                    try:
                        info = process_check_result(
                            company, check_type, status, timestamp
                        )
                        evaluate_alerts(
                            company, check_type, status, latency, info
                        )
                    except Exception as alert_error:
                        log.warning(
                            f"Alert pipeline error: {alert_error}"
                        )

                    if latency is not None:

                        log.info(
                            f"{check_type:<12}"
                            f"{target:<30}"
                            f"{status:<8}"
                            f"{latency:.2f} ms"
                        )

                    else:

                        log.info(
                            f"{check_type:<12}"
                            f"{target:<30}"
                            f"{status:<8}"
                        )

            log.info(
                f"Cycle done in {time.time() - cycle_start:.2f}s "
                f"({len(futures)} company/companies, "
                f"{poll_workers} parallel worker(s))"
            )

            # ------------------------------------------------
            # SPEEDTEST (single company, non-blocking)
            # ------------------------------------------------

            current_time = time.time()

            if (
                current_time
                - last_speedtest
                >= SPEEDTEST_INTERVAL
            ):

                run_speedtest_async()

                last_speedtest = current_time

            # ------------------------------------------------
            # DAILY RETENTION PRUNE
            # ------------------------------------------------

            if current_time - last_prune >= 86400:
                prune_old_data()
                last_prune = current_time

            # ------------------------------------------------
            # HOURLY ROLLUPS (every hour, covers recent hours)
            # ------------------------------------------------

            if current_time - last_rollup >= 3600:
                build_rollups(since_hours=48)
                last_rollup = current_time

            # ------------------------------------------------
            # WAIT
            # ------------------------------------------------

            log.info(
                "Next network check in "
                f"{CHECK_INTERVAL} seconds..."
            )

            time.sleep(
                CHECK_INTERVAL
            )

        except KeyboardInterrupt:

            log.info(
                "Monitor stopped."
            )

            break

        except Exception as error:

            log.warning(
                f"Monitor error: {error}"
            )

            log.info(
                "Retrying in 10 seconds..."
            )

            time.sleep(10)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    monitor()
