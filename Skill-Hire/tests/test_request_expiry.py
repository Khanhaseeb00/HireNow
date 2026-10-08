import unittest
from datetime import datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
import test_audit_flows as audit
from test_smoke import app,get_db


class RequestExpiryTests(unittest.TestCase):
    def setUp(self):
        self.f=audit.HireNowAuditFlows();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.bid=self.f.create_booking()

    def update(self,sql,*args):
        conn=get_db();conn.execute('UPDATE bookings SET '+sql+' WHERE id=?',(*args,self.bid));conn.commit();conn.close()

    def expire(self):
        self.update('request_expires_at=?',(datetime.utcnow()-timedelta(seconds=1)).isoformat())

    def row(self):
        conn=get_db();row=dict(conn.execute('SELECT * FROM bookings WHERE id=?',(self.bid,)).fetchone());conn.close();return row

    def test_new_booking_deadline_is_thirty_minutes(self):
        deadline=datetime.fromisoformat(self.row()['request_expires_at'])
        self.assertAlmostEqual((deadline-datetime.utcnow()).total_seconds(),1800,delta=3)

    def test_expired_request_releases_slot_and_notifies_each_role_once(self):
        self.expire()
        for _ in range(2):self.assertEqual(self.f.client.get('/api/bookings').status_code,200)
        self.assertEqual(self.row()['status'],'cancelled')
        self.assertEqual(self.row()['payment_status'],'cancelled')
        self.assertTrue(self.row()['request_expired_at'])
        conn=get_db()
        n=conn.execute("SELECT COUNT(*) AS n FROM booking_events WHERE booking_id=? AND note LIKE 'Request expired:%'",(self.bid,)).fetchone()['n']
        self.assertEqual(n,1)
        self.assertIsNone(app.find_worker_schedule_conflict(conn,self.f.worker,'2099-01-02','10:00',2))
        conn.close()
        self.assertNotEqual(self.f.create_booking(),self.bid)

    def test_worker_cannot_accept_or_reject_expired_request(self):
        self.expire();self.f.login('worker',self.f.worker)
        for action in ('accept','reject'):
            r=self.f.client.post(f'/api/worker/bookings/{self.bid}/respond',json={'action':action})
            self.assertEqual(r.status_code,409,r.get_json())
            self.assertTrue(r.get_json()['request_expired'])

    def test_scheduled_start_caps_deadline_even_if_response_window_remains(self):
        self.update("start_date='2000-01-01',start_time='10:00'")
        self.f.client.get('/api/bookings')
        self.assertTrue(self.row()['request_expired_at'])

    def test_legacy_created_timestamp_expires_and_active_request_survives(self):
        self.f.client.get('/api/bookings');self.assertEqual(self.row()['status'],'requested')
        self.update("request_expires_at=NULL,created_at='2000-01-01 00:00:00'")
        self.f.client.get('/api/bookings');self.assertEqual(self.row()['status'],'cancelled')

    def test_accepted_job_never_expires(self):
        self.f.login('worker',self.f.worker)
        self.assertEqual(self.f.client.post(f'/api/worker/bookings/{self.bid}/respond',json={'action':'accept'}).status_code,200)
        self.expire();self.f.client.get('/api/worker/bookings')
        self.assertEqual(self.row()['status'],'confirmed')
        self.assertIsNone(self.row()['request_expired_at'])

    def test_legacy_paid_request_is_refund_case_on_expiry(self):
        self.update("paid_amount=200,payment_method='online',payment_status='paid'")
        self.expire();self.f.client.get('/api/bookings')
        self.assertEqual(self.row()['payment_status'],'refund_pending')
        self.assertEqual(self.row()['paid_amount'],200)

    def test_concurrent_refresh_and_accept_cannot_revive_expired_job(self):
        self.expire()
        def run(index):
            client=app.app.test_client()
            with client.session_transaction() as sess:sess['worker_id' if index else 'hirer_id']=self.f.worker if index else self.f.hirer
            return client.post(f'/api/worker/bookings/{self.bid}/respond',json={'action':'accept'}).status_code if index else client.get('/api/bookings').status_code
        with ThreadPoolExecutor(max_workers=2) as executor:self.assertEqual(list(executor.map(run,[0,1])),[200,409])
        self.assertEqual(self.row()['status'],'cancelled')
