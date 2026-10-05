#!/usr/bin/env python3
"""
Remote-site agent for the Uptime Monitor dashboard.

Run this at a site you cannot reach directly (a subsidiary office,
a plant network, ...). It performs the four standard checks - Gateway,
Internet, DNS, HTTPS - locally, then POSTs the results to the central
dashboard's /api/ingest endpoint using a per-company token.

Setup
-----
1. In config.py on the server, mark the company as agent-managed:

       "Branch Office": {
           "gateway": "auto",
           "internet": 1.1.1.1",
           "dns": "cloudflare.com",
           "https": "https://www.cloudflare.com",
           "managed_by": "agent",          # <- server stops polling it
       }

2. On the server's /agents page (admin), click "Create token" for that
   company and copy the token it shows once.

3. Fill in SERVER and TOKEN below (or pass --server / --token, or set
   AGENT_SERVER / AGENT_TOKEN environment variables), then run:

       python agent.py                 # forever, every CHECK_INTERVAL s
       python agent.py --once          # single cycle, then exit
       python agent.py --server http://192.168.68.58:5000 --token XYZ

Standard library only - no pip install needed on the remote site.
Python 3.8+.
"""

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

# Dashboard URL (no trailing slash).
SERVER = os.environ.get("AGENT_SERVER", "http://127.0.0.1:5000")

# Token from the server's /agents page. NEVER commit a real token.
TOKEN = os.environ.get("AGENT_TOKEN", "")

# Seconds between check cycles (keep close to the server's
# CHECK_INTERVAL so charts stay even).
CHECK_INTERVAL = 60

# What to check. "gateway": "auto" uses THIS machine's gateway.
TARGETS = {
    "gateway": "auto",
    "internet": "1.1.1.1",
    "dns": "cloudflare.com",
    "https": "https://www.cloudflare.com",
}


# ============================================================
# TIME
# ============================================================

