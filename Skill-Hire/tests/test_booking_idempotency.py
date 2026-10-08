import unittest
from datetime import datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
import test_audit_flows as audit
from test_smoke import app,get_db


class BookingIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.f=audit.HireNowAuditFlows();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.login('hirer',self.f.hirer)
        self.key='request_booking_test_123456'
        self.payload=dict(worker_id=self.f.worker,start_date='2099-01-02',start_time='10:00',hours=2,payment_method='cash',address='Test address')

    def post(self,key=None,payload=None,client=None):
        return (client or self.f.client).post('/api/bookings',json=payload or self.payload,headers={'Idempotency-Key':key or self.key})

    def test_replay_returns_same_booking_without_repeat_notifications(self):
        first=self.post();notifications=app.notify.call_count;second=self.post()
        self.assertEqual(first.status_code,201,first.get_json());self.assertEqual(second.status_code,200,second.get_json())
        bid=first.get_json()['id'];self.assertEqual(second.get_json()['id'],bid);self.assertTrue(second.get_json()['replayed'])
        conn=get_db();self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM bookings WHERE hirer_id=?',(self.f.hirer,)).fetchone()['n'],1)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM booking_events WHERE booking_id=? AND status='requested'",(bid,)).fetchone()['n'],1)
        conn.close();self.assertEqual(app.notify.call_count,notifications)

    def test_reused_key_with_changed_details_is_rejected(self):
        self.assertEqual(self.post().status_code,201)
        r=self.post(payload={**self.payload,'address':'Different address'})
        self.assertEqual(r.status_code,409);self.assertIn('different booking details',r.get_json()['error'])

    def test_concurrent_same_key_returns_one_booking(self):
        def run(_):
            client=app.app.test_client()
            with client.session_transaction() as sess:sess['hirer_id']=self.f.hirer
            r=self.post(client=client);return r.status_code,r.get_json()['id']
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(run,[0,1]))
        self.assertEqual(sorted(x[0] for x in results),[200,201]);self.assertEqual(results[0][1],results[1][1])

    def test_failed_validation_does_not_reserve_key(self):
        self.assertEqual(self.post(payload={**self.payload,'hours':23}).status_code,400)
        self.assertEqual(self.post().status_code,201)

    def test_changed_worker_rate_and_closed_booking_do_not_create_duplicate(self):
        bid=self.post().get_json()['id'];conn=get_db()
        conn.execute("UPDATE workers SET daily_wage=1600,is_online=0 WHERE id=?",(self.f.worker,))
        conn.execute("UPDATE bookings SET status='cancelled',payment_status='cancelled' WHERE id=?",(bid,));conn.commit();conn.close()
        r=self.post();self.assertEqual(r.status_code,200);self.assertEqual(r.get_json()['id'],bid)
        self.assertEqual(r.get_json()['status'],'cancelled');self.assertEqual(r.get_json()['agreed_hourly_rate'],100)

    def test_same_key_is_scoped_to_hirer_and_other_account_cannot_read_booking(self):
        bid=self.post().get_json()['id'];self.f.login('hirer',self.f.other_hirer)
        payload={**self.payload,'start_time':'13:00'};r=self.post(payload=payload)
        self.assertEqual(r.status_code,201,r.get_json());self.assertNotEqual(r.get_json()['id'],bid)

    def test_different_key_does_not_bypass_schedule_conflict(self):
        self.assertEqual(self.post().status_code,201)
        self.assertEqual(self.post(key='another_booking_request_123').status_code,409)

    def test_expired_request_replay_returns_original_cancelled_job(self):
        bid=self.post().get_json()['id'];conn=get_db();conn.execute('UPDATE bookings SET request_expires_at=? WHERE id=?',((datetime.utcnow()-timedelta(seconds=1)).isoformat(),bid));conn.commit();conn.close()
        r=self.post();self.assertEqual(r.status_code,200);self.assertEqual(r.get_json()['id'],bid);self.assertEqual(r.get_json()['status'],'cancelled')

    def test_bad_key_is_rejected(self):
        for key in ('short','bad key with spaces'*2,'x'*129):self.assertEqual(self.post(key=key).status_code,400)
