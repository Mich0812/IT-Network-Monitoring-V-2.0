"""JSON API routes moved from app.py (Phase 13).

All 18 /api/* endpoints plus their two parsing helpers
(_valid_ts, _parse_hours). Endpoints are namespaced api.*;
the URL paths are unchanged, so the dashboard JS, agents and
tests keep working against the same URLs.
"""

import csv
import io
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    current_app,
    make_response,
    request,
)

from config import (
    AGENT_STALE_SECONDS,
    ALERTS,
    COMPANIES,
    RETENTION_DAYS,
    SPEEDTEST_COMPANY,
    default_company,
)
from core import (
    ALLOWED_HOURS,
    CHECK_ORDER,
    INCIDENT_MERGE_GAP,
    _TS_FMT,
    _agent_token_hash,
    _clamp_days,
    _report_company,
    _sla_rows,
    check_csrf,
    get_db,
    get_selected_company,
    group_outages,
    is_logged_in,
    require_admin,
    utc_now_str,
)

bp = Blueprint("api", __name__)


@bp.route("/api/companies/summary")
def companies_summary():
    """One payload for the overview grid: latest + uptime per company."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    hours = _parse_hours(24)

    conn = get_db()
    out = []
    for name in COMPANIES.keys():
        latest = conn.execute(
            """
            SELECT p.check_type, p.target, p.status, p.latency, p.timestamp
            FROM ping_results p
            INNER JOIN (
                SELECT check_type, MAX(id) AS max_id
                FROM ping_results WHERE company = ?
                GROUP BY check_type
            ) latest
            ON p.check_type = latest.check_type AND p.id = latest.max_id
            """,
            (name,),
        ).fetchall()

        uptime_rows = conn.execute(
            """
            SELECT check_type,
                COUNT(*) AS total,
                SUM(CASE WHEN status='UP' THEN 1 ELSE 0 END) AS up_count
            FROM ping_results
            WHERE timestamp >= datetime('now', ?) AND company = ?
            GROUP BY check_type
            """,
            (f"-{hours} hours", name),
        ).fetchall()

        uptime = {}
        for r in uptime_rows:
            total = r["total"] or 0
            up = r["up_count"] or 0
            uptime[r["check_type"]] = round(up / total * 100, 2) if total else None

        checks = {}
        last_check = None
        for r in latest:
            checks[r["check_type"]] = {
                "target": r["target"],
                "status": r["status"],
                "latency": r["latency"],
                "timestamp": r["timestamp"],
                "uptime": uptime.get(r["check_type"]),
            }
            if r["timestamp"] and (last_check is None or r["timestamp"] > last_check):
                last_check = r["timestamp"]

        # Overall mirrors dashboard logic: ok/degraded/down/idle.
        order = ["Gateway", "Internet", "DNS", "HTTPS"]
        present = [t for t in order if t in checks]
        failing = [t for t in present if checks[t]["status"] != "UP"]
        if not present:
            overall = "idle"
        elif not failing:
            overall = "ok"
        elif len(failing) == len(present):
            overall = "down"
        else:
            overall = "degraded"

        out.append({
            "name": name,
            "overall": overall,
            "failing": failing,
            "last_check": last_check,
            "checks": checks,
        })
    conn.close()
    return {"hours": hours, "companies": out}


# ============================================================
# OUTAGES / NOTIFICATIONS / ROLLUPS API
# ============================================================

@bp.route("/api/outages")
def outages_api():
    """Incidents for one company (or all when company=ALL)."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    hours = _parse_hours(24)
    company = request.args.get("company", "")
    include_open = request.args.get("open", "0") == "1"

    conn = get_db()

    sql = (
        "SELECT id, company, check_type, started_at, ended_at, "
        "duration, cause FROM outages "
        "WHERE (ended_at IS NULL OR started_at >= datetime('now', ?))"
    )
    params = [f"-{hours} hours"]

    if company and company in COMPANIES:
        sql += " AND company = ?"
        params.append(company)

    if not include_open:
        sql += " AND ended_at IS NOT NULL"

    sql += " ORDER BY started_at DESC LIMIT 200"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = [
        {
            "id": r["id"],
            "company": r["company"],
            "check_type": r["check_type"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration": r["duration"],
            "open": r["ended_at"] is None,
            "cause": r["cause"],
        }
        for r in rows
    ]

    return {"hours": hours, "outages": items, "count": len(items)}


@bp.route("/api/notifications")
def notifications_api():
    """Recent alert history (audit trail).

    Optional filters: company (or 'all'/absent = unchanged behaviour)
    and hours (lookback window for sent_at)."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    # limit: default 50, clamp to 200; malformed values are a client
    # error (400), never a 500.
    try:
        limit = int(request.args.get("limit", 50) or 50)
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}, 400
    limit = max(1, min(limit, 200))

    where = ""
    params = []

    company = (request.args.get("company") or "").strip()
    if company and company.lower() != "all":
        if company not in COMPANIES:
            return {"error": "Unknown company"}, 400
        where += " AND company = ?"
        params.append(company)

    hours_raw = request.args.get("hours")
    if hours_raw not in (None, ""):
        try:
            hours = int(hours_raw)
        except (TypeError, ValueError):
            hours = 0
        if hours < 1 or hours > 8760:
            return {"error": "hours must be between 1 and 8760"}, 400
        where += " AND sent_at >= datetime('now', ?)"
        params.append(f"-{hours} hours")

    conn = get_db()
    rows = conn.execute(
        "SELECT id, company, check_type, kind, message, channel, "
        f"sent_at, status FROM notifications WHERE 1=1{where} "
        "ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    return {
        "notifications": [dict(r) for r in rows],
        "count": len(rows),
    }




@bp.route("/api/incidents")
def incidents_api():
    """Outages grouped into incidents (overlapping or near-in-time).

    Query: hours (lookback, same windows as other chart APIs),
    company (single name, 'all' or empty = every company),
    open=1 (only incidents still ongoing).
    """
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    hours = _parse_hours(24)
    company = (request.args.get("company") or "").strip()
    if company.lower() in ("", "all"):
        company = None
    elif company not in COMPANIES:
        return {"error": "Unknown company"}, 400
    open_only = request.args.get("open", "0") == "1"

    conn = get_db()
    sql = (
        "SELECT id, company, check_type, started_at, ended_at, "
        "duration, cause FROM outages "
        "WHERE (ended_at IS NULL OR started_at >= datetime('now', ?))"
    )
    params = [f"-{hours} hours"]
    if company:
        sql += " AND company = ?"
        params.append(company)
    sql += " ORDER BY started_at ASC, id ASC LIMIT 1000"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    incidents = group_outages(rows)
    if open_only:
        incidents = [i for i in incidents if i["open"]]

    return {
        "hours": hours,
        "company": company or "all",
        "gap_seconds": INCIDENT_MERGE_GAP,
        "incidents": incidents[:200],
        "count": len(incidents),
    }


@bp.route("/api/rollups")
def rollups_api():
    """Pre-aggregated hourly availability (fast 30d queries)."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    company = request.args.get("company", "")
    if not company or company not in COMPANIES:
        company = default_company()

    try:
        hours = int(request.args.get("hours", 720))
    except ValueError:
        hours = 720

    conn = get_db()
    rows = conn.execute(
        """
        SELECT check_type, hour_utc, total, up_count,
               avg_latency, p95_latency, max_latency
        FROM rollup_hourly
        WHERE company = ?
          AND hour_utc >= datetime('now', ?)
        ORDER BY hour_utc ASC
        """,
        (company, f"-{hours} hours"),
    ).fetchall()
    conn.close()

    return {
        "company": company,
        "hours": hours,
        "rollups": [dict(r) for r in rows],
        "count": len(rows),
    }


@bp.route("/api/alerts/test", methods=["POST"])
@require_admin
def alerts_test():
    """Fire a test alert through the configured channels."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401

    try:
        import uptime_checker as uc
    except Exception as error:
        return {"error": f"checker unavailable: {error}"}, 500

    company = default_company()
    delivered = uc.dispatch_alert(
        company,
        "Test",
        "test",
        "This is a test alert from Uptime Monitor.",
        subject="[UptimeMonitor] Test alert",
    )
    return {
        "ok": True,
        "delivered": bool(delivered),
        "channels": {
            "email": bool(uc.ALERTS.get("email", {}).get("enabled")),
            "webhook": bool(uc.ALERTS.get("webhook", {}).get("enabled")),
        },
    }


# ============================================================
# REMOTE AGENT INGEST (POST /api/ingest)
#
# A remote site runs agent.py, which pushes its check results here.
# Auth: "Authorization: Bearer <token>" - one token per company,
# stored only as a SHA-256 hash (manage on the /agents page).
# The company comes from the TOKEN, never from the payload, so one
# site cannot write into another company's data.
# ============================================================

# One push carries one check cycle (Gateway/Internet/DNS/HTTPS).
MAX_INGEST_RESULTS = 20




@bp.route("/api/ingest", methods=["POST"])
def ingest_api():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return {"error": "Missing Bearer token"}, 401
    token = auth[7:].strip()
    if not token:
        return {"error": "Empty Bearer token"}, 401

    conn = get_db()
    row = conn.execute(
        "SELECT id, company FROM agent_tokens WHERE token_hash = ?",
        (_agent_token_hash(token),),
    ).fetchone()
    if row is None:
        conn.close()
        return {"error": "Invalid token"}, 401

    company = row["company"]

    # The company must still be configured AND agent-managed;
    # otherwise fall back to the local poller instead of double-counting.
    targets = COMPANIES.get(company)
    if targets is None:
        conn.close()
        return {"error": f"Company {company!r} is no longer configured"}, 409
    if targets.get("managed_by", "local") != "agent":
        conn.close()
        return {
            "error": f"Company {company!r} is not agent-managed "
                     "(set managed_by: 'agent' in config.py)"
        }, 409

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(
        payload.get("results"), list
    ):
        conn.close()
        return {"error": "Body must be an object with a 'results' list"}, 400
    results = payload["results"]
    if not results:
        conn.close()
        return {"error": "'results' must not be empty"}, 400
    if len(results) > MAX_INGEST_RESULTS:
        conn.close()
        return {"error": f"Too many results (max {MAX_INGEST_RESULTS})"}, 400

    # ---- validate every result BEFORE writing anything ----
    now = utc_now_str()
    clean = []
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            conn.close()
            return {"error": f"results[{i}] must be an object"}, 400

        check_type = item.get("check_type")
        if check_type not in CHECK_ORDER:
            conn.close()
            return {
                "error": f"results[{i}].check_type must be one of "
                         f"{CHECK_ORDER}"
            }, 400

        status = item.get("status")
        if status not in ("UP", "DOWN", "ERROR"):
            conn.close()
            return {
                "error": f"results[{i}].status must be UP, DOWN or ERROR"
            }, 400

        latency = item.get("latency")
        if latency is not None and not isinstance(latency, (int, float)):
            conn.close()
            return {"error": f"results[{i}].latency must be a number"}, 400

        target = item.get("target")
        if not isinstance(target, str) or not target.strip():
            conn.close()
            return {"error": f"results[{i}].target must be a string"}, 400

        timestamp = item.get("timestamp")
        if timestamp is not None:
            if not isinstance(timestamp, str) or not _valid_ts(timestamp):
                conn.close()
                return {
                    "error": f"results[{i}].timestamp must be "
                             "'YYYY-MM-DD HH:MM:SS' (UTC)"
                }, 400
        else:
            timestamp = now

        clean.append(
            (timestamp, company, check_type, target.strip(), status, latency)
        )

    # ---- write results + feed the state machine ----
    try:
        for ts, comp, check_type, target, status, latency in clean:
            conn.execute(
                "INSERT INTO ping_results "
                "(timestamp, company, target, check_type, status, latency, "
                " source) VALUES (?, ?, ?, ?, ?, ?, 'agent')",
                (ts, comp, target, check_type, status, latency),
            )
        conn.execute(
            "UPDATE agent_tokens SET last_seen = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()
    except sqlite3.Error as error:
        conn.close()
        return {"error": f"database error: {error}"}, 500
    conn.close()

    # Same pipeline as the local poller: outage state machine +
    # alert rules, so agent results open/close outages and alert.
    try:
        import uptime_checker as uc
        for ts, comp, check_type, target, status, latency in clean:
            info = uc.process_check_result(comp, check_type, status, ts)
            uc.evaluate_alerts(comp, check_type, status, latency, info)
    except Exception as error:
        current_app.logger.warning(f"Ingest alert pipeline error: {error}")

    return {
        "ok": True,
        "company": company,
        "accepted": len(clean),
        "server_time": now,
    }


def _valid_ts(value):
    """'YYYY-MM-DD HH:MM:SS' that sqlite datetime() will accept."""
    try:
        datetime.strptime(value, _TS_FMT)
        return True
    except (ValueError, TypeError):
        return False


# ============================================================
# HEALTH (for monitoring the monitor)
# ============================================================

@bp.route("/api/health")
def health():
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT MAX(timestamp) AS last_ts, COUNT(*) AS n "
            "FROM ping_results "
            "WHERE timestamp >= datetime('now', '-1 day')"
        ).fetchone()
        conn.close()
        return {
            "ok": True,
            "db": "up",
            "last_check": row["last_ts"] if row else None,
            "checks_24h": row["n"] if row else 0,
            "companies": list(COMPANIES.keys()),
            "retention_days": RETENTION_DAYS,
        }
    except Exception as error:
        return {"ok": False, "error": str(error)}, 500


def _parse_hours(default=24):
    try:
        hours = int(request.args.get("hours", default))
    except ValueError:
        hours = default
    if hours not in ALLOWED_HOURS:
        hours = default
    return hours


ALLOWED_RESOLUTIONS = ("raw", "hour", "day")


def _parse_resolution(default="raw"):
    """Bucket size for chart APIs. Unknown values fall back to raw."""
    value = (request.args.get("resolution") or default).strip().lower()
    if value not in ALLOWED_RESOLUTIONS:
        return "raw"
    return value


def _bucket_expr(resolution):
    if resolution == "day":
        return "substr(timestamp, 1, 10)"
    return "substr(timestamp, 1, 13) || ':00:00'"


# ============================================================
# NETWORK LATENCY API
# ============================================================

@bp.route("/api/latency")
def latency_data():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    hours = _parse_hours(24)

    company = get_selected_company()

    resolution = _parse_resolution("raw")

    conn = get_db()

    if resolution in ("hour", "day"):
        bucket = _bucket_expr(resolution)
        rows = conn.execute(
            f"""
            SELECT
                {bucket} AS bucket,
                check_type,
                AVG(CASE WHEN status = 'UP' THEN latency END) AS avg_lat,
                SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END) AS up_count
            FROM ping_results
            WHERE timestamp >= datetime('now', ?)
            AND company = ?
            GROUP BY bucket, check_type
            ORDER BY bucket ASC
            """,
            (f"-{hours} hours", company),
        ).fetchall()
        conn.close()

        buckets = sorted({r["bucket"] for r in rows})
        index = {b: i for i, b in enumerate(buckets)}
        series = {
            "Gateway": [None] * len(buckets),
            "Internet": [None] * len(buckets),
            "DNS": [None] * len(buckets),
            "HTTPS": [None] * len(buckets),
        }
        for r in rows:
            check = r["check_type"]
            if check not in series:
                continue
            # Downtime bucket: no UP samples -> None so the
            # Chart.js line cuts (spanGaps=False) and resumes
            # on the next UP bucket.
            value = (
                round(r["avg_lat"], 2)
                if (r["up_count"] or 0) > 0 and r["avg_lat"] is not None
                else None
            )
            series[check][index[r["bucket"]]] = value

        return {
            "labels": buckets,
            "gateway": series["Gateway"],
            "internet": series["Internet"],
            "dns": series["DNS"],
            "https": series["HTTPS"],
            "company": company,
            "hours": hours,
            "resolution": resolution,
        }

    results = conn.execute(
        """
        SELECT
            timestamp,
            check_type,
            target,
            status,
            latency
        FROM ping_results
        WHERE timestamp >= datetime(
            'now',
            ?
        )
        AND company = ?
        ORDER BY timestamp ASC
        """,
        (
            f"-{hours} hours",
            company
        )
    ).fetchall()

    conn.close()

    timestamp_map = {}

    for result in results:

        timestamp = result["timestamp"]

        if timestamp not in timestamp_map:

            timestamp_map[timestamp] = {
                "Gateway": None,
                "Internet": None,
                "DNS": None,
                "HTTPS": None
            }

        check_type = result["check_type"]

        if check_type in timestamp_map[timestamp]:

            timestamp_map[timestamp][check_type] = (
                result["latency"]
            )

    labels = list(
        timestamp_map.keys()
    )

    gateway = []
    internet = []
    dns = []
    https = []

    for timestamp in labels:

        gateway.append(
            timestamp_map[timestamp]["Gateway"]
        )

        internet.append(
            timestamp_map[timestamp]["Internet"]
        )

        dns.append(
            timestamp_map[timestamp]["DNS"]
        )

        https.append(
            timestamp_map[timestamp]["HTTPS"]
        )

    return {
        "labels": labels,
        "gateway": gateway,
        "internet": internet,
        "dns": dns,
        "https": https,
        "company": company,
        "hours": hours,
        "resolution": resolution
    }


# ============================================================
# CURRENT NETWORK STATUS
# ============================================================

@bp.route("/api/status")
def current_status():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    company = get_selected_company()

    conn = get_db()

    results = conn.execute(
        """
        SELECT
            p.check_type,
            p.target,
            p.status,
            p.latency,
            p.timestamp

        FROM ping_results p

        INNER JOIN (

            SELECT
                check_type,
                MAX(id) AS max_id

            FROM ping_results

            WHERE company = ?

            GROUP BY check_type

        ) latest

        ON p.check_type = latest.check_type

        AND p.id = latest.max_id

        ORDER BY p.id
        """,
        (
            company,
        )
    ).fetchall()

    conn.close()

    data = []

    for result in results:

        data.append({
            "check_type": result["check_type"],
            "target": result["target"],
            "status": result["status"],
            "latency": result["latency"],
            "timestamp": result["timestamp"]
        })

    response = {
        "results": data,
        "company": company
    }

    # Agent-managed companies: is the remote agent still pushing?
    # The UI shows a staleness badge when data stops arriving.
    if COMPANIES.get(company, {}).get("managed_by") == "agent":
        conn = get_db()
        row = conn.execute(
            "SELECT MAX(timestamp) AS last_ts FROM ping_results "
            "WHERE company = ? AND source = 'agent'",
            (company,),
        ).fetchone()
        conn.close()
        last_ts = row["last_ts"] if row else None
        stale = True
        age_seconds = None
        if last_ts:
            try:
                seen = datetime.strptime(last_ts, _TS_FMT).replace(
                    tzinfo=timezone.utc
                )
                age_seconds = max(
                    0, int((datetime.now(timezone.utc) - seen).total_seconds())
                )
                stale = age_seconds > AGENT_STALE_SECONDS
            except ValueError:
                pass
        response["agent"] = {
            "managed_by": "agent",
            "last_seen": last_ts,
            "age_seconds": age_seconds,
            "stale": stale,
            "stale_after_seconds": AGENT_STALE_SECONDS,
        }

    return response


# ============================================================
# UPTIME PERCENTAGE
# ============================================================

@bp.route("/api/uptime")
def uptime_data():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    hours = _parse_hours(24)

    company = get_selected_company()

    conn = get_db()

    results = conn.execute(
        """
        SELECT
            check_type,
            COUNT(*) AS total,
            SUM(
                CASE
                    WHEN status = 'UP' THEN 1
                    ELSE 0
                END
            ) AS up_count

        FROM ping_results

        WHERE timestamp >= datetime(
            'now',
            ?
        )

        AND company = ?

        GROUP BY check_type
        """,
        (
            f"-{hours} hours",
            company
        )
    ).fetchall()

    conn.close()

    uptime = {}

    for result in results:

        total = result["total"]

        up_count = result["up_count"] or 0

        if total > 0:

            percent = round(
                (up_count / total) * 100,
                2
            )

        else:

            percent = None

        uptime[result["check_type"]] = percent

    return {
        "uptime": uptime,
        "hours": hours,
        "company": company
    }


# ============================================================
# SPEEDTEST API
# ============================================================

@bp.route("/api/speedtest")
def speedtest_data():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    hours = _parse_hours(24)

    resolution = _parse_resolution("raw")

    # Speedtest only ever measures the connection of the machine
    # actually running speedtest.exe, so it's always tagged with
    # SPEEDTEST_COMPANY (see config.py) rather than whatever
    # company happens to be selected on the dashboard.
    conn = get_db()

    if resolution in ("hour", "day"):
        bucket = _bucket_expr(resolution)
        rows = conn.execute(
            f"""
            SELECT
                {bucket} AS bucket,
                AVG(download) AS download,
                AVG(upload) AS upload,
                AVG(ping) AS ping,
                COUNT(*) AS samples
            FROM speedtest_results
            WHERE timestamp >= datetime('now', ?)
            AND company = ?
            AND status = 'ok'
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (f"-{hours} hours", SPEEDTEST_COMPANY),
        ).fetchall()
        conn.close()
        return {
            "labels": [r["bucket"] for r in rows],
            "download": [
                round(r["download"], 2) if r["download"] is not None else None
                for r in rows
            ],
            "upload": [
                round(r["upload"], 2) if r["upload"] is not None else None
                for r in rows
            ],
            "ping": [
                round(r["ping"], 2) if r["ping"] is not None else None
                for r in rows
            ],
            "company": SPEEDTEST_COMPANY,
            "hours": hours,
            "resolution": resolution,
        }

    results = conn.execute(
        """
        SELECT
            timestamp,
            download,
            upload,
            ping,
            server

        FROM speedtest_results

        WHERE timestamp >= datetime(
            'now',
            ?
        )

        AND company = ?

        AND status = 'ok'

        ORDER BY timestamp ASC
        """,
        (
            f"-{hours} hours",
            SPEEDTEST_COMPANY
        )
    ).fetchall()

    conn.close()

    labels = []
    download = []
    upload = []
    ping = []

    for result in results:

        labels.append(
            result["timestamp"]
        )

        download.append(
            result["download"]
        )

        upload.append(
            result["upload"]
        )

        ping.append(
            result["ping"]
        )

    return {
        "labels": labels,
        "download": download,
        "upload": upload,
        "ping": ping,
        "company": SPEEDTEST_COMPANY,
        "hours": hours,
        "resolution": resolution
    }


