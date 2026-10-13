import unittest
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from test_smoke import app, get_db
import test_audit_flows as audit

class OvertimeTests(unittest.TestCase):
    def setUp(self):
        self.f=audit.HireNowAuditFlows();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.id=self.f.create_booking();self.client=self.f.client
        conn=get_db();conn.execute("UPDATE bookings SET status='checked_in',payment_method='cash',payment_status='cash_pending' WHERE id=?",(self.id,));conn.commit();conn.close()
        self.f.login('worker',self.f.worker)
        self.timer=f'/api/worker/bookings/{self.id}/work-timer'
        self.req=f'/api/worker/bookings/{self.id}/overtime';self.review=f'/api/bookings/{self.id}/overtime'
        self.assertEqual(self.client.post(self.timer,json={'action':'start'}).status_code,200)
    def elapsed(self,minutes):
        conn=get_db();conn.execute('UPDATE bookings SET work_started_at=? WHERE id=?',((datetime.utcnow()-timedelta(minutes=minutes)).isoformat(),self.id));conn.commit();conn.close()
    def request(self):
        r=self.client.post(self.req,json={'minutes':30,'reason':'Repair needs more time'});self.assertEqual(r.status_code,200,r.get_json());return r.get_json()['overtime_request_id']
    def test_no_unapproved_minutes_charged(self):
        self.elapsed(155);r=self.client.post(self.timer,json={'action':'stop'}).get_json()
        self.assertEqual(r['actual_minutes'],155);self.assertEqual(r['billable_minutes'],120);self.assertEqual(r['work_amount'],200)
    def test_approve_then_bill_actual_minutes_at_frozen_rate(self):
        token=self.request();self.f.login('hirer',self.f.hirer)
        r=self.client.post(self.review,json={'decision':'approve','request_id':token});self.assertEqual(r.status_code,200,r.get_json());self.assertEqual(r.get_json()['approved_work_minutes'],150)
        self.assertEqual(self.client.post(self.review,json={'decision':'approve','request_id':token}).status_code,409)
        conn=get_db();conn.execute('UPDATE workers SET daily_wage=8000 WHERE id=?',(self.f.worker,));conn.commit();conn.close()
        self.elapsed(140);self.f.login('worker',self.f.worker);r=self.client.post(self.timer,json={'action':'stop'}).get_json();self.assertEqual(r['billable_minutes'],140);self.assertEqual(r['work_amount'],233)
    def test_decline_and_stale_request_cannot_raise_limit(self):
        token=self.request();self.f.login('hirer',self.f.hirer);self.assertEqual(self.client.post(self.review,json={'decision':'decline','request_id':token}).status_code,200)
        self.f.login('worker',self.f.worker);new=self.request();self.assertNotEqual(token,new)
        self.f.login('hirer',self.f.hirer);self.assertEqual(self.client.post(self.review,json={'decision':'approve','request_id':token}).status_code,409)
        self.elapsed(150);self.f.login('worker',self.f.worker);r=self.client.post(self.timer,json={'action':'stop'}).get_json();self.assertEqual(r['billable_minutes'],120)
    def test_validation_ownership_and_closed_work(self):
        for minutes in [True,0,-15,30.0,'30',121]:self.assertEqual(self.client.post(self.req,json={'minutes':minutes,'reason':'x'}).status_code,400)
        self.assertEqual(self.client.post(self.req,json={'minutes':30,'reason':''}).status_code,400)
        token=self.request();self.assertEqual(self.client.post(self.req,json={'minutes':30,'reason':'x'}).status_code,409)
        self.f.login('hirer',self.f.other_hirer);self.assertEqual(self.client.post(self.review,json={'decision':'approve','request_id':token}).status_code,404)
        self.f.login('worker',self.f.worker);self.assertEqual(self.client.post(self.review,json={'decision':'approve','request_id':token}).status_code,401)
        self.client.post(self.timer,json={'action':'stop'});self.f.login('hirer',self.f.hirer);self.assertEqual(self.client.post(self.review,json={'decision':'approve','request_id':token}).status_code,409)
    def test_concurrent_approval_adds_minutes_once(self):
        token=self.request()
        def approve(_):
            c=app.app.test_client()
            with c.session_transaction() as s:s['hirer_id']=self.f.hirer
            return c.post(self.review,json={'decision':'approve','request_id':token}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(approve,range(2)))
        self.assertEqual(sorted(codes),[200,409])
    def test_legacy_running_timer_keeps_agreement(self):
        conn=get_db();conn.execute('UPDATE bookings SET approved_work_minutes=NULL WHERE id=?',(self.id,));conn.commit();conn.close()
        self.elapsed(150);self.assertEqual(self.client.post(self.req,json={'minutes':30,'reason':'x'}).status_code,409)
        self.assertEqual(self.client.post(self.timer,json={'action':'stop'}).get_json()['billable_minutes'],150)
    def test_extension_respects_next_accepted_job(self):
        conn=get_db();conn.execute("INSERT INTO bookings(hirer_id,worker_id,start_date,start_time,hours,total_amount,status,payment_method) VALUES (?,?,?,'12:10',1,100,'confirmed','cash')",(self.f.hirer,self.f.worker,'2099-01-02'));conn.commit();conn.close()
        token=self.request();self.f.login('hirer',self.f.hirer);r=self.client.post(self.review,json={'decision':'approve','request_id':token});self.assertEqual(r.status_code,409,r.get_json())

    def test_worker_cannot_run_two_timers(self):
        conn=get_db();other=conn.execute("INSERT INTO bookings(hirer_id,worker_id,start_date,start_time,hours,total_amount,status,payment_method,payment_status,agreed_hourly_rate) VALUES (?,?,?,'15:00',1,100,'checked_in','cash','cash_pending',100)",(self.f.hirer,self.f.worker,'2099-01-02')).lastrowid;conn.commit();conn.close()
        self.assertEqual(self.client.post(f'/api/worker/bookings/{other}/work-timer',json={'action':'start'}).status_code,409)
    def test_invalid_timer_input_and_shared_server_clock(self):
        for body in [[],{'action':True},{'action':12}]:self.assertEqual(self.client.post(self.timer,json=body).status_code,400)
        response=self.client.get(f'/api/worker/bookings/{self.id}').get_json();self.assertEqual(response['approved_work_minutes'],120);self.assertTrue(response['server_time'].endswith('Z'))
        self.f.login('hirer',self.f.hirer);response=self.client.get(f'/api/bookings/{self.id}').get_json();self.assertEqual(response['approved_work_minutes'],120);self.assertTrue(response['server_time'].endswith('Z'))
