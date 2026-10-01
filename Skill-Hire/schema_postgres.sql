-- HireNow PostgreSQL schema

CREATE TABLE IF NOT EXISTS workers (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT UNIQUE,
    password_hash TEXT,
    skill TEXT NOT NULL,
    skills_detail TEXT,
    city TEXT NOT NULL,
    daily_wage INTEGER NOT NULL,
    hourly_wage INTEGER,
    overtime_wage INTEGER,
    background_checked INTEGER DEFAULT 0,
    about TEXT,
    distance_km REAL,
    availability TEXT,
    rating REAL DEFAULT 0,
    jobs_completed INTEGER DEFAULT 0,
    experience_years INTEGER DEFAULT 0,
    verification_status TEXT DEFAULT 'unverified',
    id_document_path TEXT,
    id_document_data BYTEA,
    id_document_mime TEXT,
    id_document_name TEXT,
    id_document_uploaded_at TEXT,
    is_online INTEGER NOT NULL DEFAULT 1,
    rate_status TEXT NOT NULL DEFAULT 'approved',
    rate_review_note TEXT,
    account_status TEXT NOT NULL DEFAULT 'active',
    account_status_reason TEXT,
    deleted_at TEXT,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS worker_payout_accounts (
    worker_id BIGINT PRIMARY KEY REFERENCES workers(id),
    account_holder_name TEXT NOT NULL,
    account_number_last4 TEXT NOT NULL,
    account_number_encrypted TEXT,
    ifsc TEXT NOT NULL,
    bank_name TEXT,
    upi_id TEXT,
    provider_account_id TEXT,
    provider_fund_account_id TEXT,
    verification_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS hirers (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    account_status TEXT NOT NULL DEFAULT 'active',
    account_status_reason TEXT,
    deleted_at TEXT,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS admin_account_actions (
    id BIGSERIAL PRIMARY KEY,
    account_type TEXT NOT NULL,
    account_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    admin_username TEXT,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS platform_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id BIGSERIAL PRIMARY KEY,
    hirer_id BIGINT NOT NULL REFERENCES hirers(id),
    worker_id BIGINT NOT NULL REFERENCES workers(id),
    start_date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    days INTEGER NOT NULL DEFAULT 1,
    hours INTEGER NOT NULL DEFAULT 2,
    service_type TEXT NOT NULL DEFAULT 'regular',
    booking_type TEXT NOT NULL DEFAULT 'regular',
    diagnosis_fee INTEGER NOT NULL DEFAULT 0,
    diagnosis_distance_km REAL,
    diagnosis_notes TEXT,
    work_approved_at TEXT,
    work_started_at TEXT,
    work_ended_at TEXT,
    actual_minutes INTEGER NOT NULL DEFAULT 0,
    work_amount INTEGER NOT NULL DEFAULT 0,
    paid_amount INTEGER NOT NULL DEFAULT 0,
    special_instructions TEXT,
    address TEXT,
    payment_method TEXT NOT NULL DEFAULT 'online',
    worker_response_at TEXT,
    worker_rejection_reason TEXT,
    cash_otp_hash TEXT,
    cash_otp_expires_at TEXT,
    cash_verified_at TEXT,
    total_amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    payment_status TEXT NOT NULL DEFAULT 'pending',
    payment_id TEXT,
    razorpay_order_id TEXT,
    cancelled_at TEXT,
    cancellation_reason TEXT,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS booking_financials (
    booking_id BIGINT PRIMARY KEY REFERENCES bookings(id),
    worker_id BIGINT NOT NULL REFERENCES workers(id),
    gross_amount INTEGER NOT NULL DEFAULT 0,
    diagnosis_fee INTEGER NOT NULL DEFAULT 0,
    work_amount INTEGER NOT NULL DEFAULT 0,
    platform_commission INTEGER NOT NULL DEFAULT 0,
    worker_net INTEGER NOT NULL DEFAULT 0,
    payment_collected INTEGER NOT NULL DEFAULT 0,
    adjustment_amount INTEGER NOT NULL DEFAULT 0,
    settlement_status TEXT NOT NULL DEFAULT 'not_ready',
    settlement_reference TEXT,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS booking_events (
    id BIGSERIAL PRIMARY KEY,
    booking_id BIGINT NOT NULL REFERENCES bookings(id),
    status TEXT NOT NULL,
    note TEXT,
    latitude REAL,
    longitude REAL,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    booking_id BIGINT NOT NULL REFERENCES bookings(id),
    sender_role TEXT NOT NULL,
    sender_id BIGINT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE TABLE IF NOT EXISTS worker_availability (
    id BIGSERIAL PRIMARY KEY,
    worker_id BIGINT NOT NULL REFERENCES workers(id),
    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
    enabled INTEGER NOT NULL DEFAULT 1,
    start_time TEXT NOT NULL DEFAULT '08:00',
    end_time TEXT NOT NULL DEFAULT '20:00',
    UNIQUE(worker_id, weekday)
);

CREATE TABLE IF NOT EXISTS worker_unavailable_dates (
    id BIGSERIAL PRIMARY KEY,
    worker_id BIGINT NOT NULL REFERENCES workers(id),
    unavailable_date TEXT NOT NULL,
    UNIQUE(worker_id, unavailable_date)
);

CREATE TABLE IF NOT EXISTS in_app_notifications (
    id BIGSERIAL PRIMARY KEY,
    recipient_type TEXT NOT NULL CHECK(recipient_type IN ('hirer','worker')),
    recipient_id BIGINT NOT NULL,
    booking_id BIGINT,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
);

CREATE INDEX IF NOT EXISTS idx_notifications_recipient
ON in_app_notifications(recipient_type, recipient_id, is_read, created_at);

CREATE TABLE IF NOT EXISTS admin_login_attempts (
    attempt_key TEXT PRIMARY KEY,
    failed_count INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT NOT NULL,
    locked_until TEXT
);
