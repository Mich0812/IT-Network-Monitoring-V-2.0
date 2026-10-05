"""
Phase 9 - dark-mode readability guard rails.

The bug this locks down: chart hover tooltips bound their background
to the --ink token while hardcoding white text. In dark mode --ink is
near-white, so the tooltip became a pure-white box with white text -
unreadable. Three guards now protect that class of regression:

1. WCAG contrast - every text/background token pair the UI actually
   renders must clear AA (4.5:1 normal text, 3.0 for large text and
   graphics) in BOTH themes. Tokens are parsed straight from
   style.css, so any future token tweak that hurts readability fails
   the suite instead of shipping.
2. The chart tooltip must take its colours from tooltipColors(),
   which reads the live data-theme - never from hardcoded white.
3. Every template must reference style.css with the same cache
   version (?v=), otherwise a token fix reaches only some pages.
"""

import os
import re

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_PATH = os.path.join(PROJECT, "static", "style.css")
TEMPLATES_DIR = os.path.join(PROJECT, "templates")
DASHBOARD_PATH = os.path.join(TEMPLATES_DIR, "dashboard.html")


# ------------------------------------------------------------------
# style.css parsing
# ------------------------------------------------------------------

def _css_block(pattern):
    src = open(CSS_PATH, encoding="utf-8").read()
    m = re.search(pattern + r"\s*\{(.*?)\}", src, re.S)
    assert m, "style.css block %r not found" % pattern
    return m.group(1)


def _declared_tokens(block):
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{3,8})\s*;", block))


def _theme_tokens():
    """Return {'light': {...}, 'dark': {...}} - dark = root + overrides."""
    light = _declared_tokens(_css_block(r":root"))
    dark = dict(light)
    dark.update(_declared_tokens(_css_block(r'\[data-theme="dark"\]')))
    return {"light": light, "dark": dark}


# ------------------------------------------------------------------
# colour maths (WCAG 2.1 relative luminance / contrast ratio)
# ------------------------------------------------------------------

def _rgb(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    assert len(h) == 6, "unsupported colour %r" % hex_color
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(hex_color):
    out = []
    for c in _rgb(hex_color):
        c /= 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg, bg):
    l1, l2 = _luminance(fg), _luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _composite(alpha, fg, bg):
    """Flatten fg at alpha over an opaque bg - for rgba() text colours."""
    f, b = _rgb(fg), _rgb(bg)
    return "#%02x%02x%02x" % tuple(
        round(alpha * f[i] + (1 - alpha) * b[i]) for i in range(3)
    )


# ------------------------------------------------------------------
# The pairs the UI actually renders (fg token, bg token, min, note)
#   4.5 = AA normal text   3.0 = AA large text / non-text graphics
# ------------------------------------------------------------------

TEXT_PAIRS = [
    ("--ink", "--canvas", 4.5, "body text on page background"),
    ("--ink", "--surface", 4.5, "body text on cards"),
    ("--ink", "--hover", 4.5, "body text on hovered row"),
    ("--ink-2", "--surface", 4.5, "secondary text on cards"),
    ("--ink-2", "--canvas", 4.5, "secondary text on page background"),
    ("--ink-3", "--surface", 4.5, "muted text on cards (placeholders)"),
    ("--ink-3", "--canvas", 4.5, "muted text on page background"),
    ("--ink-3", "--hover", 4.5, "table-message / sla-none pill"),
    ("--brand", "--surface", 4.5, "links on cards"),
    ("--brand", "--canvas", 4.5, "links on page background"),
    ("--brand", "--brand-soft", 4.5, "public chip text"),
    ("--brand-deep", "--surface", 4.5, "deep-brand text on cards"),
    ("--brand-deep", "--brand-soft", 4.5, "soft-chip deep text"),
    ("--ink-2", "--brand-soft", 4.5, "secondary text on brand chips"),
    ("--ink-2", "--line", 4.5, "chart toggle label on track"),
    ("--ink", "--line", 4.5, "chart toggle hovered label on track"),
    ("--ink", "--surface", 4.5, "chart toggle active label"),
    ("#ffffff", "--brand-fill", 4.5, "button label on fill"),
    ("#ffffff", "--brand-fill-hover", 4.5, "button label on hover fill"),
    ("--up-ink", "--up-soft", 4.5, "sla-good pill / status mark"),
    ("--warn-ink", "--warn-soft", 4.5, "sla-warn pill / status mark"),
    ("--down-ink", "--down-soft", 4.5, "sla-down pill / status mark"),
    ("--up-ink", "--surface", 4.5, "green text on cards"),
    ("--up-ink", "--canvas", 4.5, "overview online status"),
    ("--warn-ink", "--surface", 4.5, "amber text on cards"),
    ("--warn-ink", "--canvas", 4.5, "amber text on page background"),
    ("--down-ink", "--surface", 4.5, "red text on cards / down note"),
    ("--down-ink", "--canvas", 4.5, "overview offline status"),
    # Large text only (34px bold status values): 3.0 threshold.
    ("--up", "--surface", 3.0, "status-value ONLINE (34px bold)"),
    ("--down", "--surface", 3.0, "status-value OFFLINE (34px bold)"),
]

