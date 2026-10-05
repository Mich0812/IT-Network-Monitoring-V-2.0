"""
Phase 7 tests: remote-site agent.

Covers the ping_results.source migration, agent_tokens, the
token-authenticated /api/ingest endpoint (auth, validation, state
machine + alert integration), the /agents admin page (RBAC, CSRF,
one-time token display, rotation, revocation), agent staleness in
/api/status, config validation for managed_by, the local-polling
skip, and agent.py's pure helpers.

All tests run in the sandboxed DB created by conftest.py - they can
never touch the real uptime.db.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


# ============================================================
# FIXTURES
# ============================================================

AGENT_NAME = "Agent Test Co"
AGENT_TOKEN = "tok-agent-test-1234567890"


def _valid_targets(**extra):
    targets = {
        "gateway": "10.255.255.1",
        "internet": "9.9.9.9",
        "dns": "agent.test",
        "https": "https://agent.test",
    }
    targets.update(extra)
    return targets


@pytest.fixture()
def agent_company(app_module, monkeypatch):
    """Register a temporary agent-managed company, clean up after."""
    import uptime_checker as uc

    monkeypatch.setitem(
        app_module.COMPANIES, AGENT_NAME, _valid_targets(managed_by="agent")
    )
    yield AGENT_NAME

    conn = app_module.get_db()
    for table in ("ping_results", "outages", "notifications"):
        conn.execute(f"DELETE FROM {table} WHERE company = ?", (AGENT_NAME,))
    conn.execute(
        "DELETE FROM agent_tokens WHERE company = ?", (AGENT_NAME,)
    )
    conn.commit()
    conn.close()

    for check_type in ("Gateway", "Internet", "DNS", "HTTPS"):
        uc._check_state.pop((AGENT_NAME, check_type), None)
        uc._open_outage_id.pop((AGENT_NAME, check_type), None)


def make_token(app_module, company, token=AGENT_TOKEN):
    """Insert an agent token directly (bypasses the UI on purpose)."""
    conn = app_module.get_db()
    conn.execute(
        "DELETE FROM agent_tokens WHERE company = ?", (company,)
    )
    conn.execute(
        "INSERT INTO agent_tokens (company, token_hash, created_at) "
        "VALUES (?, ?, ?)",
        (company, app_module._agent_token_hash(token),
         app_module.utc_now_str()),
    )
    conn.commit()
    conn.close()
    return token


def ingest(client, results, token=AGENT_TOKEN):
    return client.post(
        "/api/ingest",
        data=json.dumps({"results": results}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}"},
    )


def sample_results(**overrides):
    base = [
        {"check_type": "Gateway", "target": "10.255.255.1",
         "status": "UP", "latency": 2.5},
        {"check_type": "Internet", "target": "9.9.9.9",
         "status": "UP", "latency": 12.0},
        {"check_type": "DNS", "target": "agent.test",
         "status": "UP", "latency": 3.0},
        {"check_type": "HTTPS", "target": "https://agent.test",
         "status": "UP", "latency": 45.0},
    ]
    for index, patch in overrides.items():
        base[int(index)].update(patch)
    return base


# ============================================================
# SCHEMA MIGRATION
# ============================================================

def test_ping_results_has_source_column(app_module):
    conn = app_module.get_db()
    columns = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(ping_results)")
    ]
    assert "source" in columns
    conn.close()


def test_source_defaults_to_local(app_module):
    conn = app_module.get_db()
    conn.execute(
        "INSERT INTO ping_results "
        "(timestamp, company, target, check_type, status, latency) "
        "VALUES (?, 'Source Default Co', 'x', 'Gateway', 'UP', 1.0)",
        (app_module.utc_now_str(),),
    )
    row = conn.execute(
        "SELECT source FROM ping_results "
        "WHERE company = 'Source Default Co'"
    ).fetchone()
    conn.execute(
        "DELETE FROM ping_results WHERE company = 'Source Default Co'"
    )
    conn.commit()
    conn.close()
    assert row["source"] == "local"


def test_agent_tokens_table_exists(app_module):
    conn = app_module.get_db()
    columns = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(agent_tokens)")
    ]
    conn.close()
    assert {"id", "company", "token_hash", "created_at", "last_seen"} <= set(
        columns
    )


# ============================================================
# /api/ingest - AUTHENTICATION
# ============================================================

def test_ingest_requires_bearer(client):
    r = client.post("/api/ingest", json={"results": sample_results()})
    assert r.status_code == 401


def test_ingest_rejects_bad_token(client, agent_company):
    r = ingest(client, sample_results(), token="wrong-token")
    assert r.status_code == 401
    assert "Invalid token" in r.get_json()["error"]


def test_ingest_rejects_empty_bearer(client, agent_company):
    r = client.post(
        "/api/ingest",
        json={"results": sample_results()},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


# ============================================================
# /api/ingest - VALIDATION
# ============================================================

def test_ingest_requires_results_list(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = client.post(
        "/api/ingest",
        json={"nope": 1},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    )
    assert r.status_code == 400


def test_ingest_rejects_empty_results(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(client, [])
    assert r.status_code == 400


def test_ingest_rejects_too_many_results(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(client, sample_results() * 6)  # 24 > MAX_INGEST_RESULTS
    assert r.status_code == 400
    assert "Too many" in r.get_json()["error"]


def test_ingest_rejects_bad_check_type(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(client, sample_results(**{"0": {"check_type": "Printers"}}))
    assert r.status_code == 400
    assert "check_type" in r.get_json()["error"]


def test_ingest_rejects_bad_status(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(client, sample_results(**{"1": {"status": "MAYBE"}}))
    assert r.status_code == 400
    assert "status" in r.get_json()["error"]


def test_ingest_rejects_bad_latency(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(client, sample_results(**{"2": {"latency": "fast"}}))
    assert r.status_code == 400
    assert "latency" in r.get_json()["error"]


def test_ingest_rejects_empty_target(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(client, sample_results(**{"3": {"target": "  "}}))
    assert r.status_code == 400
    assert "target" in r.get_json()["error"]


def test_ingest_rejects_bad_timestamp(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(
        client, sample_results(**{"0": {"timestamp": "yesterday"}})
    )
    assert r.status_code == 400
    assert "timestamp" in r.get_json()["error"]


def test_ingest_409_for_local_company(client, app_module):
    """A token for a locally polled company must not double-count."""
    from config import COMPANIES as CFG

    token = make_token(app_module, "Company A")
    try:
        # Company A is NOT agent-managed in the test config.
        assert CFG["Company A"].get("managed_by", "local") != "agent"
        r = ingest(client, sample_results(), token=token)
        assert r.status_code == 409
        assert "agent-managed" in r.get_json()["error"]
    finally:
        conn = app_module.get_db()
        conn.execute(
            "DELETE FROM agent_tokens WHERE company = 'Company A'"
        )
        conn.commit()
        conn.close()


def test_ingest_409_for_removed_company(client, agent_company,
                                        app_module, monkeypatch):
    token = make_token(app_module, agent_company)
    monkeypatch.delitem(app_module.COMPANIES, agent_company)
    r = ingest(client, sample_results(), token=token)
    assert r.status_code == 409
    assert "no longer configured" in r.get_json()["error"]


def test_ingest_writes_nothing_on_invalid_payload(
    client, agent_company, app_module
):
    """Validation happens before any row is written."""
    make_token(app_module, agent_company)
    bad = sample_results()
    bad[2]["latency"] = "oops"
    r = ingest(client, bad)
    assert r.status_code == 400

    conn = app_module.get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM ping_results WHERE company = ?",
        (agent_company,),
    ).fetchone()["n"]
    conn.close()
    assert n == 0


# ============================================================
# /api/ingest - HAPPY PATH
# ============================================================

def test_ingest_happy_path(client, agent_company, app_module):
    make_token(app_module, agent_company)
    r = ingest(client, sample_results())
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["company"] == agent_company
    assert data["accepted"] == 4

    conn = app_module.get_db()
    rows = conn.execute(
        "SELECT check_type, status, source FROM ping_results "
        "WHERE company = ? ORDER BY id",
        (agent_company,),
    ).fetchall()
    last_seen = conn.execute(
        "SELECT last_seen FROM agent_tokens WHERE company = ?",
        (agent_company,),
    ).fetchone()["last_seen"]
    conn.close()

    assert len(rows) == 4
    assert all(row["source"] == "agent" for row in rows)
    assert [row["check_type"] for row in rows] == [
        "Gateway", "Internet", "DNS", "HTTPS",
    ]
    assert last_seen is not None


def test_ingest_company_comes_from_token_not_payload(
    client, agent_company, app_module
):
    """A pushed payload can never write into another company's data."""
    make_token(app_module, agent_company)
    r = client.post(
        "/api/ingest",
        data=json.dumps({
            "company": "Company A",
            "results": sample_results(),
        }),
        content_type="application/json",
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    )
    assert r.status_code == 200
    assert r.get_json()["company"] == agent_company

    conn = app_module.get_db()
    others = conn.execute(
        "SELECT COUNT(*) AS n FROM ping_results "
        "WHERE company = 'Company A' AND source = 'agent'"
    ).fetchone()["n"]
    conn.close()
    assert others == 0


