"""Graph resolution: Per check / Per hour / Per day averaging with downtime gaps."""

from datetime import datetime, timedelta, timezone


def _conn(app_module):
    return app_module.get_db()


def _cleanup(conn):
    conn.execute("DELETE FROM ping_results WHERE target LIKE 'UNIT-RES%'")
    conn.execute("DELETE FROM speedtest_results WHERE server LIKE 'UNIT-RES%'")
    conn.commit()


def _isolate_company(conn, company, table="ping_results"):
    # Other suites write datetime('now') rows for the same company;
    # wipe the 24h window first so bucket averages are deterministic.
    conn.execute(
        f"DELETE FROM {table} WHERE company = ? "
        "AND timestamp >= datetime('now', '-1 day')",
        (company,),
    )
    conn.commit()


def _hour_stamps():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # Two samples inside the current UTC hour, one inside the previous hour.
    cur = now.replace(minute=10, second=0)
    if cur > now:
        cur = cur - timedelta(hours=1)
    cur2 = cur + timedelta(minutes=5)
    prev = cur - timedelta(hours=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return cur.strftime(fmt), cur2.strftime(fmt), prev.strftime(fmt)


def test_latency_hour_averages_up_and_cuts_downtime(app_module, admin_client):
    from config import default_company
    company = default_company()
    t1, t2, t_prev = _hour_stamps()
    conn = _conn(app_module)
    _isolate_company(conn, company, "ping_results")
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-RES', 'Gateway', 'UP', 10.0)", (t1, company))
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-RES', 'Gateway', 'UP', 20.0)", (t2, company))
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-RES', 'Gateway', 'DOWN', NULL)", (t2, company))
    # Previous hour: only DOWN -> bucket must exist as null (line cut).
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-RES', 'Gateway', 'DOWN', NULL)", (t_prev, company))
    conn.commit()
    try:
        data = admin_client.get(
            f"/api/latency?hours=24&company={company}&resolution=hour").get_json()
        assert data["resolution"] == "hour"
        labels = data["labels"]
        assert len(labels) == 2, labels
        idx_cur = labels.index(t1[:13] + ":00:00")
        assert data["gateway"][idx_cur] == 15.0
        idx_prev = labels.index(t_prev[:13] + ":00:00")
        assert data["gateway"][idx_prev] is None
    finally:
        _cleanup(conn)
        conn.close()


def test_latency_day_averages(app_module, admin_client):
    from config import default_company
    company = default_company()
    t1, t2, _ = _hour_stamps()
    conn = _conn(app_module)
    _isolate_company(conn, company, "ping_results")
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-RES', 'Internet', 'UP', 10.0)", (t1, company))
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-RES', 'Internet', 'UP', 30.0)", (t2, company))
    conn.commit()
    try:
        data = admin_client.get(
            f"/api/latency?hours=24&company={company}&resolution=day").get_json()
        assert data["resolution"] == "day"
        assert len(data["labels"]) == 1
        assert data["internet"][0] == 20.0
    finally:
        _cleanup(conn)
        conn.close()


def test_latency_raw_is_default_and_invalid_falls_back(app_module, admin_client):
    from config import default_company
    company = default_company()
    data = admin_client.get(
        f"/api/latency?hours=1&company={company}&resolution=bogus").get_json()
    assert data["resolution"] == "raw"
    data2 = admin_client.get(
        f"/api/latency?hours=1&company={company}").get_json()
    assert data2["resolution"] == "raw"


def test_speedtest_hour_averages(app_module, admin_client):
    import config
    company = config.SPEEDTEST_COMPANY
    t1, t2, _ = _hour_stamps()
    conn = _conn(app_module)
    _isolate_company(conn, company, "speedtest_results")
    conn.execute(
        "INSERT INTO speedtest_results (timestamp, company, download, upload, ping, server, status)"
        " VALUES (?, ?, 100.0, 20.0, 5.0, 'UNIT-RES-1', 'ok')", (t1, company))
    conn.execute(
        "INSERT INTO speedtest_results (timestamp, company, download, upload, ping, server, status)"
        " VALUES (?, ?, 200.0, 40.0, 15.0, 'UNIT-RES-2', 'ok')", (t2, company))
    conn.commit()
    try:
        data = admin_client.get("/api/speedtest?hours=24&resolution=hour").get_json()
        assert data["resolution"] == "hour"
        assert len(data["labels"]) == 1
        assert data["download"][0] == 150.0
        assert data["upload"][0] == 30.0
        assert data["ping"][0] == 10.0
    finally:
        _cleanup(conn)
        conn.close()


def test_dashboard_has_resolution_dropdowns(app_module, admin_client):
    html = admin_client.get("/dashboard").get_data(as_text=True)
    assert 'id="latencyResolution"' in html
    assert 'id="speedtestResolution"' in html
    assert "Per check" in html
    assert "Per hour" in html
    assert "Per day" in html
