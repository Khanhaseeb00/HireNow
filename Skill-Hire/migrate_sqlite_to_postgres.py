"""One-time HireNow SQLite -> PostgreSQL migration.

Run only with both DATABASE_PATH (source SQLite) and DATABASE_URL (target
PostgreSQL) configured. Existing target rows are not overwritten.
"""
import os
import sqlite3
import psycopg
from psycopg.rows import dict_row

SOURCE = os.environ.get("DATABASE_PATH", "").strip()
TARGET = (os.environ.get("POSTGRES_MIGRATION_URL", "").strip()\n          or os.environ.get("DATABASE_URL", "").strip())
TABLES = [
    "workers", "hirers", "bookings", "booking_events", "messages",
    "worker_availability", "worker_unavailable_dates", "in_app_notifications",
    "admin_login_attempts",
]

if not SOURCE or not os.path.exists(SOURCE):
    raise SystemExit("DATABASE_PATH must point to the existing SQLite database")
if not TARGET:
    raise SystemExit("POSTGRES_MIGRATION_URL (or DATABASE_URL fallback) is required")

src = sqlite3.connect(SOURCE)
src.row_factory = sqlite3.Row
dst = psycopg.connect(TARGET, row_factory=dict_row)

try:
    with dst.cursor() as cur:
        for table in TABLES:
            exists = src.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                print(f"{table}: source table absent, skipped")
                continue
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"{table}: 0 rows")
                continue
            columns = list(rows[0].keys())
            names = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            conflict = "attempt_key" if table == "admin_login_attempts" else "id"
            sql = f"INSERT INTO {table} ({names}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO NOTHING"
            cur.executemany(sql, [tuple(row[col] for col in columns) for row in rows])
            print(f"{table}: copied {len(rows)} source rows")
        for table in [t for t in TABLES if t != "admin_login_attempts"]:
            cur.execute(
                f"""SELECT setval(
                    pg_get_serial_sequence('{table}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table}), 1),
                    COALESCE((SELECT MAX(id) FROM {table}), 0) > 0
                )"""
            )
    dst.commit()
    print("Migration committed successfully.")
except Exception:
    dst.rollback()
    raise
finally:
    src.close()
    dst.close()
