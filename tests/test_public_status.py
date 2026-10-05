"""Phase 8: public status page (/status) - no-login access, state
semantics, staleness, privacy (no target leaks), kill switch,
config validation, entry points (login button + sidebar link).

The /status route is intentionally unauthenticated, so most tests use
the plain anonymous `client`. Everything is sandboxed by conftest.
"""

from datetime import datetime, timedelta, timezone

import pytest

import config


COMPANY_A = "Company A"
COMPANY_B = "Company B"
OUTAGE_IDS = (90001, 90002)


def _stamp_ago(seconds):
    then = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return then.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture()
def status_seed(app_module):
    """Seed helper: one ping row per CHECK_ORDER check.

    Records the ping_results id high-water mark first, so teardown can
    remove exactly the rows this test added (later suites must not see
    them, and earlier suites' rows are never touched).
    """
    conn = app_module.get_db()
    hi = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM ping_results"
    ).fetchone()[0]
    conn.close()

    def seed(company, overrides=None, age_seconds=30):
        """Insert 4 fresh rows. overrides = {check: {"status", "latency"}}."""
        overrides = overrides or {}
        stamp = _stamp_ago(age_seconds)
        conn = app_module.get_db()
        for check in app_module.CHECK_ORDER:
            spec = dict(overrides.get(check, {}))
            status = spec.get("status", "UP")
            latency = spec.get("latency", 5.0 if status == "UP" else None)
            conn.execute(
                "INSERT INTO ping_results "
                "(timestamp, company, target, check_type, status, latency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (stamp, company, "test-target", check, status, latency),
            )
        conn.commit()
        conn.close()

    yield seed

    conn = app_module.get_db()
    conn.execute(
        "DELETE FROM ping_results "
        "WHERE id > ? AND company IN (?, ?)",
        (hi, COMPANY_A, COMPANY_B),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def outage_seed(app_module):
    """Seed one open and/or one closed outage (ids reserved = ours)."""
    conn = app_module.get_db()
    conn.execute(
        "DELETE FROM outages WHERE id IN (?, ?)", OUTAGE_IDS
    )
    conn.commit()
    conn.close()

    def seed_open(company=COMPANY_A, check="Gateway", minutes=10):
        conn = app_module.get_db()
        conn.execute(
            "INSERT INTO outages "
            "(id, company, check_type, started_at, ended_at, duration, cause) "
            "VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
            (OUTAGE_IDS[0], company, check, _stamp_ago(minutes * 60)),
        )
        conn.commit()
        conn.close()

    def seed_closed(company=COMPANY_A, check="DNS"):
        conn = app_module.get_db()
        conn.execute(
            "INSERT INTO outages "
            "(id, company, check_type, started_at, ended_at, duration, cause) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                OUTAGE_IDS[1], company, check,
                _stamp_ago(7200), _stamp_ago(6900), 300.0,
            ),
        )
        conn.commit()
        conn.close()

    yield seed_open, seed_closed

    conn = app_module.get_db()
    conn.execute(
        "DELETE FROM outages WHERE id IN (?, ?)", OUTAGE_IDS
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def rollup_seed(app_module):
    """One hourly rollup for Company A / Gateway: 9 of 10 checks up -> 90%."""
    import time as _time

    hour = _time.strftime(
        "%Y-%m-%dT%H:00:00", _time.gmtime(_time.time() // 3600 * 3600)
    )

    conn = app_module.get_db()
    # Start from nothing for this check so the percentage is exact.
    conn.execute(
        "DELETE FROM rollup_hourly "
        "WHERE company = ? AND check_type = 'Gateway'",
        (COMPANY_A,),
    )
    conn.execute(
        "INSERT INTO rollup_hourly "
        "(company, check_type, hour_utc, total, up_count, "
        " avg_latency, p95_latency, max_latency) "
        "VALUES (?, 'Gateway', ?, 10, 9, 10.0, 15.0, 40.0)",
        (COMPANY_A, hour),
    )
    conn.commit()
    conn.close()

    yield

    conn = app_module.get_db()
    conn.execute(
        "DELETE FROM rollup_hourly "
        "WHERE company = ? AND check_type = 'Gateway' AND hour_utc = ?",
        (COMPANY_A, hour),
    )
    conn.commit()
    conn.close()


# ============================================================
# ACCESS / ROUTING
# ============================================================

def test_overview_renders_without_login(client):
    r = client.get("/status")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "System status" in html
    # No redirect to the login page happened on the way.
    assert "/login" not in r.headers.get("Location", "")


def test_status_sets_no_store_header(client):
    assert client.get("/status").headers["Cache-Control"] == "no-store"
    detail = client.get(
        "/status", query_string={"company": COMPANY_A}
    )
    assert detail.headers["Cache-Control"] == "no-store"


def test_unknown_company_is_404(client):
    r = client.get("/status", query_string={"company": "NopeCorp"})
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "Unknown company" in html
    # The friendly error page still links back to the overview.
    assert 'href="/status"' in html


def test_kill_switch_redirects_to_login(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "PUBLIC_STATUS", False)
    r = client.get("/status")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ============================================================
# OVERVIEW
# ============================================================

def test_overview_lists_public_companies(client, app_module):
    html = client.get("/status").get_data(as_text=True)
    for name in app_module.COMPANIES:
        assert name in html
        # Each company is a link to its detail page.
        assert f"/status?company=" in html


def test_overview_cards_link_to_detail(client):
    html = client.get("/status").get_data(as_text=True)
    assert f"/status?company={COMPANY_A.replace(' ', '+')}" in html or (
        f"/status?company={COMPANY_A.replace(' ', '%20')}" in html
    )


def test_overview_no_targets_leaked(client, app_module):
    html = client.get("/status").get_data(as_text=True)
    for targets in app_module.COMPANIES.values():
        for key in ("gateway", "internet", "dns", "https"):
            value = str(targets[key])
            if value == "auto":
                continue
            assert value not in html, f"leaked target {value}"


# ============================================================
# STATUS SEMANTICS
# ============================================================

def test_fresh_up_rows_show_operational(client, status_seed):
    status_seed(COMPANY_A)
    html = client.get("/status").get_data(as_text=True)
    assert "Operational" in html


def test_one_down_check_shows_partial_outage(client, status_seed):
    status_seed(COMPANY_A, {"Gateway": {"status": "DOWN", "latency": None}})
    html = client.get("/status").get_data(as_text=True)
    assert "Partial outage" in html
    assert "1 check down" in html

    detail = client.get(
        "/status", query_string={"company": COMPANY_A}
    ).get_data(as_text=True)
    assert "Down" in detail
    # A down check has no latency - renders an em dash, not a crash.
    assert "&mdash;" in detail


def test_all_down_checks_show_major_outage(client, status_seed):
    status_seed(
        COMPANY_A,
        {
            check: {"status": "DOWN", "latency": None}
            for check in ("Gateway", "Internet", "DNS", "HTTPS")
        },
    )
    html = client.get("/status").get_data(as_text=True)
    assert "Major outage" in html
    assert "4 checks down" in html


def test_stale_rows_show_unknown_with_banner(client, status_seed):
    # 10 minutes old > STATUS_STALE_SECONDS (180s): monitor not reporting.
    status_seed(COMPANY_A, age_seconds=600)
    html = client.get("/status").get_data(as_text=True)
    assert "Unknown" in html
    assert "has not reported" in html

    detail = client.get(
        "/status", query_string={"company": COMPANY_A}
    ).get_data(as_text=True)
    assert "has not reported" in detail


def test_detail_renders_all_sections(client):
    r = client.get("/status", query_string={"company": COMPANY_A})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for phrase in (
        "Current checks", "Uptime", "Recent incidents",
        "Gateway", "Internet", "DNS", "HTTPS",
        "24 hours", "7 days", "30 days",
    ):
        assert phrase in html, f"missing {phrase!r}"


def test_detail_switcher_lists_every_company(client, app_module):
    html = client.get(
        "/status", query_string={"company": COMPANY_A}
    ).get_data(as_text=True)
    for name in app_module.COMPANIES:
        assert f">{name}</option>" in html


def test_uptime_percent_rendered(client, rollup_seed):
    html = client.get(
        "/status", query_string={"company": COMPANY_A}
    ).get_data(as_text=True)
    # 9 of 10 checks up in the seeded rollup.
    assert "90.0%" in html


# ============================================================
# INCIDENTS
# ============================================================

def test_open_incident_banner_and_overview_note(
    client, outage_seed
):
    seed_open, _ = outage_seed
    seed_open(minutes=10)

    overview = client.get("/status").get_data(as_text=True)
    assert "1 ongoing incident" in overview

    detail = client.get(
        "/status", query_string={"company": COMPANY_A}
    ).get_data(as_text=True)
    assert "Ongoing incident" in detail
    assert ">Ongoing<" in detail


def test_resolved_incident_listed(client, outage_seed):
    _, seed_closed = outage_seed
    seed_closed()

    detail = client.get(
        "/status", query_string={"company": COMPANY_A}
    ).get_data(as_text=True)
    assert ">Resolved<" in detail
    assert "(ended " in detail


def test_detail_no_targets_or_db_paths_leaked(client, app_module):
    html = client.get(
        "/status", query_string={"company": COMPANY_A}
    ).get_data(as_text=True)
    for targets in app_module.COMPANIES.values():
        for key in ("gateway", "internet", "dns", "https"):
            value = str(targets[key])
            if value == "auto":
                continue
            assert value not in html, f"leaked target {value}"
    assert "uptime.db" not in html
    assert "monitor.log" not in html
    assert 'href="/users"' not in html


# ============================================================
# PRIVACY SWITCH / XSS
# ============================================================

def test_private_company_hidden_everywhere(
    client, app_module, monkeypatch
):
    hidden = {**app_module.COMPANIES[COMPANY_B], "public": False}
    monkeypatch.setitem(app_module.COMPANIES, COMPANY_B, hidden)

    html = client.get("/status").get_data(as_text=True)
    assert COMPANY_B not in html

    r = client.get("/status", query_string={"company": COMPANY_B})
    assert r.status_code == 404


def test_company_name_is_escaped(client, app_module, monkeypatch):
    weird = "<script>xss-co"
    monkeypatch.setitem(
        app_module.COMPANIES, weird,
        {
            "gateway": "auto", "internet": "9.9.9.9",
            "dns": "example.org", "https": "https://example.org",
        },
    )
    html = client.get("/status").get_data(as_text=True)
    assert "<script>xss-co" not in html
    assert "&lt;script&gt;xss-co" in html


# ============================================================
# CONFIG VALIDATION
# ============================================================

def _flag_companies():
    return {
        "Flag Co": {
            "gateway": "auto",
            "internet": "1.1.1.1",
            "dns": "cloudflare.com",
            "https": "https://example.com",
        },
    }


def test_validate_rejects_non_bool_public_flag(monkeypatch):
    bad = _flag_companies()
    bad["Flag Co"]["public"] = "yes"
    monkeypatch.setattr(config, "COMPANIES", bad)
    monkeypatch.setattr(config, "SPEEDTEST_COMPANY", "Flag Co")
    with pytest.raises(ValueError, match="public"):
        config.validate_config()


def test_validate_rejects_non_bool_public_status(monkeypatch):
    monkeypatch.setattr(config, "COMPANIES", _flag_companies())
    monkeypatch.setattr(config, "SPEEDTEST_COMPANY", "Flag Co")
    monkeypatch.setattr(config, "PUBLIC_STATUS", "yes")
    with pytest.raises(ValueError, match="PUBLIC_STATUS"):
        config.validate_config()


def test_validate_rejects_small_stale_seconds(monkeypatch):
    monkeypatch.setattr(config, "COMPANIES", _flag_companies())
    monkeypatch.setattr(config, "SPEEDTEST_COMPANY", "Flag Co")
    monkeypatch.setattr(config, "STATUS_STALE_SECONDS", 30)
    with pytest.raises(ValueError, match="STATUS_STALE_SECONDS"):
        config.validate_config()


def test_validate_accepts_defaults():
    config.validate_config()  # shipped config must stay valid


# ============================================================
# ENTRY POINTS
# ============================================================

def test_login_page_has_status_button(client):
    html = client.get("/login").get_data(as_text=True)
    assert 'href="/status"' in html
    assert "View live status" in html


def test_sidebar_status_link_for_all_roles(admin_client, viewer_client):
    for c in (admin_client, viewer_client):
        html = c.get("/dashboard").get_data(as_text=True)
        assert 'href="/status"' in html
    html = admin_client.get("/incidents").get_data(as_text=True)
    assert 'href="/status"' in html
