"""Phase 12 - monitoring features.

1. Alerts admin page (/alerts) + the Send-test-alert button.
2. Speedtest failure recording: status column, failed rows, and the
   chart/recent endpoints staying clean of them.
3. Maintenance banner on the public /status page (config.MAINTENANCE).

All database writes happen in the scratch DB (conftest sets DB_PATH
before import) and every row inserted here is deleted again so sibling
suites keep seeing a clean table.
"""

import os
import time

from tests.conftest import forge

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _conn(app_module):
    return app_module.get_db()


def _cleanup(conn, sql, params=()):
    conn.execute(sql, params)
    conn.commit()


# ------------------------------------------------------------------
# 1. Alerts admin page
# ------------------------------------------------------------------

def test_alerts_page_redirects_anonymous_to_login(app_module, client):
    r = client.get("/alerts")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_alerts_page_forbidden_for_viewer(app_module, client):
    forge(client, user_id=2, username="viewer1", role="viewer")
    assert client.get("/alerts").status_code == 403


def test_alerts_page_renders_for_admin(app_module, admin_client):
    html = admin_client.get("/alerts").get_data(as_text=True)
    assert "<h1>Alerts</h1>" in html
    assert 'id="testAlertBtn"' in html
    assert "Send test alert" in html
    assert "Email (SMTP)" in html
    assert "Webhook (Teams/Slack)" in html
    assert "Maintenance windows" in html
    assert "Recent alerts" in html
    # sidebar shows the page as active for admins
    assert 'href="/alerts" class="nav-item active"' in html


def test_alerts_nav_is_admin_only_in_sidebars(app_module, viewer_client):
    for path in ("/overview", "/dashboard"):
        html = viewer_client.get(path).get_data(as_text=True)
        assert 'href="/alerts"' not in html, path


def test_alerts_page_shows_configured_maintenance(app_module, admin_client,
                                                   monkeypatch):
    monkeypatch.setattr(
        app_module, "MAINTENANCE",
        [{"company": "Company A", "days": "all",
          "start": "02:00", "end": "03:00"}],
    )
    html = admin_client.get("/alerts").get_data(as_text=True)
    assert "Company A" in html
    assert "02:00 - 03:00" in html


def test_test_alert_endpoint_ok_for_admin(admin_client):
    r = admin_client.post("/api/alerts/test")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["ok"] is True
    assert set(payload["channels"]) == {"email", "webhook"}
    assert isinstance(payload["delivered"], bool)


def test_test_alert_endpoint_rejects_others(app_module, client):
    assert client.post("/api/alerts/test").status_code == 401
    forge(client, user_id=2, username="viewer1", role="viewer")
    assert client.post("/api/alerts/test").status_code == 403


# ------------------------------------------------------------------
# 2. Speedtest failure recording
# ------------------------------------------------------------------

def test_speedtest_status_column_exists(app_module):
    conn = _conn(app_module)
    cols = [r["name"] for r in
            conn.execute("PRAGMA table_info(speedtest_results)")]
    conn.close()
    assert "status" in cols, "speedtest_results needs the status column"


def test_save_speedtest_failure_records_row(app_module):
    import uptime_checker as uc

    conn = _conn(app_module)
    uc.save_speedtest_failure("unit-test reason marker")
    row = conn.execute(
        "SELECT * FROM speedtest_results "
        "WHERE status = 'failed' AND server LIKE '%unit-test reason marker%'"
    ).fetchone()
    assert row is not None, "failed speedtest must land in the database"
    assert row["download"] is None
    assert row["upload"] is None
    assert row["server"].startswith("FAILED: ")
    assert row["company"] == uc.SPEEDTEST_COMPANY
    _cleanup(
        conn,
        "DELETE FROM speedtest_results "
        "WHERE server LIKE '%unit-test reason marker%'",
    )
    conn.close()