# ============================================================
# RECENT SPEEDTEST
# ============================================================

@bp.route("/api/speedtest/recent")
def recent_speedtest():

    if not is_logged_in():

        return {
            "error": "Unauthorized"
        }, 401

    conn = get_db()

    results = conn.execute(
        """
        SELECT
            timestamp,
            download,
            upload,
            ping,
            server

        FROM speedtest_results

        WHERE company = ?

        AND status = 'ok'

        ORDER BY id DESC

        LIMIT 10
        """,
        (
            SPEEDTEST_COMPANY,
        )
    ).fetchall()

    # Failed attempts get recorded too. Surface the latest row only
    # when the MOST RECENT attempt is a failure - then the chart gap
    # has an explanation instead of just silence.
    last = conn.execute(
        "SELECT status, timestamp, server FROM speedtest_results "
        "WHERE company = ? "
        "ORDER BY id DESC "
        "LIMIT 1",
        (SPEEDTEST_COMPANY,),
    ).fetchone()

    conn.close()

    last_failure = None
    if last and last["status"] == "failed":
        last_failure = {
            "timestamp": last["timestamp"],
            "reason": last["server"],
        }

    data = []

    for result in results:

        data.append({
            "timestamp": result["timestamp"],
            "download": result["download"],
            "upload": result["upload"],
            "ping": result["ping"],
            "server": result["server"]
        })

    return {
        "results": data,
        "company": SPEEDTEST_COMPANY,
        "last_failure": last_failure,
    }


