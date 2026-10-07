"""Authentication blueprint: /login and /logout (moved from app.py).

Endpoints are namespaced (auth.login, auth.logout); the URL paths are
unchanged, so bookmarks, redirects and tests keep working.
"""

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from core import (
    check_csrf,
    clear_failed_attempts,
    get_csrf_token,
    get_db,
    is_locked_out,
    is_logged_in,
    record_failed_attempt,
)

bp = Blueprint("auth", __name__)


# ============================================================
# LOGIN
# ============================================================

@bp.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if is_logged_in():

        return redirect(
            url_for("pages.dashboard")
        )

    if request.method == "POST":

        client_ip = request.remote_addr or "unknown"

        if is_locked_out(client_ip):

            return render_template(
                "login.html",
                error=(
                    "Too many failed attempts. "
                    "Please wait a few minutes and try again."
                ),
                csrf_token=get_csrf_token(),
            )

        if not check_csrf(request.form.get("csrf_token")):
            return render_template(
                "login.html",
                error="Session expired. Please try again.",
                csrf_token=get_csrf_token(),
            ), 400

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        conn = get_db()

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()

        conn.close()

        if (
            user
            and check_password_hash(
                user["password"],
                password
            )
        ):

            clear_failed_attempts(client_ip)

            session.clear()

            session["user_id"] = user["id"]

            session["username"] = user["username"]

            session["role"] = (
                user["role"] if "role" in user.keys() else "admin"
            )

            session.permanent = True

            get_csrf_token()

            return redirect(
                url_for("pages.dashboard")
            )

        record_failed_attempt(client_ip)

        return render_template(
            "login.html",
            error="Invalid username or password.",
            csrf_token=get_csrf_token(),
        )

    return render_template(
        "login.html",
        csrf_token=get_csrf_token(),
    )


# ============================================================
# LOGOUT
# ============================================================

@bp.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("auth.login")
    )
