-- HireNow database schema (SQLite for local dev; same schema works on Postgres
-- with minor type tweaks — see README "Moving to Postgres").

CREATE TABLE IF NOT EXISTS workers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    phone               TEXT UNIQUE,
    password_hash       TEXT,
    skill               TEXT NOT NULL,
    skills_detail       TEXT,
    city                TEXT NOT NULL,
    daily_wage          INTEGER NOT NULL,
    hourly_wage         INTEGER,
    overtime_wage       INTEGER,
    background_checked  INTEGER DEFAULT 0,
    about               TEXT,
    distance_km         REAL,
    availability        TEXT,
    rating              REAL DEFAULT 0,
    jobs_completed      INTEGER DEFAULT 0,
    experience_years    INTEGER DEFAULT 0,
    verification_status TEXT DEFAULT 'unverified',
    id_document_path    TEXT,
    account_status      TEXT NOT NULL DEFAULT 'active',
    account_status_reason TEXT,
    deleted_at          TEXT,
    service_latitude    REAL,
    service_longitude   REAL,
    service_location_updated_at TEXT,
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS worker_payout_accounts (
    worker_id INTEGER PRIMARY KEY REFERENCES workers(id),
    account_holder_name TEXT NOT NULL,
    account_number_last4 TEXT NOT NULL,
    account_number_encrypted TEXT,
    ifsc TEXT NOT NULL,
    bank_name TEXT,
    upi_id TEXT,
    provider_account_id TEXT,
    provider_fund_account_id TEXT,
    verification_status TEXT NOT NULL DEFAULT 'pending',
    account_status TEXT NOT NULL DEFAULT 'active',
    account_status_reason TEXT,
    deleted_at TEXT,
    service_latitude REAL,
    service_longitude REAL,
    service_location_updated_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hirers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    account_status TEXT NOT NULL DEFAULT 'active',
    account_status_reason TEXT,
    deleted_at     TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admin_account_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_type TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    admin_username TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS diagnosis_pricing_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT,
    skill TEXT,
    max_km REAL NOT NULL,
    fee INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    hirer_id             INTEGER NOT NULL,
    worker_id            INTEGER NOT NULL,
    start_date           TEXT NOT NULL,
    start_time           TEXT,
    end_time             TEXT,
    days                 INTEGER NOT NULL DEFAULT 1,
    hours                INTEGER NOT NULL DEFAULT 2,
    service_type         TEXT NOT NULL DEFAULT 'regular',
    booking_type         TEXT NOT NULL DEFAULT 'regular',
    diagnosis_fee        INTEGER NOT NULL DEFAULT 0,
    diagnosis_distance_km REAL,
    service_latitude     REAL,
    service_longitude    REAL,
    diagnosis_pricing_rule_id INTEGER,
    diagnosis_notes      TEXT,
    work_approved_at     TEXT,
    work_declined_at     TEXT,
    work_decline_reason  TEXT,
    work_started_at      TEXT,
    work_ended_at        TEXT,
    actual_minutes       INTEGER NOT NULL DEFAULT 0,
    work_amount          INTEGER NOT NULL DEFAULT 0,
    paid_amount          INTEGER NOT NULL DEFAULT 0,
    special_instructions TEXT,
    address              TEXT,
    payment_method       TEXT NOT NULL DEFAULT 'online',
    worker_response_at   TEXT,
    worker_rejection_reason TEXT,
    cash_otp_hash        TEXT,
    cash_otp_expires_at  TEXT,
    cash_verified_at     TEXT,
    total_amount         INTEGER NOT NULL,
    status               TEXT NOT NULL DEFAULT 'requested',
    payment_status       TEXT NOT NULL DEFAULT 'pending',
    payment_id           TEXT,
    razorpay_order_id    TEXT,
    cancelled_at         TEXT,
    cancellation_reason  TEXT,
    created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (hirer_id) REFERENCES hirers(id),
    FOREIGN KEY (worker_id) REFERENCES workers(id)
);

CREATE TABLE IF NOT EXISTS booking_financials (
    booking_id INTEGER PRIMARY KEY REFERENCES bookings(id),
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    gross_amount INTEGER NOT NULL DEFAULT 0,
    diagnosis_fee INTEGER NOT NULL DEFAULT 0,
    work_amount INTEGER NOT NULL DEFAULT 0,
    platform_commission INTEGER NOT NULL DEFAULT 0,
    worker_net INTEGER NOT NULL DEFAULT 0,
    payment_collected INTEGER NOT NULL DEFAULT 0,
    adjustment_amount INTEGER NOT NULL DEFAULT 0,
    settlement_status TEXT NOT NULL DEFAULT 'not_ready',
    settlement_reference TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payment_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    adjustment_type TEXT NOT NULL CHECK(adjustment_type IN ('balance_due','refund')),
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    provider_order_id TEXT,
    provider_payment_id TEXT,
    provider_refund_id TEXT,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payment_adjustments_booking
ON payment_adjustments(booking_id, adjustment_type, status);

CREATE TABLE IF NOT EXISTS booking_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  INTEGER NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT,
    latitude    REAL,
    longitude   REAL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (booking_id) REFERENCES bookings(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  INTEGER NOT NULL,
    sender_role TEXT NOT NULL,
    sender_id   INTEGER NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (booking_id) REFERENCES bookings(id)
);
