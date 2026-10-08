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
from flask import Flask, request, jsonify, session, render_template_string, render_template, Response, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
import secrets
import re
import math
import requests as _requests
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from bidi.algorithm import get_display
import arabic_reshaper

from db import get_db, init_db, row_to_dict, rows_to_list, table_columns, id_column_sql, for_update, text_timestamp_default, foreign_id_sql, binary_sql, is_postgres
import payments
import payment_accounting
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

SUPPORTED_LANGUAGES = {"en", "hi", "ar", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa", "ur"}
SUPPORTED_THEMES = {"light", "dark"}


def normalize_language(value):
    value = (value or "en").strip().lower()
    return value if value in SUPPORTED_LANGUAGES else "en"


def normalize_theme(value):
    value = (value or "light").strip().lower()
    return value if value in SUPPORTED_THEMES else "light"


def parse_optional_coordinates(latitude, longitude):
    if latitude in (None, "") and longitude in (None, ""):
        return None, None
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        raise ValueError("Valid latitude and longitude are required")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Latitude or longitude is outside the valid range")
    return latitude, longitude



def ensure_schema_extensions():
    """Safely add upgraded-booking fields without deleting existing rows."""
    init_db()
    conn = get_db()
    payment_accounting.ensure_schema(conn)
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
        "service_latitude": "REAL",
        "service_longitude": "REAL",
        "diagnosis_pricing_rule_id": "INTEGER",
        "diagnosis_notes": "TEXT",
        "work_approved_at": "TEXT",
        "work_declined_at": "TEXT",
        "work_decline_reason": "TEXT",
        "work_started_at": "TEXT",
        "work_ended_at": "TEXT",
        "actual_minutes": "INTEGER NOT NULL DEFAULT 0",
        "work_amount": "INTEGER NOT NULL DEFAULT 0",
        "paid_amount": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {column} {definition}")

    worker_columns = table_columns(conn, "workers")
    for column, definition in {"account_status": "TEXT NOT NULL DEFAULT 'active'", "account_status_reason": "TEXT", "deleted_at": "TEXT"}.items():
        if column not in worker_columns:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {column} {definition}")
    for column, definition in {
        "service_latitude": "REAL",
        "service_longitude": "REAL",
        "service_location_updated_at": "TEXT",
    }.items():
        if column not in worker_columns:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {column} {definition}")
    for column, definition in {
        "preferred_language": "TEXT NOT NULL DEFAULT 'en'",
        "preferred_theme": "TEXT NOT NULL DEFAULT 'light'",
        "notifications_enabled": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        if column not in worker_columns:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {column} {definition}")
    hirer_columns = table_columns(conn, "hirers")
    for column, definition in {"account_status": "TEXT NOT NULL DEFAULT 'active'", "account_status_reason": "TEXT", "deleted_at": "TEXT"}.items():
        if column not in hirer_columns:
            conn.execute(f"ALTER TABLE hirers ADD COLUMN {column} {definition}")
    for column, definition in {
        "preferred_language": "TEXT NOT NULL DEFAULT 'en'",
        "preferred_theme": "TEXT NOT NULL DEFAULT 'light'",
        "notifications_enabled": "INTEGER NOT NULL DEFAULT 1",
        "home_address": "TEXT",
        "home_city": "TEXT",
        "home_latitude": "REAL",
        "home_longitude": "REAL",
        "location_updated_at": "TEXT",
    }.items():
        if column not in hirer_columns:
            conn.execute(f"ALTER TABLE hirers ADD COLUMN {column} {definition}")
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
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS admin_account_actions (
            id {id_column_sql()}, account_type TEXT NOT NULL, account_id {foreign_id_sql()} NOT NULL,
            action TEXT NOT NULL, reason TEXT, admin_username TEXT,
            created_at TEXT DEFAULT {text_timestamp_default()}
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS diagnosis_pricing_rules (
            id {id_column_sql()},
            city TEXT,
            skill TEXT,
            max_km REAL NOT NULL,
            fee INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT {text_timestamp_default()},
            updated_at TEXT DEFAULT {text_timestamp_default()}
        )
    """)
    existing_pricing = conn.execute("SELECT COUNT(*) AS n FROM diagnosis_pricing_rules").fetchone()["n"]
    if not existing_pricing:
        now = datetime.utcnow().isoformat()
        for max_km, fee in DIAGNOSIS_DISTANCE_SLABS:
            conn.execute(
                "INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at) VALUES (NULL, NULL, ?, ?, 1, ?, ?)",
                (max_km, fee, now, now),
            )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO platform_settings(setting_key, setting_value, updated_at) VALUES ('commission_percent','10',?) ON CONFLICT(setting_key) DO NOTHING",
        (datetime.utcnow().isoformat(),),
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS booking_financials (
            booking_id {foreign_id_sql()} PRIMARY KEY,
            worker_id {foreign_id_sql()} NOT NULL,
            gross_amount INTEGER NOT NULL DEFAULT 0,
            diagnosis_fee INTEGER NOT NULL DEFAULT 0,
            work_amount INTEGER NOT NULL DEFAULT 0,
            platform_commission INTEGER NOT NULL DEFAULT 0,
            worker_net INTEGER NOT NULL DEFAULT 0,
            payment_collected INTEGER NOT NULL DEFAULT 0,
            adjustment_amount INTEGER NOT NULL DEFAULT 0,
            settlement_status TEXT NOT NULL DEFAULT 'not_ready',
            settlement_reference TEXT,
            created_at TEXT DEFAULT {text_timestamp_default()},
            updated_at TEXT DEFAULT {text_timestamp_default()},
            FOREIGN KEY(booking_id) REFERENCES bookings(id),
            FOREIGN KEY(worker_id) REFERENCES workers(id)
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS payment_adjustments (
            id {id_column_sql()},
            booking_id {foreign_id_sql()} NOT NULL,
            adjustment_type TEXT NOT NULL CHECK(adjustment_type IN ('balance_due','refund')),
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_order_id TEXT,
            provider_payment_id TEXT,
            provider_refund_id TEXT,
            note TEXT,
            created_at TEXT DEFAULT {text_timestamp_default()},
            updated_at TEXT DEFAULT {text_timestamp_default()},
            FOREIGN KEY(booking_id) REFERENCES bookings(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_adjustments_booking ON payment_adjustments(booking_id, adjustment_type, status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            attempt_key TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            first_failed_at TEXT NOT NULL,
            locked_until TEXT
        )
    """)

    # Repair legacy payment rows created before paid_amount / settlement semantics existed.
    # A booking already marked paid is authoritative evidence that checkout/OTP verification succeeded.
    legacy_paid = conn.execute(
        """SELECT id, total_amount, paid_amount, payment_method, payment_status, status, cash_verified_at
           FROM bookings
           WHERE payment_status='paid' AND COALESCE(paid_amount, 0)=0"""
    ).fetchall()
    for booking in legacy_paid:
        conn.execute(
            "UPDATE bookings SET paid_amount=total_amount WHERE id=?",
            (booking["id"],),
        )

    # Unpaid cancelled/rejected jobs have no payment lifecycle; do not label them pending.
    conn.execute(
        """UPDATE bookings
           SET payment_status='cancelled'
           WHERE status IN ('cancelled','rejected')
             AND COALESCE(paid_amount,0)=0
             AND payment_status IN ('pending','cash_pending','balance_due')"""
    )

    # Old false balance-due rows must not survive for cash jobs, paid jobs or unpaid cancellations.
    now_repair = datetime.utcnow().isoformat()
    conn.execute(
        """UPDATE payment_adjustments
           SET status='resolved', note=COALESCE(note,'Auto-resolved by payment state repair'), updated_at=?
           WHERE status='pending'
             AND booking_id IN (
                 SELECT id FROM bookings
                 WHERE payment_method='cash'
                    OR payment_status='paid'
                    OR (status IN ('cancelled','rejected') AND COALESCE(paid_amount,0)=0)
             )""",
        (now_repair,),
    )

    # Cash is paid directly to the worker. Once OTP verification says paid, there is no platform payout due.
    cash_paid = conn.execute(
        """SELECT id, total_amount FROM bookings
           WHERE status='completed' AND payment_method='cash' AND payment_status='paid'"""
    ).fetchall()
    for booking in cash_paid:
        conn.execute(
            """UPDATE booking_financials
               SET payment_collected=?, adjustment_amount=0,
                   settlement_status='settled',
                   settlement_reference=COALESCE(settlement_reference,'cash_otp'),
                   updated_at=?
               WHERE booking_id=?""",
            (int(booking["total_amount"] or 0), now_repair, booking["id"]),
        )

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


def _moderated_account_state(table, account_id):
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


def _moderated_account_blocked(row):
    return not row or row["deleted_at"] is not None or (row["account_status"] or "active") != "active"


def _moderated_account_response(row):
    status = (row["account_status"] or "active") if row else "deleted"
    reason = row["account_status_reason"] if row else None
    message = "This account is temporarily frozen by HireNow admin." if status == "frozen" else "This account is no longer active."
    payload = {"error": message, "account_status": status}
    if reason:
        payload["reason"] = reason
    return jsonify(payload), 403


@app.before_request
def enforce_moderated_accounts():
    """Block frozen/deleted hirer and worker sessions across all API actions."""
    if not request.path.startswith("/api/") or request.path.startswith("/api/admin/"):
        return None
    worker_id = session.get("worker_id")
    if worker_id:
        row = _moderated_account_state("workers", worker_id)
        if _moderated_account_blocked(row):
            session.pop("worker_id", None)
            return _moderated_account_response(row)
    hirer_id = session.get("hirer_id")
    if hirer_id:
        row = _moderated_account_state("hirers", hirer_id)
        if _moderated_account_blocked(row):
            session.pop("hirer_id", None)
            return _moderated_account_response(row)
    return None


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
    preferred_language = normalize_language(data.get("preferred_language"))
    preferred_theme = normalize_theme(data.get("preferred_theme"))
    home_address = (data.get("address") or "").strip() or None
    home_city = (data.get("city") or "").strip() or None
    try:
        home_latitude, home_longitude = parse_optional_coordinates(data.get("latitude"), data.get("longitude"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
        """INSERT INTO hirers
           (name, phone, password_hash, preferred_language, preferred_theme,
            home_address, home_city, home_latitude, home_longitude, location_updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, phone, generate_password_hash(password), preferred_language, preferred_theme,
            home_address, home_city, home_latitude, home_longitude,
            datetime.utcnow().isoformat() if home_latitude is not None else None,
        ),
    )
    conn.commit()
    hirer_id = cur.lastrowid
    conn.close()

    session["hirer_id"] = hirer_id
    return jsonify({"id": hirer_id, "name": name, "phone": phone, "preferred_language": preferred_language, "preferred_theme": preferred_theme, "home_address": home_address, "home_city": home_city, "home_latitude": home_latitude, "home_longitude": home_longitude}), 201


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
    if _moderated_account_blocked(hirer):
        return _moderated_account_response(hirer)

    session["hirer_id"] = hirer["id"]
    return jsonify({
        "id": hirer["id"], "name": hirer["name"], "phone": hirer["phone"],
        "preferred_language": hirer["preferred_language"] if "preferred_language" in hirer.keys() else "en",
        "preferred_theme": hirer["preferred_theme"] if "preferred_theme" in hirer.keys() else "light",
        "notifications_enabled": bool(hirer["notifications_enabled"]) if "notifications_enabled" in hirer.keys() else True,
        "home_address": hirer["home_address"] if "home_address" in hirer.keys() else None,
        "home_city": hirer["home_city"] if "home_city" in hirer.keys() else None,
        "home_latitude": hirer["home_latitude"] if "home_latitude" in hirer.keys() else None,
        "home_longitude": hirer["home_longitude"] if "home_longitude" in hirer.keys() else None,
    })


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
    hirer = conn.execute("SELECT id, name, phone, preferred_language, preferred_theme, notifications_enabled, home_address, home_city, home_latitude, home_longitude, location_updated_at FROM hirers WHERE id = ?", (hirer_id,)).fetchone()
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
    try:
        daily_wage = int(daily_wage)
    except (TypeError, ValueError):
        return jsonify({"error": "daily_wage must be a whole number"}), 400
    if daily_wage <= 0:
        return jsonify({"error": "daily_wage must be greater than zero"}), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM workers WHERE phone = ?", (phone,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Phone already registered"}), 409

    cur = conn.execute(
        """INSERT INTO workers (name, phone, password_hash, skill, city, daily_wage, verification_status, rate_status)
           VALUES (?, ?, ?, ?, ?, ?, 'unverified', 'pending')""",
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
    if _moderated_account_blocked(worker):
        return _moderated_account_response(worker)

    session["worker_id"] = worker["id"]
    return jsonify({
        "id": worker["id"], "name": worker["name"], "phone": worker["phone"],
        "preferred_language": worker["preferred_language"] if "preferred_language" in worker.keys() else "en",
        "preferred_theme": worker["preferred_theme"] if "preferred_theme" in worker.keys() else "light",
        "notifications_enabled": bool(worker["notifications_enabled"]) if "notifications_enabled" in worker.keys() else True,
    })


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
        "SELECT id, name, phone, skill, city, daily_wage, verification_status, rate_status, rate_review_note, rating, jobs_completed, is_online, service_latitude, service_longitude, service_location_updated_at, preferred_language, preferred_theme, notifications_enabled FROM workers WHERE id = ?", (worker_id,)
    ).fetchone()
    conn.close()
    if not worker:
        session.pop("worker_id", None)
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, **row_to_dict(worker)})


@app.get("/api/hirer/preferences")
def hirer_preferences():
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute(
        """SELECT preferred_language, preferred_theme, notifications_enabled,
                  home_address, home_city, home_latitude, home_longitude, location_updated_at
           FROM hirers WHERE id=?""",
        (current_hirer_id(),),
    ).fetchone()
    conn.close()
    return jsonify(row_to_dict(row) if row else {})


@app.put("/api/hirer/preferences")
def update_hirer_preferences():
    err = require_login()
    if err: return err
    data = request.get_json(force=True) or {}
    language = normalize_language(data.get("preferred_language"))
    theme = normalize_theme(data.get("preferred_theme"))
    notifications_enabled = 1 if data.get("notifications_enabled", True) else 0
    conn = get_db()
    conn.execute(
        "UPDATE hirers SET preferred_language=?, preferred_theme=?, notifications_enabled=? WHERE id=?",
        (language, theme, notifications_enabled, current_hirer_id()),
    )
    conn.commit(); conn.close()
    return jsonify({"preferred_language": language, "preferred_theme": theme, "notifications_enabled": bool(notifications_enabled)})


@app.get("/api/hirer/location")
def hirer_location():
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute(
        "SELECT home_address, home_city, home_latitude, home_longitude, location_updated_at FROM hirers WHERE id=?",
        (current_hirer_id(),),
    ).fetchone()
    conn.close()
    return jsonify(row_to_dict(row) if row else {})


@app.put("/api/hirer/location")
def update_hirer_location():
    err = require_login()
    if err: return err
    data = request.get_json(force=True) or {}
    address = (data.get("address") or "").strip() or None
    city = (data.get("city") or "").strip() or None
    try:
        latitude, longitude = parse_optional_coordinates(data.get("latitude"), data.get("longitude"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not address and latitude is None:
        return jsonify({"error": "Enter an address or choose a location on the map"}), 400
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        """UPDATE hirers SET home_address=?, home_city=?, home_latitude=?, home_longitude=?,
           location_updated_at=? WHERE id=?""",
        (address, city, latitude, longitude, now, current_hirer_id()),
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True, "home_address": address, "home_city": city, "home_latitude": latitude, "home_longitude": longitude, "location_updated_at": now})


@app.get("/api/worker/preferences")
def worker_preferences():
    err = require_worker_login()
    if err: return err
    conn = get_db()
    row = conn.execute(
        "SELECT preferred_language, preferred_theme, notifications_enabled FROM workers WHERE id=?",
        (current_worker_id(),),
    ).fetchone()
    conn.close()
    return jsonify(row_to_dict(row) if row else {})


@app.put("/api/worker/preferences")
def update_worker_preferences():
    err = require_worker_login()
    if err: return err
    data = request.get_json(force=True) or {}
    language = normalize_language(data.get("preferred_language"))
    theme = normalize_theme(data.get("preferred_theme"))
    notifications_enabled = 1 if data.get("notifications_enabled", True) else 0
    conn = get_db()
    conn.execute(
        "UPDATE workers SET preferred_language=?, preferred_theme=?, notifications_enabled=? WHERE id=?",
        (language, theme, notifications_enabled, current_worker_id()),
    )
    conn.commit(); conn.close()
    return jsonify({"preferred_language": language, "preferred_theme": theme, "notifications_enabled": bool(notifications_enabled)})


@app.get("/api/worker/service-location")
def get_worker_service_location():
    err = require_worker_login()
    if err: return err
    conn = get_db()
    row = conn.execute(
        "SELECT service_latitude, service_longitude, service_location_updated_at FROM workers WHERE id=?",
        (current_worker_id(),),
    ).fetchone()
    conn.close()
    if not row or row["service_latitude"] is None or row["service_longitude"] is None:
        return jsonify({"configured": False})
    return jsonify({
        "configured": True,
        "latitude": float(row["service_latitude"]),
        "longitude": float(row["service_longitude"]),
        "updated_at": row["service_location_updated_at"],
    })


@app.put("/api/worker/service-location")
def update_worker_service_location():
    err = require_worker_login()
    if err: return err
    data = request.get_json(force=True) or {}
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid latitude and longitude are required"}), 400
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return jsonify({"error": "Latitude or longitude is outside the valid range"}), 400
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE workers SET service_latitude=?, service_longitude=?, service_location_updated_at=? WHERE id=?",
        (latitude, longitude, now, current_worker_id()),
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True, "configured": True, "updated_at": now})


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
# Public discovery uses an allowlist so KYC blobs and future private columns
# cannot enter JSON responses or be loaded for marketplace requests.
PUBLIC_WORKER_SELECT = """id, name, skill, skills_detail, city, daily_wage,
    hourly_wage, overtime_wage, background_checked, about, distance_km,
    availability, rating, jobs_completed, experience_years,
    verification_status, is_online,
    CASE WHEN service_latitude IS NOT NULL AND service_longitude IS NOT NULL
         THEN 1 ELSE 0 END AS service_location_configured"""


def public_worker_payload(row):
    result = dict(row)
    result["skill"] = normalize_worker_skill(result.get("skill"))
    result["service_location_configured"] = bool(result["service_location_configured"])
    return result


@app.get("/api/workers")
def list_workers():
    skill = request.args.get("skill")
    city = request.args.get("city")
    q = request.args.get("q")

    query = f"SELECT {PUBLIC_WORKER_SELECT} FROM workers WHERE account_status = 'active' AND deleted_at IS NULL AND rate_status = 'approved'"
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
    return jsonify([public_worker_payload(w) for w in workers])


@app.get("/api/workers/<int:worker_id>")
def get_worker(worker_id):
    conn = get_db()
    worker = conn.execute(f"SELECT {PUBLIC_WORKER_SELECT} FROM workers WHERE id = ? AND account_status = 'active' AND deleted_at IS NULL AND rate_status = 'approved'", (worker_id,)).fetchone()
    conn.close()
    if not worker:
        return jsonify({"error": "Worker not found"}), 404
    return jsonify(public_worker_payload(worker))


def get_commission_percent(conn):
    row = conn.execute("SELECT setting_value FROM platform_settings WHERE setting_key='commission_percent'").fetchone()
    try:
        return max(0.0, min(100.0, float(row["setting_value"] if row else 10)))
    except (TypeError, ValueError):
        return 10.0


def reconcile_online_booking_payment(conn, booking, notify_users=False, required_order=None, required_payment=None):
    """Use captured provider receipts across all initial and balance orders."""
    old_status = booking["payment_status"] if booking else None
    if not booking:
        return {"changed": False, "payment_status": None}
    try:
        result = payment_accounting.reconcile(conn, booking, required_order, required_payment)
    except payment_accounting.PaymentPending as exc:
        return {"changed": False, "error": str(exc), "http_status": 409, "payment_status": old_status}
    except (payments.RazorpayConfigError, payments.RazorpayAPIError) as exc:
        return {"changed": False, "error": str(exc), "http_status": 502, "payment_status": old_status}
    sync_booking_financials(conn, booking["id"])
    if result["changed"]:
        if notify_users and old_status != "paid" and result["payment_status"] == "paid":
            for role, owner in (("hirer", booking["hirer_id"]), ("worker", booking["worker_id"])):
                add_in_app_notification(conn, role, owner, "payment_paid", "Payment received",
                    f"Captured payment for booking #{booking['id']} was confirmed.", booking["id"])
    return result


def sync_payment_adjustment(conn, booking_id):
    booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        return None
    paid = int(booking["paid_amount"] or 0)
    total = int(booking["total_amount"] or 0)
    now = datetime.utcnow().isoformat()

    # Cash never creates a platform balance/refund queue: OTP means the worker was paid directly.
    if booking["payment_method"] == "cash":
        conn.execute(
            "UPDATE payment_adjustments SET status='resolved', updated_at=? WHERE booking_id=? AND status='pending'",
            (now, booking_id),
        )
        return None

    # An unpaid cancelled/rejected booking has no payment pending.
    if booking["status"] in ("cancelled", "rejected") and paid <= 0:
        conn.execute(
            "UPDATE payment_adjustments SET status='resolved', updated_at=? WHERE booking_id=? AND status='pending'",
            (now, booking_id),
        )
        return None

    # A paid cancelled online booking is a refund case.
    if booking["status"] in ("cancelled", "rejected") and paid > 0:
        diff = -paid
    elif booking["status"] != "completed":
        # Before completion the original online payment can match the estimate; do not create final-bill adjustments yet.
        conn.execute(
            "UPDATE payment_adjustments SET status='resolved', updated_at=? WHERE booking_id=? AND status='pending'",
            (now, booking_id),
        )
        return None
    else:
        diff = total - paid

    if diff == 0:
        conn.execute(
            "UPDATE payment_adjustments SET status='resolved', updated_at=? WHERE booking_id=? AND status='pending'",
            (now, booking_id),
        )
        return None
    adjustment_type = "balance_due" if diff > 0 else "refund"
    amount = abs(diff)
    existing = conn.execute(
        """SELECT * FROM payment_adjustments
           WHERE booking_id=? AND adjustment_type=? AND status='pending'
           ORDER BY id DESC LIMIT 1""",
        (booking_id, adjustment_type),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE payment_adjustments SET amount=?, updated_at=? WHERE id=?",
            (amount, now, existing["id"]),
        )
        adjustment_id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO payment_adjustments
               (booking_id, adjustment_type, amount, status, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (booking_id, adjustment_type, amount, now, now),
        )
        adjustment_id = cur.lastrowid
    opposite = "refund" if adjustment_type == "balance_due" else "balance_due"
    conn.execute(
        "UPDATE payment_adjustments SET status='resolved', updated_at=? WHERE booking_id=? AND adjustment_type=? AND status='pending'",
        (now, booking_id, opposite),
    )
    return {"id": adjustment_id, "type": adjustment_type, "amount": amount, "status": "pending"}


def sync_booking_financials(conn, booking_id):
    if not is_postgres() and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(for_update("SELECT * FROM bookings WHERE id = ?"), (booking_id,)).fetchone()
    if not booking:
        return None
    gross = int(booking["total_amount"] or 0)
    diagnosis_fee = int(booking["diagnosis_fee"] or 0)
    work_amount = int(booking["work_amount"] or 0)
    paid_amount = int(booking["paid_amount"] or 0)
    commission_percent = get_commission_percent(conn)
    commission = round(gross * commission_percent / 100)
    worker_net = max(0, gross - commission)
    adjustment = paid_amount - gross
    payout = conn.execute("SELECT verification_status FROM worker_payout_accounts WHERE worker_id = ?", (booking["worker_id"],)).fetchone()
    if booking["status"] == "completed" and booking["payment_status"] == "paid":
        if booking["payment_method"] == "cash":
            # Cash is handed directly to the worker; OTP verification is the settlement event.
            settlement_status = "settled"
        else:
            settlement_status = "pending" if payout and payout["verification_status"] == "verified" else "held"
    elif booking["status"] == "completed" and booking["payment_status"] in ("balance_due", "refund_pending"):
        settlement_status = "held"
    else:
        settlement_status = "not_ready"
    now = datetime.utcnow().isoformat()
    sync_payment_adjustment(conn, booking_id)
    conn.execute("""
        INSERT INTO booking_financials
        (booking_id, worker_id, gross_amount, diagnosis_fee, work_amount, platform_commission,
         worker_net, payment_collected, adjustment_amount, settlement_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(booking_id) DO UPDATE SET
          gross_amount=excluded.gross_amount,
          diagnosis_fee=excluded.diagnosis_fee,
          work_amount=excluded.work_amount,
          platform_commission=excluded.platform_commission,
          worker_net=excluded.worker_net,
          payment_collected=excluded.payment_collected,
          adjustment_amount=excluded.adjustment_amount,
          settlement_status=CASE
            WHEN booking_financials.settlement_status='settled'
              AND excluded.settlement_status IN ('pending','settled') THEN 'settled'
            ELSE excluded.settlement_status
          END,
          updated_at=excluded.updated_at
    """, (booking_id, booking["worker_id"], gross, diagnosis_fee, work_amount, commission,
          worker_net, paid_amount, adjustment, settlement_status, now, now))
    if booking["status"] == "completed" and booking["payment_method"] == "cash" and booking["payment_status"] == "paid":
        conn.execute(
            """UPDATE booking_financials
               SET settlement_status='settled',
                   settlement_reference=COALESCE(settlement_reference,'cash_otp'),
                   payment_collected=?,
                   adjustment_amount=0,
                   updated_at=?
               WHERE booking_id=?""",
            (gross, now, booking_id),
        )
        settlement_status = "settled"
        paid_amount = gross
        adjustment = 0
    return {
        "gross_amount": gross,
        "platform_commission": commission,
        "worker_net": worker_net,
        "payment_collected": paid_amount,
        "adjustment_amount": adjustment,
        "settlement_status": settlement_status,
        "commission_percent": commission_percent,
    }


def derived_hourly_rate(worker):
    """Display/billing rate derived from the admin-reviewable daily wage."""
    return round(float(worker["daily_wage"]) / STANDARD_WORKDAY_HOURS, 2)


def haversine_distance_km(lat1, lon1, lat2, lon2):
    """Straight-line GPS distance in kilometres between two coordinates."""
    lat1, lon1, lat2, lon2 = map(float, (lat1, lon1, lat2, lon2))
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


def diagnosis_pricing_rules(conn, city=None, skill=None):
    """Choose the most-specific active pricing scope, then return its ordered slabs."""
    city = (city or "").strip()
    skill = normalize_worker_skill(skill) if skill else None
    rows = conn.execute(
        """SELECT * FROM diagnosis_pricing_rules
           WHERE is_active=1 AND (city IS NULL OR city='') AND (skill IS NULL OR skill='')
           ORDER BY max_km ASC"""
    ).fetchall()
    scopes = []
    if city and skill:
        scopes.append((city, skill))
    if city:
        scopes.append((city, None))
    if skill:
        scopes.append((None, skill))
    for scope_city, scope_skill in scopes:
        if scope_city is not None and scope_skill is not None:
            scoped = conn.execute(
                """SELECT * FROM diagnosis_pricing_rules
                   WHERE is_active=1 AND LOWER(city)=LOWER(?) AND skill=?
                   ORDER BY max_km ASC""",
                (scope_city, scope_skill),
            ).fetchall()
        elif scope_city is not None:
            scoped = conn.execute(
                """SELECT * FROM diagnosis_pricing_rules
                   WHERE is_active=1 AND LOWER(city)=LOWER(?) AND (skill IS NULL OR skill='')
                   ORDER BY max_km ASC""",
                (scope_city,),
            ).fetchall()
        else:
            scoped = conn.execute(
                """SELECT * FROM diagnosis_pricing_rules
                   WHERE is_active=1 AND (city IS NULL OR city='') AND skill=?
                   ORDER BY max_km ASC""",
                (scope_skill,),
            ).fetchall()
        if scoped:
            return scoped
    return rows


def diagnosis_fee_for_distance(distance_km, conn=None, city=None, skill=None):
    distance = max(0.0, float(distance_km or 0))
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        rules = diagnosis_pricing_rules(conn, city=city, skill=skill)
        for rule in rules:
            if distance <= float(rule["max_km"]):
                return int(rule["fee"]), float(rule["max_km"])
        return None, None
    finally:
        if own_conn:
            conn.close()


def diagnosis_quote_for_worker(conn, worker, service_latitude, service_longitude):
    if worker["service_latitude"] is None or worker["service_longitude"] is None:
        return None, "Worker has not configured a service location yet"
    try:
        service_latitude = float(service_latitude)
        service_longitude = float(service_longitude)
    except (TypeError, ValueError):
        return None, "Valid service GPS location is required"
    if not (-90 <= service_latitude <= 90 and -180 <= service_longitude <= 180):
        return None, "Service GPS location is invalid"
    distance = haversine_distance_km(
        worker["service_latitude"], worker["service_longitude"],
        service_latitude, service_longitude,
    )
    rules = diagnosis_pricing_rules(conn, city=worker["city"], skill=worker["skill"])
    matched = next((r for r in rules if distance <= float(r["max_km"])), None)
    if not matched:
        return None, "Diagnosis address is outside this worker's configured service radius"
    return {
        "distance_km": distance,
        "fee": int(matched["fee"]),
        "rule_id": int(matched["id"]),
        "max_km": float(matched["max_km"]),
        "city": matched["city"],
        "skill": matched["skill"],
    }, None


@app.get("/api/pricing/diagnosis")
def diagnosis_pricing():
    """Public pricing policy plus an optional server-computed GPS quote."""
    worker_id = request.args.get("worker_id")
    latitude = request.args.get("latitude")
    longitude = request.args.get("longitude")
    conn = get_db()
    worker = None
    if worker_id:
        worker = conn.execute(
            """SELECT id, city, skill, service_latitude, service_longitude
               FROM workers WHERE id=? AND account_status='active' AND deleted_at IS NULL AND rate_status='approved'""",
            (worker_id,),
        ).fetchone()
        if not worker:
            conn.close()
            return jsonify({"error": "Worker not found"}), 404
    rules = diagnosis_pricing_rules(conn, city=worker["city"] if worker else None, skill=worker["skill"] if worker else None)
    response = {
        "currency": "INR",
        "slabs": [{"id": int(r["id"]), "up_to_km": float(r["max_km"]), "fee": int(r["fee"]), "city": r["city"], "skill": r["skill"]} for r in rules],
        "distance_method": "gps_straight_line",
        "note": "Diagnosis fee is calculated by the server from hirer GPS to the worker service location. Road travel distance may differ. Repair work starts only after hirer approval.",
    }
    if worker and latitude is not None and longitude is not None:
        quote, error = diagnosis_quote_for_worker(conn, worker, latitude, longitude)
        if error:
            conn.close()
            return jsonify({"error": error, **response}), 409
        response["quote"] = quote
    elif worker:
        response["worker_location_configured"] = worker["service_latitude"] is not None and worker["service_longitude"] is not None
    conn.close()
    return jsonify(response)


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
    if booking["booking_type"] != "diagnosis" or booking["status"] != "checked_in":
        conn.close(); return jsonify({"error": "Diagnosis can be submitted only after the worker checks in at the site"}), 400
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
    if booking["status"] != "checked_in":
        conn.close(); return jsonify({"error": "Work can be approved only while the worker is checked in"}), 409
    if booking["work_declined_at"]:
        conn.close(); return jsonify({"error": "Work was already declined after diagnosis"}), 409
    if booking["work_approved_at"]:
        conn.close(); return jsonify({"error": "Work is already approved"}), 409
    approved_at = datetime.utcnow().isoformat()
    conn.execute("UPDATE bookings SET work_approved_at = ? WHERE id = ?", (approved_at, booking_id))
    add_in_app_notification(conn, "worker", booking["worker_id"], "work_approved", "Work approved", f"Hirer approved paid work for booking #{booking_id}. Start the timer when work begins.", booking_id)
    conn.commit(); conn.close()
    return jsonify({"ok": True, "work_approved_at": approved_at})


@app.post("/api/bookings/<int:booking_id>/decline-work")
def decline_diagnosed_work(booking_id):
    """Close a diagnosis booking without repair work; only the diagnosis fee remains billable."""
    auth_error = require_login()
    if auth_error:
        return auth_error
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "Hirer declined repair after diagnosis").strip()

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(
        for_update("SELECT * FROM bookings WHERE id = ? AND hirer_id = ?"),
        (booking_id, current_hirer_id()),
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    if booking["booking_type"] != "diagnosis" or not booking["diagnosis_notes"]:
        conn.close()
        return jsonify({"error": "A submitted diagnosis is required before declining repair work"}), 400
    if booking["work_started_at"]:
        conn.close()
        return jsonify({"error": "Paid work has already started and can no longer be declined"}), 409
    if booking["work_approved_at"]:
        conn.close()
        return jsonify({"error": "Paid work was already approved"}), 409
    if booking["status"] != "checked_in":
        conn.close()
        return jsonify({"error": "Diagnosis-only closure is available only after site check-in"}), 409

    total = int(booking["diagnosis_fee"] or 0)
    paid_amount = int(booking["paid_amount"] or 0)
    payment_status = booking["payment_status"]
    if booking["payment_method"] == "online" and payment_status == "paid":
        if paid_amount < total:
            payment_status = "balance_due"
        elif paid_amount > total:
            payment_status = "refund_pending"
    elif booking["payment_method"] == "cash" and payment_status != "paid":
        payment_status = "cash_pending"

    declined_at = datetime.utcnow().isoformat()
    conn.execute(
        """UPDATE bookings
           SET work_declined_at = ?, work_decline_reason = ?, work_amount = 0,
               actual_minutes = 0, total_amount = ?, status = 'completed', payment_status = ?
           WHERE id = ?""",
        (declined_at, reason, total, payment_status, booking_id),
    )
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'completed', ?)",
        (booking_id, f"Diagnosis completed; repair work declined. {reason}"),
    )
    add_in_app_notification(
        conn, "worker", booking["worker_id"], "diagnosis_closed", "Diagnosis visit completed",
        f"Hirer declined repair work for booking #{booking_id}. Only the diagnosis fee remains billable.",
        booking_id,
    )
    finance = sync_booking_financials(conn, booking_id)
    conn.commit()
    conn.close()
    return jsonify({
        "status": "completed",
        "diagnosis_only": True,
        "diagnosis_fee": total,
        "total_amount": total,
        "payment_status": payment_status,
        "work_declined_at": declined_at,
        "finance": finance,
    })


@app.post("/api/worker/bookings/<int:booking_id>/work-timer")
def worker_work_timer(booking_id):
    auth_error = require_worker_login()
    if auth_error:
        return auth_error
    action = ((request.get_json(force=True) or {}).get("action") or "").lower()
    if action not in ("start", "stop"):
        return jsonify({"error": "action must be start or stop"}), 400
    conn = get_db()
    if not is_postgres():
        conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(for_update("SELECT * FROM bookings WHERE id = ? AND worker_id = ?"), (booking_id, current_worker_id())).fetchone()
    if not booking:
        conn.close(); return jsonify({"error": "Booking not found"}), 404
    if booking["status"] in ("completed", "cancelled", "rejected"):
        conn.close(); return jsonify({"error": "This booking can no longer start or stop work"}), 409
    if not booking_allows_work(booking):
        conn.close(); return jsonify({"error": "Payment is not ready for work to start"}), 409
    if booking["booking_type"] == "diagnosis" and not booking["work_approved_at"]:
        conn.close(); return jsonify({"error": "Hirer must approve diagnosed work before timer starts"}), 409
    now = datetime.utcnow()
    if action == "start":
        if booking["status"] != "checked_in":
            conn.close(); return jsonify({"error": "Worker must check in at the site before starting the work timer"}), 409
        if booking["work_started_at"]:
            conn.close(); return jsonify({"error": "Work timer already started"}), 409
        conn.execute("UPDATE bookings SET work_started_at = ?, status = 'in_progress' WHERE id = ?", (now.isoformat(), booking_id))
        conn.execute(
            "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'in_progress', 'Paid work timer started by worker')",
            (booking_id,),
        )
        add_in_app_notification(
            conn, "hirer", booking["hirer_id"], "work_started", "Work started",
            f"Worker started the paid work timer for booking #{booking_id}.", booking_id
        )
        conn.commit(); conn.close()
        return jsonify({"status": "in_progress", "work_started_at": now.isoformat()})
    if booking["status"] != "in_progress" or not booking["work_started_at"]:
        conn.close(); return jsonify({"error": "Work timer is not currently running"}), 409
    if booking["work_ended_at"]:
        conn.close(); return jsonify({"error": "Work timer is already completed"}), 409
    started = datetime.fromisoformat(booking["work_started_at"])
    minutes = max(1, int((now - started).total_seconds() // 60))
    worker = conn.execute("SELECT daily_wage FROM workers WHERE id = ?", (booking["worker_id"],)).fetchone()
    hourly = derived_hourly_rate(worker)
    work_amount = round(hourly * minutes / 60)
    total = int(booking["diagnosis_fee"] or 0) + int(work_amount)
    paid_amount = int(booking["paid_amount"] or 0)
    payment_status = booking["payment_status"]
    if booking["payment_method"] == "online" and payment_status == "paid":
        if paid_amount < total:
            payment_status = "balance_due"
        elif paid_amount > total:
            payment_status = "refund_pending"
    conn.execute("""UPDATE bookings SET work_ended_at = ?, actual_minutes = ?, work_amount = ?, total_amount = ?, status = 'completed', payment_status = ? WHERE id = ?""",
                 (now.isoformat(), minutes, work_amount, total, payment_status, booking_id))
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'completed', ?)",
        (booking_id, f"Paid work timer stopped after {minutes} minute(s); final work amount ₹{work_amount}."),
    )
    add_in_app_notification(
        conn, "hirer", booking["hirer_id"], "work_completed", "Work completed",
        f"Booking #{booking_id} completed. Final amount is ₹{total}.", booking_id
    )
    finance = sync_booking_financials(conn, booking_id)
    conn.commit(); conn.close()
    return jsonify({"status": "completed", "actual_minutes": minutes, "derived_hourly_rate": hourly, "work_amount": work_amount, "diagnosis_fee": booking["diagnosis_fee"], "total_amount": total, "payment_status": payment_status, "finance": finance})



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
    service_latitude = data.get("service_latitude")
    service_longitude = data.get("service_longitude")

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
    worker = conn.execute("SELECT * FROM workers WHERE id = ? AND account_status = 'active' AND deleted_at IS NULL AND rate_status = 'approved'", (worker_id,)).fetchone()
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
    diagnosis_pricing_rule_id = None
    if booking_type == "diagnosis":
        quote, quote_error = diagnosis_quote_for_worker(conn, worker, service_latitude, service_longitude)
        if quote_error:
            conn.rollback(); conn.close()
            return jsonify({"error": quote_error}), 409
        diagnosis_distance_km = quote["distance_km"]
        diagnosis_fee = quote["fee"]
        diagnosis_pricing_rule_id = quote["rule_id"]
        service_latitude = float(service_latitude)
        service_longitude = float(service_longitude)
        total = diagnosis_fee
    else:
        try:
            service_latitude, service_longitude = parse_optional_coordinates(service_latitude, service_longitude)
        except ValueError as exc:
            conn.rollback(); conn.close()
            return jsonify({"error": str(exc)}), 400
        total = round(rate * hours)

    cur = conn.execute(
        """INSERT INTO bookings
           (hirer_id, worker_id, start_date, start_time, end_time, days, hours, service_type, booking_type,
            diagnosis_fee, diagnosis_distance_km, diagnosis_pricing_rule_id, service_latitude, service_longitude,
            special_instructions, address, payment_method, total_amount, status, payment_status)
           VALUES (?, ?, ?, ?, ?, 1, ?, 'regular', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', 'pending')""",
        (current_hirer_id(), worker_id, start_date, start_time, end_time, hours, booking_type,
         diagnosis_fee, diagnosis_distance_km, diagnosis_pricing_rule_id, service_latitude, service_longitude,
         special_instructions, address, payment_method, total),
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
        "diagnosis_distance_km": diagnosis_distance_km,
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
    if booking["payment_status"] in ("paid", "refund_pending"):
        conn.close()
        return jsonify({"error": "This booking is already paid or awaiting refund"}), 400

    if booking["status"] == "confirmed":
        if booking["payment_method"] != "online":
            conn.close()
            return jsonify({"error": "Cash-selected bookings can switch to online only after the work is completed"}), 400
        amount_due = int(booking["total_amount"] or 0)
    elif booking["status"] == "completed":
        if booking["payment_status"] == "balance_due":
            conn.close()
            return jsonify({"error": "Use the remaining-balance payment flow for this booking"}), 400
        if int(booking["paid_amount"] or 0) > 0:
            conn.close()
            return jsonify({"error": "A payment has already been recorded for this booking"}), 400

        # If a previous checkout order exists, first ask Razorpay whether it was already captured.
        if booking["razorpay_order_id"]:
            reconciled = reconcile_online_booking_payment(conn, booking, notify_users=True)
            if reconciled.get("error"):
                conn.rollback()
                conn.close()
                return jsonify({"error": reconciled["error"]}), 502
            conn.commit()
            booking = conn.execute(
                "SELECT * FROM bookings WHERE id=? AND hirer_id=?",
                (booking_id, current_hirer_id()),
            ).fetchone()
            if booking["payment_status"] == "paid" or int(booking["paid_amount"] or 0) > 0:
                conn.close()
                return jsonify({
                    "error": "Payment is already confirmed",
                    "payment_status": booking["payment_status"],
                    "receipt_url": f"/api/bookings/{booking_id}/receipt.pdf",
                }), 409
        amount_due = int(booking["total_amount"] or 0)
    else:
        conn.close()
        return jsonify({"error": "Online payment is available after worker acceptance or after work completion"}), 400

    if amount_due <= 0:
        conn.close()
        return jsonify({"error": "There is no amount due for this booking"}), 400

    try:
        order = payments.create_order(
            amount_rupees=amount_due,
            receipt=f"booking_{booking_id}_{secrets.token_hex(4)}",
            notes={
                "booking_id": str(booking_id),
                "hirer_id": str(current_hirer_id()),
                "payment_stage": "post_work" if booking["status"] == "completed" else "pre_work",
            },
        )
    except payments.RazorpayConfigError as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    except payments.RazorpayAPIError as e:
        conn.close()
        return jsonify({"error": str(e)}), 502

    if booking["razorpay_order_id"]:
        payment_accounting.register_order(conn, booking_id, booking["razorpay_order_id"])
    payment_accounting.register_order(conn, booking_id, order["id"])
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


RECEIPT_I18N = {
    "en": {"payment_receipt":"PAYMENT RECEIPT","worker_receipt":"WORKER EARNINGS RECEIPT","receipt_summary":"Receipt Summary","booking_service":"Booking & Service Details","hirer_worker":"Hirer & Worker","payment_details":"Payment Details","worker_earnings":"Worker Earnings","receipt_no":"Receipt No.","booking_id":"Booking ID","generated":"Generated","booking_status":"Booking Status","payment_status":"Payment Status","service_type":"Service Type","service_skill":"Service / Skill","service_date":"Service Date","scheduled_time":"Scheduled Time","actual_work":"Actual Work Time","work_address":"Work Address","instructions":"Instructions","hirer":"Hirer","hirer_contact":"Hirer Contact","worker":"Worker","worker_contact":"Worker Contact","worker_city":"Worker City","payment_method":"Payment Method","final_amount":"Final Booking Amount","received":"Amount Received","diagnosis_fee":"Diagnosis Fee","work_amount":"Work Amount","payment_id":"Payment ID","order_id":"Order ID","cash_verified":"Cash OTP Verified","gross":"Gross Booking Amount","commission":"Platform Commission","worker_net":"Worker Net Earning","settlement":"Settlement Status","settlement_ref":"Settlement Reference","support":"HIRE NOW - CUSTOMER SUPPORT","system_note":"This is a system-generated receipt. No signature is required."},
    "hi": {"payment_receipt":"भुगतान रसीद","worker_receipt":"कामगार कमाई रसीद","receipt_summary":"रसीद सारांश","booking_service":"बुकिंग और सेवा विवरण","hirer_worker":"हायरर और कामगार","payment_details":"भुगतान विवरण","worker_earnings":"कामगार कमाई","receipt_no":"रसीद नंबर","booking_id":"बुकिंग ID","generated":"तैयार किया गया","booking_status":"बुकिंग स्थिति","payment_status":"भुगतान स्थिति","service_type":"सेवा प्रकार","service_skill":"सेवा / कौशल","service_date":"सेवा तिथि","scheduled_time":"निर्धारित समय","actual_work":"वास्तविक कार्य समय","work_address":"कार्य पता","instructions":"निर्देश","hirer":"हायरर","hirer_contact":"हायरर संपर्क","worker":"कामगार","worker_contact":"कामगार संपर्क","worker_city":"कामगार शहर","payment_method":"भुगतान तरीका","final_amount":"अंतिम बुकिंग राशि","received":"प्राप्त राशि","diagnosis_fee":"जांच शुल्क","work_amount":"कार्य राशि","payment_id":"भुगतान ID","order_id":"ऑर्डर ID","cash_verified":"कैश OTP सत्यापित","gross":"कुल बुकिंग राशि","commission":"प्लेटफॉर्म शुल्क","worker_net":"कामगार की शुद्ध कमाई","settlement":"सेटलमेंट स्थिति","settlement_ref":"सेटलमेंट संदर्भ","support":"HIRE NOW - ग्राहक सहायता","system_note":"यह सिस्टम द्वारा बनाई गई रसीद है। हस्ताक्षर आवश्यक नहीं है।"},
    "ar": {"payment_receipt":"إيصال الدفع","worker_receipt":"إيصال أرباح العامل","receipt_summary":"ملخص الإيصال","booking_service":"تفاصيل الحجز والخدمة","hirer_worker":"العميل والعامل","payment_details":"تفاصيل الدفع","worker_earnings":"أرباح العامل","receipt_no":"رقم الإيصال","booking_id":"رقم الحجز","generated":"تاريخ الإنشاء","booking_status":"حالة الحجز","payment_status":"حالة الدفع","service_type":"نوع الخدمة","service_skill":"الخدمة / المهارة","service_date":"تاريخ الخدمة","scheduled_time":"الوقت المحدد","actual_work":"وقت العمل الفعلي","work_address":"عنوان العمل","instructions":"التعليمات","hirer":"العميل","hirer_contact":"اتصال العميل","worker":"العامل","worker_contact":"اتصال العامل","worker_city":"مدينة العامل","payment_method":"طريقة الدفع","final_amount":"المبلغ النهائي","received":"المبلغ المستلم","diagnosis_fee":"رسوم الفحص","work_amount":"مبلغ العمل","payment_id":"رقم الدفع","order_id":"رقم الطلب","cash_verified":"تم تأكيد OTP النقدي","gross":"إجمالي مبلغ الحجز","commission":"عمولة المنصة","worker_net":"صافي أرباح العامل","settlement":"حالة التسوية","settlement_ref":"مرجع التسوية","support":"HIRE NOW - دعم العملاء","system_note":"هذا إيصال مُنشأ آليًا ولا يحتاج إلى توقيع."},
    "ur": {"payment_receipt":"ادائیگی کی رسید","worker_receipt":"ورکر کمائی کی رسید","receipt_summary":"رسید کا خلاصہ","booking_service":"بکنگ اور سروس کی تفصیل","hirer_worker":"ہائرر اور ورکر","payment_details":"ادائیگی کی تفصیل","worker_earnings":"ورکر کی کمائی","receipt_no":"رسید نمبر","booking_id":"بکنگ ID","generated":"تیار ہونے کا وقت","booking_status":"بکنگ کی حالت","payment_status":"ادائیگی کی حالت","service_type":"سروس کی قسم","service_skill":"سروس / مہارت","service_date":"سروس کی تاریخ","scheduled_time":"مقررہ وقت","actual_work":"اصل کام کا وقت","work_address":"کام کا پتہ","instructions":"ہدایات","hirer":"ہائرر","hirer_contact":"ہائرر رابطہ","worker":"ورکر","worker_contact":"ورکر رابطہ","worker_city":"ورکر شہر","payment_method":"ادائیگی کا طریقہ","final_amount":"آخری بکنگ رقم","received":"موصول رقم","diagnosis_fee":"تشخیصی فیس","work_amount":"کام کی رقم","payment_id":"ادائیگی ID","order_id":"آرڈر ID","cash_verified":"کیش OTP تصدیق","gross":"کل بکنگ رقم","commission":"پلیٹ فارم فیس","worker_net":"ورکر خالص کمائی","settlement":"سیٹلمنٹ حالت","settlement_ref":"سیٹلمنٹ حوالہ","support":"HIRE NOW - کسٹمر سپورٹ","system_note":"یہ سسٹم سے تیار شدہ رسید ہے، دستخط کی ضرورت نہیں۔"},
    "bn": {"payment_receipt":"পেমেন্ট রসিদ","worker_receipt":"কর্মীর আয়ের রসিদ","receipt_summary":"রসিদের সারাংশ","booking_service":"বুকিং ও সেবার বিবরণ","hirer_worker":"হায়ারার ও কর্মী","payment_details":"পেমেন্ট বিবরণ","worker_earnings":"কর্মীর আয়","receipt_no":"রসিদ নম্বর","booking_id":"বুকিং ID","generated":"তৈরির সময়","booking_status":"বুকিং অবস্থা","payment_status":"পেমেন্ট অবস্থা","service_type":"সেবার ধরন","service_skill":"সেবা / দক্ষতা","service_date":"সেবার তারিখ","scheduled_time":"নির্ধারিত সময়","actual_work":"বাস্তব কাজের সময়","work_address":"কাজের ঠিকানা","instructions":"নির্দেশনা","hirer":"হায়ারার","hirer_contact":"হায়ারার যোগাযোগ","worker":"কর্মী","worker_contact":"কর্মী যোগাযোগ","worker_city":"কর্মীর শহর","payment_method":"পেমেন্ট পদ্ধতি","final_amount":"চূড়ান্ত বুকিং পরিমাণ","received":"প্রাপ্ত পরিমাণ","diagnosis_fee":"পরিদর্শন ফি","work_amount":"কাজের পরিমাণ","payment_id":"পেমেন্ট ID","order_id":"অর্ডার ID","cash_verified":"ক্যাশ OTP যাচাই","gross":"মোট বুকিং পরিমাণ","commission":"প্ল্যাটফর্ম ফি","worker_net":"কর্মীর নিট আয়","settlement":"সেটেলমেন্ট অবস্থা","settlement_ref":"সেটেলমেন্ট রেফারেন্স","support":"HIRE NOW - কাস্টমার সাপোর্ট","system_note":"এটি সিস্টেম-জেনারেটেড রসিদ। স্বাক্ষর প্রয়োজন নেই।"},
    "ta": {"payment_receipt":"பணம் செலுத்திய ரசீது","worker_receipt":"பணியாளர் வருமான ரசீது","receipt_summary":"ரசீது சுருக்கம்","booking_service":"முன்பதிவு மற்றும் சேவை விவரங்கள்","hirer_worker":"வாடிக்கையாளர் மற்றும் பணியாளர்","payment_details":"பணம் செலுத்திய விவரங்கள்","worker_earnings":"பணியாளர் வருமானம்","receipt_no":"ரசீது எண்","booking_id":"முன்பதிவு ID","generated":"உருவாக்கிய நேரம்","booking_status":"முன்பதிவு நிலை","payment_status":"பணம் நிலை","service_type":"சேவை வகை","service_skill":"சேவை / திறன்","service_date":"சேவை தேதி","scheduled_time":"திட்டமிட்ட நேரம்","actual_work":"உண்மையான வேலை நேரம்","work_address":"வேலை முகவரி","instructions":"வழிமுறைகள்","hirer":"வாடிக்கையாளர்","hirer_contact":"வாடிக்கையாளர் தொடர்பு","worker":"பணியாளர்","worker_contact":"பணியாளர் தொடர்பு","worker_city":"பணியாளர் நகரம்","payment_method":"பணம் செலுத்தும் முறை","final_amount":"இறுதி முன்பதிவு தொகை","received":"பெற்ற தொகை","diagnosis_fee":"ஆய்வு கட்டணம்","work_amount":"வேலை தொகை","payment_id":"பணம் ID","order_id":"ஆர்டர் ID","cash_verified":"பண OTP சரிபார்ப்பு","gross":"மொத்த முன்பதிவு தொகை","commission":"பிளாட்ஃபார்ம் கட்டணம்","worker_net":"பணியாளர் நிகர வருமானம்","settlement":"செட்டில்மெண்ட் நிலை","settlement_ref":"செட்டில்மெண்ட் குறிப்பு","support":"HIRE NOW - வாடிக்கையாளர் உதவி","system_note":"இது மின்னணு முறையில் உருவாக்கப்பட்ட ரசீது. கையொப்பம் தேவையில்லை."},
    "te": {"payment_receipt":"చెల్లింపు రసీదు","worker_receipt":"కార్మికుడి ఆదాయ రసీదు","receipt_summary":"రసీదు సారాంశం","booking_service":"బుకింగ్ మరియు సేవ వివరాలు","hirer_worker":"హైరర్ మరియు కార్మికుడు","payment_details":"చెల్లింపు వివరాలు","worker_earnings":"కార్మికుడి ఆదాయం","receipt_no":"రసీదు నంబర్","booking_id":"బుకింగ్ ID","generated":"తయారు చేసిన సమయం","booking_status":"బుకింగ్ స్థితి","payment_status":"చెల్లింపు స్థితి","service_type":"సేవ రకం","service_skill":"సేవ / నైపుణ్యం","service_date":"సేవ తేదీ","scheduled_time":"నిర్దేశిత సమయం","actual_work":"అసలు పని సమయం","work_address":"పని చిరునామా","instructions":"సూచనలు","hirer":"హైరర్","hirer_contact":"హైరర్ సంప్రదింపు","worker":"కార్మికుడు","worker_contact":"కార్మికుడి సంప్రదింపు","worker_city":"కార్మికుడి నగరం","payment_method":"చెల్లింపు విధానం","final_amount":"తుది బుకింగ్ మొత్తం","received":"అందుకున్న మొత్తం","diagnosis_fee":"తనిఖీ ఫీజు","work_amount":"పని మొత్తం","payment_id":"చెల్లింపు ID","order_id":"ఆర్డర్ ID","cash_verified":"క్యాష్ OTP ధృవీకరణ","gross":"మొత్తం బుకింగ్ మొత్తం","commission":"ప్లాట్‌ఫారమ్ ఫీజు","worker_net":"కార్మికుడి నికర ఆదాయం","settlement":"సెటిల్‌మెంట్ స్థితి","settlement_ref":"సెటిల్‌మెంట్ రిఫరెన్స్","support":"HIRE NOW - కస్టమర్ సపోర్ట్","system_note":"ఇది సిస్టమ్ సృష్టించిన రసీదు. సంతకం అవసరం లేదు."},
    "mr": {"payment_receipt":"पेमेंट पावती","worker_receipt":"कामगार कमाई पावती","receipt_summary":"पावती सारांश","booking_service":"बुकिंग आणि सेवा तपशील","hirer_worker":"हायरर आणि कामगार","payment_details":"पेमेंट तपशील","worker_earnings":"कामगार कमाई","receipt_no":"पावती क्रमांक","booking_id":"बुकिंग ID","generated":"तयार केल्याची वेळ","booking_status":"बुकिंग स्थिती","payment_status":"पेमेंट स्थिती","service_type":"सेवा प्रकार","service_skill":"सेवा / कौशल्य","service_date":"सेवा दिनांक","scheduled_time":"नियोजित वेळ","actual_work":"प्रत्यक्ष कामाचा वेळ","work_address":"कामाचा पत्ता","instructions":"सूचना","hirer":"हायरर","hirer_contact":"हायरर संपर्क","worker":"कामगार","worker_contact":"कामगार संपर्क","worker_city":"कामगार शहर","payment_method":"पेमेंट पद्धत","final_amount":"अंतिम बुकिंग रक्कम","received":"प्राप्त रक्कम","diagnosis_fee":"तपासणी शुल्क","work_amount":"कामाची रक्कम","payment_id":"पेमेंट ID","order_id":"ऑर्डर ID","cash_verified":"कॅश OTP सत्यापित","gross":"एकूण बुकिंग रक्कम","commission":"प्लॅटफॉर्म शुल्क","worker_net":"कामगार निव्वळ कमाई","settlement":"सेटलमेंट स्थिती","settlement_ref":"सेटलमेंट संदर्भ","support":"HIRE NOW - ग्राहक सहाय्य","system_note":"ही प्रणालीद्वारे तयार केलेली पावती आहे. स्वाक्षरी आवश्यक नाही."},
    "gu": {"payment_receipt":"ચુકવણી રસીદ","worker_receipt":"કામદાર કમાણી રસીદ","receipt_summary":"રસીદ સારાંશ","booking_service":"બુકિંગ અને સેવા વિગતો","hirer_worker":"હાયરર અને કામદાર","payment_details":"ચુકવણી વિગતો","worker_earnings":"કામદાર કમાણી","receipt_no":"રસીદ નંબર","booking_id":"બુકિંગ ID","generated":"બનાવ્યાનો સમય","booking_status":"બુકિંગ સ્થિતિ","payment_status":"ચુકવણી સ્થિતિ","service_type":"સેવા પ્રકાર","service_skill":"સેવા / કૌશલ્ય","service_date":"સેવાની તારીખ","scheduled_time":"નક્કી સમય","actual_work":"વાસ્તવિક કામનો સમય","work_address":"કામનું સરનામું","instructions":"સૂચનાઓ","hirer":"હાયરર","hirer_contact":"હાયરર સંપર્ક","worker":"કામદાર","worker_contact":"કામદાર સંપર્ક","worker_city":"કામદાર શહેર","payment_method":"ચુકવણી રીત","final_amount":"અંતિમ બુકિંગ રકમ","received":"મળેલ રકમ","diagnosis_fee":"તપાસ ફી","work_amount":"કામની રકમ","payment_id":"ચુકવણી ID","order_id":"ઓર્ડર ID","cash_verified":"કેશ OTP ચકાસણી","gross":"કુલ બુકિંગ રકમ","commission":"પ્લેટફોર્મ ફી","worker_net":"કામદાર નેટ કમાણી","settlement":"સેટલમેન્ટ સ્થિતિ","settlement_ref":"સેટલમેન્ટ સંદર્ભ","support":"HIRE NOW - ગ્રાહક સહાય","system_note":"આ સિસ્ટમ દ્વારા બનાવેલી રસીદ છે. સહી જરૂરી નથી."},
    "kn": {"payment_receipt":"ಪಾವತಿ ರಸೀದಿ","worker_receipt":"ಕಾರ್ಮಿಕ ಆದಾಯ ರಸೀದಿ","receipt_summary":"ರಸೀದಿ ಸಾರಾಂಶ","booking_service":"ಬುಕಿಂಗ್ ಮತ್ತು ಸೇವಾ ವಿವರಗಳು","hirer_worker":"ಹೈರರ್ ಮತ್ತು ಕಾರ್ಮಿಕ","payment_details":"ಪಾವತಿ ವಿವರಗಳು","worker_earnings":"ಕಾರ್ಮಿಕ ಆದಾಯ","receipt_no":"ರಸೀದಿ ಸಂಖ್ಯೆ","booking_id":"ಬುಕಿಂಗ್ ID","generated":"ರಚಿಸಿದ ಸಮಯ","booking_status":"ಬುಕಿಂಗ್ ಸ್ಥಿತಿ","payment_status":"ಪಾವತಿ ಸ್ಥಿತಿ","service_type":"ಸೇವೆಯ ಪ್ರಕಾರ","service_skill":"ಸೇವೆ / ಕೌಶಲ್ಯ","service_date":"ಸೇವೆಯ ದಿನಾಂಕ","scheduled_time":"ನಿಗದಿತ ಸಮಯ","actual_work":"ನಿಜವಾದ ಕೆಲಸದ ಸಮಯ","work_address":"ಕೆಲಸದ ವಿಳಾಸ","instructions":"ಸೂಚನೆಗಳು","hirer":"ಹೈರರ್","hirer_contact":"ಹೈರರ್ ಸಂಪರ್ಕ","worker":"ಕಾರ್ಮಿಕ","worker_contact":"ಕಾರ್ಮಿಕ ಸಂಪರ್ಕ","worker_city":"ಕಾರ್ಮಿಕ ನಗರ","payment_method":"ಪಾವತಿ ವಿಧಾನ","final_amount":"ಅಂತಿಮ ಬುಕಿಂಗ್ ಮೊತ್ತ","received":"ಸ್ವೀಕರಿಸಿದ ಮೊತ್ತ","diagnosis_fee":"ಪರಿಶೀಲನಾ ಶುಲ್ಕ","work_amount":"ಕೆಲಸದ ಮೊತ್ತ","payment_id":"ಪಾವತಿ ID","order_id":"ಆರ್ಡರ್ ID","cash_verified":"ಕ್ಯಾಶ್ OTP ಪರಿಶೀಲನೆ","gross":"ಒಟ್ಟು ಬುಕಿಂಗ್ ಮೊತ್ತ","commission":"ಪ್ಲಾಟ್‌ಫಾರ್ಮ್ ಶುಲ್ಕ","worker_net":"ಕಾರ್ಮಿಕ ಶುದ್ಧ ಆದಾಯ","settlement":"ಸೆಟಲ್‌ಮೆಂಟ್ ಸ್ಥಿತಿ","settlement_ref":"ಸೆಟಲ್‌ಮೆಂಟ್ ಉಲ್ಲೇಖ","support":"HIRE NOW - ಗ್ರಾಹಕ ಸಹಾಯ","system_note":"ಇದು ಸಿಸ್ಟಮ್ ರಚಿಸಿದ ರಸೀದಿ. ಸಹಿ ಅಗತ್ಯವಿಲ್ಲ."},
    "ml": {"payment_receipt":"പേയ്മെന്റ് രസീത്","worker_receipt":"തൊഴിലാളി വരുമാന രസീത്","receipt_summary":"രസീത് സംഗ്രഹം","booking_service":"ബുക്കിംഗ് & സേവന വിശദാംശങ്ങൾ","hirer_worker":"ഹയററും തൊഴിലാളിയും","payment_details":"പേയ്മെന്റ് വിശദാംശങ്ങൾ","worker_earnings":"തൊഴിലാളി വരുമാനം","receipt_no":"രസീത് നമ്പർ","booking_id":"ബുക്കിംഗ് ID","generated":"സൃഷ്ടിച്ച സമയം","booking_status":"ബുക്കിംഗ് നില","payment_status":"പേയ്മെന്റ് നില","service_type":"സേവന തരം","service_skill":"സേവനം / കഴിവ്","service_date":"സേവന തീയതി","scheduled_time":"നിശ്ചിത സമയം","actual_work":"യഥാർത്ഥ ജോലി സമയം","work_address":"ജോലി വിലാസം","instructions":"നിർദ്ദേശങ്ങൾ","hirer":"ഹയർ","hirer_contact":"ഹയർ ബന്ധപ്പെടുക","worker":"തൊഴിലാളി","worker_contact":"തൊഴിലാളി ബന്ധപ്പെടുക","worker_city":"തൊഴിലാളിയുടെ നഗരം","payment_method":"പേയ്മെന്റ് രീതി","final_amount":"അവസാന ബുക്കിംഗ് തുക","received":"ലഭിച്ച തുക","diagnosis_fee":"പരിശോധന ഫീസ്","work_amount":"ജോലി തുക","payment_id":"പേയ്മെന്റ് ID","order_id":"ഓർഡർ ID","cash_verified":"ക്യാഷ് OTP സ്ഥിരീകരണം","gross":"ആകെ ബുക്കിംഗ് തുക","commission":"പ്ലാറ്റ്ഫോം ഫീസ്","worker_net":"തൊഴിലാളി ശുദ്ധ വരുമാനം","settlement":"സെറ്റിൽമെന്റ് നില","settlement_ref":"സെറ്റിൽമെന്റ് റഫറൻസ്","support":"HIRE NOW - കസ്റ്റമർ സപ്പോർട്ട്","system_note":"ഇത് സിസ്റ്റം സൃഷ്ടിച്ച രസീത് ആണ്. ഒപ്പ് ആവശ്യമില്ല."},
    "pa": {"payment_receipt":"ਭੁਗਤਾਨ ਰਸੀਦ","worker_receipt":"ਕਾਮੇ ਦੀ ਕਮਾਈ ਰਸੀਦ","receipt_summary":"ਰਸੀਦ ਸੰਖੇਪ","booking_service":"ਬੁਕਿੰਗ ਅਤੇ ਸੇਵਾ ਵੇਰਵੇ","hirer_worker":"ਹਾਇਰਰ ਅਤੇ ਕਾਮਾ","payment_details":"ਭੁਗਤਾਨ ਵੇਰਵੇ","worker_earnings":"ਕਾਮੇ ਦੀ ਕਮਾਈ","receipt_no":"ਰਸੀਦ ਨੰਬਰ","booking_id":"ਬੁਕਿੰਗ ID","generated":"ਤਿਆਰ ਸਮਾਂ","booking_status":"ਬੁਕਿੰਗ ਸਥਿਤੀ","payment_status":"ਭੁਗਤਾਨ ਸਥਿਤੀ","service_type":"ਸੇਵਾ ਕਿਸਮ","service_skill":"ਸੇਵਾ / ਹੁਨਰ","service_date":"ਸੇਵਾ ਮਿਤੀ","scheduled_time":"ਨਿਰਧਾਰਤ ਸਮਾਂ","actual_work":"ਅਸਲ ਕੰਮ ਸਮਾਂ","work_address":"ਕੰਮ ਦਾ ਪਤਾ","instructions":"ਹਦਾਇਤਾਂ","hirer":"ਹਾਇਰਰ","hirer_contact":"ਹਾਇਰਰ ਸੰਪਰਕ","worker":"ਕਾਮਾ","worker_contact":"ਕਾਮੇ ਦਾ ਸੰਪਰਕ","worker_city":"ਕਾਮੇ ਦਾ ਸ਼ਹਿਰ","payment_method":"ਭੁਗਤਾਨ ਤਰੀਕਾ","final_amount":"ਅੰਤਿਮ ਬੁਕਿੰਗ ਰਕਮ","received":"ਪ੍ਰਾਪਤ ਰਕਮ","diagnosis_fee":"ਜਾਂਚ ਫੀਸ","work_amount":"ਕੰਮ ਰਕਮ","payment_id":"ਭੁਗਤਾਨ ID","order_id":"ਆਰਡਰ ID","cash_verified":"ਕੈਸ਼ OTP ਪੁਸ਼ਟੀ","gross":"ਕੁੱਲ ਬੁਕਿੰਗ ਰਕਮ","commission":"ਪਲੇਟਫਾਰਮ ਫੀਸ","worker_net":"ਕਾਮੇ ਦੀ ਨੈੱਟ ਕਮਾਈ","settlement":"ਸੈਟਲਮੈਂਟ ਸਥਿਤੀ","settlement_ref":"ਸੈਟਲਮੈਂਟ ਹਵਾਲਾ","support":"HIRE NOW - ਗਾਹਕ ਸਹਾਇਤਾ","system_note":"ਇਹ ਸਿਸਟਮ ਵੱਲੋਂ ਬਣਾਈ ਰਸੀਦ ਹੈ। ਦਸਤਖਤ ਦੀ ਲੋੜ ਨਹੀਂ।"}
}


RECEIPT_FONT_CONFIG = {
    "hi": ("HN-Devanagari", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf"),
    "mr": ("HN-Devanagari", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf"),
    "bn": ("HN-Bengali", "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansbengali/NotoSansBengali%5Bwdth%2Cwght%5D.ttf"),
    "ta": ("HN-Tamil", "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanstamil/NotoSansTamil%5Bwdth%2Cwght%5D.ttf"),
    "te": ("HN-Telugu", "/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanstelugu/NotoSansTelugu%5Bwdth%2Cwght%5D.ttf"),
    "gu": ("HN-Gujarati", "/usr/share/fonts/truetype/noto/NotoSansGujarati-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansgujarati/NotoSansGujarati%5Bwdth%2Cwght%5D.ttf"),
    "kn": ("HN-Kannada", "/usr/share/fonts/truetype/noto/NotoSansKannada-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanskannada/NotoSansKannada%5Bwdth%2Cwght%5D.ttf"),
    "ml": ("HN-Malayalam", "/usr/share/fonts/truetype/noto/NotoSansMalayalam-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansmalayalam/NotoSansMalayalam%5Bwdth%2Cwght%5D.ttf"),
    "pa": ("HN-Gurmukhi", "/usr/share/fonts/truetype/noto/NotoSansGurmukhi-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansgurmukhi/NotoSansGurmukhi%5Bwdth%2Cwght%5D.ttf"),
    "ar": ("HN-Arabic", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansarabic/NotoSansArabic%5Bwdth%2Cwght%5D.ttf"),
    "ur": ("HN-Arabic", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansarabic/NotoSansArabic%5Bwdth%2Cwght%5D.ttf"),
}


def receipt_text(lang, key):
    lang = normalize_language(lang)
    return RECEIPT_I18N.get(lang, RECEIPT_I18N["en"]).get(key, RECEIPT_I18N["en"].get(key, key))


def receipt_font_for_language(lang):
    lang = normalize_language(lang)
    if lang == "en":
        return "Helvetica"
    config = RECEIPT_FONT_CONFIG.get(lang)
    if not config:
        return "Helvetica"
    font_name, system_path, url = config
    try:
        pdfmetrics.getFont(font_name)
        return font_name
    except Exception:
        pass
    path = system_path
    if not os.path.exists(path):
        cache_dir = os.path.join("/tmp", "hirenow_receipt_fonts")
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, font_name + ".ttf")
        if not os.path.exists(path):
            try:
                resp = _requests.get(url, timeout=15)
                resp.raise_for_status()
                with open(path, "wb") as font_file:
                    font_file.write(resp.content)
            except Exception as exc:
                app.logger.warning("Receipt font download failed for %s: %s", lang, exc)
                return "Helvetica"
    try:
        pdfmetrics.registerFont(TTFont(font_name, path, shapable=True))
        return font_name
    except Exception as exc:
        app.logger.warning("Receipt font registration failed for %s: %s", lang, exc)
        return "Helvetica"


def receipt_visual_text(text, lang):
    text = "-" if text is None or text == "" else str(text)
    if normalize_language(lang) in ("ar", "ur"):
        try:
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
    return text


def build_payment_receipt_pdf(booking, hirer, worker, finance, audience):
    """Generate a professional branded Hire Now PDF receipt in the user's selected language."""
    lang = normalize_language((hirer["preferred_language"] if audience == "hirer" and hirer and "preferred_language" in hirer.keys() else None) or (worker["preferred_language"] if audience == "worker" and worker and "preferred_language" in worker.keys() else None) or "en")
    label = lambda key: receipt_text(lang, key)
    content_font = receipt_font_for_language(lang)
    is_rtl = lang in ("ar", "ur")
    shaping = content_font != "Helvetica" and not is_rtl
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    NAVY = colors.HexColor("#0B1E3F")
    ORANGE = colors.HexColor("#FF9F1C")
    SOFT = colors.HexColor("#F5F7FB")
    TEXT = colors.HexColor("#172033")
    MUTED = colors.HexColor("#6B7280")
    BORDER = colors.HexColor("#D9E1EC")
    LIGHT_WATERMARK = colors.HexColor("#EEF2F7")

    center_x = width / 2
    content_w = 166 * mm
    left = (width - content_w) / 2
    right = left + content_w
    y = height - 14 * mm

    pdf.setTitle(f"Hire Now Receipt #{booking['id']}")
    pdf.setAuthor("Hire Now")

    # Centered watermark.
    pdf.saveState()
    try:
        pdf.setFillAlpha(0.45)
    except Exception:
        pass
    pdf.setFillColor(LIGHT_WATERMARK)
    pdf.setFont("Helvetica-Bold", 48)
    pdf.translate(center_x, height / 2)
    pdf.rotate(32)
    pdf.drawCentredString(0, 0, "HIRE NOW")
    pdf.restoreState()

    # Header card.
    pdf.setFillColor(NAVY)
    pdf.roundRect(left, height - 66 * mm, content_w, 52 * mm, 5 * mm, fill=1, stroke=0)

    logo_path = os.path.join(os.path.dirname(__file__), "static", "brand", "hirenow-logo.webp")
    logo_drawn = False
    try:
        logo = ImageReader(logo_path)
        iw, ih = logo.getSize()
        logo_w = 39 * mm
        logo_h = logo_w * ih / iw
        max_h = 25 * mm
        if logo_h > max_h:
            logo_h = max_h
            logo_w = logo_h * iw / ih
        pdf.drawImage(
            logo,
            center_x - logo_w / 2,
            height - 42 * mm,
            width=logo_w,
            height=logo_h,
            preserveAspectRatio=True,
            mask="auto",
        )
        logo_drawn = True
    except Exception:
        app.logger.exception("Receipt logo could not be rendered")

    if not logo_drawn:
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 22)
        pdf.drawCentredString(center_x, height - 30 * mm, "Hire")
        pdf.setFillColor(ORANGE)
        pdf.drawCentredString(center_x + 18 * mm, height - 30 * mm, "Now")

    pdf.setFillColor(ORANGE)
    pdf.setFont(content_font if content_font != "Helvetica" else "Helvetica-Bold", 13)
    pdf.drawCentredString(
        center_x,
        height - 52 * mm,
        receipt_visual_text(label("payment_receipt") if audience == "hirer" else label("worker_receipt"), lang),
        direction="RTL" if is_rtl else "LTR",
        shaping=shaping,
    )
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica", 8.5)
    pdf.drawCentredString(center_x, height - 58 * mm, "Skilled People. Real Work. Faster.")

    y = height - 76 * mm

    def fit_centered_text(text, font=None, size=9, max_width=None, leading=4.4 * mm, color=TEXT):
        nonlocal y
        font = font or content_font
        max_width = max_width or (content_w - 14 * mm)
        raw = "-" if text is None or text == "" else str(text)
        words = raw.split()
        lines = []
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if stringWidth(candidate, font, size) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        if not lines:
            lines = ["-"]
        pdf.setFillColor(color)
        pdf.setFont(font, size)
        for line_text in lines:
            pdf.drawCentredString(center_x, y, receipt_visual_text(line_text, lang), direction="RTL" if is_rtl else "LTR", shaping=shaping)
            y -= leading
        return len(lines)

    def section(title, rows):
        nonlocal y
        prepared = []
        inner_w = content_w - 18 * mm
        for label, value, emphasized in rows:
            label_text = str(label)
            value_text = "-" if value is None or value == "" else str(value)
            pair = f"{label_text}: {value_text}"
            font = content_font
            size = 9.2 if emphasized else 8.6
            words = pair.split()
            line_list = []
            current = ""
            for word in words:
                candidate = word if not current else current + " " + word
                if stringWidth(candidate, font, size) <= inner_w:
                    current = candidate
                else:
                    if current:
                        line_list.append(current)
                    current = word
            if current:
                line_list.append(current)
            prepared.append((line_list or ["-"], font, size, emphasized))

        box_h = 11 * mm + sum(max(1, len(lines)) * 4.7 * mm for lines, _, _, _ in prepared) + 3 * mm
        if y - box_h < 32 * mm:
            pdf.showPage()
            # Preserve centered identity on overflow pages.
            pdf.setFillColor(LIGHT_WATERMARK)
            pdf.setFont("Helvetica-Bold", 36)
            pdf.drawCentredString(center_x, height / 2, "HIRE NOW")
            y = height - 22 * mm

        top = y
        pdf.setFillColor(SOFT)
        pdf.setStrokeColor(BORDER)
        pdf.setLineWidth(0.6)
        pdf.roundRect(left, top - box_h, content_w, box_h, 3 * mm, fill=1, stroke=1)

        pdf.setFillColor(NAVY)
        pdf.setFont(content_font, 10.5)
        pdf.drawCentredString(center_x, top - 6.5 * mm, receipt_visual_text(title, lang), direction="RTL" if is_rtl else "LTR", shaping=shaping)
        y = top - 12 * mm

        for lines, font, size, emphasized in prepared:
            pdf.setFillColor(ORANGE if emphasized else TEXT)
            pdf.setFont(font, size)
            for line_text in lines:
                pdf.drawCentredString(center_x, y, receipt_visual_text(line_text, lang), direction="RTL" if is_rtl else "LTR", shaping=shaping)
                y -= 4.7 * mm
        y = top - box_h - 5 * mm

    generated = datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")
    receipt_no = f"HN-{booking['id']}-{booking['start_date'].replace('-', '')}"
    start_time = booking["start_time"] or "-"
    end_time = booking["end_time"] or "-"
    booking_type = (booking["booking_type"] or "regular").replace("_", " ").title()
    payment_method = (booking["payment_method"] or "").replace("_", " ").title()
    payment_status = (booking["payment_status"] or "").replace("_", " ").title()
    booking_status = (booking["status"] or "").replace("_", " ").title()

    section(label("receipt_summary"), [
        (label("receipt_no"), receipt_no, True),
        (label("booking_id"), f"#{booking['id']}", False),
        (label("generated"), generated, False),
        (label("booking_status"), booking_status, False),
        (label("payment_status"), payment_status, True),
    ])

    section(label("booking_service"), [
        (label("service_type"), booking_type, False),
        (label("service_skill"), worker["skill"] if worker and "skill" in worker.keys() else "-", False),
        (label("service_date"), booking["start_date"], False),
        (label("scheduled_time"), f"{start_time} - {end_time}", False),
        (label("actual_work"), f"{int(booking['actual_minutes'] or 0)} minutes" if int(booking["actual_minutes"] or 0) else "-", False),
        (label("work_address"), booking["address"] or "-", False),
        (label("instructions"), booking["special_instructions"] or "-", False),
    ])

    section(label("hirer_worker"), [
        (label("hirer"), hirer["name"] if hirer else "-", False),
        (label("hirer_contact"), hirer["phone"] if hirer and "phone" in hirer.keys() else "-", False),
        (label("worker"), worker["name"] if worker else "-", False),
        (label("worker_contact"), worker["phone"] if worker and "phone" in worker.keys() else "-", False),
        (label("worker_city"), worker["city"] if worker and "city" in worker.keys() else "-", False),
    ])

    payment_rows = [
        (label("payment_method"), payment_method, False),
        (label("payment_status"), payment_status, True),
        (label("final_amount"), f"INR {int(booking['total_amount'] or 0)}", True),
        (label("received"), f"INR {int(booking['paid_amount'] or 0)}", True),
    ]
    if int(booking["diagnosis_fee"] or 0):
        payment_rows.append((label("diagnosis_fee"), f"INR {int(booking['diagnosis_fee'] or 0)}", False))
    if int(booking["work_amount"] or 0):
        payment_rows.append((label("work_amount"), f"INR {int(booking['work_amount'] or 0)}", False))
    if booking["payment_id"]:
        payment_rows.append((label("payment_id"), booking["payment_id"], False))
    if booking["razorpay_order_id"]:
        payment_rows.append((label("order_id"), booking["razorpay_order_id"], False))
    if booking["cash_verified_at"]:
        payment_rows.append((label("cash_verified"), booking["cash_verified_at"], False))
    section(label("payment_details"), payment_rows)

    if audience == "worker":
        finance_rows = [
            (label("gross"), f"INR {int(finance['gross_amount'] or 0)}", False),
            (label("commission"), f"INR {int(finance['platform_commission'] or 0)}", False),
            (label("worker_net"), f"INR {int(finance['worker_net'] or 0)}", True),
            (label("settlement"), (finance["settlement_status"] or "not_ready").replace("_", " ").title(), True),
            (label("settlement_ref"), finance["settlement_reference"] or "-", False),
        ]
        section(label("worker_earnings"), finance_rows)

    # Centered support/footer card.
    if y < 66 * mm:
        pdf.showPage()
        y = height - 22 * mm
    footer_h = 48 * mm
    pdf.setFillColor(NAVY)
    pdf.roundRect(left, y - footer_h, content_w, footer_h, 4 * mm, fill=1, stroke=0)

    footer_y = y - 8 * mm
    pdf.setFillColor(ORANGE)
    pdf.setFont(content_font if content_font != "Helvetica" else "Helvetica-Bold", 10.5)
    pdf.drawCentredString(center_x, footer_y, receipt_visual_text(label("support"), lang), direction="RTL" if is_rtl else "LTR", shaping=shaping)
    footer_y -= 6 * mm

    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica", 8.3)
    footer_lines = [
        "Office: Jarwa Road Tulsipur District Balrampur Pin Code 271208",
        "Customer Care: +918303478983  |  +9660503429167",
        "Email: Hirenow@gmail.com  |  Website: Hirenow.com",
        "Facebook: @HireNow  |  Instagram: @HireNow  |  X (Twitter): @HireNow",
    ]
    for footer_line in footer_lines:
        pdf.drawCentredString(center_x, footer_y, footer_line)
        footer_y -= 5 * mm

    footer_y -= 1 * mm
    pdf.setFillColor(colors.HexColor("#DCE6F5"))
    pdf.setFont(content_font if content_font != "Helvetica" else "Helvetica-Oblique", 7.5)
    pdf.drawCentredString(center_x, footer_y, receipt_visual_text(label("system_note"), lang), direction="RTL" if is_rtl else "LTR", shaping=shaping)

    pdf.save()
    buf.seek(0)
    return buf


@app.post("/api/bookings/<int:booking_id>/sync-payment")
def sync_booking_payment(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id=? AND hirer_id=?",
        (booking_id, current_hirer_id()),
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    result = reconcile_online_booking_payment(conn, booking, notify_users=True)
    if result.get("error"):
        conn.close()
        return jsonify({"error": result["error"]}), 502
    conn.commit()
    refreshed = conn.execute("SELECT payment_status, paid_amount, payment_id, total_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    return jsonify({
        "ok": True,
        "payment_status": refreshed["payment_status"],
        "paid_amount": int(refreshed["paid_amount"] or 0),
        "total_amount": int(refreshed["total_amount"] or 0),
        "payment_id": refreshed["payment_id"],
        "changed": result.get("changed", False),
    })


@app.get("/api/bookings/<int:booking_id>/receipt.pdf")
def hirer_payment_receipt(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    booking = conn.execute("SELECT * FROM bookings WHERE id=? AND hirer_id=?", (booking_id, current_hirer_id())).fetchone()
    if not booking:
        conn.close(); return jsonify({"error": "Booking not found"}), 404
    if booking["payment_status"] not in ("paid", "balance_due", "refund_pending") or int(booking["paid_amount"] or 0) <= 0:
        conn.close(); return jsonify({"error": "Receipt is available only after a successful payment"}), 409
    hirer = conn.execute("SELECT name, phone, preferred_language FROM hirers WHERE id=?", (booking["hirer_id"],)).fetchone()
    worker = conn.execute("SELECT name, phone, skill, city, preferred_language FROM workers WHERE id=?", (booking["worker_id"],)).fetchone()
    finance = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
    if not finance:
        sync_booking_financials(conn, booking_id)
        finance = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
        conn.commit()
    buf = build_payment_receipt_pdf(booking, hirer, worker, finance, "hirer")
    conn.close()
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"HireNow_Receipt_{booking_id}.pdf")


@app.get("/api/worker/bookings/<int:booking_id>/receipt.pdf")
def worker_payment_receipt(booking_id):
    auth_error = require_worker_login()
    if auth_error:
        return auth_error
    conn = get_db()
    booking = conn.execute("SELECT * FROM bookings WHERE id=? AND worker_id=?", (booking_id, current_worker_id())).fetchone()
    if not booking:
        conn.close(); return jsonify({"error": "Booking not found"}), 404
    if booking["payment_status"] not in ("paid", "balance_due", "refund_pending") or int(booking["paid_amount"] or 0) <= 0:
        conn.close(); return jsonify({"error": "Receipt is available only after a successful payment"}), 409
    hirer = conn.execute("SELECT name, phone, preferred_language FROM hirers WHERE id=?", (booking["hirer_id"],)).fetchone()
    worker = conn.execute("SELECT name, phone, skill, city, preferred_language FROM workers WHERE id=?", (booking["worker_id"],)).fetchone()
    finance = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
    if not finance:
        sync_booking_financials(conn, booking_id)
        finance = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
        conn.commit()
    buf = build_payment_receipt_pdf(booking, hirer, worker, finance, "worker")
    conn.close()
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"HireNow_Worker_Receipt_{booking_id}.pdf")


@app.get("/api/bookings/<int:booking_id>/payment-summary")
def booking_payment_summary(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id=? AND hirer_id=?",
        (booking_id, current_hirer_id()),
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    adjustment = conn.execute(
        """SELECT id, adjustment_type, amount, status, provider_order_id, provider_payment_id,
                  provider_refund_id, note, created_at, updated_at
           FROM payment_adjustments
           WHERE booking_id=? AND status!='cancelled'
           ORDER BY id DESC LIMIT 1""",
        (booking_id,),
    ).fetchone()
    result = {
        "booking_id": booking_id,
        "total_amount": int(booking["total_amount"] or 0),
        "paid_amount": int(booking["paid_amount"] or 0),
        "payment_status": booking["payment_status"],
        "payment_method": booking["payment_method"],
        "adjustment": row_to_dict(adjustment) if adjustment else None,
    }
    conn.close()
    return jsonify(result)


@app.post("/api/bookings/<int:booking_id>/create-balance-order")
def create_balance_order(booking_id):
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id=? AND hirer_id=?",
        (booking_id, current_hirer_id()),
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    if booking["payment_method"] != "online" or booking["status"] != "completed":
        conn.close()
        return jsonify({"error": "Balance payment is available only for completed online bookings"}), 400
    if booking["payment_status"] != "balance_due":
        conn.close()
        return jsonify({"error": "This booking has no outstanding balance"}), 400
    due = int(booking["total_amount"] or 0) - int(booking["paid_amount"] or 0)
    if due <= 0:
        conn.close()
        return jsonify({"error": "No outstanding balance remains"}), 400
    adjustment = sync_payment_adjustment(conn, booking_id)
    conn.commit()
    try:
        order = payments.create_order(
            amount_rupees=due,
            receipt=f"balance_{booking_id}_{adjustment['id']}",
            notes={"booking_id": str(booking_id), "adjustment_id": str(adjustment["id"]), "type": "balance_due"},
        )
    except payments.RazorpayConfigError as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    except payments.RazorpayAPIError as e:
        conn.close()
        return jsonify({"error": str(e)}), 502
    previous = conn.execute("SELECT provider_order_id FROM payment_adjustments WHERE id=?", (adjustment["id"],)).fetchone()
    if previous and previous["provider_order_id"]:
        payment_accounting.register_order(conn, booking_id, previous["provider_order_id"])
    payment_accounting.register_order(conn, booking_id, order["id"])
    conn.execute(
        "UPDATE payment_adjustments SET provider_order_id=?, updated_at=? WHERE id=?",
        (order["id"], datetime.utcnow().isoformat(), adjustment["id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({
        "adjustment_id": adjustment["id"],
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": payments.RAZORPAY_KEY_ID,
        "balance_due": due,
    })


def verify_captured_checkout(balance=False):
    auth_error = require_login()
    if auth_error:
        return auth_error
    data = request.get_json(silent=True) or {}
    order_id, payment_id, signature = (data.get(key) for key in
        ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature"))
    if not all(isinstance(value, str) and value for value in (order_id, payment_id, signature)):
        return jsonify({"error": "razorpay_order_id, razorpay_payment_id and razorpay_signature are required"}), 400
    conn = get_db()
    try:
        booking = payment_accounting.booking_for_order(conn, order_id, current_hirer_id())
        if not booking:
            return jsonify({"error": "No booking matches this order"}), 404
        if booking["status"] == "requested":
            return jsonify({"error": "Worker must accept before checkout verification"}), 409
        if booking["status"] == "confirmed" and booking["payment_method"] != "online":
            return jsonify({"error": "Cash-selected bookings can switch to online only after work completion"}), 400
        try:
            if not payments.verify_checkout_signature(order_id, payment_id, signature):
                return jsonify({"error": "Signature verification failed — payment not trusted"}), 400
        except payments.RazorpayConfigError as exc:
            return jsonify({"error": str(exc)}), 500
        result = reconcile_online_booking_payment(conn, booking, notify_users=True,
            required_order=order_id, required_payment=payment_id)
        if result.get("error"):
            conn.rollback()
            return jsonify({"error": result["error"], "payment_status": result["payment_status"]}), result.get("http_status", 502)
        conn.commit()
        return jsonify({"ok": True, "status": booking["status"],
            "payment_status": result["payment_status"], "paid_amount": result["provider_paid_amount"],
            "total_amount": int(booking["total_amount"] or 0), "payment_id": result.get("payment_id"),
            "receipt_url": f"/api/bookings/{booking['id']}/receipt.pdf"})
    finally:
        conn.close()


@app.post("/api/payments/verify-balance")
def verify_balance_payment():
    return verify_captured_checkout(balance=True)


@app.post("/api/payments/verify")
def verify_payment():
    return verify_captured_checkout()


@app.post("/api/payments/webhook")
def razorpay_webhook():
    signature = request.headers.get("X-Razorpay-Signature", "")
    try:
        if not payments.verify_webhook_signature(request.get_data(), signature):
            return jsonify({"error": "Invalid webhook signature"}), 400
    except payments.RazorpayConfigError as exc:
        return jsonify({"error": str(exc)}), 500
    event = request.get_json(silent=True) or {}
    if event.get("event") not in ("payment.captured", "refund.processed"):
        return jsonify({"ok": True})
    entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = entity.get("order_id")
    if not order_id and event.get("event") == "refund.processed":
        refund = event.get("payload", {}).get("refund", {}).get("entity", {})
        payment_id = refund.get("payment_id")
        if not payment_id:
            return jsonify({"error": "Refund payment ID required"}), 400
        try:
            order_id = payments.fetch_payment(payment_id).get("order_id")
        except (payments.RazorpayConfigError, payments.RazorpayAPIError) as exc:
            return jsonify({"error": str(exc)}), 502
    if not isinstance(order_id, str) or not order_id:
        return jsonify({"error": "Provider order ID required"}), 400
    conn = get_db()
    try:
        booking = payment_accounting.booking_for_order(conn, order_id)
        if not booking:
            return jsonify({"ok": True})
        # Fetch fresh provider state even for an old capture webhook delivered
        # after a refund. The event amount is not added to a mutable bill.
        result = reconcile_online_booking_payment(conn, booking, notify_users=True)
        if result.get("error"):
            conn.rollback()
            return jsonify({"error": result["error"]}), result.get("http_status", 502)
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


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
    for booking in rows:
        if booking["razorpay_order_id"] and booking["payment_status"] in ("pending", "cash_pending", "balance_due"):
            reconcile_online_booking_payment(conn, booking, notify_users=False)
    conn.commit()
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
    if not is_postgres():
        conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(
        for_update("SELECT * FROM bookings WHERE id = ? AND hirer_id = ?"),
        (booking_id, current_hirer_id()),
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    if booking["status"] in ("in_progress", "completed", "cancelled", "rejected"):
        conn.close()
        return jsonify({"error": "This booking can no longer be cancelled"}), 400
    paid_amount = int(booking["paid_amount"] or 0)
    payment_status = "refund_pending" if booking["payment_status"] == "paid" or paid_amount > 0 else "cancelled"
    conn.execute(
        "UPDATE bookings SET status = 'cancelled', payment_status = ?, cancelled_at = ?, cancellation_reason = ? WHERE id = ?",
        (payment_status, datetime.now().isoformat(), reason, booking_id),
    )
    conn.execute(
        "INSERT INTO booking_events (booking_id, status, note) VALUES (?, 'cancelled', ?)",
        (booking_id, reason),
    )
    if paid_amount > 0:
        sync_booking_financials(conn, booking_id)
    else:
        sync_payment_adjustment(conn, booking_id)
    conn.commit()
    conn.close()
    return jsonify({"status": "cancelled", "payment_status": payment_status})


@app.get("/api/hirer/profile")
def hirer_profile():
    auth_error = require_login()
    if auth_error:
        return auth_error
    conn = get_db()
    hirer = conn.execute("SELECT id, name, phone, preferred_language, preferred_theme, notifications_enabled, home_address, home_city, home_latitude, home_longitude, location_updated_at, created_at FROM hirers WHERE id = ?", (current_hirer_id(),)).fetchone()
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
    for booking in rows:
        if booking["razorpay_order_id"] and booking["payment_status"] in ("pending", "cash_pending", "balance_due"):
            reconcile_online_booking_payment(conn, booking, notify_users=False)
    conn.commit()
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

    if booking["status"] in ("requested", "rejected", "cancelled", "completed"):
        conn.close()
        return jsonify({"error": "This booking is not eligible for GPS progress updates"}), 400
    if not booking_allows_work(booking):
        conn.close()
        return jsonify({"error": "Payment must be ready before the worker travels or checks in"}), 400

    if booking["status"] == "confirmed":
        nxt = "en_route"
    elif booking["status"] == "en_route":
        nxt = "checked_in"
    elif booking["status"] == "checked_in":
        conn.close()
        return jsonify({"error": "Worker is already checked in. Start work using the work timer."}), 409
    elif booking["status"] == "in_progress":
        conn.close()
        return jsonify({"error": "Work is already in progress. Complete it using the work timer."}), 409
    else:
        conn.close()
        return jsonify({"error": "This booking cannot be advanced by GPS check-in"}), 409

    note_map = {
        "en_route": "Worker is heading to the job location with GPS recorded",
        "checked_in": "Worker arrived and checked in at the site with GPS",
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

    return jsonify({"status": nxt, "latitude": lat, "longitude": lng})


@app.post("/api/bookings/<int:booking_id>/cash-otp")
def generate_cash_payment_otp(booking_id):
    """Hirer generates a short-lived OTP only after a cash job is completed."""
    auth_error = require_login()
    if auth_error:
        return auth_error

    conn = get_db()
    if not is_postgres():
        conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(
        for_update("SELECT * FROM bookings WHERE id = ? AND hirer_id = ?"), (booking_id, current_hirer_id())
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404
    if booking["status"] != "completed":
        conn.close()
        return jsonify({"error": "Cash OTP can be generated only after the job is completed"}), 400
    if booking["payment_status"] == "paid":
        conn.close()
        return jsonify({"error": "Payment is already verified"}), 400
    if int(booking["paid_amount"] or 0) > 0 or booking["payment_status"] in ("balance_due", "refund_pending"):
        conn.close()
        return jsonify({"error": "Cash can be selected only when no online amount has already been paid"}), 409

    # If checkout was previously opened, reconcile first so we never accept cash after an already captured online payment.
    if booking["razorpay_order_id"]:
        reconciled = reconcile_online_booking_payment(conn, booking, notify_users=True)
        if reconciled.get("error"):
            conn.rollback()
            conn.close()
            return jsonify({"error": reconciled["error"]}), 502
        booking = conn.execute(
            for_update("SELECT * FROM bookings WHERE id = ? AND hirer_id = ?"),
            (booking_id, current_hirer_id()),
        ).fetchone()
        if booking["payment_status"] == "paid" or int(booking["paid_amount"] or 0) > 0:
            conn.commit()
            conn.close()
            return jsonify({"error": "Online payment is already confirmed for this booking"}), 409

    otp = f"{__import__('secrets').randbelow(1000000):06d}"
    expires = datetime.now() + timedelta(minutes=10)
    conn.execute(
        """UPDATE bookings SET payment_method='cash', cash_otp_hash = ?, cash_otp_expires_at = ?,
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
    if not is_postgres():
        conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(
        for_update("SELECT * FROM bookings WHERE id = ? AND worker_id = ?"), (booking_id, current_worker_id())
    ).fetchone()
    if not booking:
        conn.close()
        return jsonify({"error": "Booking not found or not assigned to you"}), 404
    if booking["status"] != "completed" or booking["payment_method"] != "cash":
        conn.close()
        return jsonify({"error": "Cash OTP is available only for a completed booking currently set to cash"}), 400
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
        """UPDATE bookings SET payment_status = 'paid', cash_verified_at = ?, paid_amount = total_amount,
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
    sync_booking_financials(conn, booking_id)
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


@app.get("/api/admin/accounts")
def admin_accounts():
    err = _check_admin_session()
    if err: return err
    conn = get_db()
    workers = conn.execute("SELECT id, name, phone, 'worker' AS account_type, account_status, account_status_reason, created_at FROM workers WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()
    hirers = conn.execute("SELECT id, name, phone, 'hirer' AS account_type, account_status, account_status_reason, created_at FROM hirers WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify(rows_to_list(workers) + rows_to_list(hirers))


@app.post("/api/admin/accounts/<account_type>/<int:account_id>/moderate")
def admin_moderate_account(account_type, account_id):
    err = _check_admin_session()
    if err: return err
    err = _check_admin_csrf()
    if err: return err
    if account_type not in ("worker", "hirer"):
        return jsonify({"error": "Invalid account type"}), 400
    data = request.get_json(force=True) or {}
    action = (data.get("action") or "").strip().lower()
    reason = (data.get("reason") or "").strip() or None
    if action not in ("activate", "freeze", "delete"):
        return jsonify({"error": "Action must be activate, freeze or delete"}), 400
    if action in ("freeze", "delete") and not reason:
        return jsonify({"error": "Reason is required"}), 400
    table = "workers" if account_type == "worker" else "hirers"
    conn = get_db()
    row = conn.execute(f"SELECT id FROM {table} WHERE id = ? AND deleted_at IS NULL", (account_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"error": "Account not found"}), 404
    if action == "delete":
        conn.execute(f"UPDATE {table} SET account_status='deleted', account_status_reason=?, deleted_at=? WHERE id=?", (reason, datetime.utcnow().isoformat(), account_id))
    else:
        status = "active" if action == "activate" else "frozen"
        conn.execute(f"UPDATE {table} SET account_status=?, account_status_reason=? WHERE id=?", (status, reason, account_id))
    conn.execute("INSERT INTO admin_account_actions(account_type, account_id, action, reason, admin_username) VALUES (?, ?, ?, ?, ?)", (account_type, account_id, action, reason, session.get("admin_username")))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "action": action})


@app.get("/api/admin/workers")
def admin_workers():
    err = _check_admin_session()
    if err:
        return err
    conn = get_db()
    rows = conn.execute(
        """SELECT id, name, phone, skill, city, daily_wage, rating,
                  jobs_completed, verification_status, rate_status, rate_review_note, id_document_path, is_online,
                  CASE WHEN service_latitude IS NOT NULL AND service_longitude IS NOT NULL THEN 1 ELSE 0 END AS service_location_configured,
                  service_location_updated_at, created_at
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


@app.get("/api/admin/diagnosis-pricing")
def admin_diagnosis_pricing():
    err = _check_admin_session()
    if err: return err
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM diagnosis_pricing_rules ORDER BY COALESCE(city,''), COALESCE(skill,''), max_km, id"
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/admin/diagnosis-pricing")
def admin_save_diagnosis_pricing():
    err = _check_admin_session()
    if err: return err
    err = _check_admin_csrf()
    if err: return err
    data = request.get_json(force=True) or {}
    rule_id = data.get("id")
    city = (data.get("city") or "").strip() or None
    raw_skill = (data.get("skill") or "").strip()
    skill = normalize_worker_skill(raw_skill, strict=True) if raw_skill else None
    if raw_skill and not skill:
        return jsonify({"error": "Invalid skill category"}), 400
    try:
        max_km = float(data.get("max_km"))
        fee = int(data.get("fee"))
    except (TypeError, ValueError):
        return jsonify({"error": "max_km and fee must be numbers"}), 400
    if max_km <= 0 or max_km > 500:
        return jsonify({"error": "max_km must be between 0 and 500"}), 400
    if fee < 0 or fee > 100000:
        return jsonify({"error": "fee is outside the allowed range"}), 400
    active = 1 if data.get("is_active", True) else 0
    now = datetime.utcnow().isoformat()
    conn = get_db()
    duplicate = conn.execute(
        """SELECT id FROM diagnosis_pricing_rules
           WHERE COALESCE(LOWER(city),'')=COALESCE(LOWER(?),'')
             AND COALESCE(skill,'')=COALESCE(?,'')
             AND ABS(max_km-?) < 0.000001
             AND (? IS NULL OR id<>?)""",
        (city, skill, max_km, rule_id, rule_id),
    ).fetchone()
    if duplicate:
        conn.close()
        return jsonify({"error": "A pricing slab already exists for this scope and distance"}), 409
    if rule_id:
        existing = conn.execute("SELECT id FROM diagnosis_pricing_rules WHERE id=?", (rule_id,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({"error": "Pricing rule not found"}), 404
        conn.execute(
            """UPDATE diagnosis_pricing_rules
               SET city=?, skill=?, max_km=?, fee=?, is_active=?, updated_at=? WHERE id=?""",
            (city, skill, max_km, fee, active, now, rule_id),
        )
        saved_id = int(rule_id)
    else:
        cur = conn.execute(
            """INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (city, skill, max_km, fee, active, now, now),
        )
        saved_id = cur.lastrowid
    conn.commit(); conn.close()
    return jsonify({"ok": True, "id": saved_id})


@app.delete("/api/admin/diagnosis-pricing/<int:rule_id>")
def admin_delete_diagnosis_pricing(rule_id):
    err = _check_admin_session()
    if err: return err
    err = _check_admin_csrf()
    if err: return err
    conn = get_db()
    row = conn.execute("SELECT id FROM diagnosis_pricing_rules WHERE id=?", (rule_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Pricing rule not found"}), 404
    conn.execute("DELETE FROM diagnosis_pricing_rules WHERE id=?", (rule_id,))
    remaining = conn.execute("SELECT COUNT(*) AS n FROM diagnosis_pricing_rules WHERE is_active=1").fetchone()["n"]
    if not remaining:
        conn.rollback(); conn.close()
        return jsonify({"error": "At least one active diagnosis pricing rule must remain"}), 409
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.get("/api/worker/earnings-summary")
def worker_earnings_summary():
    err = require_worker_login()
    if err: return err
    conn = get_db()
    stale = conn.execute(
        "SELECT * FROM bookings WHERE worker_id=? AND razorpay_order_id IS NOT NULL AND payment_status IN ('pending','cash_pending','balance_due')",
        (current_worker_id(),),
    ).fetchall()
    for booking in stale:
        reconcile_online_booking_payment(conn, booking, notify_users=False)
    completed_paid = conn.execute(
        "SELECT id FROM bookings WHERE worker_id=? AND status='completed' AND payment_status='paid'",
        (current_worker_id(),),
    ).fetchall()
    for row in completed_paid:
        sync_booking_financials(conn, row["id"])
    conn.commit()
    rows = conn.execute("""SELECT bf.*, b.start_date, b.booking_type, b.payment_status
                           FROM booking_financials bf JOIN bookings b ON b.id=bf.booking_id
                           WHERE bf.worker_id=? ORDER BY bf.booking_id DESC""", (current_worker_id(),)).fetchall()
    items = rows_to_list(rows)
    conn.close()
    return jsonify({
        "items": items,
        "gross": sum(int(x["gross_amount"] or 0) for x in items),
        "commission": sum(int(x["platform_commission"] or 0) for x in items),
        "net": sum(int(x["worker_net"] or 0) for x in items if x["settlement_status"] in ("pending","settled")),
        "pending": sum(int(x["worker_net"] or 0) for x in items if x["settlement_status"] == "pending"),
        "settled": sum(int(x["worker_net"] or 0) for x in items if x["settlement_status"] == "settled"),
    })


@app.post("/api/admin/finance/<int:booking_id>/settle")
def admin_record_worker_settlement(booking_id):
    err = _check_admin_session()
    if err: return err
    err = _check_admin_csrf()
    if err: return err
    data = request.get_json(force=True) or {}
    reference = (data.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "Settlement reference is required"}), 400
    conn = get_db()
    sync_booking_financials(conn, booking_id)
    finance = conn.execute(
        """SELECT bf.*, b.status AS booking_status, b.payment_status, b.worker_id
           FROM booking_financials bf JOIN bookings b ON b.id=bf.booking_id
           WHERE bf.booking_id=?""",
        (booking_id,),
    ).fetchone()
    if not finance:
        conn.close(); return jsonify({"error": "Finance record not found"}), 404
    if finance["booking_status"] != "completed" or finance["payment_status"] != "paid":
        conn.close(); return jsonify({"error": "Only completed and fully paid bookings can be settled"}), 409
    if finance["settlement_status"] == "held":
        conn.close(); return jsonify({"error": "Settlement is held until the worker payout account is verified"}), 409
    if finance["settlement_status"] == "settled":
        conn.close(); return jsonify({"ok": True, "settlement_status": "settled", "reference": finance["settlement_reference"]})
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE booking_financials SET settlement_status='settled', settlement_reference=?, updated_at=? WHERE booking_id=?",
        (reference, now, booking_id),
    )
    add_in_app_notification(
        conn, "worker", finance["worker_id"], "settlement_paid", "Earnings settled",
        f"Your net earnings for booking #{booking_id} were marked settled. Reference: {reference}",
        booking_id,
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True, "settlement_status": "settled", "reference": reference})


@app.get("/api/admin/finance")
def admin_finance():
    err = _check_admin_session()
    if err: return err
    conn = get_db()
    completed_paid = conn.execute(
        "SELECT id FROM bookings WHERE status='completed' AND payment_status='paid'"
    ).fetchall()
    for row in completed_paid:
        sync_booking_financials(conn, row["id"])
    conn.commit()
    rows = conn.execute("""SELECT bf.*, b.payment_status, b.status AS booking_status,
                                  w.name AS worker_name, h.name AS hirer_name
                           FROM booking_financials bf
                           JOIN bookings b ON b.id=bf.booking_id
                           JOIN workers w ON w.id=bf.worker_id
                           JOIN hirers h ON h.id=b.hirer_id
                           ORDER BY bf.booking_id DESC LIMIT 500""").fetchall()
    commission = get_commission_percent(conn)
    items = rows_to_list(rows)
    pending_adjustments = rows_to_list(conn.execute(
        """SELECT pa.*, b.payment_status, h.name AS hirer_name, w.name AS worker_name
           FROM payment_adjustments pa
           JOIN bookings b ON b.id=pa.booking_id
           JOIN hirers h ON h.id=b.hirer_id
           JOIN workers w ON w.id=b.worker_id
           WHERE pa.status='pending'
             AND b.payment_method='online'
             AND NOT (b.status IN ('cancelled','rejected') AND COALESCE(b.paid_amount,0)=0)
           ORDER BY pa.id DESC"""
    ).fetchall())
    response = {
        "commission_percent": commission,
        "items": items,
        "platform_revenue": sum(int(x["platform_commission"] or 0) for x in items if x["booking_status"]=="completed"),
        "worker_payable": sum(int(x["worker_net"] or 0) for x in items if x["settlement_status"] in ("pending","held")),
        "pending_balance_due": sum(int(x["amount"] or 0) for x in pending_adjustments if x["adjustment_type"]=="balance_due"),
        "pending_refunds": sum(int(x["amount"] or 0) for x in pending_adjustments if x["adjustment_type"]=="refund"),
        "adjustments": pending_adjustments,
    }
    conn.close()
    return jsonify(response)


@app.post("/api/admin/payment-adjustments/<int:adjustment_id>/resolve")
def admin_resolve_payment_adjustment(adjustment_id):
    err = _check_admin_session()
    if err: return err
    err = _check_admin_csrf()
    if err: return err
    data = request.get_json(silent=True) or {}
    reference = str(data.get("reference") or "").strip()
    note = str(data.get("note") or "").strip() or None
    if not reference:
        return jsonify({"error": "A processed provider refund ID is required"}), 400
    conn = get_db()
    try:
        if not is_postgres():
            conn.execute("BEGIN IMMEDIATE")
        adjustment = conn.execute("SELECT * FROM payment_adjustments WHERE id=?", (adjustment_id,)).fetchone()
        if not adjustment:
            return jsonify({"error": "Adjustment not found"}), 404
        booking = conn.execute(for_update("SELECT * FROM bookings WHERE id=?"), (adjustment["booking_id"],)).fetchone()
        adjustment = conn.execute("SELECT * FROM payment_adjustments WHERE id=?", (adjustment_id,)).fetchone()
        if adjustment["status"] == "resolved":
            return jsonify({"error": "Adjustment is already resolved"}), 409
        if adjustment["adjustment_type"] != "refund":
            return jsonify({"error": "Balance due must be paid by the hirer through checkout"}), 400
        if not booking:
            return jsonify({"error": "Booking not found"}), 404
        try:
            payment_accounting.record_processed_refund(conn, booking["id"], reference, int(adjustment["amount"]))
        except (payments.RazorpayConfigError, payments.RazorpayAPIError) as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 409
        conn.execute("UPDATE payment_adjustments SET status='resolved',provider_refund_id=?,note=?,updated_at=? WHERE id=?",
            (reference, note, datetime.utcnow().isoformat(), adjustment_id))
        result = reconcile_online_booking_payment(conn, booking)
        if result.get("error"):
            conn.rollback()
            return jsonify({"error": result["error"]}), result.get("http_status", 502)
        sync_booking_financials(conn, booking["id"])
        add_in_app_notification(conn, "hirer", booking["hirer_id"], "refund_processed", "Refund processed",
            f"Provider refund {reference} for booking #{booking['id']} was confirmed.", booking["id"])
        conn.commit()
        return jsonify({"ok": True, "adjustment_id": adjustment_id, "status": "resolved", "payment_status": result["payment_status"]})
    finally:
        conn.close()


@app.put("/api/admin/finance/commission")
def admin_set_commission():
    err = _check_admin_session()
    if err: return err
    err = _check_admin_csrf()
    if err: return err
    data = request.get_json(force=True) or {}
    try:
        percent = float(data.get("percent"))
    except (TypeError, ValueError):
        return jsonify({"error": "percent must be a number"}), 400
    if percent < 0 or percent > 50:
        return jsonify({"error": "Commission must be between 0 and 50 percent"}), 400
    conn = get_db()
    conn.execute("""INSERT INTO platform_settings(setting_key, setting_value, updated_at)
                    VALUES ('commission_percent', ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at""",
                 (str(percent), datetime.utcnow().isoformat()))
    for row in conn.execute("SELECT id FROM bookings WHERE status='completed'").fetchall():
        sync_booking_financials(conn, row["id"])
    conn.commit(); conn.close()
    return jsonify({"ok": True, "commission_percent": percent})


@app.get("/api/admin/payout-accounts")
def admin_payout_accounts():
    err = _check_admin_session()
    if err: return err
    conn = get_db()
    rows = conn.execute("""SELECT p.worker_id, p.account_holder_name, p.account_number_last4, p.ifsc,
                                  p.bank_name, p.upi_id, p.verification_status, w.name AS worker_name,
                                  w.phone AS worker_phone
                           FROM worker_payout_accounts p JOIN workers w ON w.id=p.worker_id
                           ORDER BY p.updated_at DESC""").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/admin/payout-accounts/<int:worker_id>/review")
def admin_review_payout_account(worker_id):
    err = _check_admin_session()
    if err: return err
    err = _check_admin_csrf()
    if err: return err
    data = request.get_json(force=True) or {}
    decision = (data.get("decision") or "").strip().lower()
    if decision not in ("verified", "rejected"):
        return jsonify({"error": "decision must be verified or rejected"}), 400
    conn = get_db()
    row = conn.execute("SELECT worker_id FROM worker_payout_accounts WHERE worker_id=?", (worker_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"error": "Payout account not found"}), 404
    conn.execute("UPDATE worker_payout_accounts SET verification_status=?, updated_at=? WHERE worker_id=?",
                 (decision, datetime.utcnow().isoformat(), worker_id))
    for booking in conn.execute("SELECT id FROM bookings WHERE worker_id=? AND status='completed'", (worker_id,)).fetchall():
        sync_booking_financials(conn, booking["id"])
    conn.commit(); conn.close()
    return jsonify({"ok": True, "verification_status": decision})


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
