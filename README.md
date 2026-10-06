# Uptime Monitor — What changed & how to run it

## Folder layout (matches your setup)

```
C:\Users\Michael\Desktop\Projects\Uptime Checker\
├── app.py
├── uptime_checker.py
├── config.py
├── speedtest.exe          <- your Ookla Speedtest CLI executable goes here
├── templates\
│   ├── login.html
│   └── dashboard.html
└── static\
    └── style.css
```

Flask needs `login.html` and `dashboard.html` inside a `templates` folder,
and `style.css` inside a `static` folder — that's why they're nested that
way in this download.

## Running it

```
pip install flask werkzeug
python app.py
```

Then open **http://127.0.0.1:5000**. By default, `app.py` also starts the
network/speedtest monitor loop in the background automatically, so this one
command is all you need — you don't have to run `uptime_checker.py`
separately unless you want to (set `AUTO_START_MONITOR=0` if you'd rather
run it as its own process, e.g. via Task Scheduler).

The first time you run it, it prints a randomly generated admin password
to the console — **save it**, it won't be shown again. If you'd rather set
your own, run it with environment variables instead:

```
set ADMIN_USERNAME=michael
set ADMIN_PASSWORD=your-own-password
python app.py
```

## Bugs fixed

- **Speedtest was using the wrong CLI syntax/JSON format.** The code was
  parsing the JSON shape of the old third-party `speedtest-cli` tool, but
  you're using the official Ookla CLI, which needs
  `--accept-license --accept-gdpr` (otherwise it silently hangs waiting on
  an interactive prompt the first time it runs) and returns a different
  JSON structure (`download.bandwidth` in bytes/sec, not bits, etc.).
  Both are fixed in `uptime_checker.py`. Point it at a different `.exe`
  location with the `SPEEDTEST_PATH` environment variable if needed.
- **Broken HTML.** `login.html` and `dashboard.html` had stray ` ``` `
  markdown code-fence lines literally embedded in the source (visible,
  broken markup) — removed.
- **Missing CSS.** The login page (`.login-container`, `.form-group`,
  `.login-button`, `.error-message`, etc.) and the dashboard's status
  cards (`.status-icon`, `.status-label`, `.status-value`,
  `.latency-value`) referenced classes that didn't exist anywhere in
  `style.css`, so both rendered mostly unstyled. Added.
- **Hardcoded secret key.** `app.secret_key` was a fixed string in the
  source, so anyone who read the file could forge login sessions. It's
  now randomly generated on first run and saved to `secret_key.txt` (or
  read from a `SECRET_KEY` env var if you set one).
- **Hardcoded `admin` / `admin123`**, shown right on the login page. That
  banner is removed, and the account is now created with a random
  password (or your own via env vars) instead of a fixed one.
- **No brute-force protection** on `/login`. Added a simple lockout after
  5 failed attempts from the same IP within 5 minutes.

## New features (per your criteria)

- **Uptime %.** Each status card (Gateway / Internet / DNS / HTTPS) now
  shows a rolling 24-hour uptime percentage, backed by a new
  `/api/uptime` endpoint.
- **Time ranges.** Latency and speedtest charts are now filterable by
  **1 hour / 12 hours / 1 day / 30 days**, as you specified (previously
  they only offered 1h/6h/24h/7d/30d).
- **One-command startup.** `python app.py` now runs the dashboard and the
  background monitor loop together.
- **Line/Bar toggle.** Both the Latency and Speedtest graphs now have a
  Line/Bar switch in their card header. Switching doesn't re-fetch data -
  it just redraws the chart you already have.
- **Dots hidden until hover.** On the line view, the connecting line
  stays clean with no dots, and only the exact point(s) under your
  cursor light up along with the tooltip - achieved via Chart.js's
  `pointRadius: 0` / `pointHoverRadius` plus `interaction: {mode: "index",
  intersect: false}`, so you don't have to land exactly on a dot.
- **Multi-company / subsidiary monitoring.** Added `config.py`, where you
  list every site you want to monitor - each with its own Gateway /
  Internet / DNS / HTTPS targets. A "Monitoring" dropdown in the sidebar
  switches the whole dashboard (status cards, uptime %, latency chart,
  and the recent-results table) between companies. See **Multi-company
  setup** below for details.

## Multi-company setup

Open `config.py`:

```python
COMPANIES = {
    "Company A": {
        "gateway": "auto",          # this machine's own router
        "internet": "1.1.1.1",
        "dns": "cloudflare.com",
        "https": "https://www.cloudflare.com"
    },
    "Company B": {
        "gateway": "203.0.113.1",   # a subsidiary's router/firewall IP
        "internet": "8.8.8.8",
        "dns": "google.com",
        "https": "https://www.google.com"
    }
}

