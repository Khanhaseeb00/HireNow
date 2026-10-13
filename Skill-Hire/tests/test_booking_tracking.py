import unittest
import uuid
from unittest.mock import patch
from test_smoke import app, get_db
import test_audit_flows as audit_flows


class BookingTrackingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=audit_flows.HireNowAuditFlows()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.booking=self.fixture.create_booking()
        self.client=self.fixture.client

    def test_assigned_worker_can_read_booking_rate_and_timer_without_provider_call(self):
        self.fixture.login('worker',self.fixture.worker)
        conn=get_db()
        conn.execute("UPDATE bookings SET status='in_progress',work_started_at='2026-10-13T12:00:00',razorpay_order_id='order_tracking' WHERE id=?",(self.booking,))
        conn.commit();conn.close()
        with patch.object(app.payments,'fetch_order') as provider:
            response=self.client.get(f'/api/worker/bookings/{self.booking}')
            self.assertEqual(response.status_code,200,response.get_json())
            self.assertEqual(response.get_json()['agreed_hourly_rate'],100)
            self.assertEqual(response.get_json()['work_started_at'],'2026-10-13T12:00:00')
            self.assertNotIn('cash_otp_hash',response.get_json())
            provider.assert_not_called()

    def test_other_worker_cannot_read_job(self):
        conn=get_db()
        other=conn.execute('INSERT INTO workers(name,phone,password_hash,skill,city,daily_wage,rate_status) VALUES (?,?,?,?,?,?,?)',('Other worker',uuid.uuid4().hex,'x','Plumber','Test',800,'approved')).lastrowid
        conn.commit();conn.close()
        self.fixture.login('worker',other)
        self.assertEqual(self.client.get(f'/api/worker/bookings/{self.booking}').status_code,404)

    def test_hirer_session_cannot_use_worker_tracking_endpoint(self):
        self.fixture.login('hirer',self.fixture.hirer)
        self.assertEqual(self.client.get(f'/api/worker/bookings/{self.booking}').status_code,401)
