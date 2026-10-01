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
    approves/rejects it via authenticated admin routes

What's stubbed (needs YOUR OWN third-party account + keys, can't be tested
in an offline sandbox — see notifications.py):
  - SMS notifications via Twilio. Every notify() call is wrapped so a
    missing/failed SMS never breaks the booking/payment/status flow itself.
"""
from flask import Flask, request, jsonify, session, render_template_string, render_template, Response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
import secrets
import re
import requests as _requests

from db import get_db, init_db, row_to_dict, rows_to_list, table_columns, id_column_sql, for_update, text_timestamp_default, foreign_id_sql, binary_sql, is_postgres
import payments
import notifications

app = Flask(__name__)
app.secret_key = os.environ.get("KAAMGAR_SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

STATUS_FLOW = ["requested", "confirmed", "en_route", "checked_in", "in_progress", "completed"]
STANDARD_WORKDAY_HOURS = 8
# V1 diagnosis distance policy. Keep centrally configured so Admin pricing can replace it later.
DIAGNOSIS_DISTANCE_SLABS = [(5, 100), (10, 150), (20, 250)]


def ensure_schema_extensions():
    """Safely add upgraded-booking fields without deleting existing rows."""
    init_db()
    conn = get_db()
    existing = table_columns(conn, "bookings")
    additions = {
        "start_time": "TEXT",
        "hours": "INTEGER NOT NULL DEFAULT 2",
        "service_type": "TEXT NOT NULL DEFAULT 'regular'",
        "special_instructions": "TEXT",
        "address": "TEXT",
        "payment_method": "TEXT NOT NULL DEFAULT 'online'",
        "worker_response_at": "TEXT",
        "worker_rejection_reason": "TEXT",
        "cash_otp_hash": "TEXT",
        "cash_otp_expires_at": "TEXT",
        "cash_verified_at": "TEXT",
        "cancelled_at": "TEXT",
        "cancellation_reason": "TEXT",
        "end_time": "TEXT",
        "booking_type": "TEXT NOT NULL DEFAULT 'regular'",
        "diagnosis_fee": "INTEGER NOT NULL DEFAULT 0",
        "diagnosis_distance_km": "REAL",
        "diagnosis_notes": "TEXT",
        "work_approved_at": "TEXT",
        "work_started_at": "TEXT",
        "work_ended_at": "TEXT",
        "actual_minutes": "INTEGER NOT NULL DEFAULT 0",
        "work_amount": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {column} {definition}")

    worker_columns = table_columns(conn, "workers")
    if "is_online" not in worker_columns:
        conn.execute("ALTER TABLE workers ADD COLUMN is_online INTEGER NOT NULL DEFAULT 1")
    if "rate_status" not in worker_columns:
        conn.execute("ALTER TABLE workers ADD COLUMN rate_status TEXT NOT NULL DEFAULT 'approved'")
    if "rate_review_note" not in worker_columns:
        conn.execute("ALTER TABLE workers ADD COLUMN rate_review_note TEXT")

    kyc_columns = {
        "id_document_data": binary_sql(),
        "id_document_mime": "TEXT",
        "id_document_name": "TEXT",
        "id_document_uploaded_at": "TEXT",
    }
    for column, definition in kyc_columns.items():
        if column not in worker_columns:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {column} {definition}")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS worker_payout_accounts (
            worker_id {foreign_id_sql()} PRIMARY KEY,
            account_holder_name TEXT NOT NULL,
            account_number_last4 TEXT NOT NULL,
            account_number_encrypted TEXT,
            ifsc TEXT NOT NULL,
            bank_name TEXT,
            upi_id TEXT,
            provider_account_id TEXT,
            provider_fund_account_id TEXT,
            verification_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT {text_timestamp_default()},
            updated_at TEXT DEFAULT {text_timestamp_default()},
            FOREIGN KEY(worker_id) REFERENCES workers(id)
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS worker_availability (
            id {id_column_sql()},
            worker_id {foreign_id_sql()} NOT NULL,
            weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
            enabled INTEGER NOT NULL DEFAULT 1,
            start_time TEXT NOT NULL DEFAULT '08:00',
            end_time TEXT NOT NULL DEFAULT '20:00',
            UNIQUE(worker_id, weekday),
            FOREIGN KEY(worker_id) REFERENCES workers(id)
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS worker_unavailable_dates (
            id {id_column_sql()},
            worker_id {foreign_id_sql()} NOT NULL,
            unavailable_date TEXT NOT NULL,
            UNIQUE(worker_id, unavailable_date),
            FOREIGN KEY(worker_id) REFERENCES workers(id)
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS in_app_notifications (
            id {id_column_sql()},
            recipient_type TEXT NOT NULL CHECK(recipient_type IN ('hirer','worker')),
            recipient_id {foreign_id_sql()} NOT NULL,
            booking_id {foreign_id_sql()},
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT {text_timestamp_default()}
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON in_app_notifications(recipient_type, recipient_id, is_read, created_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            attempt_key TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            first_failed_at TEXT NOT NULL,
            locked_until TEXT
        )
    """)
    conn.commit()
    conn.close()


ensure_schema_extensions()


@app.get("/api/health/database")
def database_health():
    """Non-secret diagnostic: reports active DB engine and connectivity only."""
    conn = None
    try:
        conn = get_db()
        row = conn.execute("SELECT 1 AS ok").fetchone()
        ok = bool(row and row["ok"] == 1)
        return jsonify({"ok": ok, "engine": "postgresql" if is_postgres() else "sqlite"})
    except Exception:
        app.logger.exception("Database health check failed")
        return jsonify({"ok": False, "engine": "postgresql" if is_postgres() else "sqlite"}), 503
    finally:
        if conn is not None:
            conn.close()


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