SPEEDTEST_COMPANY = "Company A"
```

- Add, rename, or remove entries freely - the dashboard's "Monitoring"
  dropdown and database automatically pick up whatever's in this file.
- `"gateway": "auto"` only makes sense for whichever company is the site
  this script is physically running on - it detects *this machine's*
  own default gateway. For every other entry, use a real IP address this
  machine can actually reach (a subsidiary's public router/firewall IP,
  or an internal one if you're linked by VPN/WAN).
- **Speedtest is intentionally not per-company.** A speedtest can only
  measure the bandwidth of the machine actually running `speedtest.exe` -
  it can't remotely test a subsidiary's connection unless something is
  also running there. So speedtest results are always tagged with
  whichever company you set as `SPEEDTEST_COMPANY`, regardless of which
  company is selected on the dashboard. If you select a different
  company, a small note appears above the Speedtest graph saying so.
- Existing databases are migrated automatically - old rows are tagged
  `'Company A'` so nothing breaks when you update.
- Checks for every company run every `CHECK_INTERVAL` (default 60s),
  one after another, so with more companies each check cycle takes a bit
  longer overall (though still well under the 60s interval in practice).

## Notes / things worth doing next

- Production server: `app.py` now uses Waitress when installed
  (`pip install -r requirements.txt`), Flask dev server otherwise.
- CSRF token added to login form (session-based, no extra dep).
- Login rate-limit is now persistent in SQLite (`login_failures` table).

## V2.1 improvements (implemented)

- **UTC everywhere:** `uptime_checker.py` writes `datetime.now(timezone.utc)`,
  matching `datetime('now')` queries. Old local-time rows age out via retention.
- **True ping latency:** parses `time=12ms` from ping output instead of
  process wall-clock. macOS `route -n get default` support added.
- **Speedtest:** default `SPEEDTEST_INTERVAL=1800` (30min, env-overridable),
  runs in background thread so checks never stall.
- **DB:** WAL mode + `busy_timeout`, indexes on `(company,timestamp)`,
  auto-prune `RETENTION_DAYS=90`.
- **Config validation:** `config.validate_config()` fails fast on bad IP/URL.
- **Health:** `GET /api/health` (auth) shows last check + 24h count.
- **Dashboard:** uptime % follows latency range (1h/12h/24h/30d captions update),
  stale-data warning if newest check >3min old.
- **Ops:** `requirements.txt`, `.gitignore` (`uptime.db`, `secret_key.txt`,
  `monitor.log`), file+console logging to `monitor.log`.
- Env knobs: `CHECK_INTERVAL`, `SPEEDTEST_INTERVAL`, `RETENTION_DAYS`,
  `GATEWAY_IP`, `SPEEDTEST_PATH`, `SECRET_KEY`, `ADMIN_USERNAME/ADMIN_PASSWORD`,
  `SECURE_COOKIES=1` behind HTTPS, `AUTO_START_MONITOR=0` to split collector.

## V2.2 Phase 1 — incidents, rollups, alerting

- **Outages** (`outages` table): UP→DOWN opens a row, DOWN→UP closes it with
  duration. State survives restarts (seeded at startup; stale open rows whose
  check is now UP get closed). Open outages = active incidents.
- **Hourly rollups** (`rollup_hourly`): per (company, check_type, hour) total,
  up%, avg/p95/max latency. Rebuilt hourly over the last 48h, UPSERT-safe —
  30-day reports don't scan `ping_results`.
- **Alerting** — `config.ALERTS`:
  - `down_failures` consecutive fails before notifying (flap guard).
  - Optional `latency_ms` threshold for `latency_failures` checks.
  - `cooldown_seconds` dedup (memory + `notifications` table, survives restart).
  - Channels: SMTP (`ALERTS["email"]`, password via `SMTP_PASSWORD` env) and
    Slack/Teams webhook (`ALERTS["webhook"]["url"]`). Disabled by default;
    when enabled but misconfigured, alerts still land in `monitor.log`.
  - **Maintenance windows** (`MAINTENANCE` list): alerts silenced per company,
    `days`/`start`/`end` HH:MM, handles windows crossing midnight.
  - Recovery notice sent when a previously-alerted check returns UP.
- **Log rotation**: `monitor.log` now rotates at 5 MB × 5 backups.
- **New API** (all auth-required):
  - `GET /api/outages?hours=&company=&open=1` — incidents, open included with
    `open=1`.
  - `GET /api/notifications?limit=` — alert audit trail.
  - `GET /api/rollups?company=&hours=` — pre-aggregated availability.
  - `POST /api/alerts/test` — fire a test alert through configured channels.

## V2.2 Phase 2 — live UI

- **Incident banner**: red alert strip under the dashboard header whenever an
  outage is currently open for the selected company (lists each check + start
  time, links to incident history).
- **Incidents table**: last 24h of outages per company — check, started,
  ended/ongoing, duration (s/m/h), cause. Auto-refreshes every 30s.
- **Toasts**: new alert notifications pop bottom-right (down = red, recovery =
  green, test = blue), auto-dismiss 8s, stack capped at 4. Fed by
  `/api/notifications` polling — first load baselines so history isn't re-toasted.
- **Dark mode**: sidebar toggle (moon/sun), honors
  `prefers-color-scheme` on first visit, persisted in `localStorage`.
  Implemented as a second token block (`[data-theme="dark"]` in `style.css`) —
  every component reads `:root` vars, so one override repaints the app;
  Chart.js re-reads tokens + re-renders on switch. Theme bootstrap runs
  pre-paint on login/dashboard/overview (no white flash).
- **Shareable company URL**: dashboard reads `?company=` (from `/overview`
  "Open →" links) and writes it back via `history.replaceState` on switch —
  back/forward + bookmarkable per-site views. sessionStorage hand-off kept as
  fallback.
- **Skeletons**: status cards shimmer while `data-state="loading"` instead of
  static "Loading…" text (dark-mode aware).

Config example:

```python
ALERTS = {
    "enabled": True,
    "down_failures": 2,
    "latency_ms": 200,        # 0 disables the latency rule
    "cooldown_seconds": 1800,
    "email": {"enabled": True, "host": "smtp.office365.com", "port": 587,
              "from": "noreply@yourco.com", "to": ["noc@yourco.com"]},
    "webhook": {"enabled": True, "url": "https://outlook.office.com/webhook/..."},
}
MAINTENANCE = [
    {"company": "Company A", "days": [6], "start": "02:00", "end": "04:00"},
]
```

## V2.2 Phase 2 fixes — dark mode polish

- **Login page theme toggle**: the login screen had no toggle at all (only a
  pre-paint bootstrap), so a user sitting on `/login` could not switch themes.
  Added a compact top-right toggle (`Dark mode` ⇄ `Light mode`, `aria-pressed`
  synced).
- **CSS cascade fix**: `.theme-toggle { width: 100% }` (later in the file) beat
  the earlier `.login-theme-toggle` overrides and stretched the button
  full-width — selector bumped to `.login-main .login-theme-toggle`.
- **Label sync fix**: the login label sync ran in `<head>` before the button
  existed, leaving "Dark mode" shown while in dark mode — now runs on
  `DOMContentLoaded`.
- **Overview flash fix**: theme bootstrap moved into `<head>` so the saved
  theme applies before first paint (was body-end → light flash).
- **Template caching fix**: `TEMPLATES_AUTO_RELOAD = True` — with `debug=False`
  Jinja cached templates forever, so template edits were silently served stale.
- Stylesheet cache-busted (`style.css?v=3`).

## V2.2 Phase 3 — parallel polling, RBAC

- **Parallel polling**: each cycle now submits every company to a
  `ThreadPoolExecutor` (`POLL_WORKERS` env, default = company count) instead of
  probing companies one after another. Cycle time ≈ slowest company instead of
  the sum; all rows are still saved and the outage/alert state machine still
  runs single-threaded in config order. Startup logs
  `Parallel polling: N worker(s)`; each cycle logs `Cycle done in X.Xs`.
- **RBAC**: `users.role` (`admin`/`viewer`, auto-migrated on existing DBs —
  current accounts default to `admin`).
  - **Admin**: everything, plus the new **Users** page (`/users`) — create
    users, reset passwords, delete accounts. Guards: no self-delete, no
    deleting the last admin, unique usernames (case-insensitive), passwords
    min 8 chars, CSRF-protected forms with status/error banners.
  - **Viewer**: read-only — can open dashboard/overview and all GET APIs;
    `403` on `/users*` and `POST /api/alerts/test` (JSON `403` for APIs).
  - Sidebar shows a **Users** link and a role badge (e.g. `ADMIN`) only where
    applicable; role is injected to all templates via a context processor.
  - `require_admin` decorator handles session check + role check per route
    (401 JSON for API clients, redirect for pages).
- Verified end-to-end: 37 automated checks (admin/viewer/anonymous paths,
  banners, guards) + browser walkthrough of the Users page.

## V2.2 Phase 4 — reports: SLA + heatmap

- New **Reports** page (`/reports`, sidebar link on every page, all roles):
  - **Controls**: company selector (incl. *All companies*), period buttons
    (7 / 30 / 90 days), **Export CSV** (downloads the SLA table).
  - **SLA summary table** per company/check: uptime % (color-coded pill),
    checks, outage count, total downtime, longest outage, average latency
    (weighted across hours) and *worst hourly p95* — uptime/latency come from
    `rollup_hourly`, outage stats from the `outages` log (open outages count
    with time-so-far as duration).
  - **Availability heatmap**: one cell per day, rows = checks for one company
    or companies in aggregate mode; colors 100% / ≥99.9% / 99–99.9% / <99% /
    no-data, hover tooltips with exact % and check counts, weekly date ticks.
  - **Backups card**: lists backup files with size/date, *Back up now* button
    (admin, CSRF-protected), restore instructions.
- New APIs (auth required): `GET /api/sla`, `GET /api/heatmap`,
  `GET /api/sla/export` (CSV), `GET /api/backups`, `POST /api/backup`
  (admin). Days are clamped (SLA 1–365, heatmap 7–120), unknown company → 400.
- Retention: `rollup_hourly` is now pruned with the same `RETENTION_DAYS`
  window as the raw rows (both `app.py` and `uptime_checker.py` prunes).

## V2.2 Phase 5 — tests, backups, production/TLS docs

### Automated tests (pytest)

- `tests/` — **62 tests**, all green: config validation, outage state
  machine, rollup math + retention, alert engine (flap guard, recovery,
  cooldown incl. cross-restart, maintenance windows, latency rule, dispatch
  audit rows), auth/RBAC (anon 401/redirects, viewer 403s, admin CRUD
  guards, real login flow), reports APIs (SLA math, heatmap aggregation,
  CSV, clamping), backups (round trip, retention, API guards).
- Run with:

  ```
  pip install -r requirements.txt
  python -m pytest -q
  ```

- Tests are fully sandboxed: `tests/conftest.py` points `DB_PATH`,
  `LOG_PATH`, `BACKUP_DIR` and `SECRET_KEY` at a temp directory **before**
  importing the app, so a test run can never touch the real `uptime.db`,
  `monitor.log` or `backups/`.
- The suite immediately paid off: it caught `_cooldown_ok` reading the UTC
  `notifications.sent_at` string with a timezone-naive `.timestamp()`
  (interpreted as local time → cooldown windows shifted by the machine's
  UTC offset). Fixed by attaching `timezone.utc`.

### Database backups

- New `backup.py` uses the SQLite **online backup API** — safe while the
  app is running (WAL included) — plus an integrity check (`PRAGMA
  integrity_check`) and retention pruning.
  - One-shot: `python backup.py` (or on a schedule via Task Scheduler).
  - In-app thread (started by `app.py`): one backup at startup, then every
    `BACKUP_INTERVAL`; keeps the newest `BACKUP_KEEP`. Disable with
    `BACKUP_AUTO=0`.
  - Files land in `backups/uptime-YYYYMMDD-HHMMSS.db` (git-ignored).
  - **Restore**: stop the app → replace `uptime.db` with the backup file
    (delete `uptime.db-wal` / `uptime.db-shm`) → start the app.
  - Manual trigger: *Back up now* on `/reports` (admin) or
    `POST /api/backup`; list via `GET /api/backups`.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DB_PATH` | `./uptime.db` | Alternate database file (tests, staging) |