def test_run_speedtest_records_failure(app_module, monkeypatch):
    import subprocess
    import uptime_checker as uc

    class Failed:
        returncode = 1
        stderr = "network unreachable"
        stdout = ""

    # Point at an existing file so the binary-existence guard passes,
    # and stub the subprocess so no real speedtest ever runs.
    monkeypatch.setattr(uc, "SPEEDTEST_EXE", os.path.abspath(__file__))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())

    assert uc.run_speedtest() is False

    conn = _conn(app_module)
    row = conn.execute(
        "SELECT * FROM speedtest_results "
        "WHERE status = 'failed' AND server LIKE '%exit 1%' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "a failed run must be recorded"
    assert row["download"] is None
    _cleanup(
        conn,
        "DELETE FROM speedtest_results WHERE server LIKE '%exit 1%'",
    )
    conn.close()


def test_speedtest_chart_excludes_failed_rows(app_module, admin_client):
    conn = _conn(app_module)
    company = app_module.SPEEDTEST_COMPANY
    conn.execute(
        "INSERT INTO speedtest_results "
        "(timestamp, company, download, upload, ping, server, status) "
        "VALUES (datetime('now'), ?, 100.0, 20.0, 5.0, 'UNIT-OK', 'ok')",
        (company,),
    )
    conn.execute(
        "INSERT INTO speedtest_results "
        "(timestamp, company, download, upload, ping, server, status) "
        "VALUES (datetime('now'), ?, NULL, NULL, NULL, "
        "'FAILED: unit chart', 'failed')",
        (company,),
    )
    conn.commit()
    try:
        data = admin_client.get("/api/speedtest?hours=1").get_json()
        assert data["download"] == [100.0], (
            "failed rows must not leak into the chart series"
        )
        assert "UNIT-OK" not in str(data)  # server not echoed anyway
    finally:
        _cleanup(
            conn,
            "DELETE FROM speedtest_results "
            "WHERE server IN ('UNIT-OK') OR server LIKE '%unit chart%'",
        )
        conn.close()


def test_recent_speedtest_reports_last_failure(app_module, admin_client):
    conn = _conn(app_module)
    company = app_module.SPEEDTEST_COMPANY
    conn.execute(
        "INSERT INTO speedtest_results "
        "(timestamp, company, download, upload, ping, server, status) "
        "VALUES (datetime('now'), ?, NULL, NULL, NULL, "
        "'FAILED: unit recent', 'failed')",
        (company,),
    )
    conn.commit()
    try:
        data = admin_client.get("/api/speedtest/recent").get_json()
        assert data["last_failure"] is not None
        assert "unit recent" in data["last_failure"]["reason"]
        assert all(
            "FAILED" not in (r["server"] or "")
            for r in data["results"]
        ), "failed rows stay out of the results list"

        # Once a later run succeeds, the failure is history again.
        conn.execute(
            "INSERT INTO speedtest_results "
            "(timestamp, company, download, upload, ping, server, status) "
            "VALUES (datetime('now'), ?, 50.0, 10.0, 4.0, 'UNIT-OK2', 'ok')",
            (company,),
        )
        conn.commit()
        data = admin_client.get("/api/speedtest/recent").get_json()
        assert data["last_failure"] is None
        assert data["results"], "the ok row must show up"
    finally:
        _cleanup(
            conn,
            "DELETE FROM speedtest_results "
            "WHERE server IN ('UNIT-OK2') OR server LIKE '%unit recent%'",
        )
        conn.close()


# ------------------------------------------------------------------
# 3. Maintenance banner on /status
# ------------------------------------------------------------------

def _covering_window(company=None):
    """A window that certainly contains the current local minute."""
    now = time.time()
    return {
        "company": company,
        "days": "all",
        "start": time.strftime("%H:%M", time.localtime(now - 60)),
        "end": time.strftime("%H:%M", time.localtime(now + 60)),
    }


def test_status_overview_shows_maintenance_banner(app_module, client,
                                                  monkeypatch):
    import uptime_checker as uc

    monkeypatch.setattr(uc, "MAINTENANCE", [_covering_window()])
    html = client.get("/status").get_data(as_text=True)
    assert "Scheduled maintenance" in html
    assert "pub-banner-info" in html


def test_status_detail_shows_maintenance_banner(app_module, client,
                                                monkeypatch):
    import uptime_checker as uc

    pubs = app_module._public_companies()
    assert pubs, "config must publish at least one company"
    monkeypatch.setattr(uc, "MAINTENANCE", [_covering_window()])
    html = client.get(
        "/status?company=%s" % pubs[0]
    ).get_data(as_text=True)
    assert "Scheduled maintenance" in html
    assert pubs[0] in html


def test_status_without_window_has_no_banner(app_module, client,
                                              monkeypatch):
    import uptime_checker as uc

    monkeypatch.setattr(uc, "MAINTENANCE", [])
    html = client.get("/status").get_data(as_text=True)
    assert "Scheduled maintenance" not in html


def test_status_survives_broken_maintenance_config(app_module, client,
                                                   monkeypatch):
    import uptime_checker as uc

    # days="bogus" makes the window logic raise (0 in "bogus" is a
    # TypeError) - the banner helper guards per company, so the
    # public page must still answer 200.
    monkeypatch.setattr(
        uc, "MAINTENANCE",
        [{"company": "Company A", "days": "bogus",
          "start": "00:00", "end": "23:59"}],
    )
    r = client.get("/status")
    assert r.status_code == 200
    assert "Scheduled maintenance" not in r.get_data(as_text=True)
