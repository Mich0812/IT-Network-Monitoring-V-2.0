# ================================================================
# COMPANY / SUBSIDIARY TARGETS
#
# Add one entry per site you want to monitor. Each one gets its
# own Gateway / Internet / DNS / HTTPS checks, its own history,
# and shows up in the "Company" switcher on the dashboard.
#
# "gateway" can be either:
#   - "auto"      -> auto-detect THIS machine's own default
#                     gateway (only meaningful for whichever site
#                     this script is actually running on)
#   - an IP string -> ping that address directly. For a remote
#                     subsidiary this is usually their router/
#                     firewall's public IP, or an internal IP if
#                     this machine reaches them over a VPN/WAN
#                     link.
#
# "internet" / "dns" can be any reachable IP / hostname.
# "https" must be a full URL.
#
# Optional per-company key:
#   "managed_by": "agent"  -> this machine does NOT poll the site.
#       A remote agent (agent.py running at that site) pushes the
#       results in via POST /api/ingest using a per-company token
#       (manage tokens on the /agents page). The targets above then
#       only document what the site checks; the agent carries its
#       own copy. Default (or "local") = polled normally from here.
#   "public": False  -> hide this company from the public status
#       page (/status). Default (or True) = shown.
# ================================================================

COMPANIES = {

    "Company A": {
        "gateway": "auto",
        "internet": "1.1.1.1",
        "dns": "cloudflare.com",
        "https": "https://www.cloudflare.com"
    },

    "Company B": {
        "gateway": "203.0.113.1",
        "internet": "8.8.8.8",
        "dns": "google.com",
        "https": "https://www.google.com"
    }

}


# A real speedtest can only measure the bandwidth of the machine
# actually running speedtest.exe - it can't measure a remote
# subsidiary's connection unless something is also running there.
# So speedtest results are recorded under a single company rather
# than looped per company. Point this at whichever entry is the
# site this machine is physically on.
SPEEDTEST_COMPANY = "Company A"

# ---- Operational defaults (env-overridable in uptime_checker/app) ----
# Kept here so all tuning lives in one place.
CHECK_INTERVAL = 60          # seconds between network check cycles
SPEEDTEST_INTERVAL = 1800   # seconds between speedtests (30 min)
RETENTION_DAYS = 90         # prune rows older than this

# Remote agent (managed_by: "agent") results older than this count
# as stale -> the dashboard shows a warning badge. 5 minutes allows
# a couple of missed pushes plus network hiccups.
AGENT_STALE_SECONDS = 300

# ================================================================
# PUBLIC STATUS PAGE (/status - no login required)
# ================================================================

# Master switch for the public status page. When False, /status
# redirects visitors to the login page.
PUBLIC_STATUS = True

# A latest check older than this shows as "Unknown" on the public
# page (instead of pretending everything is fine when the monitor
# itself stopped reporting). 3 missed poll cycles at 60s each.
STATUS_STALE_SECONDS = 180

# ================================================================
# ALERTING
#
# Fires when a check fails `down_failures` times in a row (flap
# guard) or when latency exceeds `latency_ms` for `latency_failures`
# checks. Suppressed during the maintenance windows below. Every
# sent alert is recorded in `notifications` (dedup + audit).
# ================================================================

ALERTS = {
    "enabled": True,

    # Consecutive DOWN results before notifying (flap guard).
    "down_failures": 2,

    # Latency alert: > latency_ms for N consecutive checks. 0 = off.
    "latency_ms": 0,
    "latency_failures": 3,

    # Don't re-send the same company/check alert more often than this.
    "cooldown_seconds": 1800,

    # Optional SMTP. password may come from SMTP_PASSWORD env var.
    "email": {
        "enabled": False,
        "host": "smtp.example.com",
        "port": 587,
        "starttls": True,
        "username": "",
        "password": "",
        "from": "uptime-monitor@example.com",
        "to": [],
    },

    # Optional Teams/Slack incoming webhook (JSON POST).
    "webhook": {
        "enabled": False,
        "url": "",
    },
}

# Maintenance windows: alerts silenced while inside one.
# Times are 24h HH:MM, local time. Days: 0=Mon ... 6=Sun, or "all".
MAINTENANCE = [
    # {"company": "Company A", "days": "all", "start": "02:00", "end": "03:00"},
]


def is_valid_company(name):

    return name in COMPANIES


def default_company():

    return next(iter(COMPANIES))


