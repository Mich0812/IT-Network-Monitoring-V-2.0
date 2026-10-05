"""Phase 11 - template partials and accessibility extras.

The shared head assets (favicon, fonts, style.css) and the sidebar live
in templates/partials/ now, so a cache-version bump or a nav item is a
one-file change. These tests pin that structure plus the a11y additions
(skip link, main landmark, chart-toggle aria-pressed, meta descriptions)
and the extracted dashboard.js.
"""

import os
import re

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(PROJECT, "templates")
STATIC = os.path.join(PROJECT, "static")

APP_PAGES = [
    "dashboard.html", "overview.html", "incidents.html", "reports.html",
    "users.html", "agents.html", "login.html", "public_status.html",
    "error.html",
]
SIDEBAR_PAGES = {
    "overview.html": "overview",
    "incidents.html": "incidents",
    "reports.html": "reports",
    "users.html": "users",
    "agents.html": "agents",
}


def _read(*parts):
    return open(os.path.join(TEMPLATES, *parts), encoding="utf-8").read()


def _expand(src):
    """Inline {% include "partials/..." %} so page-level assertions
    (like the skip link, which lives in the sidebar partial) hold."""
    def repl(m):
        return _read("partials", m.group(1) + ".html")
    return re.sub(r'\{%\s+include\s+"partials/([a-z_]+)\.html"\s*%\}', repl, src)


def test_every_page_includes_the_head_assets_partial():
    for name in APP_PAGES:
        assert '{% include "partials/head_assets.html" %}' in _read(name), (
            "%s must load its favicon/fonts/css via head_assets.html" % name
        )
    assets = _read("partials", "head_assets.html")
    assert "style.css') }}?v=" in assets, "cache version lives in the partial"
    assert "fonts.css') }}?v=" in assets


def test_sidebar_pages_set_active_page_before_include():
    for name, page in SIDEBAR_PAGES.items():
        src = _read(name)
        want = f'{{% set active_page = "{page}" %}}'
        pos_set = src.find(want)
        pos_inc = src.find('{% include "partials/sidebar.html" %}')
        assert pos_set != -1, "%s does not set active_page" % name
        assert pos_inc != -1, "%s does not include the sidebar" % name
        assert pos_set < pos_inc, "%s sets active_page after the include" % name


def test_sidebar_partial_gates_admin_links():
    src = _read("partials", "sidebar.html")
    gate = src.index('{% if role == "admin" %}')
    users = src.index('href="/users"')
    agents = src.index('href="/agents"')
    endif = src.index("{% endif %}", agents)
    assert gate < users < agents < endif, (
        "Users/Agents links must sit inside the admin gate"
    )
    # every nav entry is active-aware
    assert src.count("{% if active_page ==") >= 7
    # account chrome
    assert 'class="sidebar-logout"' in src
    assert "url_for('logout')" in src
    assert 'id="themeToggle"' in src
    assert 'class="skip-link"' in src


def test_dashboard_script_is_externalized():
    html = _read("dashboard.html")
    assert "static', filename='dashboard.js'" in html, (
        "dashboard must load its script from static/"
    )
    assert "tooltipColors" not in html, "inline chart JS must move out"
    path = os.path.join(STATIC, "dashboard.js")
    assert os.path.isfile(path)
    js = open(path, encoding="utf-8").read()
    assert "function tooltipColors()" in js
    assert 'setAttribute("aria-pressed"' in js, (
        "chart-type toggles must announce their pressed state"
    )
    assert "{{" not in js and "{%" not in js, "static JS must not contain Jinja"


def test_skip_link_targets_a_main_landmark():
    for name in list(SIDEBAR_PAGES) + ["dashboard.html"]:
        src = _expand(_read(name))
        assert 'class="skip-link"' in src, "%s misses the skip link" % name
        assert 'id="main-content"' in src, "%s main misses the target id" % name
    css = open(os.path.join(STATIC, "style.css"), encoding="utf-8").read()
    assert ".skip-link {" in css
    assert ".skip-link:focus {" in css
    assert "left: -9999px" in css, "skip link must start off-screen"


def test_chart_toggle_buttons_declare_pressed_state():
    html = _read("dashboard.html")
    assert html.count('data-type="line" class="active" aria-pressed="true"') == 2
    assert html.count('data-type="bar" aria-pressed="false"') == 2


def test_every_template_has_a_meta_description():
    for name in APP_PAGES:
        assert '<meta name="description"' in _read(name), (
            "%s misses <meta name=description>" % name
        )