def utc_now_str():
    """UTC 'YYYY-MM-DD HH:MM:SS' - the format the server expects."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# GATEWAY DETECTION ("auto")
# ============================================================

def get_gateway():
    manual = os.environ.get("GATEWAY_IP")
    if manual:
        return manual.strip()

    system = platform.system().lower()
    try:
        if system == "windows":
            result = subprocess.run(
                ["ipconfig"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            in_gateway_block = False
            for line in result.stdout.splitlines():
                if "Default Gateway" in line:
                    in_gateway_block = True
                elif ":" in line and line.strip():
                    in_gateway_block = False
                if in_gateway_block:
                    match = re.search(
                        r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line
                    )
                    if match:
                        return match.group(1)

        elif system == "darwin":
            result = subprocess.run(
                ["route", "-n", "get", "default"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            match = re.search(r"gateway:\s*(\S+)", result.stdout)
            if match:
                return match.group(1).strip()

        else:  # Linux and fallback
            result = subprocess.run(
                ["ip", "route"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            for line in result.stdout.splitlines():
                if line.startswith("default") and "via" in line:
                    parts = line.split()
                    return parts[parts.index("via") + 1]

    except Exception:
        pass

    return None


# ============================================================
# CHECKS
# ============================================================

PING_TIME_RE = re.compile(
    r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE
)


def parse_ping_latency(output):
    if not output:
        return None
    matches = PING_TIME_RE.findall(output)
    if not matches:
        return None
    try:
        value = float(matches[-1])
        if "time<" in output.lower() and value <= 1:
            return round(value / 2, 2) or 0.5
        return round(value, 2)
    except ValueError:
        return None


def ping(target):
    system = platform.system().lower()
    if system == "windows":
        command = ["ping", "-n", "1", "-w", "2000", target]
    else:
        command = ["ping", "-c", "1", "-W", "2", target]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            latency = parse_ping_latency(result.stdout)
            if latency is None:
                latency = parse_ping_latency(result.stderr)
            return (target, "UP", latency if latency is not None else 1.0)
        return (target, "DOWN", None)
    except Exception:
        return (target, "ERROR", None)


def dns_check(domain):
    start = time.perf_counter()
    try:
        socket.gethostbyname(domain)
        latency = (time.perf_counter() - start) * 1000
        return (domain, "UP", round(latency, 2))
    except socket.gaierror:
        return (domain, "DOWN", None)
    except Exception:
        return (domain, "ERROR", None)


def https_check(url):
    start = time.perf_counter()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "UptimeMonitor-Agent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            response.read(1)
        latency = (time.perf_counter() - start) * 1000
        return (url, "UP", round(latency, 2))
    except urllib.error.HTTPError:
        # An HTTP error answer still proves the site responded;
        # treat non-success status as DOWN (same as server's check).
        return (url, "DOWN", None)
    except Exception:
        return (url, "ERROR", None)


# ============================================================
# CYCLE
# ============================================================

def run_checks():
    """Run all four checks. Returns the list sent to the server."""
    timestamp = utc_now_str()
    results = []

    gateway_cfg = TARGETS.get("gateway", "auto")
    gateway = get_gateway() if gateway_cfg == "auto" else gateway_cfg

    if gateway:
        target, status, latency = ping(gateway)
    else:
        target, status, latency = "Unknown", "DOWN", None
    results.append({
        "timestamp": timestamp,
        "check_type": "Gateway",
        "target": target,
        "status": status,
        "latency": latency,
    })

    for check_type, value, fn in (
        ("Internet", TARGETS["internet"], ping),
        ("DNS", TARGETS["dns"], dns_check),
        ("HTTPS", TARGETS["https"], https_check),
    ):
        target, status, latency = fn(value)
        results.append({
            "timestamp": timestamp,
            "check_type": check_type,
            "target": target,
            "status": status,
            "latency": latency,
        })

    return results


def push(results):
    """POST one cycle to the server. Returns True on success."""
    body = json.dumps({"results": results}).encode("utf-8")
    req = urllib.request.Request(
        SERVER.rstrip("/") + "/api/ingest",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + TOKEN,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        print(
            f"[{utc_now_str()}] pushed {payload.get('accepted', 0)} "
            f"result(s) for {payload.get('company', '?')}"
        )
        return True
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(
            f"[{utc_now_str()}] server rejected push "
            f"(HTTP {err.code}): {detail}",
            file=sys.stderr,
        )
        return False
    except Exception as err:
        print(
            f"[{utc_now_str()}] cannot reach {SERVER}: {err}",
            file=sys.stderr,
        )
        return False


def main(argv=None):
    global SERVER, TOKEN

    parser = argparse.ArgumentParser(
        description="Uptime Monitor remote-site agent."
    )
    parser.add_argument("--server", default=SERVER,
                        help="dashboard URL, e.g. http://10.0.0.5:5000")
    parser.add_argument("--token", default=TOKEN,
                        help="ingest token from the /agents page")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL,
                        help="seconds between cycles (default 60)")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit")
    args = parser.parse_args(argv)

    SERVER = args.server.rstrip("/")
    TOKEN = args.token

    if not TOKEN:
        print(
            "No token. Create one on the server's /agents page and pass "
            "--token (or set AGENT_TOKEN).",
            file=sys.stderr,
        )
        return 2

    print(
        f"Agent started: {SERVER} every {args.interval}s "
        f"(gateway={TARGETS.get('gateway')}, "
        f"internet={TARGETS.get('internet')}, "
        f"dns={TARGETS.get('dns')}, https={TARGETS.get('https')})"
    )

    failures = 0
    while True:
        results = run_checks()
        ok = push(results)
        failures = 0 if ok else failures + 1

        if args.once:
            return 0 if ok else 1

        # Back off gently when the server is unreachable so an
        # outage does not turn into a request storm.
        if failures:
            time.sleep(min(args.interval * failures, 300))
        else:
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAgent stopped.")
        sys.exit(0)
