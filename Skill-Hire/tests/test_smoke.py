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
        for name in ("booking_type", "diagnosis_fee", "diagnosis_distance_km", "diagnosis_notes", "work_approved_at", "work_declined_at", "work_decline_reason", "work_started_at", "work_ended_at", "actual_minutes", "work_amount", "paid_amount"):
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

    def test_gps_stops_at_checked_in_and_timer_completes_work(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Lifecycle Hirer", "9000000101", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000101",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Lifecycle Worker", "9000000102", "x", "Plumber", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000102",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, start_time, hours, booking_type,
                        payment_method, total_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-02", "10:00", 2, "regular", "cash", 200, "confirmed", "cash_pending"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear()
            sess["worker_id"] = worker_id

        first = self.client.post(f"/api/worker/bookings/{booking_id}/check-in", json={"latitude": 1.0, "longitude": 2.0})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["status"], "en_route")
        second = self.client.post(f"/api/worker/bookings/{booking_id}/check-in", json={"latitude": 1.1, "longitude": 2.1})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "checked_in")
        third = self.client.post(f"/api/worker/bookings/{booking_id}/check-in", json={"latitude": 1.2, "longitude": 2.2})
        self.assertEqual(third.status_code, 409)

        started = self.client.post(f"/api/worker/bookings/{booking_id}/work-timer", json={"action": "start"})
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.get_json()["status"], "in_progress")
        stopped = self.client.post(f"/api/worker/bookings/{booking_id}/work-timer", json={"action": "stop"})
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.get_json()["status"], "completed")

        conn = get_db()
        booking = conn.execute("SELECT status, work_started_at, work_ended_at, actual_minutes FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(booking["status"], "completed")
        self.assertIsNotNone(booking["work_started_at"])
        self.assertIsNotNone(booking["work_ended_at"])
        self.assertGreaterEqual(booking["actual_minutes"], 1)

    def test_diagnosis_can_close_without_paid_repair_work(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Diagnosis Hirer", "9000000201", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000201",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Diagnosis Worker", "9000000202", "x", "Electrician", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000202",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, start_time, hours, booking_type,
                        diagnosis_fee, diagnosis_notes, payment_method, total_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-03", "10:00", 1, "diagnosis", 150,
                      "Switchboard needs replacement", "cash", 150, "checked_in", "cash_pending"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear()
            sess["hirer_id"] = hirer_id

        response = self.client.post(f"/api/bookings/{booking_id}/decline-work", json={"reason": "Repair not required today"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["diagnosis_only"])
        self.assertEqual(data["total_amount"], 150)

        conn = get_db()
        booking = conn.execute("SELECT status, total_amount, work_amount, work_declined_at, work_decline_reason FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(booking["status"], "completed")
        self.assertEqual(booking["total_amount"], 150)
        self.assertEqual(booking["work_amount"], 0)
        self.assertIsNotNone(booking["work_declined_at"])
        self.assertEqual(booking["work_decline_reason"], "Repair not required today")

    def test_new_worker_rate_requires_admin_review(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        response = self.client.post("/api/worker-auth/register", json={
            "name": "Pending Rate Worker",
            "phone": "9000000301",
            "password": "secret1",
            "skill": "Plumber",
            "city": "Test City",
            "daily_wage": 900,
        })
        self.assertEqual(response.status_code, 201)
        worker_id = response.get_json()["id"]
        conn = get_db()
        worker = conn.execute("SELECT rate_status FROM workers WHERE id=?", (worker_id,)).fetchone()
        conn.close()
        self.assertEqual(worker["rate_status"], "pending")

        public = self.client.get("/api/workers")
        self.assertEqual(public.status_code, 200)
        self.assertNotIn(worker_id, [w["id"] for w in public.get_json()])

    def test_worker_payout_endpoint_requires_login(self):
        response = self.client.get("/api/worker/payout-account")
        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
