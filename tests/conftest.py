"""
Shared fixtures for the test suite.

IMPORTANT: the environment variables that redirect the database, log
file, secret key, and backup directory to scratch space are set right
here at import time - BEFORE `app` or `uptime_checker` can read them.
A test run therefore can never touch the real uptime.db, monitor.log,
or backups/ folder.

Run with:  python -m pytest -q
"""

import os
import re
import sys
import tempfile

# ---- scratch environment (must precede any app import) ----

_TMPDIR = tempfile.mkdtemp(prefix="uptime-tests-")

os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["LOG_PATH"] = os.path.join(_TMPDIR, "test-monitor.log")
os.environ["BACKUP_DIR"] = os.path.join(_TMPDIR, "backups")
os.environ["SECRET_KEY"] = "test-secret-key-not-production"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-pass-123"
os.environ["AUTO_START_MONITOR"] = "0"
os.environ["BACKUP_AUTO"] = "0"
os.environ.pop("SECURE_COOKIES", None)

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

import pytest  # noqa: E402


# ---- app / database ----

@pytest.fixture(scope="session", autouse=True)
def app_module():
    """Import the app and build the schema once, in scratch space."""
    import app
    app.initialize_database()
    return app


@pytest.fixture()
def client(app_module):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# ---- session helpers ----

def _extract_csrf(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""


def real_login(client, username, password):
    """Perform an actual CSRF-protected login round trip."""
    page = client.get("/login").get_data(as_text=True)
    token = _extract_csrf(page)
    assert token, "login page did not render a CSRF token"
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": token,
        },
    )


def forge(client, user_id=1, username="admin", role="admin", csrf="tok123"):
    """Put a ready-made session on the client (no password needed).

    Mirrors exactly what login() stores: user_id, username, role,
    plus a csrf token we know so POSTs can be signed.
    """
    with client.session_transaction() as s:
        s.clear()
        s["user_id"] = user_id
        s["username"] = username
        s["role"] = role
        s["csrf_token"] = csrf
    return client


def page_csrf(client, path):
    """Grab the CSRF token from a rendered page (post-login)."""
    return _extract_csrf(client.get(path).get_data(as_text=True))


# ---- role-based clients ----

@pytest.fixture()
def admin_client(app_module, client):
    r = real_login(client, "admin", "admin-pass-123")
    assert r.status_code == 302, r.get_data(as_text=True)
    return client


@pytest.fixture()
def viewer_client(client):
    return forge(client, user_id=2, username="viewer1", role="viewer")
