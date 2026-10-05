"""Backups: snapshot round trip, retention, env knobs, admin API."""

import os
import sqlite3
import time

import pytest

from tests.conftest import forge


def test_backup_round_trip(app_module, tmp_path):
    import app
    import backup

    # Sentinel row so we can prove the copy carries live data.
    conn = app.get_db()
    conn.execute("DELETE FROM ping_results WHERE target = 'sentinel'")
    conn.execute(
        "INSERT INTO ping_results "
        "(timestamp, company, target, check_type, status, latency) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
         "Company A", "sentinel", "Gateway", "UP", 1.0),
    )
    conn.commit()
    conn.close()

    dest = backup.create_backup(target_dir=str(tmp_path), keep=5)
    assert os.path.exists(dest)
    assert os.path.basename(dest).startswith("uptime-")

    check = sqlite3.connect(dest)
    assert check.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0] == "ok"
    assert check.execute(
        "SELECT COUNT(*) FROM ping_results WHERE target = 'sentinel'"
    ).fetchone()[0] == 1
    check.close()


def test_retention_keeps_newest(tmp_path):
    import backup

    names = [f"uptime-2025010{i}-000000.db" for i in range(1, 6)]
    for name in names:
        (tmp_path / name).write_bytes(b"fake")

    removed = backup.prune_backups(str(tmp_path), keep=2)

    assert removed == 3
    left = sorted(p.name for p in tmp_path.glob("uptime-*.db"))
    assert left == [
        "uptime-20250104-000000.db",
        "uptime-20250105-000000.db",
    ]


def test_list_sorted_newest_first(tmp_path):
    import backup

    for i in (1, 2, 3):
        (tmp_path / f"uptime-2025010{i}-000000.db").write_bytes(b"fake")

    listed = backup.list_backups(str(tmp_path))
    assert [b["name"] for b in listed] == [
        "uptime-20250103-000000.db",
        "uptime-20250102-000000.db",
        "uptime-20250101-000000.db",
    ]
    assert all(b["size"] == 4 for b in listed)


def test_env_knobs(monkeypatch, app_module):
    import backup

    monkeypatch.setenv("BACKUP_DIR", os.path.join(os.sep, "tmp-x", "bk"))
    monkeypatch.setenv("BACKUP_KEEP", "3")
    monkeypatch.setenv("BACKUP_INTERVAL", "120")

    assert backup.backup_dir() == os.path.join(os.sep, "tmp-x", "bk")
    assert backup.backup_keep() == 3
    assert backup.backup_interval() == 120

    # Nonsense values fall back to safe defaults
    monkeypatch.setenv("BACKUP_KEEP", "banana")
    assert backup.backup_keep() == 7


def test_backup_api_admin_only(client, app_module):
    import app

    # Anonymous -> 401
    assert client.post("/api/backup").status_code == 401
    assert client.get("/api/backups").status_code == 401

    # Viewer -> 403
    c = forge(client, user_id=2, username="viewer1",
              role="viewer", csrf="tok123")
    assert c.post(
        "/api/backup", data={"csrf_token": "tok123"}
    ).status_code == 403

    # Admin -> 200 + file listed
    c = forge(client, user_id=1, username="admin",
              role="admin", csrf="tok123")
    r = c.post("/api/backup", data={"csrf_token": "tok123"})
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["ok"] is True
    assert payload["size"] > 0

    listing = c.get("/api/backups").get_json()
    assert listing["keep"] >= 1
    assert any(b["name"] == payload["file"] for b in listing["backups"])


def test_backup_api_requires_csrf(client, app_module):
    c = forge(client, user_id=1, username="admin",
              role="admin", csrf="tok123")
    r = c.post("/api/backup")               # no token
    assert r.status_code == 400
    assert "csrf" in r.get_json()["error"].lower()
