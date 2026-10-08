"""Latency gaps: any failed check in a bucket cuts the line, no red shading."""

from datetime import datetime, timedelta, timezone


def _conn(app_module):
    return app_module.get_db()


def _bucket_stamps():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    base = now - timedelta(minutes=(now.minute % 5) + 10)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (base + timedelta(minutes=1)).strftime(fmt), \
        (base + timedelta(minutes=2)).strftime(fmt)


def test_bucket_cuts_on_any_failure(app_module, admin_client):
    from config import default_company
    company = default_company()
    t1, t2 = _bucket_stamps()
    conn = _conn(app_module)
    conn.execute(
        "DELETE FROM ping_results WHERE company = ? "
        "AND timestamp >= datetime('now', '-1 day')", (company,))
    conn.commit()
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-GAP', 'Gateway', 'UP', 10.0)", (t1, company))
    conn.execute(
        "INSERT INTO ping_results (timestamp, company, target, check_type, status, latency)"
        " VALUES (?, ?, 'UNIT-GAP', 'Gateway', 'DOWN', NULL)", (t2, company))
    conn.commit()
    try:
        data = admin_client.get(
            f"/api/latency?hours=1&company={company}&resolution=5m").get_json()
        assert "down_flags" not in data, "red-shading flags must be gone"
        assert "down_detail" not in data
        assert len(data["labels"]) == 1
        assert data["gateway"][0] is None, \
            "any failure in bucket must cut the line (null)"
    finally:
        conn.execute("DELETE FROM ping_results WHERE target = 'UNIT-GAP'")
        conn.commit()
        conn.close()


def test_no_red_shading_plugin():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert "downtimeShadePlugin" not in js
    assert "$downFlags" not in js
    html = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "shades the whole bucket red" not in html