def test_ingest_honours_supplied_timestamp(
    client, agent_company, app_module
):
    make_token(app_module, agent_company)
    when = "2026-01-02 03:04:05"
    r = ingest(
        client, sample_results(**{"0": {"timestamp": when}})
    )
    assert r.status_code == 200

    conn = app_module.get_db()
    row = conn.execute(
        "SELECT timestamp FROM ping_results "
        "WHERE company = ? AND check_type = 'Gateway'",
        (agent_company,),
    ).fetchone()
    conn.close()
    assert row["timestamp"] == when


def test_ingest_defaults_timestamp_to_server_now(
    client, agent_company, app_module
):
    make_token(app_module, agent_company)
    before = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    r = ingest(client, sample_results())
    assert r.status_code == 200
    after = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = app_module.get_db()
    row = conn.execute(
        "SELECT timestamp FROM ping_results WHERE company = ?",
        (agent_company,),
    ).fetchone()
    conn.close()
    assert before <= row["timestamp"] <= after


# ============================================================
# /api/ingest - STATE MACHINE + ALERTS INTEGRATION
# ============================================================

def test_ingest_down_opens_outage_up_closes_it(
    client, agent_company, app_module
):
    """Agent results run through the same outage state machine."""
    import uptime_checker as uc

    make_token(app_module, agent_company)

    down = sample_results(**{"0": {"status": "DOWN", "latency": None}})
    assert ingest(client, down).status_code == 200

    conn = app_module.get_db()
    open_outage = conn.execute(
        "SELECT id FROM outages WHERE company = ? "
        "AND check_type = 'Gateway' AND ended_at IS NULL",
        (agent_company,),
    ).fetchone()
    conn.close()
    assert open_outage is not None, "DOWN should open an outage"

    # Second DOWN feeds the flap guard (down_failures=2) -> alert row.
    assert ingest(client, down).status_code == 200

    conn = app_module.get_db()
    alerts = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE company = ?",
        (agent_company,),
    ).fetchone()["n"]
    conn.close()
    assert alerts >= 1, "2nd consecutive DOWN should raise an alert"

    up = sample_results()
    assert ingest(client, up).status_code == 200

    conn = app_module.get_db()
    closed = conn.execute(
        "SELECT ended_at FROM outages WHERE id = ?",
        (open_outage["id"],),
    ).fetchone()
    conn.close()
    assert closed["ended_at"] is not None, "UP should close the outage"

    # Leave no state behind for other tests.
    uc._check_state.pop((agent_company, "Gateway"), None)
    uc._open_outage_id.pop((agent_company, "Gateway"), None)