| `LOG_PATH` | `./monitor.log` | Alternate monitor log file |
| `HOST` / `PORT` | `0.0.0.0` / `5000` | Bind address — use `HOST=127.0.0.1` behind a reverse proxy |
| `BACKUP_DIR` | `./backups` | Where snapshots are written |
| `BACKUP_INTERVAL` | `86400` s | Seconds between automatic backups |
| `BACKUP_KEEP` | `7` | How many backups to keep |
| `BACKUP_AUTO` | `1` | `0` disables the in-app backup thread |
| `SECURE_COOKIES` | `0` | `1` marks the session cookie HTTPS-only |
| `SECRET_KEY` | key file | Session signing key (file auto-created otherwise) |
| `POLL_WORKERS` | company count | Parallel polling workers |
| `RETENTION_DAYS` | `90` | Raw rows + rollup retention |

### Production hardening & TLS

The built-in server speaks plain HTTP only — for production, terminate TLS
in front of the app:

1. **Reverse proxy + Let's Encrypt (Linux, recommended)**
   - Run the app bound to loopback: `HOST=127.0.0.1 python app.py` (or
     waitress behind the proxy).
   - nginx:

     ```nginx
     server {
         listen 80;
         server_name monitor.example.com;
         location / {
             proxy_pass http://127.0.0.1:5000;
             proxy_set_header Host $host;
             proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
             proxy_set_header X-Forwarded-Proto $scheme;
         }
     }
     ```

   - `certbot --nginx -d monitor.example.com` for the certificate + renewal.
   - Once HTTPS works: `SECURE_COOKIES=1` so the session cookie is
     HTTPS-only.
