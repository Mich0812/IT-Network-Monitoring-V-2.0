"""Phase 6: incident grouping API, alert-history filters, incidents page.

Grouping is pure logic (app.group_outages), so most edge cases are
tested without the database; the rest go through the HTTP API with
seeded outage rows - always for a single company, cleaned up after.
"""

import time

import pytest

from tests.conftest import forge

COMPANY = "Company A"

# One frozen timestamp base for the whole module: every ts() below
# derives from it, so exact duration math in assertions can't drift
# when two calls straddle a wall-clock second boundary.
_EPOCH = int(time.time())


def ts(offset_sec, base=None):
    """UTC timestamp string `offset_sec` seconds before `base`."""
    b = _EPOCH if base is None else base
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(b - offset_sec))


def row(oid, company, check_type, start, end=None, duration=None, cause=None):
    """One outage row shaped exactly like the SQL select returns."""
    return {
        "id": oid,
        "company": company,
        "check_type": check_type,
        "started_at": start,
        "ended_at": end,
        "duration": duration,
        "cause": cause,
    }


# ============================================================
# group_outages() - pure grouping logic
# ============================================================

def test_overlapping_outages_group_into_one_incident():
    import app

    rows = [
        row(1, COMPANY, "Gateway", ts(3900), ts(3600), 300),
        row(2, COMPANY, "Internet", ts(3800), ts(3500), 300),
    ]
    incidents = app.group_outages(rows)

    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["checks"] == ["Gateway", "Internet"]
    assert inc["outage_count"] == 2
    assert inc["open"] is False
    assert inc["ended_at"] is not None


def test_gap_within_two_minutes_stays_one_incident():
    import app

    # First outage ends at t-3600, next starts at t-3540: 60s gap.
    rows = [
        row(1, COMPANY, "Gateway", ts(3900), ts(3600), 300),
        row(2, COMPANY, "DNS", ts(3540), ts(3480), 60),
    ]
    incidents = app.group_outages(rows)

    assert len(incidents) == 1
    assert incidents[0]["checks"] == ["Gateway", "DNS"]


def test_gap_over_two_minutes_splits_incidents():
    import app

    # First ends t-3600, next starts t-3400: 200s gap > 120s.
    rows = [
        row(1, COMPANY, "Gateway", ts(3900), ts(3600), 300),
        row(2, COMPANY, "HTTPS", ts(3400), ts(3340), 60),
    ]
    incidents = app.group_outages(rows)

    assert len(incidents) == 2
    assert incidents[0]["checks"] == ["HTTPS"]     # newest first
    assert incidents[1]["checks"] == ["Gateway"]


def test_companies_never_group_together():
    import app

    rows = [
        row(1, COMPANY, "Gateway", ts(3900), ts(3600), 300),
        row(2, "Company B", "Gateway", ts(3850), ts(3550), 300),
    ]
    incidents = app.group_outages(rows)

    assert len(incidents) == 2
    assert {i["company"] for i in incidents} == {COMPANY, "Company B"}


def test_root_cause_follows_dependency_chain():
    import app

    # Gateway + DNS -> gateway is the upstream cause.
    rows = [
        row(1, COMPANY, "DNS", ts(3900), ts(3700), 200),
        row(2, COMPANY, "Gateway", ts(3890), ts(3690), 200),
    ]
    assert app.group_outages(rows)[0]["root_cause"] == "Gateway"

    # DNS + HTTPS, no gateway -> DNS.
    rows = [
        row(1, COMPANY, "HTTPS", ts(3900), ts(3700), 200),
        row(2, COMPANY, "DNS", ts(3890), ts(3690), 200),
    ]
    assert app.group_outages(rows)[0]["root_cause"] == "DNS"

    # HTTPS alone -> HTTPS.
    rows = [row(1, COMPANY, "HTTPS", ts(3900), ts(3700), 200)]
    assert app.group_outages(rows)[0]["root_cause"] == "HTTPS"


def test_checks_are_deduped_and_chain_ordered():
    import app

    rows = [
        row(1, COMPANY, "Internet", ts(3900), ts(3600), 300),
        row(2, COMPANY, "Gateway", ts(3890), ts(3590), 300),
        row(3, COMPANY, "Internet", ts(3550), ts(3500), 50),
    ]
    inc = app.group_outages(rows)[0]

    assert inc["checks"] == ["Gateway", "Internet"]   # chain order
    assert inc["outage_count"] == 3


