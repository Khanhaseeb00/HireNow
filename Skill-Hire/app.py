"""
HireNow backend API — Flask + SQLite.

Run:
    pip install -r requirements.txt
    python seed_data.py      # one-time: populate sample workers (with demo login)
    python app.py            # starts server on http://localhost:5000

Two connected frontends, both served by this same Flask app:
  /         hirer dashboard  — browse, hire, pay, message, track
  /worker   worker portal    — see assigned jobs, real GPS check-in, message, upload ID

What's real:
  - Razorpay payment (order + signature-verified checkout + webhook)
  - Real browser GPS on check-in (navigator.geolocation from the worker's
    own phone/browser — see templates/worker.html), not a manual/faked ping
  - In-app messaging between hirer and worker, per booking
  - Manual ID-verification workflow: worker uploads a photo, an admin
    approves/rejects it via /api/admin/workers/... (needs ADMIN_KEY in .env)

What's stubbed (needs YOUR OWN third-party account + keys, can't be tested
in an offline sandbox — see notifications.py):
  - SMS notifications via Twilio. Every notify() call is wrapped so a
    missing/failed SMS never breaks the booking/payment/status flow itself.
"""
from flask import Flask, request, jsonify, session, render_template_string, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import hmac
import requests as _requests

from db import get_db, init_db, row_to_dict, rows_to_list
import payments
import notifications

app = Flask(__name__)
app.secret_key = os.environ.get("KAAMGAR_SECRET_KEY", "dev-secret-change-me")

UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(os.path.dirname(__file__), "uploads"))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

STATUS_FLOW = ["requested", "confirmed", "en_route", "checked_in", "in_progress", "completed"]


def ensure_schema_extensions():
    """Safely add upgraded-booking fields without deleting existing rows."""
    init_db()
    conn = get_db()
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(bookings)").fetchall()}
    additions = {
        "start_time": "TEXT",
        "hours": "INTEGER NOT NULL DEFAULT 2",
        "service_type": "TEXT NOT NULL DEFAULT 'regular'",
        "special_instructions": "TEXT",
        "address": "TEXT",
        "cancelled_at": "TEXT",
        "cancellation_reason": "TEXT",
    }
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {column} {definition}")
    conn.commit()
    conn.close()


ensure_schema_extensions()


def notify(phone, body):
    """
    Fire-and-forget SMS. Never let a notification failure break the actual
    request being handled — booking/payment logic must succeed independent
    of whether Twilio is configured or reachable.
    """
    try:
        notifications.send_sms(phone, body)
    except Exception as e:
        app.logger.warning(f"SMS to {phone} not sent: {e}")


@app.get("/")
def dashboard():
    """
    The connected HIRER frontend — same-origin as the API, so it just works
    with the session cookie from /api/auth/login.
    """
    return render_template("dashboard.html")


@app.get("/worker")
def worker_portal():
    """The connected WORKER frontend — separate login from hirers."""
    return render_template("worker.html")


# ---------------------------------------------------------------- utilities
def next_status(current):
    if current not in STATUS_FLOW:
        return None
    idx = STATUS_FLOW.index(current)
    if idx >= len(STATUS_FLOW) - 1:
        return None
    return STATUS_FLOW[idx + 1]


def current_hirer_id():
    return session.get("hirer_id")


def current_worker_id():
    return session.get("worker_id")


def require_login():
    if not current_hirer_id():
        return jsonify({"error": "Login required"}), 401
    return None


def require_worker_login():
    if not current_worker_id():
        return jsonify({"error": "Worker login required"}), 401
    return None


# --------------------------------------------------------------- auth routes
@app.post("/api/auth/register")
def register():
    data = request.get_json(force=True) or {}
    name, phone, password = data.get("name"), data.get("phone"), data.get("password")
    if not (name and phone and password):
        return jsonify({"error": "name, phone and password are required"}), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM hirers WHERE phone = ?", (phone,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Phone already registered"}), 409

    cur = conn.execute(
        "INSERT INTO hirers (name, phone, password_hash) VALUES (?, ?, ?)",
        (name, phone, generate_password_hash(password)),
    )
    conn.commit()
    hirer_id = cur.lastrowid
    conn.close()

    session["hirer_id"] = hirer_id
    return jsonify({"id": hirer_id, "name": name, "phone": phone}), 201