2. **Caddy (simplest)** — one site block with `reverse_proxy 127.0.0.1:5000`
   gives automatic HTTPS via Let's Encrypt.
3. **Windows / internal CA** — bind a certificate in IIS or use a
   corporate/internal CA; the same `SECURE_COOKIES=1` applies. Keep
   `HOST=127.0.0.1` and let the proxy own port 443.
4. **General**
   - Firewall: only the proxy's port (443) reachable; the app port stays
     local.
   - `secret_key.txt` is created with `0600` permissions — back it up; if it
     changes, all sessions are invalidated (users just log in again).
   - `waitress` is installed and **is** what's serving now (confirm with
     the `Server: waitress` response header); the app only falls back to
     the Flask dev server if it's ever uninstalled.
   - Rate limiting: 5 failed logins per 5 minutes per IP, lockout message
     on the login page.

## V2.2 Phase 6 — incident grouping + history

Outages are stored one row per check, so a single dead gateway shows up as
four separate rows. Phase 6 folds them back into **incidents** — no schema
change, grouping happens on read:

- **Grouping rule** (`group_outages()` in `app.py`): outage rows of the
  *same company* that overlap or start within **120 s** of the group so far
  belong to one incident. Open outages extend to "now", so anything failing
  while an incident is ongoing joins the same one.
- **Root-cause hint**: each incident reports `root_cause` = the leftmost
  failing step of the dependency chain **Gateway → Internet → DNS → HTTPS**
  (a dead gateway also breaks everything behind it).
- Each incident carries: `started_at` / `ended_at` (UTC, `ended_at: null`
  while open), `open`, `duration` (span first start → end/now), `downtime`
  (sum of member outage durations), `checks` (chain-ordered, deduped),
  `outage_count`, `root_cause` and the full `members` list.
- **APIs**
  - `GET /api/incidents?hours=&company=&open=1` — grouped incidents,
    newest first (capped at 200). `hours` uses the usual 1/12/24/720
    windows (bad value falls back to 24), `company` accepts a single name
    or `all`/empty (unknown name → 400), `open=1` returns only ongoing
    incidents. Login required (401 otherwise).
  - `GET /api/notifications` gained optional `company` and `hours`
    filters (unknown company / `hours` outside 1–8760 → 400; malformed
    `limit` → 400 instead of 500). Existing callers (`?limit=10`) behave
    exactly as before.
- **UI**
  - New **`/incidents`** page: company selector + 1 h / 12 h / 24 h / 30 d
    period buttons, an Incidents table (status pill, checks affected,
    start/end, duration, likely cause) that auto-refreshes every 30 s, and
    an **Alert history** table (same filters, newest first).
  - Sidebar now links **Incidents** from every page (Overview, Dashboard,
    Reports, Users).
  - The dashboard's incident card and active-incident banner switched
    from raw outages to the grouped view, with a link to the full page.
  - CSS cache-bumped to `?v=5`.
- **Live check**: Company B's flapping placeholder gateway produced
  **26 raw outage rows → 6 incidents** in 24 h, root cause Gateway — the
  grouping does exactly what it should on real data.