def add_in_app_notification(conn, recipient_type, recipient_id, event_type, title, message, booking_id=None):
    conn.execute(
        """INSERT INTO in_app_notifications
           (recipient_type, recipient_id, booking_id, event_type, title, message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (recipient_type, recipient_id, booking_id, event_type, title, message),
    )


def notification_payload(conn, recipient_type, recipient_id):
    rows = conn.execute(
        """SELECT id, booking_id, event_type, title, message, is_read, created_at
           FROM in_app_notifications
           WHERE recipient_type = ? AND recipient_id = ?
           ORDER BY id DESC LIMIT 100""",
        (recipient_type, recipient_id),
    ).fetchall()
    items = rows_to_list(rows)
    return {"items": items, "unread_count": sum(1 for item in items if not item["is_read"])}


def mark_notification_read(conn, recipient_type, recipient_id, notification_id=None):
    if notification_id is None:
        conn.execute(
            "UPDATE in_app_notifications SET is_read = 1 WHERE recipient_type = ? AND recipient_id = ?",
            (recipient_type, recipient_id),
        )
    else:
        conn.execute(
            "UPDATE in_app_notifications SET is_read = 1 WHERE id = ? AND recipient_type = ? AND recipient_id = ?",
            (notification_id, recipient_type, recipient_id),
        )


@app.get("/api/notifications")
def hirer_notifications():
    if not current_hirer_id():
        return jsonify({"error": "Hirer login required"}), 401
    conn = get_db()
    payload = notification_payload(conn, "hirer", current_hirer_id())
    conn.close()
    return jsonify(payload)


@app.post("/api/notifications/read")
def hirer_notifications_read():
    if not current_hirer_id():
        return jsonify({"error": "Hirer login required"}), 401
    data = request.get_json(silent=True) or {}
    conn = get_db()
    mark_notification_read(conn, "hirer", current_hirer_id(), data.get("id"))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.get("/api/worker/notifications")
def worker_notifications():
    if not current_worker_id():
        return jsonify({"error": "Worker login required"}), 401
    conn = get_db()
    payload = notification_payload(conn, "worker", current_worker_id())
    conn.close()
    return jsonify(payload)


@app.post("/api/worker/notifications/read")
def worker_notifications_read():
    if not current_worker_id():
        return jsonify({"error": "Worker login required"}), 401
    data = request.get_json(silent=True) or {}
    conn = get_db()
    mark_notification_read(conn, "worker", current_worker_id(), data.get("id"))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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


@app.get("/admin")
def admin_portal():
    """HireNow operations dashboard. Admin APIs require an authenticated admin session."""
    return render_template("admin.html")


# ---------------------------------------------------------------- utilities
def next_status(current):
    if current not in STATUS_FLOW:
        return None
    idx = STATUS_FLOW.index(current)
    if idx >= len(STATUS_FLOW) - 1:
        return None
    return STATUS_FLOW[idx + 1]


def booking_allows_work(booking):
    """Online jobs require paid status; cash jobs can run after worker acceptance."""
    method = booking["payment_method"] if "payment_method" in booking.keys() else "online"
    if method == "cash":
        return booking["payment_status"] in ("cash_pending", "paid")
    return booking["payment_status"] == "paid"


def parse_booking_start(start_date, start_time):
    """Parse a booking start using the date/time formats accepted by the UI."""
    if not start_time:
        return None
    raw = str(start_time).strip().upper()
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            parsed_time = datetime.strptime(raw, fmt).time()
            parsed_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            return datetime.combine(parsed_date, parsed_time)
        except ValueError:
            continue
    raise ValueError("start_time must be HH:MM AM/PM or HH:MM")


def find_worker_schedule_conflict(conn, worker_id, start_date, start_time, hours, exclude_booking_id=None, include_requested=True):
    """Return an overlapping active booking for this worker, if one exists."""
    blocked = {"confirmed", "en_route", "checked_in", "in_progress"}
    if include_requested:
        blocked.add("requested")
    rows = conn.execute(
        "SELECT id, start_date, start_time, end_time, hours, status FROM bookings WHERE worker_id = ? AND start_date = ?",
        (worker_id, start_date),
    ).fetchall()
    new_start = parse_booking_start(start_date, start_time) if start_time else None
    new_end = new_start + timedelta(hours=int(hours)) if new_start else None
    for row in rows:
        if exclude_booking_id is not None and int(row["id"]) == int(exclude_booking_id):
            continue
        if row["status"] not in blocked:
            continue
        if not new_start or not row["start_time"]:
            return row
        try:
            old_start = parse_booking_start(row["start_date"], row["start_time"])
        except ValueError:
            return row
        if row["end_time"]:
            try:
                old_end = datetime.combine(old_start.date(), datetime.strptime(row["end_time"], "%H:%M").time())
            except ValueError:
                old_end = old_start + timedelta(hours=int(row["hours"] or 2))
        else:
            old_end = old_start + timedelta(hours=int(row["hours"] or 2))
        if new_start < old_end and old_start < new_end:
            return row
    return None


WORKER_SKILLS = (
    "Painter", "Mistri / Mason", "Carpenter", "Tile Worker", "Electrician",
    "AC Technician", "Welder", "Plumber", "Helper / Majdoor", "Cleaner", "Driver",
)


def normalize_worker_skill(value, strict=False):
    """Map common spelling/case variants to one marketplace category name."""
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    key = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    aliases = {
        "painter": "Painter", "painting": "Painter", "paint": "Painter",
        "mistri": "Mistri / Mason", "mason": "Mistri / Mason", "mistri mason": "Mistri / Mason",
        "carpenter": "Carpenter", "carpentry": "Carpenter",
        "tile": "Tile Worker", "tiles": "Tile Worker", "tile worker": "Tile Worker",
        "electric": "Electrician", "electrical": "Electrician", "electrician": "Electrician",
        "ac": "AC Technician", "ac technician": "AC Technician", "ac repair": "AC Technician",
        "air conditioner technician": "AC Technician", "air conditioning technician": "AC Technician",
        "welder": "Welder", "welding": "Welder",
        "plumber": "Plumber", "plumbing": "Plumber",
        "helper": "Helper / Majdoor", "majdoor": "Helper / Majdoor", "mazdoor": "Helper / Majdoor",
        "helper majdoor": "Helper / Majdoor", "helper mazdoor": "Helper / Majdoor",
        "cleaner": "Cleaner", "cleaning": "Cleaner",
        "driver": "Driver", "driving": "Driver",
    }
    canonical = aliases.get(key)
    if canonical:
        return canonical
    if strict:
        return None
    return raw.title() if raw else raw


def default_worker_schedule():
    return [
        {"weekday": day, "enabled": 1, "start_time": "08:00", "end_time": "20:00"}
        for day in range(7)
    ]


def get_worker_schedule(conn, worker_id):
    rows = conn.execute(
        "SELECT weekday, enabled, start_time, end_time FROM worker_availability WHERE worker_id = ? ORDER BY weekday",
        (worker_id,),
    ).fetchall()
    if not rows:
        return default_worker_schedule()
    by_day = {int(row["weekday"]): row_to_dict(row) for row in rows}
    return [by_day.get(day, {"weekday": day, "enabled": 0, "start_time": "08:00", "end_time": "20:00"}) for day in range(7)]


def worker_available_for_slot(conn, worker_id, start_date, start_time, hours):
    worker = conn.execute("SELECT is_online FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not worker or not int(worker["is_online"]):
        return False, "Worker is currently not accepting new bookings"
    if not start_time:
        return True, None
    try:
        start = parse_booking_start(start_date, start_time)
    except ValueError:
        return False, "Invalid booking time"
    blocked = conn.execute(
        "SELECT 1 FROM worker_unavailable_dates WHERE worker_id = ? AND unavailable_date = ?",
        (worker_id, start_date),
    ).fetchone()
    if blocked:
        return False, "Worker is unavailable on this date"
    schedule = get_worker_schedule(conn, worker_id)
    day = schedule[start.weekday()]
    if not int(day["enabled"]):
        return False, "Worker is not working on this day"
    try:
        open_time = datetime.strptime(day["start_time"], "%H:%M").time()
        close_time = datetime.strptime(day["end_time"], "%H:%M").time()
    except ValueError:
        return False, "Worker availability schedule is invalid"
    day_open = datetime.combine(start.date(), open_time)
    day_close = datetime.combine(start.date(), close_time)
    end = start + timedelta(hours=int(hours))
    if start < day_open or end > day_close:
        return False, "Selected time is outside the worker's working hours"
    return True, None


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
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""
    if not (name and phone and password):
        return jsonify({"error": "name, phone and password are required"}), 400
    if len(name) < 2:
        return jsonify({"error": "Please enter your full name"}), 400
    if not re.fullmatch(r"\+?[0-9]{10,15}", phone):
        return jsonify({"error": "Enter a valid phone number"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

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
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""
    if not phone or not password:
        return jsonify({"error": "Phone number and password are required"}), 400
    if not re.fullmatch(r"\+?[0-9]{10,15}", phone):
        return jsonify({"error": "Enter a valid phone number"}), 400
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
    skill, city, daily_wage = normalize_worker_skill(data.get("skill"), strict=True), data.get("city"), data.get("daily_wage")
    if not (name and phone and password and skill and city and daily_wage):
        return jsonify({"error": "name, phone, password, a valid skill category, city and daily_wage are required"}), 400

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
        "SELECT id, name, phone, skill, city, daily_wage, verification_status, rate_status, rate_review_note, rating, jobs_completed, is_online FROM workers WHERE id = ?", (worker_id,)
    ).fetchone()
    conn.close()
    if not worker:
        session.pop("worker_id", None)
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, **row_to_dict(worker)})


@app.get("/api/worker/payout-account")
def get_worker_payout_account():
    err = require_worker_login()
    if err: return err
    conn = get_db()
    row = conn.execute("SELECT account_holder_name, account_number_last4, ifsc, bank_name, upi_id, verification_status FROM worker_payout_accounts WHERE worker_id = ?", (current_worker_id(),)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row) if row else {})


@app.put("/api/worker/payout-account")
def save_worker_payout_account():
    err = require_worker_login()
    if err: return err
    data = request.get_json(force=True) or {}
    holder = (data.get("account_holder_name") or "").strip()
    account = re.sub(r"\s+", "", str(data.get("account_number") or ""))
    ifsc = (data.get("ifsc") or "").strip().upper()
    bank = (data.get("bank_name") or "").strip() or None
    upi = (data.get("upi_id") or "").strip() or None
    if not holder or not account.isdigit() or len(account) < 6 or not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        return jsonify({"error": "Valid account holder, account number and IFSC are required"}), 400
    # V1 intentionally does not return the full account number. Provider tokenization/encryption is required before production payouts.
    conn = get_db()
    existing = conn.execute("SELECT worker_id FROM worker_payout_accounts WHERE worker_id = ?", (current_worker_id(),)).fetchone()
    if existing:
        conn.execute("UPDATE worker_payout_accounts SET account_holder_name=?, account_number_last4=?, account_number_encrypted=NULL, ifsc=?, bank_name=?, upi_id=?, verification_status='pending', updated_at=? WHERE worker_id=?", (holder, account[-4:], ifsc, bank, upi, datetime.utcnow().isoformat(), current_worker_id()))
    else:
        conn.execute("INSERT INTO worker_payout_accounts (worker_id, account_holder_name, account_number_last4, ifsc, bank_name, upi_id) VALUES (?, ?, ?, ?, ?, ?)", (current_worker_id(), holder, account[-4:], ifsc, bank, upi))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "account_number_last4": account[-4:], "verification_status": "pending"})


@app.get("/api/worker/availability")
def worker_get_availability():
    worker_id = current_worker_id()
    if not worker_id:
        return jsonify({"error": "Worker login required"}), 401
    conn = get_db()
    worker = conn.execute("SELECT is_online FROM workers WHERE id = ?", (worker_id,)).fetchone()
    days = get_worker_schedule(conn, worker_id)
    dates = [row["unavailable_date"] for row in conn.execute(
        "SELECT unavailable_date FROM worker_unavailable_dates WHERE worker_id = ? ORDER BY unavailable_date",
        (worker_id,),
    ).fetchall()]
    conn.close()
    return jsonify({"is_online": bool(worker["is_online"]), "days": days, "unavailable_dates": dates})


@app.put("/api/worker/availability")
def worker_update_availability():
    worker_id = current_worker_id()
    if not worker_id:
        return jsonify({"error": "Worker login required"}), 401
    data = request.get_json(force=True) or {}
    days = data.get("days")
    dates = data.get("unavailable_dates", [])
    is_online = 1 if data.get("is_online", True) else 0
    if not isinstance(days, list) or len(days) != 7:
        return jsonify({"error": "days must contain all 7 weekdays"}), 400
    normalized = []
    seen = set()
    for item in days:
        try:
            weekday = int(item.get("weekday"))
            enabled = 1 if item.get("enabled") else 0
            start_time = str(item.get("start_time") or "08:00")
            end_time = str(item.get("end_time") or "20:00")
            datetime.strptime(start_time, "%H:%M")
            datetime.strptime(end_time, "%H:%M")
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid availability day or time"}), 400
        if weekday < 0 or weekday > 6 or weekday in seen:
            return jsonify({"error": "Each weekday 0-6 must appear once"}), 400
        if enabled and start_time >= end_time:
            return jsonify({"error": "Working start time must be before end time"}), 400
        seen.add(weekday)
        normalized.append((weekday, enabled, start_time, end_time))
    clean_dates = []
    for value in dates:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Unavailable dates must use YYYY-MM-DD"}), 400
        if parsed >= datetime.now().date():
            clean_dates.append(parsed.isoformat())
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE workers SET is_online = ? WHERE id = ?", (is_online, worker_id))
    for weekday, enabled, start_time, end_time in normalized:
        conn.execute(
            """INSERT INTO worker_availability(worker_id, weekday, enabled, start_time, end_time)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(worker_id, weekday) DO UPDATE SET
                 enabled=excluded.enabled, start_time=excluded.start_time, end_time=excluded.end_time""",
            (worker_id, weekday, enabled, start_time, end_time),
        )
    conn.execute("DELETE FROM worker_unavailable_dates WHERE worker_id = ?", (worker_id,))
    for value in sorted(set(clean_dates)):
        conn.execute(
            "INSERT INTO worker_unavailable_dates(worker_id, unavailable_date) VALUES (?, ?) ON CONFLICT(worker_id, unavailable_date) DO NOTHING",
            (worker_id, value),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.get("/api/workers/<int:worker_id>/available-slots")
def worker_available_slots(worker_id):
    date_value = (request.args.get("date") or "").strip()
    try:
        hours = int(request.args.get("hours", 2))
    except ValueError:
        return jsonify({"error": "hours must be a number"}), 400
    if hours < 1 or hours > 12:
        return jsonify({"error": "hours must be between 1 and 12"}), 400
    try:
        target_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    if target_date < datetime.now().date():
        return jsonify({"slots": []})
    conn = get_db()
    worker = conn.execute("SELECT id, is_online FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not worker:
        conn.close()
        return jsonify({"error": "Worker not found"}), 404
    schedule = get_worker_schedule(conn, worker_id)
    day = schedule[target_date.weekday()]
    slots = []
    if int(worker["is_online"]) and int(day["enabled"]):
        blocked_date = conn.execute(
            "SELECT 1 FROM worker_unavailable_dates WHERE worker_id = ? AND unavailable_date = ?",
            (worker_id, date_value),
        ).fetchone()
        if not blocked_date:
            cursor = datetime.combine(target_date, datetime.strptime(day["start_time"], "%H:%M").time())
            close = datetime.combine(target_date, datetime.strptime(day["end_time"], "%H:%M").time())
            while cursor + timedelta(hours=hours) <= close:
                label = cursor.strftime("%I:%M %p")
                if cursor > datetime.now():
                    conflict = find_worker_schedule_conflict(conn, worker_id, date_value, label, hours, include_requested=True)
                    if not conflict:
                        slots.append(label)
                cursor += timedelta(hours=1)
    conn.close()
    return jsonify({"slots": slots, "date": date_value, "hours": hours})


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
        w["skill"] = normalize_worker_skill(w.get("skill"))
        w.pop("password_hash", None)
        w.pop("phone", None)
        w.pop("id_document_path", None)
    return jsonify(result)


@app.get("/api/workers/<int:worker_id>")
def get_worker(worker_id):
    conn = get_db()
    worker = conn.execute(for_update("SELECT * FROM workers WHERE id = ?"), (worker_id,)).fetchone()
    conn.close()
    if not worker:
        return jsonify({"error": "Worker not found"}), 404
    result = row_to_dict(worker)
    result["skill"] = normalize_worker_skill(result.get("skill"))
    result.pop("password_hash", None)
    result.pop("phone", None)
    result.pop("id_document_path", None)
    return jsonify(result)


def derived_hourly_rate(worker):
    """Display/billing rate derived from the admin-reviewable daily wage."""
    return round(float(worker["daily_wage"]) / STANDARD_WORKDAY_HOURS, 2)


def diagnosis_fee_for_distance(distance_km):
    distance = max(0.0, float(distance_km or 0))
    for max_km, fee in DIAGNOSIS_DISTANCE_SLABS:
        if distance <= max_km:
            return fee, max_km
    return None, None


@app.get("/api/pricing/diagnosis")
def diagnosis_pricing():
    """Transparent public policy: same slabs are shown to hirer and worker."""
    return jsonify({
        "currency": "INR",
        "slabs": [{"up_to_km": km, "fee": fee} for km, fee in DIAGNOSIS_DISTANCE_SLABS],
        "note": "Diagnosis/inspection only. Repair work starts only after hirer approval."
    })


@app.post("/api/bookings/<int:booking_id>/diagnosis")
def submit_diagnosis(booking_id):
    auth_error = require_worker_login()
    if auth_error:
        return auth_error
    data = request.get_json(force=True) or {}
    notes = (data.get("notes") or "").strip()
    if not notes:
        return jsonify({"error": "Diagnosis notes are required"}), 400
    conn = get_db()
    booking = conn.execute(for_update("SELECT * FROM bookings WHERE id = ? AND worker_id = ?"), (booking_id, current_worker_id())).fetchone()
    if not booking:
        conn.close(); return jsonify({"error": "Booking not found"}), 404
    if booking["booking_type"] != "diagnosis" or booking["status"] not in ("confirmed", "checked_in"):
        conn.close(); return jsonify({"error": "Diagnosis can be submitted only for an active diagnosis booking"}), 400
    conn.execute("UPDATE bookings SET diagnosis_notes = ? WHERE id = ?", (notes, booking_id))
    add_in_app_notification(conn, "hirer", booking["hirer_id"], "diagnosis_ready", "Diagnosis ready", f"Worker submitted diagnosis for booking #{booking_id}. Review it before starting paid work.", booking_id)
    conn.commit(); conn.close()
    return jsonify({"ok": True, "diagnosis_notes": notes})


@app.post("/api/bookings/<int:booking_id>/approve-work")
def approve_diagnosed_work(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    booking = conn.execute(for_update("SELECT * FROM bookings WHERE id = ? AND hirer_id = ?"), (booking_id, current_hirer_id())).fetchone()
    if not booking:
        conn.close(); return jsonify({"error": "Booking not found"}), 404
    if booking["booking_type"] != "diagnosis" or not booking["diagnosis_notes"]:
        conn.close(); return jsonify({"error": "Worker diagnosis is required before approval"}), 400
    approved_at = datetime.utcnow().isoformat()
    conn.execute("UPDATE bookings SET work_approved_at = ? WHERE id = ?", (approved_at, booking_id))
    add_in_app_notification(conn, "worker", booking["worker_id"], "work_approved", "Work approved", f"Hirer approved paid work for booking #{booking_id}. Start the timer when work begins.", booking_id)
    conn.commit(); conn.close()
    return jsonify({"ok": True, "work_approved_at": approved_at})


@app.post("/api/worker/bookings/<int:booking_id>/work-timer")
def worker_work_timer(booking_id):
    auth_error = require_worker_login()
    if auth_error:
        return auth_error
    action = ((request.get_json(force=True) or {}).get("action") or "").lower()
    if action not in ("start", "stop"):
        return jsonify({"error": "action must be start or stop"}), 400
    conn = get_db()
    booking = conn.execute(for_update("SELECT * FROM bookings WHERE id = ? AND worker_id = ?"), (booking_id, current_worker_id())).fetchone()
    if not booking:
        conn.close(); return jsonify({"error": "Booking not found"}), 404
    if booking["booking_type"] == "diagnosis" and not booking["work_approved_at"]:
        conn.close(); return jsonify({"error": "Hirer must approve diagnosed work before timer starts"}), 409
    now = datetime.utcnow()
    if action == "start":
        if booking["work_started_at"]:
            conn.close(); return jsonify({"error": "Work timer already started"}), 409
        conn.execute("UPDATE bookings SET work_started_at = ?, status = 'in_progress' WHERE id = ?", (now.isoformat(), booking_id))
        conn.commit(); conn.close()
        return jsonify({"status": "in_progress", "work_started_at": now.isoformat()})
    if not booking["work_started_at"]:
        conn.close(); return jsonify({"error": "Work timer has not started"}), 409
    started = datetime.fromisoformat(booking["work_started_at"])
    minutes = max(1, int((now - started).total_seconds() // 60))
    worker = conn.execute("SELECT daily_wage FROM workers WHERE id = ?", (booking["worker_id"],)).fetchone()
    hourly = derived_hourly_rate(worker)
    work_amount = round(hourly * minutes / 60)
    total = int(booking["diagnosis_fee"] or 0) + int(work_amount)
    conn.execute("""UPDATE bookings SET work_ended_at = ?, actual_minutes = ?, work_amount = ?, total_amount = ?, status = 'completed' WHERE id = ?""",
                 (now.isoformat(), minutes, work_amount, total, booking_id))
    conn.commit(); conn.close()
    return jsonify({"status": "completed", "actual_minutes": minutes, "derived_hourly_rate": hourly, "work_amount": work_amount, "diagnosis_fee": booking["diagnosis_fee"], "total_amount": total})



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
    end_time = (data.get("end_time") or "").strip() or None
    booking_type = (data.get("booking_type") or "regular").strip().lower()
    payment_method = (data.get("payment_method") or "cash").strip().lower()
    special_instructions = (data.get("special_instructions") or "").strip() or None
    address = (data.get("address") or "").strip() or None

    try:
        hours = int(data.get("hours", 2))
    except (TypeError, ValueError):
        return jsonify({"error": "hours must be a number"}), 400
    if hours < 1 or hours > 12:
        return jsonify({"error": "hours must be between 1 and 12"}), 400
    if booking_type not in ("regular", "diagnosis"):
        return jsonify({"error": "booking_type must be regular or diagnosis"}), 400
    if payment_method not in ("cash", "online"):
        return jsonify({"error": "payment_method must be cash or online"}), 400
    if not worker_id or not start_date:
        return jsonify({"error": "worker_id and start_date are required"}), 400
    try:
        booking_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "start_date must be YYYY-MM-DD"}), 400
    if booking_date < datetime.now().date():
        return jsonify({"error": "Past dates cannot be booked"}), 400
    if start_time:
        try:
            booking_start = parse_booking_start(start_date, start_time)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if booking_start <= datetime.now():
            return jsonify({"error": "Please choose a future booking time"}), 400
        if end_time:
            try:
                end_clock = datetime.strptime(end_time, "%H:%M").time()
                booking_end = datetime.combine(booking_date, end_clock)
            except ValueError:
                return jsonify({"error": "end_time must use 24-hour HH:MM format"}), 400
            if booking_end <= booking_start:
                return jsonify({"error": "End time must be after start time"}), 400
            minutes = int((booking_end - booking_start).total_seconds() // 60)
            hours = max(1, int((minutes + 59) // 60))

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    worker = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not worker:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Worker not found"}), 404

    available, unavailable_reason = worker_available_for_slot(conn, worker_id, start_date, start_time, hours)
    if not available:
        conn.rollback()
        conn.close()
        return jsonify({"error": unavailable_reason}), 409

    conflict = find_worker_schedule_conflict(
        conn, worker_id, start_date, start_time, hours, include_requested=True
    )
    if conflict:
        conn.rollback()
        conn.close()
        return jsonify({
            "error": "This worker already has a booking that overlaps the selected date and time. Please choose another slot."
        }), 409

    rate = derived_hourly_rate(worker)
    diagnosis_fee = 0
    diagnosis_distance_km = None
    if booking_type == "diagnosis":
        try:
            diagnosis_distance_km = float(data.get("distance_km"))
        except (TypeError, ValueError):
            conn.rollback(); conn.close()
            return jsonify({"error": "distance_km is required for diagnosis bookings"}), 400
        diagnosis_fee, _ = diagnosis_fee_for_distance(diagnosis_distance_km)
        if diagnosis_fee is None:
            conn.rollback(); conn.close()
            return jsonify({"error": "Diagnosis address is outside the current service radius"}), 409
        total = diagnosis_fee
    else:
        total = round(rate * hours)

    cur = conn.execute(
        """INSERT INTO bookings
           (hirer_id, worker_id, start_date, start_time, end_time, days, hours, service_type, booking_type,
            diagnosis_fee, diagnosis_distance_km, special_instructions, address, payment_method, total_amount, status, payment_status)
           VALUES (?, ?, ?, ?, ?, 1, ?, 'regular', ?, ?, ?, ?, ?, ?, ?, 'requested', 'pending')""",
        (current_hirer_id(), worker_id, start_date, start_time, end_time, hours, booking_type,
         diagnosis_fee, diagnosis_distance_km, special_instructions, address, payment_method, total),
    )
    booking_id = cur.lastrowid
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'requested', ?)",
        (booking_id, f"Booking request sent to worker. Payment method: {payment_method}."),
    )
    hirer = conn.execute("SELECT name FROM hirers WHERE id = ?", (current_hirer_id(),)).fetchone()
    add_in_app_notification(
        conn, "worker", worker_id, "booking_request", "New booking request",
        f"{hirer['name'] if hirer else 'A hirer'} requested {hours} hr on {start_date} {start_time or ''}.",
        booking_id,
    )
    conn.commit()
    conn.close()

    if worker["phone"]:
        notify(
            worker["phone"],
            f"HireNow: Nayi booking request #{booking_id} — {hirer['name'] if hirer else 'Hirer'}, "
            f"{start_date} {start_time or ''}, {hours} hr. Worker portal me Accept/Reject karein."
        )

    return jsonify({
        "id": booking_id,
        "total_amount": total,
        "derived_hourly_rate": rate,
        "booking_type": booking_type,
        "diagnosis_fee": diagnosis_fee,
        "hours": hours,
        "payment_method": payment_method,
        "status": "requested",
        "payment_status": "pending",
    }), 201


@app.post("/api/bookings/<int:booking_id>/create-order")
def create_razorpay_order(booking_id):
    """Create an online-payment order only after the worker accepts the request."""
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
    if booking["payment_method"] != "online":
        conn.close()
        return jsonify({"error": "This booking is set to Cash after work"}), 400
    if booking["status"] != "confirmed":
        conn.close()
        return jsonify({"error": "Worker must accept the booking before online payment"}), 400
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
    if booking["payment_method"] != "online":
        conn.close()
        return jsonify({"error": "This booking is not an online-payment booking"}), 400
    if booking["status"] != "confirmed":
        conn.close()
        return jsonify({"error": "Worker must accept the booking before payment"}), 400

    try:
        valid = payments.verify_checkout_signature(order_id, payment_id, signature)
    except payments.RazorpayConfigError as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

    if not valid:
        conn.close()
        return jsonify({"error": "Signature verification failed — payment not trusted"}), 400

    conn.execute(
        "UPDATE bookings SET payment_status = 'paid', payment_id = ? WHERE id = ?",
        (payment_id, booking["id"]),
    )
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'confirmed', 'Online payment verified')",
        (booking["id"],),
    )
    worker = conn.execute("SELECT name, phone FROM workers WHERE id = ?", (booking["worker_id"],)).fetchone()
    hirer = conn.execute("SELECT name, phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
    add_in_app_notification(conn, "hirer", booking["hirer_id"], "payment_paid", "Payment successful", f"Online payment for booking #{booking['id']} was verified.", booking["id"])
    add_in_app_notification(conn, "worker", booking["worker_id"], "payment_paid", "Booking paid", f"Online payment for booking #{booking['id']} is complete. You can proceed with the job.", booking["id"])
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
        if booking and booking["payment_method"] == "online" and booking["status"] == "confirmed" and booking["payment_status"] != "paid":
            conn.execute(
                "UPDATE bookings SET payment_status = 'paid', payment_id = ? WHERE id = ?",
                (payment_id, booking["id"]),
            )
            conn.execute(
                "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'confirmed', 'Online payment confirmed via webhook')",
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
                  w.rating AS worker_rating, w.daily_wage, w.distance_km
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
                  w.rating AS worker_rating, w.daily_wage,
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
    if booking["status"] in ("in_progress", "completed", "cancelled", "rejected"):
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
    """Deprecated: job progress is controlled only by the assigned worker."""
    return jsonify({
        "error": "Job status can only be updated by the assigned worker from the worker portal."
    }), 403


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


@app.post("/api/worker/bookings/<int:booking_id>/respond")
def worker_respond_booking(booking_id):
    """Worker explicitly accepts or rejects a new hire request."""
    auth_error = require_worker_login()
    if auth_error:
        return auth_error

    data = request.get_json(force=True) or {}
    action = (data.get("action") or "").strip().lower()
    reason = (data.get("reason") or "").strip() or None
    if action not in ("accept", "reject"):
        return jsonify({"error": "action must be accept or reject"}), 400

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(
        for_update("SELECT * FROM bookings WHERE id = ? AND worker_id = ?"), (booking_id, current_worker_id())
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found or not assigned to you"}), 404
    if booking["status"] != "requested":
        conn.close()
        return jsonify({"error": "This request has already been answered"}), 400

    now = datetime.now().isoformat()
    hirer = conn.execute("SELECT name, phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
    worker = conn.execute("SELECT name FROM workers WHERE id = ?", (current_worker_id(),)).fetchone()

    if action == "reject":
        conn.execute(
            """UPDATE bookings SET status = 'rejected', worker_response_at = ?,
               worker_rejection_reason = ? WHERE id = ?""",
            (now, reason, booking_id),
        )
        conn.execute(
            "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'rejected', ?)",
            (booking_id, reason or "Worker rejected the request"),
        )
        add_in_app_notification(
            conn, "hirer", booking["hirer_id"], "booking_rejected", "Booking declined",
            f"{worker['name'] if worker else 'Worker'} declined booking #{booking_id}.", booking_id
        )
        conn.commit()
        conn.close()
        if hirer and hirer["phone"]:
            notify(hirer["phone"], f"HireNow: {worker['name'] if worker else 'Worker'} ne booking #{booking_id} reject kar di.")
        return jsonify({"status": "rejected"})

    conflict = find_worker_schedule_conflict(
        conn, current_worker_id(), booking["start_date"], booking["start_time"],
        booking["hours"] or 2, exclude_booking_id=booking_id, include_requested=False
    )
    if conflict:
        conn.rollback()
        conn.close()
        return jsonify({
            "error": "You already have another accepted job that overlaps this booking time."
        }), 409

    payment_status = "cash_pending" if booking["payment_method"] == "cash" else "pending"
    conn.execute(
        """UPDATE bookings SET status = 'confirmed', payment_status = ?,
           worker_response_at = ? WHERE id = ?""",
        (payment_status, now, booking_id),
    )
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'confirmed', 'Worker accepted the booking request')",
        (booking_id,),
    )
    add_in_app_notification(
        conn, "hirer", booking["hirer_id"], "booking_accepted", "Booking accepted",
        f"{worker['name'] if worker else 'Worker'} accepted booking #{booking_id}.", booking_id
    )
    conn.commit()
    conn.close()

    if hirer and hirer["phone"]:
        payment_msg = "Online payment ab complete karein." if booking["payment_method"] == "online" else "Cash ka payment kaam complete hone ke baad OTP se verify hoga."
        notify(hirer["phone"], f"HireNow: {worker['name'] if worker else 'Worker'} ne booking #{booking_id} accept kar li. {payment_msg}")

    return jsonify({"status": "confirmed", "payment_status": payment_status, "payment_method": booking["payment_method"]})


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

    if booking["status"] in ("requested", "rejected", "cancelled"):
        conn.close()
        return jsonify({"error": "Booking request pehle accept honi chahiye"}), 400
    if not booking_allows_work(booking):
        conn.close()
        return jsonify({"error": "Online payment pending hai — hirer payment kare tab kaam start karein"}), 400

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
    add_in_app_notification(
        conn, "hirer", booking["hirer_id"], "booking_status", "Booking update",
        f"Booking #{booking_id}: {note_map.get(nxt, nxt)}", booking_id
    )
    conn.commit()
    conn.close()

    if hirer and hirer["phone"]:
        notify(hirer["phone"], f"HireNow: Booking #{booking_id} status — {note_map.get(nxt, nxt)}")
        if nxt == "completed" and booking["payment_method"] == "cash" and booking["payment_status"] != "paid":
            notify(hirer["phone"], f"HireNow: Kaam complete ho gaya. Cash dene ke baad app me Cash Payment OTP generate karke worker ko dein.")

    return jsonify({"status": nxt, "latitude": lat, "longitude": lng})


@app.post("/api/bookings/<int:booking_id>/cash-otp")
def generate_cash_payment_otp(booking_id):
    """Hirer generates a short-lived OTP only after a cash job is completed."""
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
    if booking["payment_method"] != "cash":
        conn.close()
        return jsonify({"error": "This booking uses online payment"}), 400
    if booking["status"] != "completed":
        conn.close()
        return jsonify({"error": "Cash OTP can be generated only after the job is completed"}), 400
    if booking["payment_status"] == "paid":
        conn.close()
        return jsonify({"error": "Cash payment is already verified"}), 400

    otp = f"{__import__('secrets').randbelow(1000000):06d}"
    expires = datetime.now() + timedelta(minutes=10)
    conn.execute(
        """UPDATE bookings SET cash_otp_hash = ?, cash_otp_expires_at = ?,
           payment_status = 'cash_pending' WHERE id = ?""",
        (generate_password_hash(otp), expires.isoformat(), booking_id),
    )
    hirer = conn.execute("SELECT phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
    conn.commit()
    conn.close()

    if hirer and hirer["phone"]:
        notify(hirer["phone"], f"HireNow: Booking #{booking_id} cash payment OTP {otp}. Ye OTP worker ko cash dene ke baad hi batayein. 10 min valid.")

    return jsonify({"otp": otp, "expires_in_minutes": 10, "payment_status": "cash_pending"})


@app.post("/api/worker/bookings/<int:booking_id>/verify-cash-otp")
def verify_cash_payment_otp(booking_id):
    """Worker verifies the hirer's OTP after physically receiving cash."""
    auth_error = require_worker_login()
    if auth_error:
        return auth_error
    data = request.get_json(force=True) or {}
    otp = (data.get("otp") or "").strip()
    if not otp:
        return jsonify({"error": "OTP required"}), 400

    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id = ? AND worker_id = ?", (booking_id, current_worker_id())
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found or not assigned to you"}), 404
    if booking["payment_method"] != "cash" or booking["status"] != "completed":
        conn.close()
        return jsonify({"error": "Cash OTP is available only after a completed cash booking"}), 400
    if booking["payment_status"] == "paid":
        conn.close()
        return jsonify({"error": "Payment already verified"}), 400
    if not booking["cash_otp_hash"] or not booking["cash_otp_expires_at"]:
        conn.close()
        return jsonify({"error": "Hirer has not generated a cash OTP yet"}), 400
    try:
        expired = datetime.now() > datetime.fromisoformat(booking["cash_otp_expires_at"])
    except ValueError:
        expired = True
    if expired:
        conn.close()
        return jsonify({"error": "OTP expired — ask hirer to generate a new OTP"}), 400
    if not check_password_hash(booking["cash_otp_hash"], otp):
        conn.close()
        return jsonify({"error": "Invalid OTP"}), 400

    verified_at = datetime.now().isoformat()
    conn.execute(
        """UPDATE bookings SET payment_status = 'paid', cash_verified_at = ?,
           cash_otp_hash = NULL, cash_otp_expires_at = NULL WHERE id = ?""",
        (verified_at, booking_id),
    )
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'completed', 'Cash payment verified by OTP')",
        (booking_id,),
    )
    hirer = conn.execute("SELECT phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
    add_in_app_notification(conn, "hirer", booking["hirer_id"], "cash_verified", "Cash payment verified", f"Cash payment for booking #{booking_id} was verified by OTP.", booking_id)
    add_in_app_notification(conn, "worker", booking["worker_id"], "cash_verified", "Cash payment received", f"Cash payment for booking #{booking_id} is verified and added to earnings.", booking_id)
    conn.commit()
    conn.close()

    if hirer and hirer["phone"]:
        notify(hirer["phone"], f"HireNow: Booking #{booking_id} cash payment OTP se verify ho gaya.")

    return jsonify({"payment_status": "paid", "cash_verified": True})


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
        add_in_app_notification(conn, "worker", booking["worker_id"], "message", "New message", f"New message on booking #{booking_id}: {body[:80]}", booking_id)
    else:
        other = conn.execute("SELECT phone FROM hirers WHERE id = ?", (booking["hirer_id"],)).fetchone()
        add_in_app_notification(conn, "hirer", booking["hirer_id"], "message", "New message", f"New message on booking #{booking_id}: {body[:80]}", booking_id)
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
    allowed_mimes = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".pdf": "application/pdf",
    }
    if ext not in allowed_mimes:
        return jsonify({"error": "Only jpg, png or pdf files are allowed"}), 400

    document = file.read(8 * 1024 * 1024 + 1)
    if not document:
        return jsonify({"error": "Uploaded document is empty"}), 400
    if len(document) > 8 * 1024 * 1024:
        return jsonify({"error": "Verification document must be 8 MB or smaller"}), 413

    expected_mime = allowed_mimes[ext]
    if file.mimetype and file.mimetype != expected_mime:
        return jsonify({"error": "File type does not match the uploaded document"}), 400

    stored_name = f"worker_{current_worker_id()}_{int(datetime.now().timestamp())}{ext}"
    conn = get_db()
    conn.execute(
        """UPDATE workers
           SET id_document_data = ?, id_document_mime = ?, id_document_name = ?,
               id_document_uploaded_at = ?, id_document_path = NULL,
               verification_status = 'pending'
           WHERE id = ?""",
        (document, expected_mime, stored_name, datetime.utcnow().isoformat(), current_worker_id()),
    )
    conn.commit()
    conn.close()
    return jsonify({"verification_status": "pending"})