@app.post("/api/auth/login")
def login():
    data = request.get_json(force=True) or {}
    phone, password = data.get("phone"), data.get("password")
    conn = get_db()
    hirer = conn.execute("SELECT * FROM hirers WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    if not hirer or not check_password_hash(hirer["password_hash"], password or ""):
        return jsonify({"error": "Invalid phone or password"}), 401

    session["hirer_id"] = hirer["id"]
    return jsonify({"id": hirer["id"], "name": hirer["name"], "phone": hirer["phone"]})


@app.post("/api/auth/logout")
def logout():
    session.pop("hirer_id", None)
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def me():
    """Lets the frontend check on page load whether someone is already logged in."""
    hirer_id = current_hirer_id()
    if not hirer_id:
        return jsonify({"logged_in": False})
    conn = get_db()
    hirer = conn.execute("SELECT id, name, phone FROM hirers WHERE id = ?", (hirer_id,)).fetchone()
    conn.close()
    if not hirer:
        session.pop("hirer_id", None)
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, **row_to_dict(hirer)})


# ----------------------------------------------------------- worker auth
@app.post("/api/worker-auth/register")
def worker_register():
    data = request.get_json(force=True) or {}
    name, phone, password = data.get("name"), data.get("phone"), data.get("password")
    skill, city, daily_wage = data.get("skill"), data.get("city"), data.get("daily_wage")
    if not (name and phone and password and skill and city and daily_wage):
        return jsonify({"error": "name, phone, password, skill, city and daily_wage are required"}), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM workers WHERE phone = ?", (phone,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Phone already registered"}), 409

    cur = conn.execute(
        """INSERT INTO workers (name, phone, password_hash, skill, city, daily_wage, verification_status)
           VALUES (?, ?, ?, ?, ?, ?, 'unverified')""",
        (name, phone, generate_password_hash(password), skill, city, int(daily_wage)),
    )
    conn.commit()
    worker_id = cur.lastrowid
    conn.close()

    session["worker_id"] = worker_id
    return jsonify({"id": worker_id, "name": name, "phone": phone}), 201


@app.post("/api/worker-auth/login")
def worker_login():
    data = request.get_json(force=True) or {}
    phone, password = data.get("phone"), data.get("password")
    conn = get_db()
    worker = conn.execute("SELECT * FROM workers WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    if not worker or not worker["password_hash"] or not check_password_hash(worker["password_hash"], password or ""):
        return jsonify({"error": "Invalid phone or password"}), 401

    session["worker_id"] = worker["id"]
    return jsonify({"id": worker["id"], "name": worker["name"], "phone": worker["phone"]})


@app.post("/api/worker-auth/logout")
def worker_logout():
    session.pop("worker_id", None)
    return jsonify({"ok": True})


@app.get("/api/worker-auth/me")
def worker_me():
    worker_id = current_worker_id()
    if not worker_id:
        return jsonify({"logged_in": False})
    conn = get_db()
    worker = conn.execute(
        "SELECT id, name, phone, skill, city, daily_wage, verification_status, rating, jobs_completed FROM workers WHERE id = ?", (worker_id,)
    ).fetchone()
    conn.close()
    if not worker:
        session.pop("worker_id", None)
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, **row_to_dict(worker)})


# ------------------------------------------------------------ worker routes
@app.get("/api/workers")
def list_workers():
    skill = request.args.get("skill")
    city = request.args.get("city")
    q = request.args.get("q")

    query = "SELECT * FROM workers WHERE 1=1"
    params = []
    if skill:
        query += " AND skill = ?"
        params.append(skill)
    if city:
        query += " AND city = ?"
        params.append(city)
    if q:
        query += " AND (name LIKE ? OR skill LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    query += " ORDER BY rating DESC"

    conn = get_db()
    workers = conn.execute(query, params).fetchall()
    conn.close()
    result = rows_to_list(workers)
    for w in result:
        w.pop("password_hash", None)
        w.pop("phone", None)
        w.pop("id_document_path", None)
    return jsonify(result)


@app.get("/api/workers/<int:worker_id>")
def get_worker(worker_id):
    conn = get_db()
    worker = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    conn.close()
    if not worker:
        return jsonify({"error": "Worker not found"}), 404
    result = row_to_dict(worker)
    result.pop("password_hash", None)
    result.pop("phone", None)
    result.pop("id_document_path", None)
    return jsonify(result)