def test_open_outage_marks_incident_open():
    import app

    rows = [
        row(1, COMPANY, "Gateway", ts(1800), None, None),
        row(2, COMPANY, "HTTPS", ts(1750), None, None),
    ]
    inc = app.group_outages(rows)[0]

    assert inc["open"] is True
    assert inc["ended_at"] is None
    # Span runs from first start to *now* (>= 30 min), not fixed.
    assert inc["duration"] >= 1800
    # Downtime sums both members up to now: ~1800 + ~1750 s.
    assert inc["downtime"] >= 3500


def test_duration_and_downtime_math():
    import app

    rows = [
        row(1, COMPANY, "Gateway", ts(4800), ts(4500), 300),   # 5 min
        row(2, COMPANY, "DNS", ts(4700), ts(4580), 120),       # 2 min
    ]
    inc = app.group_outages(rows)[0]

    # Span: first start t-80m to latest end t-75m = 300 s.
    assert inc["duration"] == 300.0
    # Downtime: 300 + 120 (overlapping members each count fully).
    assert inc["downtime"] == 420.0


def test_empty_input_returns_empty_list():
    import app

    assert app.group_outages([]) == []


# ============================================================
# /api/incidents - HTTP API
# ============================================================

@pytest.fixture(autouse=True)
def seeded(app_module):
    """Two incidents for Company A: a 3-outage chain and a lone HTTPS blip.

    Incident 1 (closed): Gateway t-40m..t-35m, Internet t-38m..t-33m,
    DNS t-34m..t-32m  -> one group (overlapping / <=120s gaps).
    Incident 2 (closed): HTTPS t-10m..t-9m -> 22 min gap, separate.
    """
    import app

    conn = app.get_db()
    conn.execute("DELETE FROM outages WHERE company = ?", (COMPANY,))
    conn.execute("DELETE FROM notifications WHERE company = ?", (COMPANY,))
    conn.executemany(
        "INSERT INTO outages "
        "(id, company, check_type, started_at, ended_at, duration, cause) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (101, COMPANY, "Gateway", ts(2400), ts(2100), 300.0, None),
            (102, COMPANY, "Internet", ts(2280), ts(1980), 300.0, None),
            (103, COMPANY, "DNS", ts(2040), ts(1920), 120.0, None),
            (104, COMPANY, "HTTPS", ts(600), ts(540), 60.0, None),
        ],
    )
    conn.commit()
    conn.close()

    yield

    conn = app.get_db()
    conn.execute("DELETE FROM outages WHERE company = ?", (COMPANY,))
    conn.execute("DELETE FROM notifications WHERE company = ?", (COMPANY,))
    # The company-filter test also inserts a Company B notification.
    conn.execute(
        "DELETE FROM notifications "
        "WHERE company = 'Company B' AND message = 'dns down'"
    )
    conn.commit()
    conn.close()


def test_incidents_requires_login(client):
    r = client.get("/api/incidents")
    assert r.status_code == 401


def test_incidents_groups_seeded_outages(admin_client, seeded):
    r = admin_client.get(
        "/api/incidents", query_string={"company": COMPANY}
    )
    assert r.status_code == 200
    data = r.get_json()

    assert data["company"] == COMPANY
    assert data["gap_seconds"] == 120
    assert data["count"] == 2

    # Newest first: the t-10m HTTPS blip leads, the t-40m chain follows.
    blip, chain = data["incidents"][0], data["incidents"][1]

    assert blip["started_at"] == ts(600)
    assert blip["checks"] == ["HTTPS"]
    assert blip["root_cause"] == "HTTPS"
    assert blip["open"] is False

    assert chain["checks"] == ["Gateway", "Internet", "DNS"]
    assert chain["root_cause"] == "Gateway"
    assert chain["outage_count"] == 3
    # Span t-40m..t-32m = 480 s; downtime 300+300+120 = 720 s.
    assert chain["duration"] == 480.0
    assert chain["downtime"] == 720.0
    assert chain["id"] == 101              # earliest member outage id


def test_incidents_open_filter(admin_client, seeded):
    import app

    conn = app.get_db()
    conn.execute(
        "INSERT INTO outages "
        "(company, check_type, started_at) VALUES (?, ?, ?)",
        (COMPANY, "Gateway", ts(200)),
    )
    conn.commit()
    conn.close()

    r = admin_client.get(
        "/api/incidents",
        query_string={"company": COMPANY, "open": "1"},
    )
    assert r.status_code == 200
    data = r.get_json()

    assert data["count"] == 1
    inc = data["incidents"][0]
    assert inc["open"] is True
    assert inc["ended_at"] is None
    assert inc["duration"] >= 200


