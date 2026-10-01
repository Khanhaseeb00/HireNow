"""Central account-status enforcement for HireNow.

This module wraps the existing Flask app without duplicating app.py. It ensures
admin moderation is effective immediately for both new logins and existing
sessions, and prevents moderated workers from appearing in public discovery.
"""
from flask import request, session, jsonify

from app import app
from db import get_db


def _account_state(table, account_id):
    if not account_id:
        return None
    conn = get_db()
    try:
        return conn.execute(
            f"SELECT account_status, account_status_reason, deleted_at FROM {table} WHERE id = ?",
            (account_id,),
        ).fetchone()
    finally:
        conn.close()


def _blocked_payload(row):
    status = (row["account_status"] or "active") if row else "deleted"
    reason = row["account_status_reason"] if row else None
    if status == "frozen":
        message = "This account is temporarily frozen by HireNow admin."
    else:
        message = "This account is no longer active."
    payload = {"error": message, "account_status": status}
    if reason:
        payload["reason"] = reason
    return payload


def _is_blocked(row):
    return not row or row["deleted_at"] is not None or row["account_status"] != "active"


@app.before_request
def enforce_moderated_accounts():
    """Invalidate existing moderated sessions before protected APIs execute."""
    path = request.path

    # Admin APIs have their own authentication and must remain available so an
    # admin can reactivate a frozen account.
    if path.startswith("/api/admin/"):
        return None

    worker_id = session.get("worker_id")
    if worker_id and path.startswith("/api/"):
        row = _account_state("workers", worker_id)
        if _is_blocked(row):
            session.pop("worker_id", None)
            return jsonify(_blocked_payload(row)), 403

    hirer_id = session.get("hirer_id")
    if hirer_id and path.startswith("/api/"):
        row = _account_state("hirers", hirer_id)
        if _is_blocked(row):
            session.pop("hirer_id", None)
            return jsonify(_blocked_payload(row)), 403

    return None


@app.after_request
def enforce_login_and_public_visibility(response):
    """Reject moderated logins and hide moderated workers from discovery."""
    path = request.path

    # A successful login handler has already established the session. Re-check
    # the account status before that success response reaches the client.
    if response.status_code < 400 and path == "/api/auth/login":
        hirer_id = session.get("hirer_id")
        row = _account_state("hirers", hirer_id)
        if _is_blocked(row):
            session.pop("hirer_id", None)
            return jsonify(_blocked_payload(row)), 403

    if response.status_code < 400 and path == "/api/worker-auth/login":
        worker_id = session.get("worker_id")
        row = _account_state("workers", worker_id)
        if _is_blocked(row):
            session.pop("worker_id", None)
            return jsonify(_blocked_payload(row)), 403

    # Public marketplace must never advertise frozen or soft-deleted workers.
    if request.method == "GET" and path == "/api/workers" and response.is_json and response.status_code == 200:
        data = response.get_json(silent=True)
        if isinstance(data, list) and data:
            ids = [item.get("id") for item in data if item.get("id") is not None]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn = get_db()
                try:
                    rows = conn.execute(
                        f"SELECT id FROM workers WHERE id IN ({placeholders}) AND account_status = 'active' AND deleted_at IS NULL",
                        ids,
                    ).fetchall()
                finally:
                    conn.close()
                allowed = {int(row["id"]) for row in rows}
                data = [item for item in data if int(item.get("id", -1)) in allowed]
                response.set_data(app.json.dumps(data))
                response.headers["Content-Type"] = "application/json"
                response.headers["Content-Length"] = str(len(response.get_data()))

    # A direct public worker profile must also disappear once moderated.
    if request.method == "GET" and path.startswith("/api/workers/") and not path.endswith("/available-slots") and response.status_code == 200:
        worker_id = path.rsplit("/", 1)[-1]
        if worker_id.isdigit():
            row = _account_state("workers", int(worker_id))
            if _is_blocked(row):
                return jsonify({"error": "Worker not found"}), 404

    return response