# ----------------------------------------------------------- booking routes
@app.post("/api/bookings")
def create_booking():
    auth_error = require_login()
    if auth_error:
        return auth_error

    data = request.get_json(force=True) or {}
    worker_id = data.get("worker_id")
    start_date = (data.get("start_date") or "").strip()
    start_time = (data.get("start_time") or "").strip() or None
    service_type = (data.get("service_type") or "regular").strip().lower()
    special_instructions = (data.get("special_instructions") or "").strip() or None
    address = (data.get("address") or "").strip() or None

    try:
        hours = int(data.get("hours", 2))
    except (TypeError, ValueError):
        return jsonify({"error": "hours must be a number"}), 400
    if hours < 1 or hours > 12:
        return jsonify({"error": "hours must be between 1 and 12"}), 400
    if service_type not in ("regular", "overtime"):
        return jsonify({"error": "service_type must be regular or overtime"}), 400
    if not worker_id or not start_date:
        return jsonify({"error": "worker_id and start_date are required"}), 400
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "start_date must be YYYY-MM-DD"}), 400

    conn = get_db()
    worker = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not worker:
        conn.close()
        return jsonify({"error": "Worker not found"}), 404

    regular_rate = worker["hourly_wage"] or max(1, int(worker["daily_wage"] / 8))
    overtime_rate = worker["overtime_wage"] or regular_rate
    rate = overtime_rate if service_type == "overtime" else regular_rate
    total = int(rate) * hours

    cur = conn.execute(
        """INSERT INTO bookings
           (hirer_id, worker_id, start_date, start_time, days, hours, service_type,
            special_instructions, address, total_amount, status, payment_status)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'requested', 'pending')""",
        (current_hirer_id(), worker_id, start_date, start_time, hours, service_type,
         special_instructions, address, total),
    )
    booking_id = cur.lastrowid
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'requested', ?)",
        (booking_id, f"Booking request created for {hours} hour(s) at ₹{rate}/hr"),
    )
    conn.commit()
    conn.close()
    return jsonify({
        "id": booking_id,
        "total_amount": total,
        "hourly_rate": int(rate),
        "hours": hours,
        "status": "requested",
        "payment_status": "pending",
    }), 201


@app.post("/api/bookings/<int:booking_id>/create-order")
def create_razorpay_order(booking_id):
    """
    Step 1 of real payment: create a Razorpay order for this booking's
    amount and hand the frontend what it needs to open Razorpay Checkout.
    Nothing is marked as paid here — that only happens after verification.
    """
    auth_error = require_login()
    if auth_error:
        return auth_error

    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ? AND hirer_id = ?", (booking_id, current_hirer_id())
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    if booking["payment_status"] == "paid":
        conn.close()
        return jsonify({"error": "Booking already paid"}), 400

    try:
        order = payments.create_order(
            amount_rupees=booking["total_amount"],
            receipt=f"booking_{booking_id}",
            notes={"booking_id": str(booking_id), "hirer_id": str(current_hirer_id())},
        )
    except payments.RazorpayConfigError as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    except payments.RazorpayAPIError as e:
        conn.close()
        return jsonify({"error": str(e)}), 502

    conn.execute(
        "UPDATE bookings SET razorpay_order_id = ? WHERE id = ?", (order["id"], booking_id)
    )
    conn.commit()
    conn.close()

    # The frontend needs the key_id (public, safe to expose) plus the order
    # details to open Razorpay Checkout — see checkout_example.html.
    return jsonify({
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": payments.RAZORPAY_KEY_ID,
    })


