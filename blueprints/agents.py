"""Admin page for remote-agent ingest tokens (Phase 13): /agents and /agents/<company>/token|revoke. Endpoints are namespaced agents.*; the URL paths are unchanged.
"""

import secrets

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import COMPANIES, is_valid_company

from core import (
    _agent_token_hash,
    check_csrf,
    get_csrf_token,
    get_db,
    require_admin,
    utc_now_str,
)

bp = Blueprint("agents", __name__)


# ============================================================
# REMOTE AGENTS (admin only): per-company ingest tokens
# ============================================================

def _agents_redirect(msg=None, err=None):
    return redirect(url_for("agents.agents_page", msg=msg, err=err))


@bp.route("/agents")
@require_admin
def agents_page():
    conn = get_db()
    tokens = {
        row["company"]: dict(row)
        for row in conn.execute(
            "SELECT company, created_at, last_seen FROM agent_tokens"
        ).fetchall()
    }
    conn.close()

    companies = []
    for name, targets in COMPANIES.items():
        managed_by = targets.get("managed_by", "local")
        tok = tokens.get(name)
        companies.append({
            "name": name,
            "managed_by": managed_by,
            "has_token": tok is not None,
            "created_at": tok["created_at"] if tok else None,
            "last_seen": tok["last_seen"] if tok else None,
        })

    # Plaintext token shown exactly once, right after creation.
    # It travels via the session (not a URL) so it never lands in
    # browser history or server access logs.
    new_token = None
    new_token_company = None
    pending = session.pop("pending_agent_token", None)
    if pending:
        new_token, new_token_company = pending

    return render_template(
        "agents.html",
        username=session.get("username", "User"),
        companies=companies,
        csrf_token=get_csrf_token(),
        new_token=new_token,
        new_token_company=new_token_company,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@bp.route("/agents/<company>/token", methods=["POST"])
@require_admin
def agents_create_token(company):
    if not check_csrf(request.form.get("csrf_token")):
        return _agents_redirect(err="Session expired. Please try again.")
    if not is_valid_company(company):
        return _agents_redirect(err="Unknown company.")

    token = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "DELETE FROM agent_tokens WHERE company = ?",
        (company,),
    )
    conn.execute(
        "INSERT INTO agent_tokens (company, token_hash, created_at) "
        "VALUES (?, ?, ?)",
        (company, _agent_token_hash(token), utc_now_str()),
    )
    conn.commit()
    conn.close()

    # One-shot display via the session (popped on the next render).
    session["pending_agent_token"] = (token, company)
    return _agents_redirect(
        msg=f"Token created for {company}. Copy it now - it is shown "
            "only once.",
    )


@bp.route("/agents/<company>/revoke", methods=["POST"])
@require_admin
def agents_revoke_token(company):
    if not check_csrf(request.form.get("csrf_token")):
        return _agents_redirect(err="Session expired. Please try again.")
    if not is_valid_company(company):
        return _agents_redirect(err="Unknown company.")

    conn = get_db()
    cur = conn.execute(
        "DELETE FROM agent_tokens WHERE company = ?",
        (company,),
    )
    conn.commit()
    conn.close()

    if cur.rowcount:
        return _agents_redirect(msg=f"Token for {company} revoked.")
    return _agents_redirect(err=f"No token exists for {company}.")


