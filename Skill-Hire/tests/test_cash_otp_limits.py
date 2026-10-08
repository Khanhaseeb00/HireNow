import unittest
from datetime import datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
import test_audit_flows as audit
from test_smoke import app,get_db


class CashOTPLimitTests(unittest.TestCase):
    def setUp(self):
        self.f=audit.HireNowAuditFlows();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.bid=self.f.create_booking();self.update("status='completed',payment_status='cash_pending'")
        self.otp=self.generate().get_json()['otp'];self.bad='000000' if self.otp!='000000' else '999999'

    def update(self,sql,*args):
        conn=get_db();conn.execute('UPDATE bookings SET '+sql+' WHERE id=?',(*args,self.bid));conn.commit();conn.close()

    def row(self):
        conn=get_db();row=dict(conn.execute('SELECT * FROM bookings WHERE id=?',(self.bid,)).fetchone());conn.close();return row

    def generate(self):
        self.f.login('hirer',self.f.hirer);return self.f.client.post(f'/api/bookings/{self.bid}/cash-otp')

    def verify(self,otp=None):
        self.f.login('worker',self.f.worker);return self.f.client.post(f'/api/worker/bookings/{self.bid}/verify-cash-otp',json={'otp':otp or self.bad})

    def lock(self):
        for _ in range(4):self.assertEqual(self.verify().status_code,400)
        return self.verify()

    def test_five_wrong_attempts_lock_and_never_mark_paid(self):
        for left in (4,3,2,1):
            r=self.verify();self.assertEqual(r.get_json()['attempts_remaining'],left)
        r=self.verify();self.assertEqual(r.status_code,429);self.assertEqual(r.headers['Retry-After'],'900')
        self.assertIsNone(self.row()['cash_otp_hash']);self.assertEqual(self.row()['paid_amount'],0)
        self.assertEqual(self.verify(self.otp).status_code,429)
        self.assertEqual(self.generate().status_code,429)

    def test_after_lock_wait_fresh_otp_is_required_and_can_verify(self):
        self.lock();self.update('cash_otp_locked_until=?,cash_otp_issued_at=?',(datetime.utcnow()-timedelta(seconds=1)).isoformat(),(datetime.utcnow()-timedelta(minutes=16)).isoformat())
        self.assertEqual(self.verify(self.otp).status_code,400)
        fresh=self.generate().get_json()['otp'];self.assertEqual(self.row()['cash_otp_attempts'],0)
        self.assertEqual(self.verify(fresh).status_code,200)
        self.assertEqual(self.row()['payment_status'],'paid');self.assertIsNone(self.row()['cash_otp_locked_until'])
        self.assertEqual(self.verify(fresh).status_code,400)

    def test_regeneration_cooldown_and_attempts_survive_new_code(self):
        self.assertEqual(self.generate().status_code,429)
        self.verify();self.verify();self.update('cash_otp_issued_at=?',(datetime.utcnow()-timedelta(seconds=61)).isoformat())
        self.assertEqual(self.generate().status_code,200);self.assertEqual(self.row()['cash_otp_attempts'],2)

    def test_concurrent_wrong_attempts_cannot_bypass_limit(self):
        for _ in range(4):self.verify()
        def run(_):
            client=app.app.test_client()
            with client.session_transaction() as sess:sess['worker_id']=self.f.worker
            return client.post(f'/api/worker/bookings/{self.bid}/verify-cash-otp',json={'otp':self.bad}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:self.assertEqual(list(executor.map(run,[0,1])),[429,429])
        self.assertEqual(self.row()['cash_otp_attempts'],5)
        conn=get_db();n=conn.execute("SELECT COUNT(*) AS n FROM booking_events WHERE booking_id=? AND status='cash_otp_locked'",(self.bid,)).fetchone()['n'];conn.close();self.assertEqual(n,1)

    def test_invalid_payloads_and_expired_code_never_verify(self):
        self.f.login('worker',self.f.worker)
        for payload in ({'otp':123456},{'otp':{}},{'otp':'abcdef'},[],{'otp':'１２３４５６'}):
            self.assertEqual(self.f.client.post(f'/api/worker/bookings/{self.bid}/verify-cash-otp',json=payload).status_code,400)
        self.assertEqual(self.row()['cash_otp_attempts'],0)
        self.update('cash_otp_expires_at=?',(datetime.utcnow()-timedelta(seconds=1)).isoformat())
        self.assertEqual(self.verify(self.otp).status_code,400);self.assertEqual(self.row()['paid_amount'],0)

    def test_otp_hash_is_not_exposed_to_either_role(self):
        self.f.login('hirer',self.f.hirer)
        for route in ('/api/bookings',f'/api/bookings/{self.bid}'):
            data=self.f.client.get(route).get_json();row=next(x for x in data if x['id']==self.bid) if isinstance(data,list) else data
            self.assertNotIn('cash_otp_hash',row)
        self.f.login('worker',self.f.worker)
        self.assertTrue(all('cash_otp_hash' not in x for x in self.f.client.get('/api/worker/bookings').get_json()))

    def test_success_resets_attempts_and_other_hirer_cannot_regenerate(self):
        self.verify();self.assertEqual(self.row()['cash_otp_attempts'],1)
        self.f.login('hirer',self.f.other_hirer)
        self.assertEqual(self.f.client.post(f'/api/bookings/{self.bid}/cash-otp').status_code,404)
        self.assertEqual(self.verify(self.otp).status_code,200)
        self.assertEqual(self.row()['cash_otp_attempts'],0)

    def test_concurrent_generations_issue_only_one_new_code(self):
        self.update('cash_otp_issued_at=?',(datetime.utcnow()-timedelta(seconds=61)).isoformat())
        def run(_):
            client=app.app.test_client()
            with client.session_transaction() as sess:sess['hirer_id']=self.f.hirer
            return client.post(f'/api/bookings/{self.bid}/cash-otp').status_code
        with ThreadPoolExecutor(max_workers=2) as executor:self.assertEqual(sorted(executor.map(run,[0,1])),[200,429])
