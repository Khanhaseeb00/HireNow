-- HireNow database schema (SQLite for local dev; same schema works on Postgres
-- with minor type tweaks — see README "Moving to Postgres").

CREATE TABLE IF NOT EXISTS workers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    phone               TEXT UNIQUE,
    password_hash       TEXT,               -- set once worker registers/logs in
    skill               TEXT NOT NULL,
    skills_detail       TEXT,               -- comma-separated specific skills
    city                TEXT NOT NULL,
    daily_wage          INTEGER NOT NULL,   -- in rupees
    hourly_wage         INTEGER,            -- in rupees, optional — shown on worker list/profile cards
    overtime_wage       INTEGER,            -- in rupees/hr, optional — shown on worker profile as "Overtime Rate"
    background_checked  INTEGER DEFAULT 0,  -- 0/1 — separate from ID verification, shown as its own badge
    about               TEXT,               -- short bio shown on worker profile "About" section
    distance_km         REAL,               -- distance from hirer, filled in once real geolocation matching exists
    availability        TEXT,               -- 'today' | 'tomorrow' | NULL (unknown) — worker-set availability status
    rating              REAL DEFAULT 0,
    jobs_completed      INTEGER DEFAULT 0,
    experience_years    INTEGER DEFAULT 0,
    verification_status TEXT DEFAULT 'unverified', -- unverified | pending | verified | rejected
    id_document_path    TEXT,               -- uploaded ID photo, reviewed manually by admin
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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hirers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bookings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    hirer_id             INTEGER NOT NULL,
    worker_id            INTEGER NOT NULL,
    start_date           TEXT NOT NULL,
    start_time           TEXT,
    days                 INTEGER NOT NULL DEFAULT 1,
    hours                INTEGER NOT NULL DEFAULT 2,
    service_type         TEXT NOT NULL DEFAULT 'regular',
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

-- Every status change and every location ping is one row here.
-- This is what powers the "work tracking" and "location tracking" screens.
-- latitude/longitude here now come from the WORKER's real browser GPS
-- (navigator.geolocation) when they check in from the worker portal.
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

-- In-app chat between a hirer and a worker, scoped to one booking.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  INTEGER NOT NULL,
    sender_role TEXT NOT NULL,   -- 'hirer' | 'worker'
    sender_id   INTEGER NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (booking_id) REFERENCES bookings(id)
);
