"""Authentication + RBAC: who may see and do what."""

import re
from urllib.parse import unquote_plus

import pytest

from tests.conftest import forge, real_login


def _loc(r):
    """Decoded redirect target, so banner text can be matched plainly."""
    return unquote_plus(r.headers["Location"])


# ---- anonymous -----------------------------------------------------

def test_anon_pages_redirect_to_login(client):
    for path in ("/dashboard", "/overview", "/reports", "/users"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert "/login" in r.headers["Location"], path


def test_anon_apis_return_401(client):
    for path in ("/api/sla", "/api/heatmap", "/api/sla/export",
                 "/api/backups"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/backup").status_code == 401
    assert client.post("/api/alerts/test").status_code == 401


def test_anon_post_to_users_redirects(client):
    r = client.post("/users/create")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ---- login flow ----------------------------------------------------

def test_good_login_reaches_dashboard(app_module, client):
    r = real_login(client, "admin", "admin-pass-123")
    assert r.status_code == 302
    assert "/dashboard" in r.headers["Location"]

    dash = client.get("/dashboard")
    assert dash.status_code == 200
    html = dash.get_data(as_text=True)
    assert 'href="/reports"' in html
    assert 'href="/users"' in html          # admin sees the Users link


def test_bad_password_rejected(app_module, client):
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = client.post("/login", data={
        "username": "admin",
        "password": "definitely-wrong",
        "csrf_token": token,
    })
    assert r.status_code == 200
    assert "Invalid username or password" in r.get_data(as_text=True)


def test_viewer_login_works(app_module, client):
    from werkzeug.security import generate_password_hash
    import app as app_mod

    conn = app_mod.get_db()
    conn.execute("DELETE FROM users WHERE username = 'viewer-login'")
    conn.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
        ("viewer-login",
         generate_password_hash("viewer-pass-123"),
         "viewer"),
    )
    conn.commit()
    conn.close()

    r = real_login(client, "viewer-login", "viewer-pass-123")
    assert r.status_code == 302
    assert "/dashboard" in r.headers["Location"]

    conn = app_mod.get_db()
    conn.execute("DELETE FROM users WHERE username = 'viewer-login'")
    conn.commit()
    conn.close()


# ---- viewer role ----------------------------------------------------

def test_viewer_can_read_reports_and_apis(client):
    c = forge(client, user_id=2, username="viewer1", role="viewer")

    assert c.get("/reports").status_code == 200
    assert c.get(
        "/api/sla", query_string={"days": 7}
    ).status_code == 200
    assert c.get(
        "/api/heatmap", query_string={"days": 7}
    ).status_code == 200
    assert c.get(
        "/api/sla/export", query_string={"days": 7}
    ).status_code == 200


def test_viewer_blocked_from_admin_everything(client):
    c = forge(client, user_id=2, username="viewer1", role="viewer")

    assert c.get("/users").status_code == 403
    assert c.post("/users/create").status_code == 403
    assert c.post("/users/1/password").status_code == 403
    assert c.post("/users/1/delete").status_code == 403
    assert c.post(
        "/api/backup", data={"csrf_token": "tok123"}
    ).status_code == 403
    assert c.post("/api/alerts/test").status_code == 403

    # No Users entry point anywhere in the UI
    for path in ("/dashboard", "/overview", "/reports"):
        html = c.get(path).get_data(as_text=True)
        assert 'href="/users"' not in html, path
        assert 'href="/reports"' in html, path


def test_viewer_forbidden_page_is_html_403(client):
    c = forge(client, user_id=2, username="viewer1", role="viewer")
    r = c.get("/users")
    assert r.status_code == 403
    assert "text/html" in r.headers.get("Content-Type", "")


# ---- admin page -----------------------------------------------------

def test_admin_sees_users_and_reports_pages(client):
    c = forge(client, user_id=1, username="admin", role="admin")

    users = c.get("/users")
    assert users.status_code == 200
    assert "All users" in users.get_data(as_text=True)

    reports = c.get("/reports")
    assert reports.status_code == 200
    html = reports.get_data(as_text=True)
    for needle in ("SLA summary", "Availability heatmap",
                   "Backups", "Export CSV"):
        assert needle in html, needle


# ---- users CRUD guards ---------------------------------------------

@pytest.fixture(autouse=True)
def only_admin(app_module):
    """Deterministic user table: just the admin, id captured."""
    import app
    conn = app.get_db()
    conn.execute("DELETE FROM users WHERE username <> 'admin'")
    conn.commit()
    row = conn.execute(
        "SELECT id FROM users WHERE username = 'admin'"
    ).fetchone()
    conn.close()
    admin_id = row["id"]
    yield admin_id
    conn = app.get_db()
    conn.execute("DELETE FROM users WHERE username <> 'admin'")
    conn.commit()
    conn.close()


def _admin(c, admin_id, csrf="tok123"):
    return forge(c, user_id=admin_id, username="admin",
                 role="admin", csrf=csrf)


def test_duplicate_username_rejected(client, only_admin):
    c = _admin(client, only_admin)
    r = c.post("/users/create", data={
        "username": "ADMIN",          # case-insensitive clash
        "password": "goodpass123",
        "role": "viewer",
        "csrf_token": "tok123",
    })
    assert r.status_code == 302
    assert "err=" in _loc(r)
    assert "exists" in _loc(r)


def test_weak_password_rejected(client, only_admin):
    c = _admin(client, only_admin)
    r = c.post("/users/create", data={
        "username": "newguy",
        "password": "short",
        "role": "viewer",
        "csrf_token": "tok123",
    })
    assert r.status_code == 302
    assert "err=" in _loc(r)
    assert "least" in _loc(r)


def test_csrf_required_on_user_forms(client, only_admin):
    c = _admin(client, only_admin)
    r = c.post("/users/create", data={
        "username": "csrfguy",
        "password": "goodpass123",
        "role": "viewer",
        # no csrf_token
    })
    assert r.status_code == 302
    assert "err=" in _loc(r)
    assert "expired" in _loc(r).lower()


def test_cannot_delete_self(client, only_admin):
    c = _admin(client, only_admin)
    r = c.post(f"/users/{only_admin}/delete",
               data={"csrf_token": "tok123"})
    assert r.status_code == 302
    assert "err=" in _loc(r)
    assert "own" in _loc(r)


def test_cannot_delete_last_admin(client, only_admin):
    # Session role says admin, but user_id points elsewhere, so the
    # self-delete check passes and the last-admin guard must fire.
    c = forge(client, user_id=999, username="someone",
              role="admin", csrf="tok123")
    r = c.post(f"/users/{only_admin}/delete",
               data={"csrf_token": "tok123"})
    assert r.status_code == 302
    assert "err=" in _loc(r)
    assert "last admin" in _loc(r)


def test_delete_unknown_user(client, only_admin):
    c = _admin(client, only_admin)
    r = c.post("/users/99999/delete", data={"csrf_token": "tok123"})
    assert r.status_code == 302
    assert "err=" in _loc(r)
    assert "not found" in _loc(r)


def test_create_and_delete_happy_path(client, only_admin, app_module):
    import app
    c = _admin(client, only_admin)

    r = c.post("/users/create", data={
        "username": "tempadmin",
        "password": "goodpass123",
        "role": "admin",
        "csrf_token": "tok123",
    })
    assert r.status_code == 302
    assert "msg=" in _loc(r)

    # Banner shows on the redirect target
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "created" in page

    conn = app.get_db()
    row = conn.execute(
        "SELECT id FROM users WHERE username = 'tempadmin'"
    ).fetchone()
    conn.close()
    assert row is not None

    # Two admins now - deleting the other one is allowed
    r = c.post(f"/users/{row['id']}/delete",
               data={"csrf_token": "tok123"})
    assert r.status_code == 302
    assert "msg=" in r.headers["Location"]

    page = c.get("/users").get_data(as_text=True)
    assert "tempadmin" not in page
