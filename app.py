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
    MAINTENANCE,
)


from core import (
    ALLOWED_HOURS,
    CHECK_ORDER,
    DATABASE,
    INCIDENT_MERGE_GAP,
    LOCKOUT_WINDOW,
    MAX_ATTEMPTS,
    RETENTION_DAYS,
    BASE_DIR,
    _TS_FMT,
    _agent_token_hash,
    _clamp_days,
    _failed_attempts_mem,
    _load_attempts,
    _report_company,
    _sla_rows,
    check_csrf,
    clear_failed_attempts,
    current_role,
    get_csrf_token,
    get_db,
    get_selected_company,
    group_outages,
    initialize_database,
    is_admin,
    is_locked_out,
    is_logged_in,
    prune_old_data,
    record_failed_attempt,
    require_admin,
    utc_now_str,
)


# ============================================================
# FLASK CONFIGURATION
# ============================================================

app = Flask(__name__)

# Re-read templates on every render so edits show up without a server
# restart. Without this, Jinja caches templates forever when debug=False,
# which silently serves stale pages after a template change.
app.config["TEMPLATES_AUTO_RELOAD"] = True


# Only used for the automatic in-process monitor thread. Set
# AUTO_START_MONITOR=0 in the environment if you'd rather run
# uptime_checker.py as its own separate process/service.
AUTO_START_MONITOR = os.environ.get(
    "AUTO_START_MONITOR",
    "1"
) != "0"


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


# Content-Security-Policy: everything is self-hosted since Phase 11
# (Chart.js in static/vendor, fonts in static/fonts) - no CDN origins.
# 'unsafe-inline' is required by the inline <script> blocks and style
# attributes in the templates. frame-ancestors 'none' is the modern
# clickjacking defence; X-Frame-Options DENY below stays for older
# browsers. Deliberately NO upgrade-insecure-requests - the LAN
# install runs plain HTTP.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
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



@app.context_processor
def inject_role():
    """Expose the current user's role to every template."""
    return {"role": current_role()}






# ============================================================
# BLUEPRINTS (Phase 13 - routes split out of app.py)
# ============================================================

import sys

# Blueprints read config flags (PUBLIC_STATUS, ...) back from the app
# module at request time (core.live_flags); tests patch them on this
# module, so a plain "from config import ..." inside a blueprint would
# miss the patched value.
app.extensions["app_module"] = sys.modules[__name__]

from blueprints.admin import bp as admin_bp
from blueprints.agents import bp as agents_bp
from blueprints.api import bp as api_bp
from blueprints.auth import bp as auth_bp
from blueprints.pages import bp as pages_bp
from blueprints.status import _public_companies  # re-export for tests
from blueprints.status import bp as status_bp

app.register_blueprint(admin_bp)
app.register_blueprint(agents_bp)
app.register_blueprint(api_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(pages_bp)
app.register_blueprint(status_bp)


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