# rgba() text colours, flattened over their background first.
ALPHA_PAIRS = [
    (0.92, "#ffffff", "--brand-fill", 4.5, "login pitch paragraph"),
]


def _failures(theme_name, tokens):
    bad = []
    for fg, bg, need, note in TEXT_PAIRS:
        if fg.startswith("#"):
            fg_value = fg
        else:
            assert fg in tokens, "%s theme missing token %s" % (theme_name, fg)
            fg_value = tokens[fg]
        assert bg in tokens, "%s theme missing token %s" % (theme_name, bg)
        bg_value = tokens[bg]
        ratio = _contrast(fg_value, bg_value)
        if ratio < need:
            bad.append(
                "%s: %s -> %s %s on %s %s = %.2f (need %.1f)"
                % (theme_name, note, fg, fg_value, bg, bg_value, ratio, need)
            )
    for alpha, fg, bg, need, note in ALPHA_PAIRS:
        assert bg in tokens, "%s theme missing token %s" % (theme_name, bg)
        bg_value = tokens[bg]
        flat = _composite(alpha, fg, bg_value)
        ratio = _contrast(flat, bg_value)
        if ratio < need:
            bad.append(
                "%s: %s -> rgba(white,%.2f) %s on %s %s = %.2f (need %.1f)"
                % (theme_name, note, alpha, flat, bg, bg_value, ratio, need)
            )
    return bad


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

def test_light_theme_text_contrast_aa():
    tokens = _theme_tokens()["light"]
    assert not _failures("light", tokens), "\n".join(_failures("light", tokens))


def test_dark_theme_text_contrast_aa():
    tokens = _theme_tokens()["dark"]
    assert not _failures("dark", tokens), "\n".join(_failures("dark", tokens))


def test_dark_theme_has_brand_fill_tokens():
    """Dark mode must override the fill tokens - a dark button fill
    that keeps the light --brand value fails white-label contrast."""
    tokens = _theme_tokens()["dark"]
    for name in ("--brand-fill", "--brand-fill-hover", "--brand"):
        assert name in tokens, "missing %s in dark theme" % name


def test_chart_tooltip_colours_are_theme_aware():
    """The pure-white-tooltip-in-dark-mode bug must not return."""
    src = open(DASHBOARD_PATH, encoding="utf-8").read()

    # helper exists and the tooltip consumes it
    assert "function tooltipColors()" in src
    assert "const tt = tooltipColors();" in src
    assert "backgroundColor: tt.backgroundColor" in src
    assert "titleColor: tt.titleColor" in src
    assert "bodyColor: tt.bodyColor" in src

    # All raw token-based tooltip colours live INSIDE the helper:
    # dark branch = surface box + ink text, light branch = ink box +
    # white text. Nothing outside the helper may paint its own
    # background from a theme token (that inversion was the bug).
    m = re.search(r"function tooltipColors\(\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "tooltipColors() body not found"
    body = m.group(1)
    assert 'backgroundColor: token("--surface"' in body, (
        "dark branch must paint a surface-coloured tooltip box"
    )
    assert 'titleColor: token("--ink"' in body, (
        "dark branch must use ink-coloured tooltip title text"
    )
    assert 'bodyColor: token("--ink-2"' in body, (
        "dark branch must use ink-coloured tooltip body text"
    )
    assert 'backgroundColor: token("--ink"' in body, (
        "light branch keeps the dark ink tooltip box"
    )
    assert src.count("backgroundColor: token(") == 2, (
        "raw token tooltip backgrounds must only exist inside "
        "tooltipColors() - bind new ones through tt.* instead"
    )


def test_all_templates_share_one_css_cache_version():
    """style.css must ship with one ?v= everywhere - otherwise a
    token fix reaches only some pages and dark mode stays broken."""
    versions = set()
    for name in sorted(os.listdir(TEMPLATES_DIR)):
        if not name.endswith(".html"):
            continue
        src = open(os.path.join(TEMPLATES_DIR, name), encoding="utf-8").read()
        found = re.findall(r"style\.css'\)\s*\}\}\?v=(\d+)", src)
        if found:
            versions.update(found)
    assert len(versions) == 1, (
        "templates disagree on style.css cache version: %s"
        % sorted(versions)
    )


def test_brand_fill_tokens_exist_in_both_themes():
    tokens = _theme_tokens()
    for theme in ("light", "dark"):
        for name in ("--brand-fill", "--brand-fill-hover"):
            assert name in tokens[theme], "%s missing %s" % (theme, name)
