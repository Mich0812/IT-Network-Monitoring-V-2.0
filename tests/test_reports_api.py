"""Reports APIs: SLA math, heatmap aggregation, CSV export."""

import pytest

from tests.conftest import forge

COMPANY = "Company A"


@pytest.fixture(autouse=True)
def seeded(app_module):
    """Two hourly rollup rows + one closed outage for Company A."""
    import app
    import time as _time

    conn = app.get_db()
    conn.execute("DELETE FROM rollup_hourly WHERE company = ?", (COMPANY,))
    conn.execute("DELETE FROM outages WHERE company = ?", (COMPANY,))
    conn.commit()
    conn.close()

    now_h = int(_time.time() // 3600) * 3600

    def hour(offset):
        return _time.strftime(
            "%Y-%m-%dT%H:00:00", _time.gmtime(now_h - offset * 3600)
        )

    conn = app.get_db()
    conn.executemany(
        "INSERT INTO rollup_hourly "
        "(company, check_type, hour_utc, total, up_count, "
        " avg_latency, p95_latency, max_latency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # 9/10 up, mean 10, p95 15
            (COMPANY, "Gateway", hour(1), 10, 9, 10.0, 15.0, 40.0),
            # 10/10 up, mean 20, p95 25
            (COMPANY, "Gateway", hour(2), 10, 10, 20.0, 25.0, 50.0),
        ],
    )
    started = _time.strftime(
        "%Y-%m-%d %H:%M:%S", _time.gmtime(_time.time() - 3 * 3600)
    )
    ended = _time.strftime(
        "%Y-%m-%d %H:%M:%S", _time.gmtime(_time.time() - 3 * 3600 + 300)
    )
    conn.execute(
        "INSERT INTO outages "
        "(company, check_type, started_at, ended_at, duration, cause) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (COMPANY, "Gateway", started, ended, 300.0, None),
    )
    conn.commit()
    conn.close()

    yield

    conn = app.get_db()
    conn.execute("DELETE FROM rollup_hourly WHERE company = ?", (COMPANY,))
    conn.execute("DELETE FROM outages WHERE company = ?", (COMPANY,))
    conn.commit()
    conn.close()


def test_sla_aggregation_math(admin_client, seeded):
    r = admin_client.get(
        "/api/sla", query_string={"days": 7, "company": COMPANY}
    )
    assert r.status_code == 200
    data = r.get_json()

    assert data["days"] == 7
    assert data["company"] == COMPANY

    row = next(x for x in data["rows"] if x["check_type"] == "Gateway")
    assert row["checks"] == 20
    assert row["up_checks"] == 19
    assert row["uptime_pct"] == 95.0          # 19/20
    assert row["avg_latency"] == 15.0         # (10*10 + 20*10) / 20
    assert row["p95_latency"] == 25.0         # worst hourly p95
    assert row["outages"] == 1
    assert row["downtime_minutes"] == 5.0     # 300 s
    assert row["longest_minutes"] == 5.0


def test_sla_all_companies_mode(admin_client, seeded):
    r = admin_client.get(
        "/api/sla", query_string={"days": 7, "company": "all"}
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["company"] == "all"
    assert any(x["company"] == COMPANY for x in data["rows"])


def test_sla_unknown_company_400(admin_client):
    r = admin_client.get(
        "/api/sla", query_string={"company": "No Such Corp"}
    )
    assert r.status_code == 400


def test_sla_days_clamped(admin_client):
    r = admin_client.get(
        "/api/sla", query_string={"days": 99999, "company": COMPANY}
    )
    assert r.status_code == 200
    assert r.get_json()["days"] == 365        # upper bound


def test_heatmap_per_check_series(admin_client, seeded):
    r = admin_client.get(
        "/api/heatmap", query_string={"days": 7, "company": COMPANY}
    )
    assert r.status_code == 200
    data = r.get_json()

    assert data["company"] == COMPANY
    assert len(data["days_list"]) == 7
    assert [s["name"] for s in data["series"]] == ["Gateway"]

    cells = data["series"][0]["cells"]
    assert len(cells) == 7

    # Rollup hours may straddle UTC midnight, so assert the totals
    # across all day-cells instead of a fixed cell position.
    total_checks = sum(c["checks"] for c in cells)
    assert total_checks == 20
    pct_values = {c["uptime_pct"] for c in cells if c["uptime_pct"]}
    assert pct_values <= {90.0, 95.0, 100.0}


def test_heatmap_all_mode_groups_by_company(admin_client, seeded):
    r = admin_client.get(
        "/api/heatmap", query_string={"days": 7, "company": "all"}
    )
    assert r.status_code == 200
    data = r.get_json()
    assert [s["name"] for s in data["series"]] == [COMPANY]


def test_heatmap_days_clamped(admin_client):
    r = admin_client.get(
        "/api/heatmap", query_string={"days": 99999, "company": COMPANY}
    )
    assert r.status_code == 200
    assert r.get_json()["days"] == 120        # upper bound


def test_csv_export(admin_client, seeded):
    r = admin_client.get(
        "/api/sla/export", query_string={"days": 7, "company": COMPANY}
    )
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]

    lines = r.get_data(as_text=True).splitlines()
    assert lines[0].startswith("company,check,uptime_pct")
    assert any(line.startswith("Company A,Gateway,95.0")
               for line in lines)


def test_empty_period_returns_empty_rows(admin_client):
    """No rollups for a company - the API still answers cleanly."""
    r = admin_client.get(
        "/api/sla", query_string={"days": 7, "company": "Company B"}
    )
    assert r.status_code == 200
    assert r.get_json()["rows"] == []