def test_incidents_all_companies_mode(admin_client, seeded):
    r = admin_client.get("/api/incidents", query_string={"company": "all"})
    assert r.status_code == 200
    data = r.get_json()
    assert any(i["company"] == COMPANY for i in data["incidents"])


def test_incidents_unknown_company_400(admin_client):
    r = admin_client.get(
        "/api/incidents", query_string={"company": "No Such Corp"}
    )
    assert r.status_code == 400


def test_incidents_invalid_hours_falls_back_to_default(admin_client, seeded):
    r = admin_client.get(
        "/api/incidents",
        query_string={"company": COMPANY, "hours": 999},
    )
    assert r.status_code == 200
    assert r.get_json()["hours"] == 24


def test_incidents_hours_window_filters(admin_client, seeded):
    # All seeded outages started >= 10 min ago, so a 1h window keeps
    # both; the grouped result must be stable inside the window.
    r = admin_client.get(
        "/api/incidents",
        query_string={"company": COMPANY, "hours": 1},
    )
    assert r.status_code == 200
    assert r.get_json()["count"] == 2


# ============================================================
# /api/notifications - new company/hours filters
# ============================================================

def test_notifications_requires_login(client):
    r = client.get("/api/notifications")
    assert r.status_code == 401


def test_notifications_company_filter(admin_client, seeded):
    import app

    conn = app.get_db()
    conn.execute(
        "INSERT INTO notifications "
        "(company, check_type, kind, message, channel, sent_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (COMPANY, "Gateway", "down", "gw down", "email", ts(300), "sent"),
    )
    conn.execute(
        "INSERT INTO notifications "
        "(company, check_type, kind, message, channel, sent_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Company B", "DNS", "down", "dns down", "email", ts(300), "sent"),
    )
    conn.commit()
    conn.close()

    r = admin_client.get(
        "/api/notifications", query_string={"company": COMPANY}
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["count"] >= 1
    assert all(n["company"] == COMPANY for n in data["notifications"])


def test_notifications_hours_filter(admin_client, seeded):
    import app

    conn = app.get_db()
    # Recent + stale alerts for the same company.
    conn.execute(
        "INSERT INTO notifications "
        "(company, check_type, kind, message, channel, sent_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (COMPANY, "Gateway", "down", "recent", "email", ts(7200), "sent"),
    )
    conn.execute(
        "INSERT INTO notifications "
        "(company, check_type, kind, message, channel, sent_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (COMPANY, "Gateway", "down", "stale", "email", ts(90000), "sent"),
    )
    conn.commit()
    conn.close()

    r = admin_client.get(
        "/api/notifications",
        query_string={"company": COMPANY, "hours": 24, "limit": 200},
    )
    assert r.status_code == 200
    messages = [n["message"] for n in r.get_json()["notifications"]]
    assert "recent" in messages
    assert "stale" not in messages


def test_notifications_unknown_company_400(admin_client):
    r = admin_client.get(
        "/api/notifications", query_string={"company": "No Such Corp"}
    )
    assert r.status_code == 400


def test_notifications_bad_hours_400(admin_client):
    r = admin_client.get(
        "/api/notifications", query_string={"hours": 99999}
    )
    assert r.status_code == 400


def test_notifications_bad_limit_400(admin_client):
    """A malformed limit must be a clean 400, not a 500 (regression:
    the incidents page once built a double-? query string)."""
    r = admin_client.get(
        "/api/notifications", query_string={"limit": "100?company=Company A"}
    )
    assert r.status_code == 400


def test_notifications_without_filters_still_works(admin_client, seeded):
    """Backwards compatibility: dashboard toasts call ?limit=10 only."""
    r = admin_client.get("/api/notifications", query_string={"limit": 10})
    assert r.status_code == 200
    assert "notifications" in r.get_json()


# ============================================================
# /incidents page
# ============================================================

def test_incidents_page_requires_login(client):
    r = client.get("/incidents")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_incidents_page_renders(admin_client):
    r = admin_client.get("/incidents")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Incidents" in html
    assert "/api/incidents" in html
    assert "/api/notifications" in html


def test_sidebars_link_to_incidents(admin_client):
    for path in ("/dashboard", "/overview", "/incidents", "/reports"):
        html = admin_client.get(path).get_data(as_text=True)
        assert 'href="/incidents"' in html, f"no Incidents link in {path}"


def test_dashboard_uses_grouped_incidents_api(admin_client):
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert "/api/incidents?hours=24" in html
    assert "/api/outages" not in html
