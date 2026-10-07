"""Page routes moved from app.py (Phase 13): /, /dashboard, /overview,
/reports and /incidents.

Endpoints are namespaced pages.*; the URL paths are unchanged.
"""

from flask import (
    Blueprint,
    redirect,
    render_template,
    session,
    url_for,
)

from config import COMPANIES, default_company
from core import get_csrf_token, is_logged_in

bp = Blueprint("pages", __name__)


# ============================================================
# HOME
# ============================================================

@bp.route("/")
def index():

    if is_logged_in():

        return redirect(
            url_for("pages.dashboard")
        )

    return redirect(
        url_for("auth.login")
    )


# ============================================================
# DASHBOARD
# ============================================================

@bp.route("/dashboard")
def dashboard():

    if not is_logged_in():

        return redirect(
            url_for("auth.login")
        )

    return render_template(
        "dashboard.html",
        username=session.get(
            "username",
            "User"
        ),
        companies=list(COMPANIES.keys()),
        default_company=default_company()
    )


# ============================================================
# OVERVIEW (all companies, single page)
# ============================================================

@bp.route("/overview")
def overview():

    if not is_logged_in():

        return redirect(
            url_for("auth.login")
        )

    return render_template(
        "overview.html",
        username=session.get(
            "username",
            "User"
        ),
        companies=list(COMPANIES.keys()),
        default_company=default_company()
    )


@bp.route("/reports")
def reports_page():
    if not is_logged_in():
        return redirect(url_for("auth.login"))
    return render_template(
        "reports.html",
        username=session.get("username", "User"),
        companies=list(COMPANIES.keys()),
        default_company=default_company(),
        csrf_token=get_csrf_token(),
    )


@bp.route("/incidents")
def incidents_page():
    if not is_logged_in():
        return redirect(url_for("auth.login"))
    return render_template(
        "incidents.html",
        username=session.get("username", "User"),
        companies=list(COMPANIES.keys()),
        default_company=default_company(),
    )
