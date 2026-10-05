"""
Phase 11 - self-hosted assets.

Chart.js and both web fonts are served from static/ since Phase 11.
The whole app - charts, typography, error pages - must render with
the internet down, so no template or stylesheet may reference an
external origin. This also keeps the CSP free of CDN exceptions.
"""

import os
import re

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(PROJECT, "templates")
STATIC = os.path.join(PROJECT, "static")

FORBIDDEN_HOSTS = ("cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com")


def _template_names():
    return sorted(
        n for n in os.listdir(TEMPLATES) if n.endswith(".html")
    )


def _read(path):
    return open(path, encoding="utf-8").read()


def _expand(src):
    """Inline {% include "partials/..." %} so source checks see the
    partial's content - every page loads its assets via head_assets."""
    def repl(m):
        return _read(os.path.join(TEMPLATES, "partials", m.group(1) + ".html"))
    return re.sub(r'\{%\s+include\s+"partials/([a-z_]+)\.html"\s*%\}', repl, src)


def test_no_template_references_the_internet():
    for name in _template_names():
        src = _read(os.path.join(TEMPLATES, name))
        for host in FORBIDDEN_HOSTS:
            assert host not in src, "%s still references %s" % (name, host)
        assert 'src="http' not in src, "%s loads an external script" % name
        assert 'href="http' not in src, "%s links an external resource" % name


def test_dashboard_uses_local_chartjs():
    src = _read(os.path.join(TEMPLATES, "dashboard.html"))
    assert "vendor/chart.umd.min.js" in src
    assert "cdn.jsdelivr.net" not in src


def test_chartjs_bundle_is_present_and_local():
    path = os.path.join(STATIC, "vendor", "chart.umd.min.js")
    assert os.path.isfile(path)
    size = os.path.getsize(path)
    assert size > 100_000, "chart.umd.min.js looks truncated (%d bytes)" % size
    head = _read(path)[:300]
    assert "Chart" in head


def test_fonts_css_is_local_only_and_files_exist():
    css = _read(os.path.join(STATIC, "fonts.css"))
    assert "http://" not in css
    assert "https://" not in css
    urls = re.findall(r"url\((/static/fonts/[^)]+)\)", css)
    assert len(urls) >= 4, "expected several @font-face sources"
    for url in urls:
        local = os.path.join(PROJECT, url.lstrip("/"))
        assert os.path.isfile(local), "missing font file %s" % url
    assert css.count("@font-face") >= 4
    assert "Bricolage Grotesque" in css
    assert "Instrument Sans" in css


def test_font_files_on_disk():
    fonts_dir = os.path.join(STATIC, "fonts")
    woff2 = [f for f in os.listdir(fonts_dir) if f.endswith(".woff2")]
    assert len(woff2) >= 5


def test_every_page_links_the_local_fonts():
    for name in _template_names():
        src = _expand(_read(os.path.join(TEMPLATES, name)))
        assert "fonts.css" in src, "%s does not load the local fonts" % name