def validate_config():
    """Fail fast on bad config. Called at startup by app + checker."""
    import ipaddress
    import urllib.parse

    if not COMPANIES:
        raise ValueError("COMPANIES is empty - add at least one site.")

    if SPEEDTEST_COMPANY not in COMPANIES:
        raise ValueError(
            f"SPEEDTEST_COMPANY={SPEEDTEST_COMPANY!r} not in COMPANIES. "
            f"Valid: {list(COMPANIES)}"
        )

    if COMPANIES[SPEEDTEST_COMPANY].get("managed_by", "local") == "agent":
        raise ValueError(
            "SPEEDTEST_COMPANY cannot be managed by a remote agent - "
            "a speedtest can only measure THIS machine's link."
        )

    for name, targets in COMPANIES.items():
        if not isinstance(targets, dict):
            raise ValueError(f"[{name}] entry must be a dict.")
        for key in ("gateway", "internet", "dns", "https"):
            if key not in targets or not targets[key]:
                raise ValueError(f"[{name}] missing required key: {key!r}.")

        managed_by = targets.get("managed_by", "local")
        if managed_by not in ("local", "agent"):
            raise ValueError(
                f"[{name}] managed_by must be 'local' or 'agent', "
                f"got {managed_by!r}."
            )

        public = targets.get("public", True)
        if not isinstance(public, bool):
            raise ValueError(
                f"[{name}] public must be True or False, got {public!r}."
            )

        gw = targets["gateway"]
        if gw != "auto":
            try:
                ipaddress.ip_address(gw)
            except ValueError:
                raise ValueError(
                    f"[{name}] gateway must be 'auto' or an IP, got {gw!r}."
                )

        # internet should be pingable IP/hostname - light sanity check
        if not str(targets["internet"]).strip():
            raise ValueError(f"[{name}] internet target is empty.")
        if not str(targets["dns"]).strip():
            raise ValueError(f"[{name}] dns target is empty.")

        https = targets["https"]
        parsed = urllib.parse.urlparse(https)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"[{name}] https must be a full URL, got {https!r}."
            )

    if CHECK_INTERVAL < 10:
        raise ValueError("CHECK_INTERVAL too low - minimum 10s.")
    if SPEEDTEST_INTERVAL < 300:
        raise ValueError("SPEEDTEST_INTERVAL too low - minimum 300s.")

    # ---- Public status page sanity ----
    if not isinstance(PUBLIC_STATUS, bool):
        raise ValueError("PUBLIC_STATUS must be True or False.")
    if not isinstance(STATUS_STALE_SECONDS, int) or STATUS_STALE_SECONDS < 60:
        raise ValueError("STATUS_STALE_SECONDS must be an int >= 60.")

    # ---- Alerting config sanity ----
    if not isinstance(ALERTS, dict):
        raise ValueError("ALERTS must be a dict.")
    if ALERTS.get("down_failures", 1) < 1:
        raise ValueError("ALERTS['down_failures'] must be >= 1.")
    if ALERTS.get("cooldown_seconds", 0) < 60:
        raise ValueError("ALERTS['cooldown_seconds'] must be >= 60.")

    email = ALERTS.get("email", {})
    if email.get("enabled"):
        for key in ("host", "from"):
            if not email.get(key):
                raise ValueError(f"ALERTS['email'] enabled but {key!r} is empty.")
        recipients = email.get("to") or []
        if not recipients:
            raise ValueError("ALERTS['email'] enabled but 'to' is empty.")
        if not isinstance(recipients, (list, tuple)):
            raise ValueError("ALERTS['email']['to'] must be a list.")

    webhook = ALERTS.get("webhook", {})
    if webhook.get("enabled") and not webhook.get("url", "").startswith(
        ("http://", "https://")
    ):
        raise ValueError("ALERTS['webhook'] enabled but 'url' is not http(s).")

    # ---- Maintenance windows sanity ----
    if not isinstance(MAINTENANCE, list):
        raise ValueError("MAINTENANCE must be a list.")

    def _hhmm(value, label):
        try:
            hh, mm = value.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except Exception:
            raise ValueError(f"MAINTENANCE {label} must be HH:MM, got {value!r}.")

    for window in MAINTENANCE:
        if not isinstance(window, dict):
            raise ValueError("Each MAINTENANCE entry must be a dict.")
        company = window.get("company")
        if company is not None and company not in COMPANIES:
            raise ValueError(
                f"MAINTENANCE company {company!r} not in COMPANIES."
            )
        _hhmm(window.get("start", ""), "start")
        _hhmm(window.get("end", ""), "end")
        days = window.get("days", "all")
        if days != "all" and (
            not isinstance(days, (list, tuple))
            or not all(isinstance(d, int) and 0 <= d <= 6 for d in days)
        ):
            raise ValueError(
                "MAINTENANCE 'days' must be 'all' or list of 0..6."
            )
