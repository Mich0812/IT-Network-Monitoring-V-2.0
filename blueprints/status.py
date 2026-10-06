"""Public status page /status - moved from app.py (Phase 13).

Deliberately unauthenticated: the login page links to it so
visitors can check service status without an account. It only
ever renders company names, check names, status, latency,
uptime % and incident summaries - never target addresses,
usernames or anything else internal.

The PUBLIC_STATUS kill-switch is read at request time through
live_flags() so tests can patch it on the app module and this
route still sees the new value.
"""

from datetime import datetime, timezone

from flask import (
    Blueprint,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from config import COMPANIES, STATUS_STALE_SECONDS
from core import (
    CHECK_ORDER,
    _TS_FMT,
    _sla_rows,
    get_db,
    group_outages,
    live_flags,
    utc_now_str,
)

bp = Blueprint("status", __name__)


# ============================================================
# PUBLIC STATUS PAGE (no login required)
#
# Deliberately unauthenticated: the login page links to it so
# visitors can check service status without an account. It only
# ever renders company names, check names, status, latency,
# uptime % and incident summaries - never target addresses,
# usernames or anything else internal.
# ============================================================

# state key -> (label, sla-pill CSS class)
_PUBLIC_STATES = {
    "operational": ("Operational", "sla-good"),
    "partial": ("Partial outage", "sla-warn"),
    "major": ("Major outage", "sla-bad"),
    "unknown": ("Unknown", "sla-none"),
}


def _public_companies():
    """Company names published on /status, in config order."""
    return [
        name for name, targets in COMPANIES.items()
        if targets.get("public", True)
    ]


def _age_seconds(ts, now=None):
    """UTC 'YYYY-MM-DD HH:MM:SS' -> seconds elapsed (None if junk)."""
    if not ts:
        return None
    try:
        then = datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - then).total_seconds()))


