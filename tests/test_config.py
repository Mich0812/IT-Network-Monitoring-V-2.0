"""config.py validation rules (fail fast on bad config)."""

import pytest

import config


def test_real_config_is_valid():
    """The shipped config.py must pass validation as-is."""
    config.validate_config()  # must not raise


def test_empty_companies_rejected(monkeypatch):
    monkeypatch.setattr(config, "COMPANIES", {})
    with pytest.raises(ValueError, match="empty"):
        config.validate_config()


def test_missing_target_rejected(monkeypatch):
    monkeypatch.setattr(config, "COMPANIES", {
        "X": {
            "gateway": "auto",
            "internet": "1.1.1.1",
            "dns": "a.example.com",
            # https missing
        },
    })
    monkeypatch.setattr(config, "SPEEDTEST_COMPANY", "X")
    with pytest.raises(ValueError, match="https"):
        config.validate_config()


def test_non_ip_gateway_rejected(monkeypatch):
    monkeypatch.setattr(config, "COMPANIES", {
        "X": {
            "gateway": "not-an-ip",
            "internet": "1.1.1.1",
            "dns": "a.example.com",
            "https": "https://a.example.com",
        },
    })
    monkeypatch.setattr(config, "SPEEDTEST_COMPANY", "X")
    with pytest.raises(ValueError, match="gateway"):
        config.validate_config()


def test_bad_https_url_rejected(monkeypatch):
    monkeypatch.setattr(config, "COMPANIES", {
        "X": {
            "gateway": "auto",
            "internet": "1.1.1.1",
            "dns": "a.example.com",
            "https": "ftp://a.example.com",
        },
    })
    monkeypatch.setattr(config, "SPEEDTEST_COMPANY", "X")
    with pytest.raises(ValueError, match="https"):
        config.validate_config()


def test_speedtest_company_must_exist(monkeypatch):
    monkeypatch.setattr(config, "SPEEDTEST_COMPANY", "Ghost Corp")
    with pytest.raises(ValueError, match="SPEEDTEST_COMPANY"):
        config.validate_config()


def test_down_failures_minimum(monkeypatch):
    bad = dict(config.ALERTS)
    bad["down_failures"] = 0
    monkeypatch.setattr(config, "ALERTS", bad)
    with pytest.raises(ValueError, match="down_failures"):
        config.validate_config()


def test_cooldown_minimum(monkeypatch):
    bad = dict(config.ALERTS)
    bad["cooldown_seconds"] = 10
    monkeypatch.setattr(config, "ALERTS", bad)
    with pytest.raises(ValueError, match="cooldown_seconds"):
        config.validate_config()


def test_maintenance_bad_time_rejected(monkeypatch):
    monkeypatch.setattr(config, "MAINTENANCE", [
        {"days": "all", "start": "25:00", "end": "26:00"},
    ])
    with pytest.raises(ValueError, match="HH:MM"):
        config.validate_config()


def test_maintenance_unknown_company_rejected(monkeypatch):
    monkeypatch.setattr(config, "MAINTENANCE", [
        {"company": "Ghost Corp", "days": "all",
         "start": "02:00", "end": "03:00"},
    ])
    with pytest.raises(ValueError, match="MAINTENANCE company"):
        config.validate_config()


def test_maintenance_bad_days_rejected(monkeypatch):
    monkeypatch.setattr(config, "MAINTENANCE", [
        {"days": [0, 9], "start": "02:00", "end": "03:00"},
    ])
    with pytest.raises(ValueError, match="days"):
        config.validate_config()
