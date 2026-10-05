"""
Phase 10 - production hardening.

Locks down:
1. The Content-Security-Policy (plus the pre-existing security
   headers) on normal responses AND on error responses.
2. Branded 404/500 pages: no Werkzeug branding, no tracebacks, a
   way back into the app, dark-theme aware.
3. The /status inline 404 stays inline - the global errorhandler
   must not hijack it (Phase 8 contract).
4. The error page stays free of CDN/web-font requests so it renders
   when the internet is down.
"""

import os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ERROR_TEMPLATE = os.path.join(PROJECT, "templates", "error.html")

# Registered at import time (collection runs before the first
# request; Flask 3 refuses new routes after that). This route exists
# only inside the sandboxed test process - production never imports
# this module.
import app as _app  # noqa: E402  (conftest already set the sandbox env)


@_app.app.route("/__test_boom")
def _test_boom():
    raise RuntimeError("intentional test error")


# ------------------------------------------------------------------
# Security headers
# ------------------------------------------------------------------

def test_csp_header_is_present_and_restrictive(client):
    r = client.get("/login")
    csp = r.headers.get("Content-Security-Policy", "")
    assert csp, "Content-Security-Policy header missing"
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    # Plain-HTTP LAN install: never force HTTPS upgrades.
    assert "upgrade-insecure-requests" not in csp


def test_csp_declares_no_third_party_origins(client):
    """Phase 11 self-hosts Chart.js and both fonts - no CDN origin
    may reappear in the policy (or the app breaks when the WAN is
    down, and supply-chain risk comes back)."""
    csp = client.get("/login").headers.get("Content-Security-Policy", "")
    assert "cdn.jsdelivr.net" not in csp
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp
    assert "https://" not in csp and "http://" not in csp


def test_pre_existing_security_headers_still_present(client):
    r = client.get("/login")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    policy = r.headers["Permissions-Policy"]
    assert "camera=()" in policy
    assert "microphone=()" in policy
    assert "geolocation=()" in policy


def test_headers_apply_to_static_files_too(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers.get(
        "Content-Security-Policy", ""
    )


# ------------------------------------------------------------------
# Branded 404
# ------------------------------------------------------------------

def test_unknown_url_renders_branded_404(client):
    r = client.get("/definitely-not-a-real-page")
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "Page not found" in html
    assert "error-card" in html
    assert 'href="/dashboard"' in html
    assert "Uptime Monitor" in html
    # not the stock framework page
    assert "<h1>Not Found</h1>" not in html
    assert "Werkzeug" not in html
    # security headers ride along on error responses
    assert "frame-ancestors 'none'" in r.headers.get(
        "Content-Security-Policy", ""
    )


def test_404_for_nested_unknown_path_too(client):
    r = client.get("/api/definitely-not-a-real-endpoint")
    assert r.status_code == 404
    assert "error-card" in r.get_data(as_text=True)


def test_status_unknown_company_keeps_its_inline_404(client):
    """Phase 8 renders its own friendly 404 in-template. The global
    errorhandler must not take that over."""
    r = client.get("/status", query_string={"company": "NopeCorp"})
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "Unknown company" in html
    assert "error-card" not in html


# ------------------------------------------------------------------
# Branded 500
# ------------------------------------------------------------------

def test_500_is_branded_and_leaks_nothing(app_module):
    # The throwing route was registered at module import (see top of
    # file) - Flask 3 forbids adding routes after the first request.
    old = app_module.app.config.get("PROPAGATE_EXCEPTIONS")
    app_module.app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        with app_module.app.test_client() as c:
            r = c.get("/__test_boom")
    finally:
        app_module.app.config["PROPAGATE_EXCEPTIONS"] = old

    assert r.status_code == 500
    html = r.get_data(as_text=True)
    assert "Something went wrong" in html
    assert "error-card" in html
    assert "Traceback" not in html
    assert "RuntimeError" not in html
    assert "intentional test error" not in html
    assert "Werkzeug" not in html
    assert "frame-ancestors 'none'" in r.headers.get(
        "Content-Security-Policy", ""
    )


# ------------------------------------------------------------------
# Error page template hygiene
# ------------------------------------------------------------------

def test_error_page_is_dark_theme_aware():
    src = open(ERROR_TEMPLATE, encoding="utf-8").read()
    assert 'setAttribute("data-theme"' in src
    assert "var(--surface)" in src and "var(--ink-2)" in src


def test_error_page_makes_no_internet_requests():
    """Must render with the WAN down: no CDN, no web fonts. Only
    same-origin or data: URIs may appear in fetch attributes (the
    SVG xmlns namespace URI is an identifier, not a request)."""
    src = open(ERROR_TEMPLATE, encoding="utf-8").read()
    assert "cdn.jsdelivr.net" not in src
    assert "fonts.googleapis.com" not in src
    assert "fonts.gstatic.com" not in src
    assert 'href="http' not in src
    assert 'src="http' not in src
    assert 'url(http' not in src


def test_error_page_has_meta_description_and_single_h1():
    src = open(ERROR_TEMPLATE, encoding="utf-8").read()
    assert 'name="description"' in src
    assert src.count("<h1") == 1
