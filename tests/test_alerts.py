"""Alert engine: flap guard, recovery, cooldown, maintenance."""

import time
from datetime import datetime

import pytest


@pytest.fixture()
def uc(app_module):
    import uptime_checker as module
    yield module
    module._last_alert_ts.clear()
    module._latency_state.clear()


@pytest.fixture()
def alerts_on(uc, monkeypatch):
    cfg = {
        "enabled": True,
        "down_failures": 2,
        "latency_ms": 0,
        "latency_failures": 3,
        "cooldown_seconds": 60,
        "email": {"enabled": False},
        "webhook": {"enabled": False},
    }
    monkeypatch.setattr(uc, "ALERTS", cfg)
    monkeypatch.setattr(uc, "MAINTENANCE", [])
    uc._last_alert_ts.clear()
    uc._latency_state.clear()
    yield cfg
    uc._last_alert_ts.clear()
    uc._latency_state.clear()


def _spy(monkeypatch, uc):
    calls = []
    monkeypatch.setattr(
        uc, "dispatch_alert",
        lambda *a, **k: calls.append(a) or False,
    )
    return calls


# ---- evaluate_alerts rules ----------------------------------------

def test_disabled_alerting_sends_nothing(uc, monkeypatch):
    monkeypatch.setattr(uc, "ALERTS", {"enabled": False})
    calls = _spy(monkeypatch, uc)
    uc.evaluate_alerts(
        "T-Off", "Gateway", "DOWN", None,
        {"event": "down", "fails": 9},
    )
    assert calls == []


def test_flap_guard_requires_two_failures(uc, alerts_on, monkeypatch):
    calls = _spy(monkeypatch, uc)

    uc.evaluate_alerts(
        "T-Flap", "Gateway", "DOWN", None,
        {"event": "down", "fails": 1},
    )
    assert calls == []                       # 1 failure: below guard

    uc.evaluate_alerts(
        "T-Flap", "Gateway", "DOWN", None,
        {"event": "down", "fails": 2},
    )
    assert len(calls) == 1
    assert calls[0][0] == "T-Flap"
    assert calls[0][2] == "down"              # kind


def test_recovery_only_after_down_alert(uc, alerts_on, monkeypatch):
    calls = _spy(monkeypatch, uc)

    # UP without a prior down alert -> no recovery notice
    uc.evaluate_alerts(
        "T-Rec", "Gateway", "UP", None,
        {"event": "up", "fails": 0},
    )
    assert calls == []

    # With a prior down alert -> recovery fires and the marker clears
    uc._last_alert_ts[("T-Rec", "Gateway", "down")] = time.time()
    uc.evaluate_alerts(
        "T-Rec", "Gateway", "UP", None,
        {"event": "up", "fails": 0},
    )
    assert len(calls) == 1
    assert calls[0][2] == "recovery"
    assert ("T-Rec", "Gateway", "down") not in uc._last_alert_ts


def test_maintenance_suppresses_everything(uc, alerts_on, monkeypatch):
    monkeypatch.setattr(
        uc, "_in_maintenance", lambda company, now_dt=None: True
    )
    calls = _spy(monkeypatch, uc)
    uc.evaluate_alerts(
        "T-Mnt", "Gateway", "DOWN", None,
        {"event": "down", "fails": 9},
    )
    assert calls == []


def test_latency_rule_fires_after_consecutive(uc, alerts_on, monkeypatch):
    alerts_on["latency_ms"] = 100
    alerts_on["latency_failures"] = 3
    calls = _spy(monkeypatch, uc)

    for _ in range(2):
        uc.evaluate_alerts(
            "T-Lat", "Gateway", "UP", 150.0,
            {"event": "still_up", "fails": 0},
        )
    assert calls == []                        # not yet 3 in a row

    uc.evaluate_alerts(
        "T-Lat", "Gateway", "UP", 150.0,
        {"event": "still_up", "fails": 0},
    )
    assert len(calls) == 1
    assert calls[0][2] == "latency"

    # A good sample resets the streak
    uc.evaluate_alerts(
        "T-Lat", "Gateway", "UP", 20.0,
        {"event": "still_up", "fails": 0},
    )
    assert uc._latency_state[("T-Lat", "Gateway")] == 0


# ---- _in_maintenance window logic ---------------------------------