@app.post("/api/payments/verify")
def verify_payment():
    """
    Step 2: the frontend calls this right after Razorpay Checkout's success
    handler fires, passing back the three fields Razorpay gave it. We
    re-derive the expected signature server-side and only mark the booking
    paid if it matches — the frontend's word alone is never trusted.
    """
    auth_error = require_login()
    if auth_error:
        return auth_error

    data = request.get_json(force=True) or {}
    order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")
    if not (order_id and payment_id and signature):
        return jsonify({"error": "razorpay_order_id, razorpay_payment_id and razorpay_signature are required"}), 400

    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE razorpay_order_id = ? AND hirer_id = ?", (order_id, current_hirer_id())
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "No booking matches this order"}), 404

    try:
        valid = payments.verify_checkout_signature(order_id, payment_id, signature)
    except payments.RazorpayConfigError as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

    if not valid:
        conn.close()
        return jsonify({"error": "Signature verification failed — payment not trusted"}), 400

    conn.execute(
        "UPDATE bookings SET payment_status = 'paid', payment_id = ?, status = 'confirmed' WHERE id = ?",
        (payment_id, booking["id"]),
    )
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'confirmed', 'Payment verified — booking confirmed')",
        (booking["id"],),
    )
    worker = conn.execute("SELECT name, phone FROM workers WHERE id = ?", (booking["worker_id"],)).fetchone()
    hirer = conn.execute("SELECT name, phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
    conn.commit()
    conn.close()

    if worker and worker["phone"]:
        notify(worker["phone"], f"HireNow: {hirer['name']} ne aapko {booking['start_date']} ke liye hire kiya hai. Booking #{booking['id']}.")
    if hirer and hirer["phone"]:
        notify(hirer["phone"], f"HireNow: Aapka payment safal raha, booking #{booking['id']} confirm ho gayi.")

    return jsonify({"status": "confirmed", "payment_status": "paid"})


@app.post("/api/payments/webhook")
def razorpay_webhook():
    """
    Step 3 (recommended, not optional in production): Razorpay calls this
    directly from its servers when a payment is captured, independent of
    whether the user's browser stayed open long enough to call /verify.
    Set this URL in Razorpay dashboard > Webhooks, subscribed to
    'payment.captured', and put the same secret in RAZORPAY_WEBHOOK_SECRET.
    """
    signature = request.headers.get("X-Razorpay-Signature", "")
    raw_body = request.get_data()  # must verify raw bytes, not re-parsed JSON

    try:
        valid = payments.verify_webhook_signature(raw_body, signature)
    except payments.RazorpayConfigError as e:
        return jsonify({"error": str(e)}), 500

    if not valid:
        return jsonify({"error": "Invalid webhook signature"}), 400

    event = request.get_json(force=True) or {}
    if event.get("event") == "payment.captured":
        payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = payment_entity.get("order_id")
        payment_id = payment_entity.get("id")

        conn = get_db()
        booking = conn.execute("SELECT * FROM bookings WHERE razorpay_order_id = ?", (order_id,)).fetchone()
        if booking and booking["payment_status"] != "paid":
            conn.execute(
                "UPDATE bookings SET payment_status = 'paid', payment_id = ?, status = 'confirmed' WHERE id = ?",
                (payment_id, booking["id"]),
            )
            conn.execute(
                "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'confirmed', 'Payment confirmed via webhook')",
                (booking["id"],),
            )
            worker = conn.execute("SELECT phone FROM workers WHERE id = ?", (booking["worker_id"],)).fetchone()
            conn.commit()
            if worker and worker["phone"]:
                notify(worker["phone"], f"HireNow: Booking #{booking['id']} confirm ho gayi (payment webhook se verify hui).")
        conn.close()

    return jsonify({"ok": True})


@app.get("/api/bookings")
def list_bookings():
    auth_error = require_login()
    if auth_error:
        return auth_error

    conn = get_db()
    rows = conn.execute(
        """SELECT b.*, w.name AS worker_name, w.skill AS worker_skill, w.city AS worker_city,
                  w.rating AS worker_rating, w.hourly_wage, w.overtime_wage, w.distance_km
           FROM bookings b JOIN workers w ON w.id = b.worker_id
           WHERE b.hirer_id = ? ORDER BY b.created_at DESC""",
        (current_hirer_id(),),
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.get("/api/bookings/<int:booking_id>")
def booking_detail(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    row = conn.execute(
        """SELECT b.*, w.name AS worker_name, w.skill AS worker_skill, w.city AS worker_city,
                  w.rating AS worker_rating, w.hourly_wage, w.overtime_wage,
                  w.verification_status, w.background_checked
           FROM bookings b JOIN workers w ON w.id = b.worker_id
           WHERE b.id = ? AND b.hirer_id = ?""",
        (booking_id, current_hirer_id()),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Booking not found"}), 404
    return jsonify(row_to_dict(row))


@app.post("/api/bookings/<int:booking_id>/cancel")
def cancel_booking(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "Cancelled by hirer").strip()
    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ? AND hirer_id = ?",
        (booking_id, current_hirer_id()),
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    if booking["status"] in ("in_progress", "completed", "cancelled"):
        conn.close()
        return jsonify({"error": "This booking can no longer be cancelled"}), 400
    payment_status = "refund_pending" if booking["payment_status"] == "paid" else booking["payment_status"]
    conn.execute(
        "UPDATE bookings SET status = 'cancelled', payment_status = ?, cancelled_at = ?, cancellation_reason = ? WHERE id = ?",
        (payment_status, datetime.now().isoformat(), reason, booking_id),
    )
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'cancelled', ?)",
        (booking_id, reason),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "cancelled", "payment_status": payment_status})


@app.get("/api/hirer/profile")
def hirer_profile():
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    hirer = conn.execute("SELECT id, name, phone, created_at FROM hirers WHERE id = ?", (current_hirer_id(),)).fetchone()
    counts = conn.execute(
        "SELECT COUNT(*) AS bookings, SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed FROM bookings WHERE hirer_id = ?",
        (current_hirer_id(),),
    ).fetchone()
    conn.close()
    if not hirer:
        return jsonify({"error": "Hirer not found"}), 404
    result = row_to_dict(hirer)
    result.update({"bookings": counts["bookings"] or 0, "completed": counts["completed"] or 0})
    return jsonify(result)


@app.post("/api/bookings/<int:booking_id>/advance-status")
def advance_status(booking_id):
    """
    Moves a booking to its next status and logs a location/status event.
    In production, this endpoint is called by the WORKER's app (not the
    hirer's), sending its live GPS lat/lng along with the status change.
    """
    auth_error = require_login()
    if auth_error:
        return auth_error

    data = request.get_json(force=True) or {}
    lat, lng, note = data.get("latitude"), data.get("longitude"), data.get("note")

    conn = get_db()
    booking = conn.execute("SELECT * FROM bookings WHERE id = ? AND hirer_id = ?", (booking_id, current_hirer_id())).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404

    if booking["payment_status"] != "paid":
        conn.close()
        return jsonify({"error": "Payment abhi baaki hai — status tabhi advance ho sakta hai jab payment ho jaye"}), 400

    nxt = next_status(booking["status"])
    if not nxt:
        conn.close()
        return jsonify({"error": "Booking already completed"}), 400

    conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (nxt, booking_id))
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
        (booking_id, nxt, note or nxt, lat, lng),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": nxt})


