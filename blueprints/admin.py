"""Admin pages moved from app.py (Phase 13): /users, /users/* and /alerts. Endpoints are namespaced admin.*; the URL paths are unchanged. /alerts reads MAINTENANCE through live_flags() so the phase-12 patch on the app module stays visible.
"""

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from werkzeug.security import generate_password_hash

from core import (
    check_csrf,
    get_csrf_token,
    get_db,
    live_flags,
    require_admin,
)

bp = Blueprint("admin", __name__)


# ============================================================
# USER MANAGEMENT (admin only)
# ============================================================

def _users_redirect(msg=None, err=None):
    """Back to the user list carrying a status banner."""
    return redirect(url_for("admin.users_page", msg=msg, err=err))


def _valid_password(password):
    return isinstance(password, str) and len(password) >= 8


@bp.route("/users")
@require_admin
def users_page():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, role FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return render_template(
        "users.html",
        username=session.get("username", "User"),
        users=[dict(row) for row in rows],
        csrf_token=get_csrf_token(),
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@bp.route("/users/create", methods=["POST"])
@require_admin
def user_create():
    if not check_csrf(request.form.get("csrf_token")):
        return _users_redirect(err="Session expired. Please try again.")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role") or "viewer"

    if not (3 <= len(username) <= 48):
        return _users_redirect(err="Username must be 3-48 characters.")
    if not _valid_password(password):
        return _users_redirect(err="Password must be at least 8 characters.")
    if role not in ("admin", "viewer"):
        return _users_redirect(err="Unknown role.")

    conn = get_db()
    clash = conn.execute(
        "SELECT id FROM users WHERE lower(username) = lower(?)",
        (username,),
    ).fetchone()
    if clash:
        conn.close()
        return _users_redirect(err="That username already exists.")

    conn.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), role),
    )
    conn.commit()
    conn.close()
    return _users_redirect(msg=f"User '{username}' created.")


@bp.route("/users/<int:user_id>/password", methods=["POST"])
@require_admin
def user_set_password(user_id):
    if not check_csrf(request.form.get("csrf_token")):
        return _users_redirect(err="Session expired. Please try again.")

    password = request.form.get("password") or ""
    if not _valid_password(password):
        return _users_redirect(err="Password must be at least 8 characters.")

    conn = get_db()
    row = conn.execute(
        "SELECT id, username FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        conn.close()
        return _users_redirect(err="User not found.")

    conn.execute(
        "UPDATE users SET password = ? WHERE id = ?",
        (generate_password_hash(password), user_id),
    )
    conn.commit()
    conn.close()
    return _users_redirect(msg=f"Password updated for '{row['username']}'.")


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@require_admin
def user_delete(user_id):
    if not check_csrf(request.form.get("csrf_token")):
        return _users_redirect(err="Session expired. Please try again.")

    if user_id == session.get("user_id"):
        return _users_redirect(err="You cannot delete your own account.")

    conn = get_db()
    row = conn.execute(
        "SELECT id, username, role FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        conn.close()
        return _users_redirect(err="User not found.")

    if row["role"] == "admin":
        admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"
        ).fetchone()["n"]
        if admins <= 1:
            conn.close()
            return _users_redirect(err="Cannot delete the last admin.")

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return _users_redirect(msg=f"User '{row['username']}' deleted.")


# ============================================================
# ALERTS (admin only)
# ============================================================

@bp.route("/alerts")
@require_admin
def alerts_page():
    """Admin page: alert channels, thresholds, maintenance windows,
    the Send-test-alert button and the recent notifications log."""
    import uptime_checker as uc

    conn = get_db()
    recent = conn.execute(
        """
        SELECT company, check_type, kind, message, channel, sent_at, status
        FROM notifications
        ORDER BY sent_at DESC, id DESC
        LIMIT 20
        """
    ).fetchall()
    conn.close()

    return render_template(
        "alerts.html",
        username=session.get("username", "User"),
        alerts_cfg=uc.ALERTS,
        maintenance_windows=live_flags().MAINTENANCE,
        recent_alerts=[dict(row) for row in recent],
    )