def _fmt_age(seconds):
    """Human label for an age: '12s', '7m', '3h', '2d', 'never'."""
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 129600:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _fmt_duration(seconds):
    """Human label for a duration in seconds."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 129600:
        return f"{seconds // 3600}h"
    return f"{round(seconds / 86400, 1)}d"


_CHECK_VIEW_CLASSES = {
    "up": "sla-good",
    "down": "sla-bad",
    "unknown": "sla-none",
}


def _check_view(check):
    """(view_state, label, pill css class) for one check dict."""
    if check.get("fresh") and check.get("status"):
        if check["status"] == "UP":
            return "up", "Up", _CHECK_VIEW_CLASSES["up"]
        return "down", "Down", _CHECK_VIEW_CLASSES["down"]
    return "unknown", "Unknown", _CHECK_VIEW_CLASSES["unknown"]


def _company_public_state(checks):
    """Aggregate a company's checks into one status pill state.

    Any fresh DOWN wins (partial, or major when all four are down).
    No DOWN but some stale/missing -> Unknown (the monitor itself
    may be dead; never claim "Operational" without fresh data).
    """
    known = [c for c in checks if c.get("fresh") and c.get("status")]
    down = [c for c in known if c["status"] != "UP"]
    if not known:
        return "unknown"
    if down:
        return "major" if len(down) == len(CHECK_ORDER) else "partial"
    if len(known) < len(CHECK_ORDER):
        return "unknown"
    return "operational"


def _latest_public_checks(companies):
    """Latest row per (company, check_type), every CHECK_ORDER slot.

    Returns {company: [check dict, ...]} where a check dict is
    name/status/latency/timestamp/age_seconds/fresh. Checks with no
    data at all come back as fresh=False, status=None (=> Unknown).
    """
    result = {}
    if not companies:
        return result

    conn = get_db()
    placeholders = ",".join("?" * len(companies))
    rows = conn.execute(
        f"""
        SELECT p.company, p.check_type, p.status, p.latency, p.timestamp
        FROM ping_results p
        JOIN (SELECT company, check_type, MAX(id) AS mid
              FROM ping_results
              WHERE company IN ({placeholders})
              GROUP BY company, check_type) m
          ON p.id = m.mid
        """,
        list(companies),
    ).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    latest = {}
    for r in rows:
        age = _age_seconds(r["timestamp"], now)
        latest[(r["company"], r["check_type"])] = {
            "name": r["check_type"],
            "status": r["status"],
            "latency": r["latency"],
            "timestamp": r["timestamp"],
            "age_seconds": age,
            "fresh": age is not None and age <= STATUS_STALE_SECONDS,
        }

    empty = {
        "name": None, "status": None, "latency": None,
        "timestamp": None, "age_seconds": None, "fresh": False,
    }
    for company in companies:
        checks = []
        for name in CHECK_ORDER:
            check = dict(latest.get((company, name)) or empty)
            check["name"] = name
            checks.append(check)
        result[company] = checks
    return result


def _public_incidents(company=None, hours=720):
    """Outages grouped into incidents (same query as /api/incidents,
    minus the auth check) - default: last 30 days, all companies."""
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
    return group_outages(rows)


def _public_uptime(company):
    """{check_type: {1: pct|None, 7: ..., 30: ...}} via _sla_rows."""
    out = {}
    for days in (1, 7, 30):
        for row in _sla_rows(days, company):
            slot = out.setdefault(
                row["check_type"], {1: None, 7: None, 30: None}
            )
            slot[days] = row["uptime_pct"]
    return out


def _active_maintenance(companies):
    """Companies currently inside a maintenance window from config.

    Reuses the checker's window logic (uptime_checker._in_maintenance)
    so the public banner and the alert suppression can never disagree.
    The status page must never 500 over banner trouble, hence the guard.
    """
    try:
        import uptime_checker as uc
    except Exception:
        return []
    active = []
    for name in companies:
        try:
            if uc._in_maintenance(name):
                active.append(name)
        except Exception:
            continue
    return active


@bp.route("/status")
def public_status():
    """Public status page - intentionally NO login check.

    /status                -> every published company
    /status?company=X      -> one company's detail page
    Unknown/private X      -> friendly 404
    PUBLIC_STATUS=False    -> redirect to login
    """
    if not live_flags().PUBLIC_STATUS:
        return redirect(url_for("auth.login"))

    companies = _public_companies()
    company = (request.args.get("company") or "").strip()
    now_label = utc_now_str()

    def render(context, status=200):
        resp = make_response(
            render_template("public_status.html", **context), status
        )
        # Status data must never be served stale by a proxy.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    if company and company not in companies:
        return render({
            "error": f"Unknown company: {company}",
            "companies": companies,
            "now_label": now_label,
        }, status=404)

    checks_map = _latest_public_checks(companies)
    incidents_all = _public_incidents()

    # ---- overview ----
    if not company:
        open_counts = {}
        for inc in incidents_all:
            if inc["open"]:
                open_counts[inc["company"]] = (
                    open_counts.get(inc["company"], 0) + 1
                )

        cards = []
        any_stale = False
        oldest_stale = None
        for name in companies:
            checks = checks_map.get(name, [])
            state = _company_public_state(checks)
            state_label, state_class = _PUBLIC_STATES[state]

            for c in checks:
                view, label, view_class = _check_view(c)
                c["view"], c["view_label"] = view, label
                c["view_class"] = view_class
                if view == "unknown":
                    any_stale = True
                    age = c["age_seconds"]
                    if age is not None and (
                        oldest_stale is None or age > oldest_stale
                    ):
                        oldest_stale = age

            ages = [c["age_seconds"] for c in checks
                    if c["age_seconds"] is not None]
            last_age = min(ages) if ages else None
            cards.append({
                "name": name,
                "state": state,
                "state_label": state_label,
                "state_class": state_class,
                "checks": checks,
                "down_count": sum(
                    1 for c in checks
                    if c["view"] == "down"
                ),
                "last_age": last_age,
                "last_label": _fmt_age(last_age),
                "open_incidents": open_counts.get(name, 0),
            })

        stale_banner = None
        if any_stale:
            stale_banner = (
                f"Monitoring has not reported for "
                f"{_fmt_age(oldest_stale)} - statuses may be out of date."
                if oldest_stale is not None else
                "Monitoring has not reported any checks yet."
            )

        return render({
            "companies": companies,
            "cards": cards,
            "stale_banner": stale_banner,
            "maintenance_active": _active_maintenance(companies),
            "now_label": now_label,
        })

    # ---- detail ----
    checks = checks_map.get(company, [])
    state = _company_public_state(checks)
    state_label, state_class = _PUBLIC_STATES[state]
    for c in checks:
        view, label, view_class = _check_view(c)
        c["view"], c["view_label"] = view, label
        c["view_class"] = view_class
        c["age_label"] = _fmt_age(c["age_seconds"])

    ages = [c["age_seconds"] for c in checks if c["age_seconds"] is not None]
    last_age = min(ages) if ages else None

    any_stale = any(c["view"] == "unknown" for c in checks)
    stale_banner = (
        f"Monitoring has not reported for {_fmt_age(last_age)} - "
        f"statuses may be out of date."
        if any_stale and last_age is not None else
        "Monitoring has not reported any checks yet."
        if any_stale else None
    )

    uptime = _public_uptime(company)
    for c in checks:
        c["uptime"] = uptime.get(c["name"], {1: None, 7: None, 30: None})

    incidents = _public_incidents(company=company)[:10]
    for inc in incidents:
        inc["duration_label"] = _fmt_duration(inc["duration"])
    ongoing = next((i for i in incidents if i["open"]), None)

    return render({
        "companies": companies,
        "company": company,
        "maintenance_active": _active_maintenance([company]),
        "checks": checks,
        "state": state,
        "state_label": state_label,
        "state_class": state_class,
        "last_label": _fmt_age(last_age),
        "stale_banner": stale_banner,
        "uptime": uptime,
        "incidents": incidents,
        "ongoing": ongoing,
        "now_label": now_label,
    })