# ============================================================
# /agents ADMIN PAGE
# ============================================================

def test_agents_page_requires_login(client):
    r = client.get("/agents")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_agents_page_forbidden_for_viewer(viewer_client):
    r = viewer_client.get("/agents")
    assert r.status_code == 403


def test_agents_page_renders_for_admin(admin_client):
    r = admin_client.get("/agents")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Company tokens" in html
    assert "Company A" in html
    assert "Company B" in html
    assert 'action="/agents/Company A/token"' in html


def test_agents_link_in_admin_sidebars(admin_client):
    for path in ("/dashboard", "/overview", "/incidents", "/reports",
                 "/users"):
        html = admin_client.get(path).get_data(as_text=True)
        assert 'href="/agents"' in html, f"missing Agents link on {path}"


def test_agents_link_hidden_from_viewer(viewer_client):
    for path in ("/dashboard", "/overview", "/incidents", "/reports"):
        html = viewer_client.get(path).get_data(as_text=True)
        assert 'href="/agents"' not in html, f"leak on {path}"


def test_agent_token_create_requires_csrf(admin_client, agent_company,
                                           app_module):
    r = admin_client.post(f"/agents/{agent_company}/token")
    assert r.status_code == 302
    conn = app_module.get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_tokens WHERE company = ?",
        (agent_company,),
    ).fetchone()["n"]
    conn.close()
    assert n == 0


_TOKEN_RE = re.compile(
    r'<code class="agent-token-value">([^<]+)</code>'
)