@bp.route("/api/sla")
def sla_api():
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    days = _clamp_days()
    company, err = _report_company()
    if err:
        return err
    return {
        "days": days,
        "company": company or "all",
        "rows": _sla_rows(days, company),
    }


@bp.route("/api/heatmap")
def heatmap_api():
    """Daily availability per check (specific company) or per company."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    days = _clamp_days(default=30, minimum=7, maximum=120)
    company, err = _report_company()
    if err:
        return err

    # Per-check rows for one company, per-company rows for aggregate mode.
    name_col = "check_type" if company else "company"

    conn = get_db()
    params = [f"-{days} days"]
    where = "WHERE hour_utc >= datetime('now', ?)"
    if company:
        where += " AND company = ?"
        params.append(company)

    rows = conn.execute(
        f"""
        SELECT substr(hour_utc, 1, 10) AS day,
               {name_col} AS name,
               SUM(total) AS checks,
               SUM(up_count) AS up_checks
        FROM rollup_hourly
        {where}
        GROUP BY day, name
        """,
        params,
    ).fetchall()
    conn.close()

    today = datetime.now(timezone.utc).date()
    days_list = [
        (today - timedelta(days=i)).isoformat()
        for i in range(days - 1, -1, -1)
    ]

    grid = {}
    names = set()
    for r in rows:
        grid.setdefault(r["name"], {})[r["day"]] = (
            r["checks"],
            r["up_checks"],
        )
        names.add(r["name"])

    if company:
        preferred = CHECK_ORDER
    else:
        preferred = list(COMPANIES.keys())
    ordered = [n for n in preferred if n in names]
    ordered += sorted(names - set(preferred))

    series = []
    for name in ordered:
        cells = []
        for day in days_list:
            vals = grid.get(name, {}).get(day)
            if vals and vals[0]:
                cells.append({
                    "day": day,
                    "checks": vals[0],
                    "uptime_pct": round(vals[1] * 100.0 / vals[0], 3),
                })
            else:
                cells.append({"day": day, "checks": 0, "uptime_pct": None})
        series.append({"name": name, "cells": cells})

    return {
        "days": days,
        "company": company or "all",
        "days_list": days_list,
        "series": series,
    }


@bp.route("/api/sla/export")
def sla_export():
    """CSV download of the SLA table."""
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    import csv
    import io

    days = _clamp_days()
    company, err = _report_company()
    if err:
        return err
    rows = _sla_rows(days, company)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "company", "check", "uptime_pct", "checks", "outages",
        "downtime_minutes", "longest_outage_minutes",
        "avg_latency_ms", "worst_hourly_p95_ms",
    ])
    for r in rows:
        writer.writerow([
            r["company"], r["check_type"], r["uptime_pct"], r["checks"],
            r["outages"], r["downtime_minutes"], r["longest_minutes"],
            r["avg_latency"], r["p95_latency"],
        ])

    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="sla_{company or "all"}_{days}d.csv"'
    )
    return resp


# ============================================================
# BACKUPS
# ============================================================

@bp.route("/api/backups")
def backups_list_api():
    if not is_logged_in():
        return {"error": "Unauthorized"}, 401
    import backup as backup_mod
    return {
        "backups": backup_mod.list_backups(),
        "keep": backup_mod.backup_keep(),
        "interval_seconds": backup_mod.backup_interval(),
        "dir": backup_mod.backup_dir(),
    }


@bp.route("/api/backup", methods=["POST"])
@require_admin
def backup_create_api():
    """Snapshot the live database now (admin only, CSRF-protected)."""
    import backup as backup_mod

    token = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
    )
    if not check_csrf(token):
        return {"error": "CSRF token missing or expired"}, 400

    try:
        path = backup_mod.create_backup()
    except Exception as error:
        return {"error": str(error)}, 500

    return {
        "ok": True,
        "file": os.path.basename(path),
        "size": os.path.getsize(path),
    }


