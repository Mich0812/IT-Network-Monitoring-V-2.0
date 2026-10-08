"""ADMIN_PASSWORD env must reset/sync the admin password on startup.

Bug: initialize_database() only honors ADMIN_USERNAME/ADMIN_PASSWORD when
the users table is empty. In a container (Railway) with a persisted
uptime.db, setting ADMIN_PASSWORD later does nothing -> "Login not working".
"""

import os


def test_admin_password_env_resets_existing_admin(app_module, client):
    import app as app_mod
    from werkzeug.security import generate_password_hash
    from tests.conftest import real_login

    conn = app_mod.get_db()
    conn.execute("DELETE FROM users WHERE username NOT IN ('admin')")
    conn.execute(
        "UPDATE users SET password = ? WHERE username = 'admin'",
        (generate_password_hash("old-pass-xyz"),),
    )
    conn.commit()
    conn.close()

    old_user = os.environ.get("ADMIN_USERNAME", "admin")
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "new-pass-abc-123"
    try:
        app_mod.initialize_database()
    finally:
        pass

    try:
        r = real_login(client, "admin", "new-pass-abc-123")
        assert r.status_code == 302, (
            f"login with ADMIN_PASSWORD failed: {r.status_code} "
            f"{r.get_data(as_text=True)[:300]}"
        )
        assert "/dashboard" in r.headers["Location"]
    finally:
        os.environ["ADMIN_PASSWORD"] = "admin-pass-123"
        os.environ["ADMIN_USERNAME"] = old_user
        # restore conftest password
        conn = app_mod.get_db()
        conn.execute(
            "UPDATE users SET password = ? WHERE username = 'admin'",
            (generate_password_hash("admin-pass-123"),),
        )
        conn.commit()
        conn.close()
