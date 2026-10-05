"""Hourly rollups: aggregation math + retention pruning."""

import time
from datetime import datetime, timedelta, timezone

import pytest

COMPANY = "T-Roll"


@pytest.fixture(autouse=True)
def clean(app_module):
    import app
    conn = app.get_db()
    conn.execute("DELETE FROM ping_results WHERE company = ?", (COMPANY,))
    conn.execute("DELETE FROM rollup_hourly WHERE company = ?", (COMPANY,))
    conn.commit()
    conn.close()
    yield
    conn = app.get_db()
    conn.execute("DELETE FROM ping_results WHERE company = ?", (COMPANY,))
    conn.execute("DELETE FROM rollup_hourly WHERE company = ?", (COMPANY,))
    conn.commit()
    conn.close()


def _insert_ping(ts, status, latency):
    import app
    conn = app.get_db()
    conn.execute(
        "INSERT INTO ping_results "
        "(timestamp, company, target, check_type, status, latency) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, COMPANY, "1.1.1.1", "Gateway", status, latency),
    )
    conn.commit()
    conn.close()


def _rollup_rows():
    import app
    conn = app.get_db()
    rows = conn.execute(
        "SELECT * FROM rollup_hourly WHERE company = ?", (COMPANY,)
    ).fetchall()
    conn.close()
    return rows


def test_build_rollups_math(app_module):
    import uptime_checker as uc

    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    _insert_ping(hour.replace(minute=10).strftime("%Y-%m-%d %H:%M:%S"),
                 "UP", 10.0)
    _insert_ping(hour.replace(minute=20).strftime("%Y-%m-%d %H:%M:%S"),
                 "UP", 20.0)
    _insert_ping(hour.replace(minute=30).strftime("%Y-%m-%d %H:%M:%S"),
                 "DOWN", None)
    _insert_ping(hour.replace(minute=40).strftime("%Y-%m-%d %H:%M:%S"),
                 "UP", 30.0)

    uc.build_rollups(since_hours=2)

    rows = _rollup_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["check_type"] == "Gateway"
    assert r["total"] == 4
    assert r["up_count"] == 3
    assert r["avg_latency"] == 20.0            # mean(10, 20, 30)
    assert r["max_latency"] == 30.0
    assert r["p95_latency"] == 30.0            # index min(2, int(3*.95))
    assert r["hour_utc"] == hour.strftime("%Y-%m-%dT%H:00:00")


def test_build_rollups_is_idempotent(app_module):
    import uptime_checker as uc

    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    _insert_ping(hour.replace(minute=5).strftime("%Y-%m-%d %H:%M:%S"),
                 "UP", 5.0)

    uc.build_rollups(since_hours=2)
    uc.build_rollups(since_hours=2)

    rows = _rollup_rows()
    assert len(rows) == 1
    assert rows[0]["total"] == 1               # UPSERT, not duplicate


def test_prune_removes_old_keeps_recent(app_module):
    import app

    today = datetime.now(timezone.utc).date()
    old_day = (today - timedelta(days=200)).isoformat()
    new_day = today.isoformat()

    conn = app.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO rollup_hourly "
        "(company, check_type, hour_utc, total, up_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (COMPANY, "Gateway", f"{old_day}T00:00:00", 60, 60),
    )
    conn.execute(
        "INSERT OR REPLACE INTO rollup_hourly "
        "(company, check_type, hour_utc, total, up_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (COMPANY, "Gateway", f"{new_day}T00:00:00", 60, 60),
    )
    conn.commit()
    conn.close()

    app.prune_old_data()   # RETENTION_DAYS defaults to 90

    rows = _rollup_rows()
    days = sorted(r["hour_utc"][:10] for r in rows)
    assert days == [new_day]