- **Tests**: `tests/test_incidents.py` adds **27 tests** (89 total, all
  green) covering gap merge/split boundaries, company isolation, open
  incidents, chain ordering, duration/downtime math, API auth/filters/
  clamping, notification filters and page/sidebar rendering.
- Regression caught by the browser walkthrough (not pytest): the page
  initially built `?limit=100?company=…` (double `?`), which 500'd on
  `int()` — fixed in the template and hardened server-side with a
  regression test.

## V2.2 Phase 7 — remote-site agent (push monitoring)

Phase 6 assumed every site is reachable from this machine. Sites behind
NAT/firewall (or just too far away to ping reliably) are now covered by a
small **agent that pushes results to this server**.

### Config: `managed_by`

- Each company may set `"managed_by": "agent"` (default `"local"`).
  The monitor loop **skips agent-managed companies** — the startup banner
  marks them with `(remote agent pushes results)` and the polling line
  reports `N local company/companies (M agent-managed)`.
- `validate_config()` rejects any other value, and the
  `SPEEDTEST_COMPANY` must stay `local` (a speedtest always measures
  *this* machine's link, so it cannot come from a remote site).
- `AGENT_STALE_SECONDS = 300` — how old the last report may be before an
  agent is considered stale.

### Tokens & the `/agents` admin page

- New `agent_tokens` table: one row per company, storing only a
  **SHA-256 hash** of the token (`secrets.token_urlsafe(32)` plaintext is
  shown **once** on creation — session-held, never put in a redirect URL,
  so it cannot leak into logs or browser history).
- New admin-only **`/agents`** page (CSRF-protected): Create / Rotate /
  Revoke per company, with last-seen time. A "How this works" card
  documents the flow. The sidebar gained a robot-icon **Agents** link
  (admin pages only; viewers never see it).

### `POST /api/ingest` — the ingest endpoint

- **Auth**: `Authorization: Bearer <token>` → 401 if missing/empty/unknown.
  The company comes from the token, never from the payload.
- **409** if the token's company was removed from `config.py` or is not
  `managed_by: "agent"` (a locally polled company must not be
  double-counted).
- **400 before anything is written** for bad `check_type`/`status`/
  `latency`/`target`/`timestamp`, an empty `results` list, or more than
  `MAX_INGEST_RESULTS = 20` results.
- Accepted rows are inserted with `source = 'agent'` (new column,
  `'local'` default — added by a try/except `ALTER TABLE` migration) and
  then run through the **same pipeline as the local poller**:
  `process_check_result()` + `evaluate_alerts()`, so agent results open
  and close outages, respect the flap guard and appear in alert history.
- Responds `{ok, company, accepted, server_time}`; updates the token's
  `last_seen`.

### Status API & dashboard badge

- `/api/status` gains an `agent` object for agent-managed companies:
  `{managed_by, last_seen, age_seconds, stale, stale_after_seconds}`.
  Stale = no report in `AGENT_STALE_SECONDS` (or never reported).
- The dashboard shows a badge under the incident banner —
  *Remote agent — last report Ns ago / stale N min ago / never reported* —
  and keeps it hidden for locally-managed companies.

### `agent.py` — the remote runner

Stdlib-only single file to drop on the remote machine:

```
python agent.py --server http://<server>:5000 --token <token> [--interval 60] [--once]
```

(env `AGENT_SERVER` / `AGENT_TOKEN` also work). Runs the four checks
locally — gateway `auto` detects the site's own default route — and POSTs
results each cycle; backs off on failures; exits `2` without a token.

### Tests & verification

- `tests/test_agent.py` adds **48 tests (137 total, all green)**: schema
  migration, ingest auth/validation/happy-path/state-machine integration,
  `/agents` RBAC + CSRF + one-time display + rotation/revocation,
  staleness rules, config validation, monitor-skip, and agent.py's pure
  helpers (network monkeypatched).
- Two real bugs were caught and fixed by them: the ingest `INSERT` had
  `target`/`check_type` transposed, and `process_check_result()` forgot
  the outage id opened on a *first observation*, so a later UP could not
  close it in the same run.
- **Live check**: 33/33 scripted checks against the running server
  (token lifecycle, 401/400/409 paths, real `agent.py --once` push,
  staleness transitions, monitor skip proven in `monitor.log`) plus a
  browser walkthrough of `/agents` and the dashboard badge — zero console
  errors. CSS cache-bumped to `?v=6`.

## V2.2 Phase 8 — public status page

A login-free status board at `/status` for visitors (the login page
links to it), built from the same data the dashboard uses but scrubbed
of anything internal.

### Routes & behavior

- `GET /status` — every published company as a card: status pill
  (Operational / Partial outage / Major outage / Unknown), one dot per
  check, last-check age, ongoing-incident note. Cards link to detail.
- `GET /status?company=X` — one company: current checks (status,
  latency, last-checked time), uptime % for 24h / 7d / 30d, and the
  last 30 days of incidents, with a banner while one is open. A
  dropdown switches companies without going back; unknown `X` → a
  friendly 404.
- HTML only — no public JSON API. Responses carry
  `Cache-Control: no-store`; the page auto-refreshes every 60 s.
- The route deliberately skips the inline login guard used everywhere
  else, and never renders target addresses, usernames, file paths or
  admin links (all tested).

### Status semantics

- Fresh rows (age ≤ `STATUS_STALE_SECONDS`) decide the pill: any DOWN
  = Partial outage, all four DOWN = Major outage.
- A stale/missing row flips its check to Unknown and the company with
  it — the page never claims "Operational" on old data; a banner says
  how long the monitor has been quiet.

### Config (config.py)

- `PUBLIC_STATUS = True` — kill switch; `False` redirects `/status`
  to the login page.
- `STATUS_STALE_SECONDS = 180` — freshness window (3 missed cycles),
  validated as an int ≥ 60.
- Optional per-company `"public": False` hides a company from the page
  entirely (its detail URL 404s). Default: shown.
- `validate_config()` rejects a non-bool `PUBLIC_STATUS` or
  per-company `public`.

### Entry points

- Login page: "View live status →" link under the form.
- Sidebar on every app page: a Status link (outside the admin gate —
  viewers see it too), ordered after Reports.

### Tests & verification

- `tests/test_public_status.py` adds **25 tests (162 total, all
  green)**: anonymous access, `no-store` headers, 404 + kill switch,
  all four pill states, stale banner, uptime rendering, incident
  banner/list, privacy (`public: False`, target/DB-path leak checks,
  HTML escaping of company names), config validation, and both entry
  points. Seeded rows sit behind an id high-water mark, so other
  suites' data is never touched.
- **Live check**: 14/14 scripted anonymous checks against the running
  server (200s, `no-store`, no leaks, 404, login button) plus a
  browser walkthrough: overview → detail → company switch → back,
  sidebar link click-through, dark theme — zero console errors. CSS
  cache-bumped to `?v=7`.

## V2.3 Phase 9 — dark-mode readability

Fixes the reported bug (hovering a chart in dark mode showed a pure
white box with unreadable text) and puts every text/background pair
the UI renders under an automated WCAG AA guard.

### The chart tooltip bug

- The tooltip background was bound to the `--ink` token with hardcoded
  white text. Light mode: dark box + white text (correct). Dark mode:
  `--ink` is near-white, so the box became white-on-white — exactly
  the "pure white, unreadable" hover that was reported.
- A new `tooltipColors()` helper reads the live `data-theme` on every
  render: dark = surface box + ink title/body + line border; light =
  unchanged ink box + white text. Both charts (latency, speedtest)
  share it through `chartTypeOptions()`.
- The theme toggle sets `data-theme` *before* rebuilding both charts,
  so the tooltip colours re-read on every switch — verified live in
  both directions.

### Token & contrast fixes

- `--brand-fill` / `--brand-fill-hover` split out: buttons and the
  login panel keep white labels at 5.3:1 (dark) while `--brand`
  brightened to `#7b8cf5` so chip/link text clears 4.5:1 on the soft
  chip background (was 4.05).
- Light `--ink-3` darkened `#66727f` → `#626d7a` (4.49 → 4.82 on the
  page background).
- Input placeholders now use `var(--ink-3)` (the old `#8b96a2` was
  3.0:1 on white).
- `.overview-status` (14px bold Online/Offline) and `.overall-mark`
  icons switch to the `*-ink` status tokens (was 3.46:1 with `--up`).
- Login pitch text 0.86 → 0.92 alpha, signal-graphic labels solid
  white — both clear 4.5:1 over the brand panel in either theme.
- Chart type toggle track `#eceff4` (hardcoded light, stayed white in
  dark mode → 1.78 with light text) → `var(--line)`.
- The big 34px status values keep the vivid `--up`/`--down`: large
  text passes at the 3:1 threshold (3.46 / 4.64).

### Guard rails

- `tests/test_dark_contrast.py` (6 tests): parses `:root` and
  `[data-theme="dark"]` straight from `style.css`, computes WCAG
  ratios for 30+ pairs in both themes (including alpha-composited
  login text), asserts the tooltip stays helper-driven, the dark fill
  tokens exist, and all 8 templates share one `?v=` cache version.
- CSS cache-bumped to `?v=8` in all 8 templates.

### Verification

- `python -m pytest -q` → **168 passed**.
- Live browser: dark tooltip = `#161d26` box + `#e8edf3` title +
  `#aab6c3` body; light tooltip unchanged; theme toggles re-read
  correctly; served `?v=8`; overview statuses, public-status pills and
  chips render at the ink colours; zero console errors.

## V2.3 Phase 10 — network deployment & hardening

Two workstreams: make the app comfortably reachable from other
computers on the LAN, and harden it now that it's a real deployment.

### A — LAN access (other computers can open it)

- **Production server**: `waitress` installed (3.0.2) — the app now
  serves with 8 worker threads instead of the Flask dev server.
  Confirmed by the `Server: waitress` response header; no new
  development-server warnings appear in `monitor.log`.
- **Address**: the server listens on all interfaces, so any machine
  on your network opens **`http://192.168.68.64:5000`** (check
  `ipconfig` for your current IP — DHCP can change it) and
  **`/status`** needs no login at all. Verified live from this
  machine against the LAN IP.
- **Autostart**: Scheduled task **"Uptime Monitor"** runs the app
  hidden (`pythonw app.py`, working dir = project folder) at logon,
  with no execution time limit and up to 5 automatic restarts at
  1-minute intervals if it ever crashes. Manage it with
  `Start-ScheduledTask "Uptime Monitor"` /
  `Stop-ScheduledTask "Uptime Monitor"`.
  - A *boot-time* trigger was denied (this account is not an
    administrator). With an admin shell:
    `Register-ScheduledTask ... (New-ScheduledTaskTrigger -AtStartup)`
    adds "runs even before anyone logs in".
- **Firewall**: inbound **TCP 5000** is already allowed by the
  existing `Monitoring` / `SpeedTestMonitoring` rules — but only on
  the **Private** profile. Keep the Wi-Fi network profile Private
  (Settings → Network → Wi-Fi → your network → Private), or add a
  Public-profile rule from an admin shell:
  `New-NetFirewallRule -DisplayName "Uptime Monitor" -Direction Inbound -Protocol TCP -LocalPort 5000 -Profile Private,Public -Action Allow`
- **Stable URL**: reserve the current IP for this machine in your
  router's DHCP settings (router admin → DHCP reservation), or use
  the computer name (`http://BCGI-LPTP-ORT00:5000`) — then the
  address never changes when DHCP renews.
- **Wire security**: plain HTTP over Wi-Fi means the admin password
  travels unencrypted. On a trusted home/office LAN that's a
  judgment call; for anything else, terminate TLS in front (see
  *Production hardening & TLS* above) and set `SECURE_COOKIES=1`.

### B — Hardening

- **Content-Security-Policy** added to every response (alongside the
  existing `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy`):
  `default-src 'self'`, Chart.js + Google Fonts origins (dropped in
  Phase 11 when they're self-hosted), `frame-ancestors 'none'`,
  `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`.
  Deliberately **no** `upgrade-insecure-requests` — the LAN install
  is plain HTTP.
- **Branded error pages** (`templates/error.html`): 404 and 500 now
  render a dark-theme-aware card with links back to the dashboard and
  the public status page — no Werkzeug branding, and 500s never echo
  the exception (details only in `monitor.log`). The page loads no
  CDN/web-font resources, so it renders with the internet down.
  `/status`'s own inline 404 stays untouched (tested).
- **Version control**: `git init` on `main`, `.gitignore` extended
  (`monitor.log*` rotations + `speedtest.exe` vendor binary; the
  existing entries already excluded `uptime.db`, `secret_key.txt`,
  `backups/`, caches). Initial commit `5e90e4f` — verified that no
  secret, database, log, backup or binary is tracked.
- **Tests**: `tests/test_hardening.py` — 11 tests covering the CSP
  shape (and its current asset origins), header persistence on static
  + error responses, branded 404/500 with no leaks, the preserved
  `/status` inline 404, and error-page hygiene.

### Verification

- `python -m pytest -q` → **179 passed**.
- Live probe **22/22**: waitress header, full security-header set,
  branded 404 (status + content + headers), `/status` still
  `no-store`, LAN URL answering 200 on waitress, static with nosniff.
- Lighthouse after the fixes: dashboard accessibility **94 → 98**
  (color-contrast gone), `/status` accessibility **100**. Remaining
  findings (heading-order, meta descriptions, label mismatch) are
  Phase 11 items.

## V2.3 Phase 11 - self-hosted assets, partials & accessibility

Everything now ships from the repo - no CDN, no Google Fonts, no
Chart.js CDN - and the shared template code collapsed into partials.

### A - Self-hosted assets

- Chart.js 4.5.1 vendored at `static/vendor/chart.umd.min.js`
  (the dashboard tag points here with `?v=4.5.1`).
- The five Google fonts mirrored to `static/fonts/*.woff2` and
  wrapped by `static/fonts.css` (zero external `url()`s); every
  template links `fonts.css?v=1` instead of `fonts.googleapis.com`.
- CSP no longer allows any third-party origin:
  `test_csp_declares_no_third_party_origins` fails the build if an
  `http(s)://` origin sneaks back in.

### B - Template partials (edit one file, not nine)

- `templates/partials/head_assets.html` - favicon + fonts.css +
  style.css links. The single `?v=` cache version lives here, so a
  token fix is now one edit + one bump (currently `?v=9`). All nine
  templates include it.
- `templates/partials/sidebar.html` - the canonical sidebar:
  `{% set active_page = ... %}` drives the `active` class, the
  `{% if role == "admin" %}` gate wraps Users/Agents, plus user
  chip, theme toggle, logout and the skip link. overview, incidents,
  reports, users and agents include it; the dashboard keeps its
  bespoke anchor-nav sidebar (Latency/Speedtest section links +
  company switcher) by design.
- `static/dashboard.js` - the dashboard's ~1900-line inline script
  extracted verbatim (it contains no Jinja); the template loads it
  with `?v=1`. The small head theme-init scripts stay inline (login
  and /status bundle `toggleTheme` there).

### C - Accessibility & SEO

- `<meta name="description">` on all nine templates.
- Skip link (`Skip to main content` -> `#main-content`) in the
  sidebar partial and the dashboard; off-screen until focused.
  `id="main-content"` added to all six `<main class="main-content">`.
- Chart-type toggles expose `aria-pressed` (initial state in HTML,
  flipped by `setLatencyChartType`/`setSpeedtestChartType`).
- `/status` and `/login` theme toggle: dropped
  `aria-label="Toggle dark mode"` - it clashed with the visible
  "Light mode"/"Dark mode" text (axe `label-content-name-mismatch`);
  the visible label is the accessible name and `aria-pressed`
  conveys state, matching the other six pages.
- Status-card `<h3>`s promoted to `<h2>` (fixes the h1 -> h3
  heading-order gap).
- Chart canvases keep `role="img"` + aria-label summaries.

### Tests & verification

- New `tests/test_partials.py` (7 tests): include wiring,
  `active_page` ordering, admin-gate placement, dashboard.js
  externalization, skip-link/main landmark, pressed states, meta
  descriptions.
- Existing source-scan tests updated for the layout: the fonts test
  resolves `{% include %}`, the cache-version test walks
  `templates/` (partial included), the grouped-incidents API test
  reads `static/dashboard.js`.
- `python -m pytest -q` -> **192 passed**.
- Lighthouse (live) on dashboard, `/status` and `/login`:
  accessibility **100**, best-practices **100**, SEO **100**, zero
  failures, zero console messages on all three.

## V2.3 Phase 12 - monitoring features

### A - Alerts admin page (`/alerts`, admin only)

- New `templates/alerts.html` + `GET /alerts` (`require_admin`):
  channels (email/webhook state + targets), thresholds
  (down/latency/cooldown), maintenance windows, and the last 20
  entries of the `notifications` history.
- **Send test alert** button POSTs `/api/alerts/test` (the endpoint
  existed since Phase 1 - this gives it a UI) and reports the
  outcome inline; every attempt, delivered or not, lands in the
  history.
- The sidebar partial and the dashboard's own nav gained a gated
  Alerts item (bell icon).

### B - Speedtest failure recording (schema migration)

- `speedtest_results.status` (`'ok'` default / `'failed'`) added in
  both initializers (app + checker) via CREATE + `ALTER TABLE ...
  DEFAULT` migration - existing databases upgrade in place and old
  rows stay `'ok'`.
- `run_speedtest()` records every failure path (binary missing, exit
  code, no result object, timeout, bad JSON, exceptions) through
  `save_speedtest_failure()`: NULL speeds and `FAILED: <reason>` in
  `server`.
- Charts and the recent list filter `status='ok'`, so no nulls reach
  Chart.js; `/api/speedtest/recent` additionally returns
  `last_failure` - only when the most recent attempt failed, which
  turns an unexplained chart gap into a stated cause.

### C - Maintenance banner on `/status`

- The `MAINTENANCE` config (which already silenced alerts in the
  checker) now surfaces publicly: `_active_maintenance()` reuses
  `uptime_checker._in_maintenance()`, so the banner and the alert
  suppression can never disagree. Overview and detail pages render a
  `.pub-banner-info` "Scheduled maintenance" banner for the affected
  companies. The helper guards per company - a broken window
  definition can never take the public page down (tested).

### Tests & verification

- New `tests/test_phase12.py` - 16 tests: page access per role,
  admin-only nav, config display, test-alert endpoint semantics,
  the status column, failure recording (including a stubbed
  `run_speedtest` - no real speedtest ever runs in tests),
  chart/recent filtering, and banner presence/absence/broken-config.
- `python -m pytest -q` -> **208 passed**.
- Live: `/alerts` renders (active nav, four cards), Send test
  alert answered inline and recorded `kind=test` rows;
  `/status` unaffected with an empty window list; Lighthouse on
  `/status` + `/alerts`: **100/100/100**, zero console messages.

## V2.3 Phase 13 - blueprint split of app.py

`app.py` had grown to ~3,000 lines holding every route, helper and
constant. It is now a thin 390-line shell: configuration, CSP/error
pages, the context processor, blueprint registration and the
`__main__` startup block. Shared helpers and constants moved to
`core.py`; all 34 routes moved into six blueprints:

| File | Endpoints | Routes |
| --- | --- | --- |
| `blueprints/auth.py` | `auth.*` | `/login`, `/logout` |
| `blueprints/pages.py` | `pages.*` | `/`, `/dashboard`, `/overview`, `/reports`, `/incidents` |
| `blueprints/status.py` | `status.*` | `/status` + its private helpers |
| `blueprints/api.py` | `api.*` | all 18 `/api/*` routes + `_valid_ts`, `_parse_hours` |
| `blueprints/admin.py` | `admin.*` | `/users`, `/users/*`, `/alerts` |
| `blueprints/agents.py` | `agents.*` | `/agents`, `/agents/<company>/token`, `/agents/<company>/revoke` |

`app.py` re-exports every name it used to own (`from core import
get_db, require_admin, ...`) so existing test access like
`app.get_db()` and `app_module.PUBLIC_STATUS` keeps working.

### Key decisions

- **URL paths never changed** - only endpoint names gained a
  `blueprint.` prefix, so bookmarks, the path-based sidebar links,
  dashboard JS and every HTTP-level test work untouched. Template
  and in-code `url_for` call sites were updated to the namespaced
  names (`auth.login`, `auth.logout`, `pages.dashboard`,
  `status.public_status`, `admin.users_page`, `agents.agents_page`).
- **Patched flags stay patchable** - tests do
  `monkeypatch.setattr(app_module, "PUBLIC_STATUS", ...)`. Moved
  routes therefore never `from config import PUBLIC_STATUS`; they
  read flags at request time through `core.live_flags()`, which
  returns the app module stored in `app.extensions["app_module"]`.
  `/status` uses it for `PUBLIC_STATUS`, `/alerts` for
  `MAINTENANCE`, so the phase-8/12 test patches still reach them.
- **No circular imports** - blueprints import from `core` and
  `config`, never from `app`. `app.py` imports the blueprints last,
  after `app` and `app.extensions` exist.

### Tests & verification

- `python -m pytest -q` -> **208 passed** (baseline unchanged;
  the suite was re-run green after every extraction step).
- Live (fresh server, exactly one listener on `:5000`): `/login`
  200 (form posts to `/login`, status link points to `/status`),
  `/status` 200 (footer login link, live data), `/dashboard` 302 ->
  `/login` when anonymous, logout -> login round-trip, and
  `/api/health` + `/api/status` answer
  `401 {"error": "Unauthorized"}` for anonymous callers.
- Hygiene: real DB `users = [admin]`, `login_failures = 0`.

