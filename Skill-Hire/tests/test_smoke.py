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
        conn.close()
        for name in ("booking_type", "diagnosis_fee", "diagnosis_distance_km", "diagnosis_notes", "work_approved_at", "work_started_at", "work_ended_at", "actual_minutes", "work_amount"):
            self.assertIn(name, booking_cols)
        for cols in (worker_cols, hirer_cols):
            self.assertIn("account_status", cols)
            self.assertIn("account_status_reason", cols)
            self.assertIn("deleted_at", cols)
        self.assertIsNotNone(payout_table)
        self.assertIsNotNone(action_table)

    def test_worker_payout_endpoint_requires_login(self):
        response = self.client.get("/api/worker/payout-account")
        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