def test_agent_token_shown_once_and_hashed(admin_client, agent_company,
                                            app_module):
    page = admin_client.get("/agents").get_data(as_text=True)
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"', page
    ).group(1)

    r = admin_client.post(
        f"/agents/{agent_company}/token",
        data={"csrf_token": csrf},
    )
    assert r.status_code == 302

    # First render shows the plaintext token exactly once.
    first = admin_client.get("/agents").get_data(as_text=True)
    match = _TOKEN_RE.search(first)
    assert match, "plaintext token not shown after creation"
    plaintext = match.group(1)
    assert len(plaintext) >= 32

    # Second render does NOT show it again.
    second = admin_client.get("/agents").get_data(as_text=True)
    assert _TOKEN_RE.search(second) is None

    # The database stores only the SHA-256 hash.
    conn = app_module.get_db()
    row = conn.execute(
        "SELECT token_hash FROM agent_tokens WHERE company = ?",
        (agent_company,),
    ).fetchone()
    conn.close()
    assert row["token_hash"] != plaintext
    assert row["token_hash"] == app_module._agent_token_hash(plaintext)


def test_agent_token_rotation_invalidates_old(
    admin_client, client, agent_company, app_module
):
    page = admin_client.get("/agents").get_data(as_text=True)
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"', page
    ).group(1)

    admin_client.post(
        f"/agents/{agent_company}/token", data={"csrf_token": csrf}
    )
    first = _TOKEN_RE.search(
        admin_client.get("/agents").get_data(as_text=True)
    ).group(1)
    assert ingest(client, sample_results(), token=first).status_code == 200

    # Rotate -> the old token dies immediately.
    admin_client.post(
        f"/agents/{agent_company}/token", data={"csrf_token": csrf}
    )
    second = _TOKEN_RE.search(
        admin_client.get("/agents").get_data(as_text=True)
    ).group(1)
    assert second != first
    assert ingest(
        client, sample_results(), token=first
    ).status_code == 401
    assert ingest(
        client, sample_results(), token=second
    ).status_code == 200


def test_agent_token_revoke(admin_client, client, agent_company,
                            app_module):
    make_token(app_module, agent_company)
    assert ingest(client, sample_results()).status_code == 200

    page = admin_client.get("/agents").get_data(as_text=True)
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"', page
    ).group(1)
    r = admin_client.post(
        f"/agents/{agent_company}/revoke", data={"csrf_token": csrf}
    )
    assert r.status_code == 302
    assert ingest(client, sample_results()).status_code == 401

    conn = app_module.get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_tokens WHERE company = ?",
        (agent_company,),
    ).fetchone()["n"]
    conn.close()
    assert n == 0


def test_revoke_without_token_reports_error(admin_client,
                                            agent_company, app_module):
    page = admin_client.get("/agents").get_data(as_text=True)
    csrf = re.search(
        r'name="csrf_token" value="([^"]+)"', page
    ).group(1)
    r = admin_client.post(
        f"/agents/{agent_company}/revoke",
        data={"csrf_token": csrf},
        follow_redirects=True,
    )
    assert "No token exists" in r.get_data(as_text=True)


# ============================================================
# AGENT STALENESS IN /api/status
# ============================================================

def _insert_agent_row(app_module, company, age_seconds):
    when = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn = app_module.get_db()
    conn.execute(
        "INSERT INTO ping_results "
        "(timestamp, company, target, check_type, status, latency, "
        " source) VALUES (?, ?, 'x', 'Gateway', 'UP', 1.0, 'agent')",
        (when, company),
    )
    conn.commit()
    conn.close()


def test_status_includes_agent_info_when_fresh(
    admin_client, agent_company, app_module
):
    _insert_agent_row(app_module, agent_company, age_seconds=10)
    r = admin_client.get(
        "/api/status", query_string={"company": agent_company}
    )
    agent = r.get_json()["agent"]
    assert agent["managed_by"] == "agent"
    assert agent["stale"] is False
    assert agent["age_seconds"] < 60
    assert agent["last_seen"] is not None


def test_status_flags_stale_agent(admin_client, agent_company,
                                  app_module):
    _insert_agent_row(app_module, agent_company, age_seconds=600)
    r = admin_client.get(
        "/api/status", query_string={"company": agent_company}
    )
    agent = r.get_json()["agent"]
    assert agent["stale"] is True
    assert agent["age_seconds"] >= 600


def test_status_never_reported_is_stale(admin_client, agent_company):
    r = admin_client.get(
        "/api/status", query_string={"company": agent_company}
    )
    agent = r.get_json()["agent"]
    assert agent["stale"] is True
    assert agent["last_seen"] is None