ADMIN_LOGIN_MAX_FAILURES = 5
ADMIN_LOGIN_WINDOW = timedelta(minutes=15)
ADMIN_LOGIN_LOCK = timedelta(minutes=15)


def _admin_attempt_key(username):
    remote = request.remote_addr or "unknown"
    return f"{remote}|{username.lower()}"


def _admin_login_lock_status(conn, attempt_key):
    row = conn.execute(
        "SELECT failed_count, first_failed_at, locked_until FROM admin_login_attempts WHERE attempt_key = ?",
        (attempt_key,),
    ).fetchone()
    if not row:
        return 0
    now = datetime.utcnow()
    if row["locked_until"]:
        try:
            locked_until = datetime.fromisoformat(row["locked_until"])
            if locked_until > now:
                return max(1, int((locked_until - now).total_seconds()))
        except ValueError:
            pass
    try:
        first_failed = datetime.fromisoformat(row["first_failed_at"])
    except ValueError:
        first_failed = now
    if now - first_failed > ADMIN_LOGIN_WINDOW:
        conn.execute("DELETE FROM admin_login_attempts WHERE attempt_key = ?", (attempt_key,))
        conn.commit()
    return 0


def _record_admin_login_failure(conn, attempt_key):
    now = datetime.utcnow()
    row = conn.execute(
        "SELECT failed_count, first_failed_at FROM admin_login_attempts WHERE attempt_key = ?",
        (attempt_key,),
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO admin_login_attempts(attempt_key, failed_count, first_failed_at) VALUES (?, 1, ?)",
            (attempt_key, now.isoformat()),
        )
        conn.commit()
        return 0
    try:
        first_failed = datetime.fromisoformat(row["first_failed_at"])
    except ValueError:
        first_failed = now
    count = int(row["failed_count"])
    if now - first_failed > ADMIN_LOGIN_WINDOW:
        count = 1
        first_failed = now
    else:
        count += 1
    locked_until = None
    retry_after = 0
    if count >= ADMIN_LOGIN_MAX_FAILURES:
        locked_until_dt = now + ADMIN_LOGIN_LOCK
        locked_until = locked_until_dt.isoformat()
        retry_after = int(ADMIN_LOGIN_LOCK.total_seconds())
    conn.execute(
        """INSERT INTO admin_login_attempts(attempt_key, failed_count, first_failed_at, locked_until)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(attempt_key) DO UPDATE SET
             failed_count=excluded.failed_count,
             first_failed_at=excluded.first_failed_at,
             locked_until=excluded.locked_until""",
        (attempt_key, count, first_failed.isoformat(), locked_until),
    )
    conn.commit()
    return retry_after