def test_maintenance_window_logic(uc, monkeypatch):
    # 2026-09-28 is a Monday; 2026-10-04 is a Sunday.
    monkeypatch.setattr(uc, "MAINTENANCE", [
        {"company": "T-Co", "days": [0, 1, 2, 3, 4],
         "start": "22:00", "end": "02:00"},
    ])

    assert uc._in_maintenance("T-Co", datetime(2026, 9, 28, 12, 0)) is False
    assert uc._in_maintenance("T-Co", datetime(2026, 9, 28, 23, 0)) is True
    # window crosses midnight
    assert uc._in_maintenance("T-Co", datetime(2026, 9, 28, 1, 0)) is True
    # Sunday falls outside the day list
    assert uc._in_maintenance("T-Co", datetime(2026, 10, 4, 1, 0)) is False
    # company filter
    assert uc._in_maintenance("Other", datetime(2026, 9, 28, 23, 0)) is False

    # window without a company applies to everyone
    monkeypatch.setattr(uc, "MAINTENANCE", [
        {"days": "all", "start": "12:00", "end": "13:00"},
    ])
    assert uc._in_maintenance("Any", datetime(2026, 9, 28, 12, 30)) is True
    assert uc._in_maintenance("Any", datetime(2026, 9, 28, 13, 30)) is False


# ---- _cooldown_ok --------------------------------------------------

def test_cooldown_in_memory(uc, alerts_on):
    key = ("T-CD", "Gateway", "down")
    assert uc._cooldown_ok(*key) is True      # never alerted before

    uc._last_alert_ts[key] = time.time()
    assert uc._cooldown_ok(*key) is False     # inside the window

    uc._last_alert_ts[key] = time.time() - 3600
    assert uc._cooldown_ok(*key) is True      # window expired


def test_cooldown_survives_restart(uc, alerts_on, app_module):
    """A fresh process (no memory) still honors recent notifications."""
    import app
    import uptime_checker as ucmod

    conn = app.get_db()
    conn.execute(
        "DELETE FROM notifications WHERE company = 'T-CDDB'"
    )
    conn.execute(
        "INSERT INTO notifications "
        "(company, check_type, kind, message, channel, sent_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T-CDDB", "Gateway", "down", "m", "email",
         ucmod.utc_now_str(), "sent"),
    )
    conn.commit()
    conn.close()

    assert uc._cooldown_ok("T-CDDB", "Gateway", "down") is False

    # The first call also memorized the cooldown in-memory; clear it
    # to simulate yet another fresh restart before testing the stale row.
    uc._last_alert_ts.clear()

    conn = app.get_db()
    conn.execute(
        "UPDATE notifications SET sent_at = '2020-01-01 00:00:00' "
        "WHERE company = 'T-CDDB'"
    )
    conn.commit()
    conn.close()

    assert uc._cooldown_ok("T-CDDB", "Gateway", "down") is True


# ---- dispatch_alert ------------------------------------------------

def test_dispatch_records_and_dedupes(uc, alerts_on, app_module):
    import app

    conn = app.get_db()
    conn.execute("DELETE FROM notifications WHERE company = 'T-Disp'")
    conn.commit()
    conn.close()

    delivered = uc.dispatch_alert(
        "T-Disp", "Gateway", "down", "Gateway is DOWN."
    )
    assert delivered is False                 # channels disabled

    conn = app.get_db()
    rows = conn.execute(
        "SELECT channel, status FROM notifications "
        "WHERE company = 'T-Disp' ORDER BY id"
    ).fetchall()
    conn.close()
    assert [(r["channel"], r["status"]) for r in rows] == [
        ("email", "disabled"),
        ("webhook", "disabled"),
    ]
    assert uc._last_alert_ts[("T-Disp", "Gateway", "down")] > 0

    # Immediate second send is held back by the cooldown
    uc.dispatch_alert("T-Disp", "Gateway", "down", "Gateway is DOWN.")
    conn = app.get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE company = 'T-Disp'"
    ).fetchone()["n"]
    conn.close()
    assert count == 2                         # unchanged


def test_dispatch_suppressed_in_maintenance(uc, alerts_on, monkeypatch,
                                            app_module):
    import app

    monkeypatch.setattr(
        uc, "_in_maintenance", lambda company, now_dt=None: True
    )

    conn = app.get_db()
    conn.execute("DELETE FROM notifications WHERE company = 'T-Mnt2'")
    conn.commit()
    conn.close()

    delivered = uc.dispatch_alert(
        "T-Mnt2", "Gateway", "down", "msg"
    )
    assert delivered is False

    conn = app.get_db()
    row = conn.execute(
        "SELECT channel, status FROM notifications "
        "WHERE company = 'T-Mnt2'"
    ).fetchone()
    conn.close()
    assert row["channel"] == "suppressed"
    assert row["status"] == "maintenance"


def test_dispatch_disabled_when_alerting_off(uc, monkeypatch, app_module):
    import app

    monkeypatch.setattr(uc, "ALERTS", {"enabled": False})

    conn = app.get_db()
    conn.execute("DELETE FROM notifications WHERE company = 'T-Off2'")
    conn.commit()
    conn.close()

    assert uc.dispatch_alert("T-Off2", "Gateway", "down", "msg") is False

    conn = app.get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE company = 'T-Off2'"
    ).fetchone()["n"]
    conn.close()
    assert count == 0                         # nothing recorded at all
