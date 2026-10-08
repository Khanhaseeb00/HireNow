import os
import tempfile
import unittest
from unittest.mock import patch

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
        self.assertEqual(
            [(float(x["up_to_km"]), int(x["fee"])) for x in data["slabs"]],
            [(5.0, 100), (10.0, 150), (20.0, 250)],
        )
        self.assertEqual(data["distance_method"], "gps_straight_line")
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
        adjustment_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payment_adjustments'").fetchone()
        diagnosis_pricing_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='diagnosis_pricing_rules'").fetchone()
        conn.close()
        for name in ("booking_type", "diagnosis_fee", "diagnosis_distance_km", "service_latitude", "service_longitude", "diagnosis_pricing_rule_id", "diagnosis_notes", "work_approved_at", "work_declined_at", "work_decline_reason", "work_started_at", "work_ended_at", "actual_minutes", "work_amount", "paid_amount"):
            self.assertIn(name, booking_cols)
        for name in ("service_latitude", "service_longitude", "service_location_updated_at", "preferred_language", "preferred_theme", "notifications_enabled"):
            self.assertIn(name, worker_cols)
        for name in ("preferred_language", "preferred_theme", "notifications_enabled", "home_address", "home_city", "home_latitude", "home_longitude", "location_updated_at"):
            self.assertIn(name, hirer_cols)
        for cols in (worker_cols, hirer_cols):
            self.assertIn("account_status", cols)
            self.assertIn("account_status_reason", cols)
            self.assertIn("deleted_at", cols)
        self.assertIsNotNone(payout_table)
        self.assertIsNotNone(action_table)
        self.assertIsNotNone(finance_table)
        self.assertIsNotNone(settings_table)
        self.assertIsNotNone(adjustment_table)
        self.assertIsNotNone(diagnosis_pricing_table)

    def test_haversine_distance_and_scoped_diagnosis_pricing(self):
        self.assertAlmostEqual(app.haversine_distance_km(0, 0, 0, 1), 111.20, delta=0.2)
        conn = get_db()
        conn.execute("DELETE FROM diagnosis_pricing_rules")
        now = "2099-01-01T00:00:00"
        conn.execute("""INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at)
                        VALUES (NULL,NULL,5,100,1,?,?)""", (now, now))
        conn.execute("""INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at)
                        VALUES (NULL,NULL,10,150,1,?,?)""", (now, now))
        conn.execute("""INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at)
                        VALUES ('Test City','Plumber',5,120,1,?,?)""", (now, now))
        conn.execute("""INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at)
                        VALUES ('Test City','Plumber',10,180,1,?,?)""", (now, now))
        conn.commit()
        self.assertEqual(app.diagnosis_fee_for_distance(4, conn=conn, city="Test City", skill="Plumber"), (120, 5.0))
        self.assertEqual(app.diagnosis_fee_for_distance(8, conn=conn, city="Test City", skill="Plumber"), (180, 10.0))
        self.assertEqual(app.diagnosis_fee_for_distance(4, conn=conn, city="Other", skill="Cleaner"), (100, 5.0))
        conn.close()

    def test_gps_diagnosis_quote_is_server_computed(self):
        conn = get_db()
        conn.execute("DELETE FROM diagnosis_pricing_rules")
        now = "2099-01-01T00:00:00"
        for km, fee in ((5,100),(10,150),(20,250)):
            conn.execute("""INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at)
                            VALUES (NULL,NULL,?,?,1,?,?)""", (km, fee, now, now))
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status,
                        service_latitude, service_longitude, service_location_updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     ("GPS Worker", "9000000602", "x", "Plumber", "Test City", 800, "approved",
                      28.6139, 77.2090, now))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000602",)).fetchone()["id"]
        worker = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        quote, error = app.diagnosis_quote_for_worker(conn, worker, 28.6200, 77.2090)
        conn.commit()
        conn.close()
        self.assertIsNone(error)
        self.assertLess(quote["distance_km"], 5)
        self.assertEqual(quote["fee"], 100)
        self.assertIsNotNone(quote["rule_id"])

        response = self.client.get(
            f"/api/pricing/diagnosis?worker_id={worker_id}&latitude=28.6200&longitude=77.2090"
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["quote"]["fee"], 100)
        self.assertLess(data["quote"]["distance_km"], 5)

    def test_diagnosis_booking_uses_server_gps_quote_not_client_distance(self):
        conn = get_db()
        conn.execute("DELETE FROM diagnosis_pricing_rules")
        now = "2099-01-01T00:00:00"
        for km, fee in ((5,100),(10,150),(20,250)):
            conn.execute("""INSERT INTO diagnosis_pricing_rules(city, skill, max_km, fee, is_active, created_at, updated_at)
                            VALUES (NULL,NULL,?,?,1,?,?)""", (km, fee, now, now))
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("GPS Booking Hirer", "9000000610", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000610",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status,
                        service_latitude, service_longitude, service_location_updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     ("GPS Booking Worker", "9000000611", "x", "Plumber", "Test City", 800, "approved",
                      28.6139, 77.2090, now))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000611",)).fetchone()["id"]
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear()
            sess["hirer_id"] = hirer_id

        with patch.object(app, "worker_available_for_slot", return_value=(True, None)),              patch.object(app, "find_worker_schedule_conflict", return_value=None),              patch.object(app, "notify", return_value=None):
            response = self.client.post("/api/bookings", json={
                "worker_id": worker_id,
                "start_date": "2099-01-10",
                "start_time": "10:00",
                "end_time": "11:00",
                "booking_type": "diagnosis",
                "payment_method": "cash",
                "address": "Test service address",
                "service_latitude": 28.6200,
                "service_longitude": 77.2090,
                "distance_km": 9999,
            })
        self.assertEqual(response.status_code, 201, response.get_json())
        data = response.get_json()
        self.assertEqual(data["diagnosis_fee"], 100)
        self.assertLess(data["diagnosis_distance_km"], 5)
        booking_id = data["id"]
        conn = get_db()
        booking = conn.execute("""SELECT diagnosis_fee, diagnosis_distance_km, service_latitude,
                                  service_longitude, diagnosis_pricing_rule_id
                                  FROM bookings WHERE id=?""", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(booking["diagnosis_fee"], 100)
        self.assertLess(booking["diagnosis_distance_km"], 5)
        self.assertAlmostEqual(booking["service_latitude"], 28.6200, places=4)
        self.assertIsNotNone(booking["diagnosis_pricing_rule_id"])

    def test_public_worker_payload_hides_exact_service_coordinates(self):
        conn = get_db()
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status,
                        service_latitude, service_longitude)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     ("Private GPS Worker", "9000000603", "x", "Cleaner", "Test City", 800, "approved", 25.2, 55.3))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000603",)).fetchone()["id"]
        conn.commit(); conn.close()
        response = self.client.get(f"/api/workers/{worker_id}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["service_location_configured"])
        self.assertNotIn("service_latitude", data)
        self.assertNotIn("service_longitude", data)

    def test_legacy_worker_schema_is_extended_with_service_location(self):
        conn = get_db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
        for name in ("service_location_updated_at", "service_longitude", "service_latitude"):
            if name in cols:
                conn.execute(f"ALTER TABLE workers DROP COLUMN {name}")
        conn.commit(); conn.close()

        app.ensure_schema_extensions()

        conn = get_db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(workers)").fetchall()}
        conn.close()
        self.assertIn("service_latitude", cols)
        self.assertIn("service_longitude", cols)
        self.assertIn("service_location_updated_at", cols)

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
        conn.execute("UPDATE bookings SET agreed_hourly_rate=100 WHERE id=?", (booking_id,))
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

    def test_payment_reconciliation_tracks_balance_and_refund(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Reconcile Hirer", "9000000401", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000401",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Reconcile Worker", "9000000402", "x", "Plumber", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000402",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-04", 2, "online", 600, 500, 600, "completed", "balance_due"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        balance = app.sync_payment_adjustment(conn, booking_id)
        conn.commit()
        self.assertEqual(balance["type"], "balance_due")
        self.assertEqual(balance["amount"], 100)

        conn.execute("UPDATE bookings SET total_amount=450, paid_amount=500, payment_status='refund_pending' WHERE id=?", (booking_id,))
        refund = app.sync_payment_adjustment(conn, booking_id)
        conn.commit()
        self.assertEqual(refund["type"], "refund")
        self.assertEqual(refund["amount"], 50)

        pending_balance = conn.execute("""SELECT COUNT(*) AS n FROM payment_adjustments
                                          WHERE booking_id=? AND adjustment_type='balance_due' AND status='pending'""", (booking_id,)).fetchone()["n"]
        pending_refund = conn.execute("""SELECT COUNT(*) AS n FROM payment_adjustments
                                         WHERE booking_id=? AND adjustment_type='refund' AND status='pending'""", (booking_id,)).fetchone()["n"]
        self.assertEqual(pending_balance, 0)
        self.assertEqual(pending_refund, 1)

        conn.execute("UPDATE bookings SET total_amount=500, paid_amount=500, payment_status='paid' WHERE id=?", (booking_id,))
        none_left = app.sync_payment_adjustment(conn, booking_id)
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) AS n FROM payment_adjustments WHERE booking_id=? AND status='pending'", (booking_id,)).fetchone()["n"]
        conn.close()
        self.assertIsNone(none_left)
        self.assertEqual(remaining, 0)

    def test_payment_summary_reports_outstanding_adjustment(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Summary Hirer", "9000000501", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000501",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Summary Worker", "9000000502", "x", "Electrician", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000502",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-05", 2, "online", 700, 500, "completed", "balance_due"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        app.sync_payment_adjustment(conn, booking_id)
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear()
            sess["hirer_id"] = hirer_id
        response = self.client.get(f"/api/bookings/{booking_id}/payment-summary")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total_amount"], 700)
        self.assertEqual(data["paid_amount"], 500)
        self.assertEqual(data["adjustment"]["adjustment_type"], "balance_due")
        self.assertEqual(data["adjustment"]["amount"], 200)

    def test_provider_reconciliation_repairs_stale_paid_booking(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Pay Hirer", "9000000701", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000701",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Pay Worker", "9000000702", "x", "Plumber", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000702",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, status, payment_status, razorpay_order_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-06", 2, "online", 500, 0, "confirmed", "pending", "order_test_1"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

        with patch.object(app.payments, "fetch_order", return_value={"id":"order_test_1","currency":"INR","status":"paid","amount_paid":50000}), \
             patch.object(app.payments, "fetch_order_payments", return_value=[{"id":"pay_test_1","order_id":"order_test_1","currency":"INR","status":"captured","amount":50000,"created_at":1}]):
            result = app.reconcile_online_booking_payment(conn, booking, notify_users=False)
        conn.commit()
        refreshed = conn.execute("SELECT payment_status, paid_amount, payment_id FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()

        self.assertTrue(result["changed"])
        self.assertEqual(refreshed["payment_status"], "paid")
        self.assertEqual(refreshed["paid_amount"], 500)
        self.assertEqual(refreshed["payment_id"], "pay_test_1")

    def test_hirer_and_worker_pdf_receipts_after_payment(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Receipt Hirer", "9000000711", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000711",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Receipt Worker", "9000000712", "x", "Electrician", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000712",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status, payment_id, razorpay_order_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-07", 2, "online", 500, 500, 500, "completed", "paid", "pay_receipt_1", "order_receipt_1"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        app.sync_booking_financials(conn, booking_id)
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear(); sess["hirer_id"] = hirer_id
        hirer_pdf = self.client.get(f"/api/bookings/{booking_id}/receipt.pdf")
        self.assertEqual(hirer_pdf.status_code, 200)
        self.assertEqual(hirer_pdf.mimetype, "application/pdf")
        self.assertTrue(hirer_pdf.data.startswith(b"%PDF"))

        with self.client.session_transaction() as sess:
            sess.clear(); sess["worker_id"] = worker_id
        worker_pdf = self.client.get(f"/api/worker/bookings/{booking_id}/receipt.pdf")
        self.assertEqual(worker_pdf.status_code, 200)
        self.assertEqual(worker_pdf.mimetype, "application/pdf")
        self.assertTrue(worker_pdf.data.startswith(b"%PDF"))

    def test_cash_paid_booking_is_immediately_settled_and_no_adjustment(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Cash Hirer", "9000000801", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000801",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Cash Worker", "9000000802", "x", "Painter", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000802",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status, cash_verified_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-08", 2, "cash", 248, 248, 248, "completed", "paid", "2099-01-08T12:00:00"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        finance = app.sync_booking_financials(conn, booking_id)
        conn.commit()
        row = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
        adjustments = conn.execute("SELECT COUNT(*) AS n FROM payment_adjustments WHERE booking_id=? AND status='pending'", (booking_id,)).fetchone()["n"]
        conn.close()
        self.assertEqual(finance["settlement_status"], "settled")
        self.assertEqual(row["settlement_status"], "settled")
        self.assertEqual(row["settlement_reference"], "cash_otp")
        self.assertEqual(row["payment_collected"], 248)
        self.assertEqual(adjustments, 0)

    def test_cancelled_unpaid_booking_has_no_payment_pending(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Cancel Hirer", "9000000811", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000811",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Cancel Worker", "9000000812", "x", "Painter", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000812",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-09", 2, "online", 248, 0, "confirmed", "pending"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear()
            sess["hirer_id"] = hirer_id
        response = self.client.post(f"/api/bookings/{booking_id}/cancel", json={"reason":"No longer needed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["payment_status"], "cancelled")

        conn = get_db()
        booking = conn.execute("SELECT status, payment_status, paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
        pending = conn.execute("SELECT COUNT(*) AS n FROM payment_adjustments WHERE booking_id=? AND status='pending'", (booking_id,)).fetchone()["n"]
        conn.close()
        self.assertEqual(booking["status"], "cancelled")
        self.assertEqual(booking["payment_status"], "cancelled")
        self.assertEqual(booking["paid_amount"], 0)
        self.assertEqual(pending, 0)

    def test_legacy_paid_amount_repair_and_cash_settlement(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Legacy Hirer", "9000000821", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000821",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Legacy Worker", "9000000822", "x", "Painter", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000822",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status, cash_verified_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-10", 2, "cash", 750, 0, 750, "completed", "paid", "2099-01-10T12:00:00"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.execute("""INSERT INTO booking_financials(booking_id, worker_id, gross_amount, work_amount,
                        platform_commission, worker_net, payment_collected, adjustment_amount, settlement_status)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (booking_id, worker_id, 750, 750, 75, 675, 0, -750, "pending"))
        conn.commit(); conn.close()

        app.ensure_schema_extensions()

        conn = get_db()
        booking = conn.execute("SELECT paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
        finance = conn.execute("SELECT payment_collected, adjustment_amount, settlement_status, settlement_reference FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(booking["paid_amount"], 750)
        self.assertEqual(finance["payment_collected"], 750)
        self.assertEqual(finance["adjustment_amount"], 0)
        self.assertEqual(finance["settlement_status"], "settled")
        self.assertEqual(finance["settlement_reference"], "cash_otp")

    def test_completed_cash_booking_can_switch_to_online_payment(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Switch Hirer", "9000000901", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000901",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Switch Worker", "9000000902", "x", "Painter", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000902",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-11", 2, "cash", 500, 0, 500, "completed", "cash_pending"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear(); sess["hirer_id"] = hirer_id

        fake_order={"id":"order_switch_1","amount":50000,"currency":"INR"}
        with patch.object(app.payments, "create_order", return_value=fake_order):
            response = self.client.post(f"/api/bookings/{booking_id}/create-order")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["order_id"], "order_switch_1")

        conn = get_db()
        booking = conn.execute("SELECT razorpay_order_id, payment_method, payment_status FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(booking["razorpay_order_id"], "order_switch_1")
        self.assertEqual(booking["payment_method"], "cash")
        self.assertEqual(booking["payment_status"], "cash_pending")

    def test_completed_online_selected_booking_can_choose_cash_if_unpaid(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Cash Choice Hirer", "9000000911", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000911",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Cash Choice Worker", "9000000912", "x", "Painter", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000912",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-12", 2, "online", 500, 0, 500, "completed", "pending"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear(); sess["hirer_id"] = hirer_id
        response = self.client.post(f"/api/bookings/{booking_id}/cash-otp")
        self.assertEqual(response.status_code, 200, response.get_json())

        conn = get_db()
        booking = conn.execute("SELECT payment_method, payment_status, cash_otp_hash FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(booking["payment_method"], "cash")
        self.assertEqual(booking["payment_status"], "cash_pending")
        self.assertIsNotNone(booking["cash_otp_hash"])

    def test_online_verification_switches_completed_cash_booking_to_online(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Verify Switch Hirer", "9000000921", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000921",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Verify Switch Worker", "9000000922", "x", "Painter", "Test City", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000922",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status, razorpay_order_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-13", 2, "cash", 500, 0, 500, "completed", "cash_pending", "order_switch_verify"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.commit(); conn.close()

        with self.client.session_transaction() as sess:
            sess.clear(); sess["hirer_id"] = hirer_id
        with patch.object(app.payments, "verify_checkout_signature", return_value=True), \
             patch.object(app.payments, "fetch_order", return_value={"id":"order_switch_verify","currency":"INR"}), \
             patch.object(app.payments, "fetch_order_payments", return_value=[{"id":"pay_switch_verify","order_id":"order_switch_verify","currency":"INR","status":"captured","amount":50000}]), \
             patch.object(app, "notify", return_value=None):
            response = self.client.post("/api/payments/verify", json={
                "razorpay_order_id":"order_switch_verify",
                "razorpay_payment_id":"pay_switch_verify",
                "razorpay_signature":"sig",
            })
        self.assertEqual(response.status_code, 200, response.get_json())

        conn = get_db()
        booking = conn.execute("SELECT payment_method, payment_status, paid_amount, payment_id FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        self.assertEqual(booking["payment_method"], "online")
        self.assertEqual(booking["payment_status"], "paid")
        self.assertEqual(booking["paid_amount"], 500)
        self.assertEqual(booking["payment_id"], "pay_switch_verify")

    def test_receipt_uses_selected_language(self):
        conn = get_db()
        conn.execute("""INSERT INTO hirers(name, phone, password_hash, preferred_language)
                        VALUES (?,?,?,?)""", ("Hindi Hirer", "9000001011", "x", "hi"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000001011",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status, preferred_language)
                        VALUES (?,?,?,?,?,?,?,?)""", ("Hindi Worker", "9000001012", "x", "Electrician", "Balrampur", 800, "approved", "hi"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000001012",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, hours, payment_method,
                        total_amount, paid_amount, work_amount, status, payment_status)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-21", 2, "cash", 500, 500, 500, "completed", "paid"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        app.sync_booking_financials(conn, booking_id)
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        hirer = conn.execute("SELECT name, phone, preferred_language FROM hirers WHERE id=?", (hirer_id,)).fetchone()
        worker = conn.execute("SELECT name, phone, skill, city, preferred_language FROM workers WHERE id=?", (worker_id,)).fetchone()
        finance = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
        conn.close()
        with patch.object(app, "receipt_font_for_language", return_value="Helvetica"):
            pdf_buffer = app.build_payment_receipt_pdf(booking, hirer, worker, finance, "hirer")
        self.assertTrue(pdf_buffer.getvalue().startswith(b"%PDF"))
        self.assertEqual(app.receipt_text("hi", "payment_receipt"), "भुगतान रसीद")

    def test_branded_receipt_contains_professional_contact_details(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Brand Hirer", "9000000991", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000000991",)).fetchone()["id"]
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Brand Worker", "9000000992", "x", "Plumber", "Balrampur", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000000992",)).fetchone()["id"]
        conn.execute("""INSERT INTO bookings(hirer_id, worker_id, start_date, start_time, end_time, hours, address,
                        special_instructions, booking_type, payment_method, total_amount, paid_amount, work_amount,
                        actual_minutes, status, payment_status, payment_id, razorpay_order_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (hirer_id, worker_id, "2099-01-20", "10:00", "12:00", 2, "Jarwa Road, Tulsipur",
                      "Test receipt details", "regular", "online", 500, 500, 500, 120, "completed", "paid",
                      "pay_brand_1", "order_brand_1"))
        booking_id = conn.execute("SELECT id FROM bookings ORDER BY id DESC LIMIT 1").fetchone()["id"]
        app.sync_booking_financials(conn, booking_id)
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        hirer = conn.execute("SELECT name, phone FROM hirers WHERE id=?", (hirer_id,)).fetchone()
        worker = conn.execute("SELECT name, phone, skill, city FROM workers WHERE id=?", (worker_id,)).fetchone()
        finance = conn.execute("SELECT * FROM booking_financials WHERE booking_id=?", (booking_id,)).fetchone()
        conn.close()

        pdf_buffer = app.build_payment_receipt_pdf(booking, hirer, worker, finance, "hirer")
        pdf_bytes = pdf_buffer.getvalue()
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 5000)

    def test_hirer_registration_saves_map_location_and_preferences(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        response = self.client.post("/api/auth/register", json={
            "name": "Map Hirer",
            "phone": "9000001001",
            "password": "secret1",
            "address": "Jarwa Road, Tulsipur",
            "city": "Balrampur",
            "latitude": 27.533,
            "longitude": 82.417,
            "preferred_language": "hi",
            "preferred_theme": "dark",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        data = response.get_json()
        self.assertEqual(data["preferred_language"], "hi")
        self.assertEqual(data["preferred_theme"], "dark")
        self.assertAlmostEqual(data["home_latitude"], 27.533, places=3)

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        me_data = me.get_json()
        self.assertEqual(me_data["home_city"], "Balrampur")
        self.assertEqual(me_data["preferred_language"], "hi")

    def test_hirer_location_and_preferences_can_be_updated(self):
        conn = get_db()
        conn.execute("INSERT INTO hirers(name, phone, password_hash) VALUES (?,?,?)", ("Prefs Hirer", "9000001002", "x"))
        hirer_id = conn.execute("SELECT id FROM hirers WHERE phone=?", ("9000001002",)).fetchone()["id"]
        conn.commit(); conn.close()
        with self.client.session_transaction() as sess:
            sess.clear(); sess["hirer_id"] = hirer_id

        pref = self.client.put("/api/hirer/preferences", json={"preferred_language":"ur","preferred_theme":"dark","notifications_enabled":False})
        self.assertEqual(pref.status_code, 200)
        self.assertEqual(pref.get_json()["preferred_language"], "ur")
        loc = self.client.put("/api/hirer/location", json={"address":"Test Area","city":"Lucknow","latitude":26.8467,"longitude":80.9462})
        self.assertEqual(loc.status_code, 200)
        self.assertAlmostEqual(loc.get_json()["home_longitude"], 80.9462, places=4)

    def test_worker_preferences_are_persisted(self):
        conn = get_db()
        conn.execute("""INSERT INTO workers(name, phone, password_hash, skill, city, daily_wage, rate_status)
                        VALUES (?,?,?,?,?,?,?)""", ("Prefs Worker", "9000001003", "x", "Plumber", "Balrampur", 800, "approved"))
        worker_id = conn.execute("SELECT id FROM workers WHERE phone=?", ("9000001003",)).fetchone()["id"]
        conn.commit(); conn.close()
        with self.client.session_transaction() as sess:
            sess.clear(); sess["worker_id"] = worker_id

        response = self.client.put("/api/worker/preferences", json={"preferred_language":"bn","preferred_theme":"dark","notifications_enabled":False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["preferred_language"], "bn")
        me = self.client.get("/api/worker-auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["preferred_theme"], "dark")
        self.assertFalse(bool(me.get_json()["notifications_enabled"]))

    def test_worker_payout_endpoint_requires_login(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        response = self.client.get("/api/worker/payout-account")
        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
