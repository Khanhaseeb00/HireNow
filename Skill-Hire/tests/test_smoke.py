import os
import tempfile
import unittest

# Force an isolated SQLite database before importing the application.
_tmp = tempfile.NamedTemporaryFile(prefix="hirenow-test-", suffix=".db", delete=False)
_tmp.close()
os.environ.pop("DATABASE_URL", None)
os.environ.pop("POSTGRES_MIGRATION_URL", None)
os.environ["DATABASE_PATH"] = _tmp.name
os.environ["FLASK_ENV"] = "development"

import app  # noqa: E402
from db import get_db  # noqa: E402


class HireNowSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.ensure_schema_extensions()
        app.app.config.update(TESTING=True)
        cls.client = app.app.test_client()

    def test_app_boots_and_public_pages_respond(self):
        self.assertLess(self.client.get("/").status_code, 500)
        self.assertLess(self.client.get("/worker").status_code, 500)

    def test_diagnosis_pricing_policy_is_transparent(self):
        response = self.client.get("/api/pricing/diagnosis")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["currency"], "INR")
        self.assertEqual(data["slabs"], [
            {"up_to_km": 5, "fee": 100},
            {"up_to_km": 10, "fee": 150},
            {"up_to_km": 20, "fee": 250},
        ])
        self.assertIn("hirer approval", data["note"].lower())

    def test_distance_boundaries(self):
        self.assertEqual(app.diagnosis_fee_for_distance(0), (100, 5))
        self.assertEqual(app.diagnosis_fee_for_distance(5), (100, 5))
        self.assertEqual(app.diagnosis_fee_for_distance(5.1), (150, 10))
        self.assertEqual(app.diagnosis_fee_for_distance(10), (150, 10))
        self.assertEqual(app.diagnosis_fee_for_distance(20), (250, 20))
        self.assertEqual(app.diagnosis_fee_for_distance(20.1), (None, None))

    def test_schema_contains_new_booking_and_moderation_fields(self):
        conn = get_db()
        booking_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()}
        worker_cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
        hirer_cols = {r["name"] for r in conn.execute("PRAGMA table_info(hirers)").fetchall()}
        payout_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='worker_payout_accounts'").fetchone()
        action_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admin_account_actions'").fetchone()
        finance_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='booking_financials'").fetchone()
        settings_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='platform_settings'").fetchone()
        conn.close()
        for name in ("booking_type", "diagnosis_fee", "diagnosis_distance_km", "diagnosis_notes", "work_approved_at", "work_started_at", "work_ended_at", "actual_minutes", "work_amount", "paid_amount"):
            self.assertIn(name, booking_cols)
        for cols in (worker_cols, hirer_cols):
            self.assertIn("account_status", cols)
            self.assertIn("account_status_reason", cols)
            self.assertIn("deleted_at", cols)
        self.assertIsNotNone(payout_table)
        self.assertIsNotNone(action_table)
        self.assertIsNotNone(finance_table)
        self.assertIsNotNone(settings_table)

    def test_finance_ledger_uses_configured_commission(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Test Hirer", "9000000001", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000001",)).fetchone()["id"]
        conn.execute("INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage) VALUES (?,?,?,?,?,?)", ("Test Worker", "9000000002", "x", "Plumber", "Test City", 800))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000002",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, total_amount, status, payment_status, paid_amount, work_amount, diagnosis_fee)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""", (hirer_id, worker_id, "2099-01-01", 2, 500, "completed", "paid", 500, 400, 100))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        result = app.sync_booking_financials(conn, booking_id)
        conn.commit()
        row = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(result["commission_percent"], 10.0)
        self.assertEqual(row["platform_commission"], 50)
        self.assertEqual(row["worker_net"], 450)
        self.assertEqual(row["settlement_status"], "held")

    def test_worker_payout_endpoint_requires_login(self):
        response = self.client.get("/api/worker/payout-account")
        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