def _check_admin_session():
    if not session.get("admin_authenticated"):
        return jsonify({"error": "Admin login required"}), 401
    return None


def _check_admin_csrf():
    expected = session.get("admin_csrf", "")
    provided = request.headers.get("X-CSRF-Token", "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        return jsonify({"error": "Invalid security token. Refresh the admin page and try again."}), 403
    return None


@app.post("/api/admin/auth/login")
def admin_login():
    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        return jsonify({"error": "Admin credentials are not configured on the server"}), 503
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    attempt_key = _admin_attempt_key(username)
    conn = get_db()
    retry_after = _admin_login_lock_status(conn, attempt_key)
    if retry_after:
        conn.close()
        response = jsonify({"error": "Too many failed login attempts. Try again later.", "retry_after": retry_after})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    username_ok = secrets.compare_digest(username, ADMIN_USERNAME)
    password_ok = check_password_hash(ADMIN_PASSWORD_HASH, password)
    if not username_ok or not password_ok:
        retry_after = _record_admin_login_failure(conn, attempt_key)
        conn.close()
        if retry_after:
            response = jsonify({"error": "Too many failed login attempts. Login is locked for 15 minutes.", "retry_after": retry_after})
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response
        return jsonify({"error": "Invalid username or password"}), 401
    conn.execute("DELETE FROM admin_login_attempts WHERE attempt_key = ?", (attempt_key,))
    conn.commit()
    conn.close()
    session.clear()
    session["admin_authenticated"] = True
    session["admin_username"] = ADMIN_USERNAME
    session["admin_csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify({"ok": True, "username": ADMIN_USERNAME, "csrf_token": session["admin_csrf"]})


@app.get("/api/admin/auth/me")
def admin_me():
    err = _check_admin_session()
    if err:
        return err
    return jsonify({
        "authenticated": True,
        "username": session.get("admin_username"),
        "csrf_token": session.get("admin_csrf"),
    })


@app.post("/api/admin/auth/logout")
def admin_logout():
    err = _check_admin_session()
    if err:
        return err
    err = _check_admin_csrf()
    if err:
        return err
    session.pop("admin_authenticated", None)
    session.pop("admin_username", None)
    session.pop("admin_csrf", None)
    return jsonify({"ok": True})


@app.get("/api/admin/overview")
def admin_overview():
    err = _check_admin_session()
    if err:
        return err
    conn = get_db()
    stats = {
        "workers": conn.execute("SELECT COUNT(*) AS n FROM workers").fetchone()["n"],
        "hirers": conn.execute("SELECT COUNT(*) AS n FROM hirers").fetchone()["n"],
        "pending_verifications": conn.execute("SELECT COUNT(*) AS n FROM workers WHERE verification_status = 'pending'").fetchone()["n"],
        "active_bookings": conn.execute("SELECT COUNT(*) AS n FROM bookings WHERE status IN ('requested','confirmed','en_route','checked_in','in_progress')").fetchone()["n"],
        "completed_bookings": conn.execute("SELECT COUNT(*) AS n FROM bookings WHERE status = 'completed'").fetchone()["n"],
        "paid_value": conn.execute("SELECT COALESCE(SUM(total_amount),0) AS n FROM bookings WHERE payment_status = 'paid'").fetchone()["n"],
    }
    conn.close()
    return jsonify(stats)


@app.get("/api/admin/workers")
def admin_workers():
    err = _check_admin_session()
    if err:
        return err
    conn = get_db()
    rows = conn.execute(
        """SELECT id, name, phone, skill, city, daily_wage, rating,
                  jobs_completed, verification_status, rate_status, rate_review_note, id_document_path, is_online, created_at
           FROM workers ORDER BY id DESC LIMIT 500"""
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/admin/workers/<int:worker_id>/rate-review")
def admin_rate_review(worker_id):
    err = _check_admin_session()
    if err:
        return err
    err = _check_admin_csrf()
    if err:
        return err
    data = request.get_json(force=True) or {}
    decision = (data.get("decision") or "").strip().lower()
    note = (data.get("note") or "").strip() or None
    if decision not in ("approved", "rejected", "change_requested"):
        return jsonify({"error": "Invalid rate-review decision"}), 400
    conn = get_db()
    worker = conn.execute("SELECT id, daily_wage FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if not worker:
        conn.close()
        return jsonify({"error": "Worker not found"}), 404
    conn.execute("UPDATE workers SET rate_status = ?, rate_review_note = ? WHERE id = ?", (decision, note, worker_id))
    add_in_app_notification(conn, "worker", worker_id, "rate_review", "Daily rate review", "Admin reviewed your daily rate.", None)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "rate_status": decision, "note": note})


@app.get("/api/admin/bookings")
def admin_bookings():
    err = _check_admin_session()
    if err:
        return err
    conn = get_db()
    rows = conn.execute(
        """SELECT b.id, b.start_date, b.start_time, b.end_time, b.hours, b.booking_type, b.diagnosis_fee, b.work_amount, b.actual_minutes, b.total_amount, b.status,
                  b.payment_status, b.payment_method, b.created_at,
                  h.name AS hirer_name, w.name AS worker_name, w.skill AS worker_skill
           FROM bookings b
           JOIN hirers h ON h.id = b.hirer_id
           JOIN workers w ON w.id = b.worker_id
           ORDER BY b.id DESC LIMIT 500"""
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.get("/api/admin/workers/<int:worker_id>/document")
def admin_worker_document(worker_id):
    err = _check_admin_session()
    if err:
        return err
    conn = get_db()
    worker = conn.execute(
        "SELECT id_document_data, id_document_mime, id_document_name FROM workers WHERE id = ?",
        (worker_id,),
    ).fetchone()
    conn.close()
    if not worker or not worker["id_document_data"]:
        return jsonify({"error": "Verification document not found"}), 404
    response = Response(bytes(worker["id_document_data"]), mimetype=worker["id_document_mime"] or "application/octet-stream")
    response.headers["Content-Disposition"] = f'inline; filename="{worker["id_document_name"] or "verification-document"}"'
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/api/admin/workers/pending")
def admin_pending_workers():
    err = _check_admin_session()
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
    err = _check_admin_session()
    if err:
        return err
    err = _check_admin_csrf()
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
    add_in_app_notification(
        conn, "worker", worker_id, "verification",
        "ID verified" if approve else "ID verification update",
        "Your HireNow ID verification is approved." if approve else "Your ID could not be verified. Please upload a clear document again.",
    )
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
