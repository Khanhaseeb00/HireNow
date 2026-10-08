import os
import time
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
import test_audit_flows as audit
from test_smoke import app,get_db


FIXED=datetime(2030,1,1,20,0,tzinfo=timezone.utc)

class Clock(datetime):
    @classmethod
    def now(cls,tz=None):
        return FIXED.astimezone(tz) if tz else FIXED.astimezone().replace(tzinfo=None)
    @classmethod
    def utcnow(cls):
        return FIXED.replace(tzinfo=None)


class ServiceTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.f=audit.HireNowAuditFlows();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.login('hirer',self.f.hirer)
        self.clock=patch.object(app,'datetime',Clock);self.clock.start();self.addCleanup(self.clock.stop)

    def test_service_date_does_not_follow_server_timezone(self):
        previous=os.environ.get('TZ')
        try:
            for zone in ('UTC','Asia/Riyadh','America/Los_Angeles'):
                os.environ['TZ']=zone;time.tzset()
                self.assertEqual(app.service_now(),datetime(2030,1,2,1,30))
        finally:
            if previous is None:os.environ.pop('TZ',None)
            else:os.environ['TZ']=previous
            time.tzset()

    def test_schedule_conversion_crosses_utc_date_boundary(self):
        self.assertEqual(app.schedule_utc(datetime(2030,1,2,1,35)),datetime(2030,1,1,20,5))

    def test_previous_india_day_rejected_even_while_utc_today(self):
        r=self.f.client.post('/api/bookings',json=dict(worker_id=self.f.worker,start_date='2030-01-01',start_time='23:00',hours=1))
        self.assertEqual(r.status_code,400)

    def test_near_start_deadline_and_late_accept_use_india_clock(self):
        with patch.object(app,'worker_available_for_slot',return_value=(True,None)):
            r=self.f.client.post('/api/bookings',json=dict(worker_id=self.f.worker,start_date='2030-01-02',start_time='01:35',hours=1,payment_method='cash'))
        self.assertEqual(r.status_code,201,r.get_json())
        self.assertEqual(r.get_json()['request_expires_at'],'2030-01-01T20:05:00Z')
        bid=r.get_json()['id']
        conn=get_db();conn.execute("UPDATE bookings SET start_time='01:25' WHERE id=?",(bid,));conn.commit();conn.close()
        self.f.login('worker',self.f.worker)
        response=self.f.client.post(f'/api/worker/bookings/{bid}/respond',json={'action':'accept'})
        self.assertEqual(response.status_code,409,response.get_json())
        self.assertTrue(response.get_json()['request_expired'])

    def test_slots_filter_service_today_and_return_timezone(self):
        old=self.f.client.get(f'/api/workers/{self.f.worker}/available-slots?date=2030-01-01')
        self.assertEqual(old.get_json()['slots'],[])
        today=self.f.client.get(f'/api/workers/{self.f.worker}/available-slots?date=2030-01-02')
        self.assertEqual(today.get_json()['slots'][0],'08:00 AM')
        self.assertEqual(today.get_json()['service_timezone'],'Asia/Kolkata')

    def test_hirer_and_worker_receive_same_service_timezone(self):
        for route in ('/','/worker'):
            html=self.f.client.get(route).get_data(as_text=True)
            self.assertIn('data-service-timezone="Asia/Kolkata"',html)