def test_status_omits_agent_for_local_company(admin_client):
    r = admin_client.get(
        "/api/status", query_string={"company": "Company A"}
    )
    assert "agent" not in r.get_json()


def test_dashboard_has_agent_badge_markup(admin_client):
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert 'id="agentBadge"' in html
    assert "agentBadgeText" in html


# ============================================================
# CONFIG VALIDATION + MONITOR SKIP
# ============================================================

def test_validate_config_rejects_bad_managed_by(app_module, monkeypatch):
    monkeypatch.setitem(
        app_module.COMPANIES, AGENT_NAME,
        _valid_targets(managed_by="sometimes"),
    )
    with pytest.raises(ValueError, match="managed_by"):
        app_module.validate_config()


def test_validate_config_allows_agent_managed(app_module, monkeypatch):
    monkeypatch.setitem(
        app_module.COMPANIES, AGENT_NAME,
        _valid_targets(managed_by="agent"),
    )
    app_module.validate_config()  # must not raise


def test_validate_config_rejects_agent_speedtest_company(
    app_module, monkeypatch
):
    from config import SPEEDTEST_COMPANY

    monkeypatch.setitem(
        app_module.COMPANIES, SPEEDTEST_COMPANY,
        _valid_targets(managed_by="agent"),
    )
    with pytest.raises(ValueError, match="SPEEDTEST_COMPANY"):
        app_module.validate_config()


def test_local_companies_skips_agent_managed(app_module, monkeypatch):
    import uptime_checker as uc

    monkeypatch.setitem(
        app_module.COMPANIES, AGENT_NAME,
        _valid_targets(managed_by="agent"),
    )
    polled = uc.local_companies()
    assert AGENT_NAME not in polled
    assert "Company A" in polled
    assert "Company B" in polled


def test_monitor_banner_mentions_agent_skip(app_module, monkeypatch):
    """The startup banner marks agent-managed sites."""
    import inspect
    import uptime_checker as uc

    source = inspect.getsource(uc.monitor)
    assert "remote agent pushes results" in source
    assert "local_companies_map" in source


# ============================================================
# agent.py - PURE HELPERS (no network in tests)
# ============================================================

def test_agent_module_imports():
    import agent
    assert callable(agent.main)
    assert callable(agent.run_checks)
    assert callable(agent.push)


def test_agent_utc_now_str_format():
    import agent

    value = agent.utc_now_str()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value)


def test_agent_parse_ping_latency():
    import agent

    assert agent.parse_ping_latency(
        "Reply from 1.1.1.1: bytes=32 time=12ms TTL=57"
    ) == 12.0
    assert agent.parse_ping_latency(
        "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=8.42 ms"
    ) == 8.42
    assert agent.parse_ping_latency("") is None
    assert agent.parse_ping_latency("Request timed out.") is None


def test_agent_run_checks_shape(monkeypatch):
    """run_checks builds a full cycle without touching the network."""
    import agent

    monkeypatch.setattr(agent, "get_gateway", lambda: "10.0.0.1")
    monkeypatch.setattr(
        agent, "ping",
        lambda target: (target, "UP", 5.0),
    )
    monkeypatch.setattr(
        agent, "dns_check",
        lambda target: (target, "UP", 2.0),
    )
    monkeypatch.setattr(
        agent, "https_check",
        lambda target: (target, "UP", 30.0),
    )

    results = agent.run_checks()
    assert [r["check_type"] for r in results] == [
        "Gateway", "Internet", "DNS", "HTTPS",
    ]
    assert all(r["status"] == "UP" for r in results)
    assert all(r["latency"] is not None for r in results)
    assert all(
        re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", r["timestamp"])
        for r in results
    )


def test_agent_main_without_token_exits_2(monkeypatch):
    import agent

    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    assert agent.main(["--token", ""]) == 2


def test_agent_auto_gateway_down_when_undetectable(monkeypatch):
    """gateway: 'auto' with no detectable gateway reports DOWN."""
    import agent

    monkeypatch.setattr(agent, "get_gateway", lambda: None)
    # Keep the other checks off the network too.
    monkeypatch.setattr(agent, "ping", lambda t: (t, "UP", 5.0))
    monkeypatch.setattr(agent, "dns_check", lambda t: (t, "UP", 2.0))
    monkeypatch.setattr(agent, "https_check", lambda t: (t, "UP", 30.0))
    results = [
        r for r in agent.run_checks() if r["check_type"] == "Gateway"
    ]
    assert results[0]["status"] == "DOWN"
    assert results[0]["latency"] is None