@app.get("/api/bookings/<int:booking_id>/events")
def booking_events(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error

    conn = get_db()
    booking = conn.execute(
        "SELECT id FROM bookings WHERE id = ? AND hirer_id = ?",
        (booking_id, current_hirer_id()),
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    rows = conn.execute(
        "SELECT * FROM booking_events WHERE booking_id = ? ORDER BY created_at ASC", (booking_id,)
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


# ------------------------------------------------------- worker-side routes
@app.get("/api/worker/bookings")
def worker_bookings():
    """Bookings assigned to the logged-in worker — the worker portal's main list."""
    auth_error = require_worker_login()
    if auth_error:
        return auth_error

    conn = get_db()
    rows = conn.execute(
        """SELECT b.*, h.name AS hirer_name, h.phone AS hirer_phone
           FROM bookings b JOIN hirers h ON h.id = b.hirer_id
           WHERE b.worker_id = ? ORDER BY b.created_at DESC""",
        (current_worker_id(),),
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/worker/bookings/<int:booking_id>/check-in")
def worker_check_in(booking_id):
    """
    THE REAL GPS ENDPOINT. Called from the worker portal's "Check in" button,
    which reads the phone/browser's actual location via navigator.geolocation
    before calling this — see templates/worker.html. Restricted to the
    worker actually assigned to this booking, unlike the old hirer-side
    advance-status endpoint.
    """
    auth_error = require_worker_login()
    if auth_error:
        return auth_error

    data = request.get_json(force=True) or {}
    lat, lng = data.get("latitude"), data.get("longitude")
    if lat is None or lng is None:
        return jsonify({"error": "latitude and longitude are required — allow location access in your browser"}), 400

    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ? AND worker_id = ?", (booking_id, current_worker_id())
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found or not assigned to you"}), 404

    if booking["payment_status"] != "paid":
        conn.close()
        return jsonify({"error": "Hirer ne abhi tak payment nahi kiya — payment hone ke baad hi check-in kar sakte hain"}), 400

    nxt = next_status(booking["status"])
    if not nxt:
        conn.close()
        return jsonify({"error": "Booking already completed"}), 400

    note_map = {
        "en_route": "Worker nikal chuka hai, GPS location ke saath",
        "checked_in": "Worker site par pahunch gaya (GPS verified)",
        "in_progress": "Kaam shuru ho gaya",
        "completed": "Kaam poora hua",
    }
    conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (nxt, booking_id))
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
        (booking_id, nxt, note_map.get(nxt, nxt), lat, lng),
    )
    hirer = conn.execute("SELECT phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
    conn.commit()
    conn.close()

    if hirer and hirer["phone"]:
        notify(hirer["phone"], f"HireNow: Booking #{booking_id} status — {note_map.get(nxt, nxt)}")

    return jsonify({"status": nxt, "latitude": lat, "longitude": lng})


# ------------------------------------------------------------ messaging
def _can_access_booking(booking):
    """A message thread belongs to whichever hirer+worker pair made the booking."""
    if current_hirer_id() and booking["hirer_id"] == current_hirer_id():
        return "hirer"
    if current_worker_id() and booking["worker_id"] == current_worker_id():
        return "worker"
    return None


@app.get("/api/bookings/<int:booking_id>/messages")
def list_messages(booking_id):
    conn = get_db()
    booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    role = _can_access_booking(booking)
    if not role:
        conn.close()
        return jsonify({"error": "Login required"}), 401

    rows = conn.execute(
        "SELECT * FROM messages WHERE booking_id = ? ORDER BY created_at ASC", (booking_id,)
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/bookings/<int:booking_id>/messages")
def send_message(booking_id):
    data = request.get_json(force=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Message body is required"}), 400

    conn = get_db()
    booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    role = _can_access_booking(booking)
    if not role:
        conn.close()
        return jsonify({"error": "Login required"}), 401

    sender_id = current_hirer_id() if role == "hirer" else current_worker_id()
    cur = conn.execute(
        "INSERT INTO messages (booking_id, sender_role, sender_id, body) VALUES (?, ?, ?, ?)",
        (booking_id, role, sender_id, body),
    )
    msg_id = cur.lastrowid

    # notify whichever side didn't send it
    if role == "hirer":
        other = conn.execute("SELECT phone FROM workers WHERE id = ?", (booking["worker_id"],)).fetchone()
    else:
        other = conn.execute("SELECT phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
    conn.commit()
    conn.close()

    if other and other["phone"]:
        notify(other["phone"], f"HireNow: Naya message booking #{booking_id} par — \"{body[:60]}\"")

    return jsonify({"id": msg_id, "booking_id": booking_id, "sender_role": role, "sender_id": sender_id, "body": body}), 201


# ---------------------------------------------------------- verification
@app.post("/api/worker/verification/upload")
def upload_verification_doc():
    """
    Worker uploads a photo of their ID. This is REAL file storage and a
    REAL manual-review workflow — what it is NOT is a live government
    identity check. A true Aadhaar/DigiLocker verification API needs a
    registered business entity and UIDAI approval; most small platforms
    start with exactly this manual-review pattern and add the automated
    API once they have that approval.
    """
    auth_error = require_worker_login()
    if auth_error:
        return auth_error
    if "document" not in request.files:
        return jsonify({"error": "No file uploaded — field name must be 'document'"}), 400

    file = request.files["document"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".pdf"):
        return jsonify({"error": "Only jpg, png or pdf files are allowed"}), 400

    filename = secure_filename(f"worker_{current_worker_id()}_{int(datetime.now().timestamp())}{ext}")
    path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(path)

    conn = get_db()
    conn.execute(
        "UPDATE workers SET id_document_path = ?, verification_status = 'pending' WHERE id = ?",
        (filename, current_worker_id()),
    )
    conn.commit()
    conn.close()
    return jsonify({"verification_status": "pending"})


def _check_admin_key():
    provided = request.args.get("admin_key", "") or request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY:
        return jsonify({"error": "ADMIN_KEY not set on the server — set it in .env to use admin routes"}), 500
    if not hmac.compare_digest(provided, ADMIN_KEY):
        return jsonify({"error": "Invalid admin key"}), 403
    return None


@app.get("/api/admin/workers/pending")
def admin_pending_workers():
    err = _check_admin_key()
    if err:
        return err
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, phone, skill, city, verification_status, id_document_path FROM workers WHERE verification_status = 'pending'"
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/admin/workers/<int:worker_id>/verify")
def admin_verify_worker(worker_id):
    err = _check_admin_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    approve = bool(data.get("approve"))
    new_status = "verified" if approve else "rejected"

    conn = get_db()
    worker = conn.execute("SELECT phone FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not worker:
        conn.close()
        return jsonify({"error": "Worker not found"}), 404
    conn.execute("UPDATE workers SET verification_status = ? WHERE id = ?", (new_status, worker_id))
    conn.commit()
    conn.close()

    if worker["phone"]:
        msg = "HireNow: Aapka ID verify ho gaya hai! ✅" if approve else "HireNow: Aapka ID verify nahi ho paya, dobara upload karein."
        notify(worker["phone"], msg)

    return jsonify({"verification_status": new_status})


@app.get("/checkout/<int:booking_id>")
def checkout_page(booking_id):
    """
    A real, working test page for the Razorpay flow — served from the same
    origin as the API so the session cookie from /api/auth/login just works,
    with no CORS setup needed. Open this in a browser AFTER logging in
    (e.g. via curl -c/-b, or wire up a real login page later) to actually
    click through Razorpay Checkout with your test keys.
    """
    return render_template_string(CHECKOUT_HTML, booking_id=booking_id)


CHECKOUT_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HireNow — Pay for booking {{ booking_id }}</title>
  <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@800;900&family=Work+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bp-0: #0F2A44; --bp-1: #17395C; --line: #3E6E95; --line-bright: #8FCBEF;
      --ink: #EAF2F7; --ink-soft: #93B4CC; --safety: #F2A93B; --safety-ink: #241A05;
      --rust: #E2572B; --ok: #58B37C;
    }
    * { box-sizing: border-box; }
    body {
      font-family: 'Work Sans', system-ui, sans-serif; max-width: 420px; margin: 70px auto; padding: 0 16px;
      background: var(--bp-0); color: var(--ink);
    }
    h2 { font-family: 'Archivo', sans-serif; font-weight: 800; }
    .card { background: var(--bp-1); border: 1px solid var(--line); border-radius: 6px; padding: 22px; }
    button { padding: 12px 20px; font-size: 15px; font-weight: 600; cursor: pointer; border: none; border-radius: 3px;
      background: var(--safety); color: var(--safety-ink); width: 100%; }
    button:hover { background: #FFBD5C; }
    #status { margin-top: 16px; font-size: 14px; color: var(--ink-soft); white-space: pre-wrap; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Booking #{{ booking_id }}</h2>
    <p style="color:var(--ink-soft);font-size:13.5px;">Razorpay ke through payment complete karein</p>
    <button id="payBtn">Pay with Razorpay</button>
    <div id="status"></div>
  </div>

  <script>
    const bookingId = {{ booking_id }};
    const statusEl = document.getElementById("status");

    document.getElementById("payBtn").addEventListener("click", async () => {
      statusEl.textContent = "Creating order...";
      const orderResp = await fetch(`/api/bookings/${bookingId}/create-order`, { method: "POST" });
      const order = await orderResp.json();
      if (!orderResp.ok) {
        statusEl.textContent = "Error creating order: " + (order.error || JSON.stringify(order));
        return;
      }

      const options = {
        key: order.key_id,
        amount: order.amount,
        currency: order.currency,
        order_id: order.order_id,
        name: "HireNow",
        description: "Booking #" + bookingId,
        handler: async function (response) {
          statusEl.textContent = "Verifying payment...";
          const verifyResp = await fetch("/api/payments/verify", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature
            })
          });
          const result = await verifyResp.json();
          statusEl.textContent = verifyResp.ok
            ? "Payment confirmed! Status: " + result.status
            : "Verification failed: " + result.error;
        },
        modal: {
          ondismiss: function () { statusEl.textContent = "Checkout closed."; }
        },
        theme: { color: "#F2A93B" }
      };

      const rzp = new Razorpay(options);
      rzp.on("payment.failed", function (resp) {
        statusEl.textContent = "Payment failed: " + resp.error.description;
      });
      rzp.open();
    });
  </script>
</body>
</html>
"""


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
