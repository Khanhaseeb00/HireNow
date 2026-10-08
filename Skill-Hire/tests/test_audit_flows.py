"""Role boundaries and marketplace discovery after a verification upload."""
import io
import unittest
import uuid
from unittest.mock import patch

from test_smoke import app, get_db


class HireNowAuditFlows(unittest.TestCase):
    def setUp(self):
        app.ensure_schema_extensions()
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()
        conn = get_db()
        suffix = uuid.uuid4().hex
        self.hirer = conn.execute(
            "INSERT INTO hirers(name,phone,password_hash) VALUES (?,?,?)",
            ("Audit hirer", suffix + "h", "x"),
        ).lastrowid
        self.other_hirer = conn.execute(
            "INSERT INTO hirers(name,phone,password_hash) VALUES (?,?,?)",
            ("Other hirer", suffix + "o", "x"),
        ).lastrowid
        self.worker = conn.execute(
            "INSERT INTO workers(name,phone,password_hash,skill,city,daily_wage,rate_status) VALUES (?,?,?,?,?,?,?)",
            ("Audit worker", suffix + "w", "x", "Plumber", "Test City", 800, "approved"),
        ).lastrowid
        conn.commit(); conn.close()
        self.notifications = patch.object(app, "notify", return_value=None)
        self.notifications.start()
        self.addCleanup(self.notifications.stop)

    def login(self, role, identifier):
        with self.client.session_transaction() as sess:
            sess.clear(); sess[role + "_id"] = identifier

    def create_booking(self):
        self.login("hirer", self.hirer)
        response = self.client.post("/api/bookings", json={
            "worker_id": self.worker, "start_date": "2099-01-02",
            "start_time": "10:00", "hours": 2, "payment_method": "cash",
            "address": "Test address",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["id"]

    def test_uploaded_id_never_breaks_or_leaks_into_public_discovery(self):
        self.login("worker", self.worker)
        response = self.client.post("/api/worker/verification/upload", data={
            "document": (io.BytesIO(b"%PDF-1.4\nprivate audit identity document"), "private-id.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.login("hirer", self.hirer)
        for endpoint in ("/api/workers", f"/api/workers/{self.worker}"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            worker = next(x for x in payload if x["id"] == self.worker) if isinstance(payload, list) else payload
            for field in ("password_hash", "phone", "id_document_path", "id_document_data",
                          "id_document_name", "id_document_mime", "id_document_uploaded_at",
                          "service_latitude", "service_longitude", "account_status_reason"):
                self.assertNotIn(field, worker)
            self.assertEqual(worker["name"], "Audit worker")
            self.assertEqual(worker["daily_wage"], 800)

    def test_cash_booking_lifecycle_and_role_boundaries(self):
        booking_id = self.create_booking()
        self.login("hirer", self.other_hirer)
        for endpoint in (f"/api/bookings/{booking_id}", f"/api/bookings/{booking_id}/events",
                         f"/api/bookings/{booking_id}/messages"):
            self.assertIn(self.client.get(endpoint).status_code, (401, 404))
        self.assertEqual(self.client.post(f"/api/bookings/{booking_id}/advance-status").status_code, 403)
        self.login("worker", self.worker)
        self.assertEqual(self.client.post(f"/api/worker/bookings/{booking_id}/respond", json={"action": "accept"}).status_code, 200)
        for status in ("en_route", "checked_in"):
            result = self.client.post(f"/api/worker/bookings/{booking_id}/check-in", json={"latitude": 28.6, "longitude": 77.2})
            self.assertEqual(result.get_json()["status"], status)
        for action in ("start", "stop"):
            self.assertEqual(self.client.post(f"/api/worker/bookings/{booking_id}/work-timer", json={"action": action}).status_code, 200)
        self.login("hirer", self.hirer)
        otp = self.client.post(f"/api/bookings/{booking_id}/cash-otp").get_json()["otp"]
        self.login("worker", self.worker)
        result = self.client.post(f"/api/worker/bookings/{booking_id}/verify-cash-otp", json={"otp": otp})
        self.assertEqual(result.get_json()["payment_status"], "paid")
        earnings = self.client.get("/api/worker/earnings-summary").get_json()
        self.assertTrue(any(x["booking_id"] == booking_id for x in earnings["items"]))
        self.assertEqual(self.client.get(f"/api/worker/bookings/{booking_id}/receipt.pdf").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/finance").status_code, 401)

    def test_admin_mutations_require_security_token(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["admin_csrf"] = "audit-token"
        response = self.client.post(f"/api/admin/workers/{self.worker}/rate-review", json={"decision": "approved"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(f"/api/admin/workers/{self.worker}/rate-review", json={"decision": "approved"}, headers={"X-CSRF-Token": "audit-token"})
        self.assertEqual(response.status_code, 200)
