"""Outage state machine: process_check_result transitions."""

import pytest


COMPANY = "T-State"


@pytest.fixture(autouse=True)
def clean(app_module):
    import app
    import uptime_checker as uc

    uc._check_state.clear()
    uc._open_outage_id.clear()

    conn = app.get_db()
    conn.execute("DELETE FROM outages WHERE company = ?", (COMPANY,))
    conn.commit()
    conn.close()

    yield

    uc._check_state.clear()
    uc._open_outage_id.clear()


def _outages():
    import app
    conn = app.get_db()
    rows = conn.execute(
        "SELECT * FROM outages WHERE company = ? ORDER BY id",
        (COMPANY,),
    ).fetchall()
    conn.close()
    return rows


def test_first_up_opens_nothing():
    import uptime_checker as uc
    info = uc.process_check_result(
        COMPANY, "Gateway", "UP", "2026-10-01 10:00:00"
    )
    assert info["event"] == "still_up"
    assert info["fails"] == 0
    assert info["open_outage_id"] is None
    assert _outages() == []


def test_first_down_opens_outage():
    import uptime_checker as uc
    info = uc.process_check_result(
        COMPANY, "Gateway", "DOWN", "2026-10-01 10:00:00"
    )
    assert info["event"] == "down"
    assert info["fails"] == 1
    assert info["open_outage_id"] is not None
    rows = _outages()
    assert len(rows) == 1
    assert rows[0]["ended_at"] is None
    assert rows[0]["started_at"] == "2026-10-01 10:00:00"


def test_down_to_up_closes_with_duration():
    import uptime_checker as uc

    uc.process_check_result(COMPANY, "Gateway", "UP", "2026-10-01 10:00:00")
    info = uc.process_check_result(
        COMPANY, "Gateway", "DOWN", "2026-10-01 10:01:00"
    )
    assert info["event"] == "down"

    info = uc.process_check_result(
        COMPANY, "Gateway", "DOWN", "2026-10-01 10:02:00"
    )
    assert info["event"] == "still_down"
    assert info["fails"] == 2

    info = uc.process_check_result(
        COMPANY, "Gateway", "UP", "2026-10-01 10:04:00"
    )
    assert info["event"] == "up"
    assert info["fails"] == 0
    assert info["open_outage_id"] is None

    rows = _outages()
    assert len(rows) == 1
    assert rows[0]["ended_at"] == "2026-10-01 10:04:00"
    assert rows[0]["duration"] == 180.0  # 10:01:00 -> 10:04:00


def test_error_counts_as_down():
    import uptime_checker as uc

    uc.process_check_result(COMPANY, "Gateway", "UP", "2026-10-01 10:00:00")
    info = uc.process_check_result(
        COMPANY, "Gateway", "ERROR", "2026-10-01 10:01:00"
    )
    assert info["event"] == "down"
    assert info["fails"] == 1
    assert len(_outages()) == 1


def test_down_up_down_opens_second_outage():
    import uptime_checker as uc

    uc.process_check_result(COMPANY, "Gateway", "UP", "2026-10-01 10:00:00")
    uc.process_check_result(COMPANY, "Gateway", "DOWN", "2026-10-01 10:01:00")
    uc.process_check_result(COMPANY, "Gateway", "UP", "2026-10-01 10:02:00")
    info = uc.process_check_result(
        COMPANY, "Gateway", "DOWN", "2026-10-01 10:03:00"
    )
    assert info["event"] == "down"

    rows = _outages()
    assert len(rows) == 2
    assert rows[0]["ended_at"] is not None
    assert rows[1]["ended_at"] is None
