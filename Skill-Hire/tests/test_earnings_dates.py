import unittest
from datetime import datetime
from unittest.mock import patch
import test_audit_flows as audit
from test_smoke import app,get_db


class EarningsDateTests(unittest.TestCase):
    def setUp(self):
        self.f=audit.HireNowAuditFlows();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.bid=self.f.create_booking()
        self.change("status='completed',payment_status='paid',paid_amount=200,work_ended_at='2026-10-08T20:00:00'")
        self.f.login('worker',self.f.worker)
        self.clock=patch.object(app,'service_now',return_value=datetime(2026,10,9,1,30));self.clock.start();self.addCleanup(self.clock.stop)

    def change(self,sql):
        conn=get_db();conn.execute('UPDATE bookings SET '+sql+' WHERE id=?',(self.bid,));app.sync_booking_financials(conn,self.bid);conn.commit();conn.close()

    def summary(self):
        r=self.f.client.get('/api/worker/earnings-summary');self.assertEqual(r.status_code,200);return r.get_json()

    def test_completion_day_and_today_net_ignore_scheduled_date(self):
        data=self.summary();item=data['items'][0]
        self.assertEqual(item['start_date'],'2099-01-02')
        self.assertEqual(item['completion_date'],'2026-10-09')
        self.assertEqual(data['today_net'],item['worker_net'])
        self.assertEqual(data['today_net'],data['net'])
        self.assertEqual(data['service_timezone'],'Asia/Kolkata')

    def test_held_work_does_not_inflate_available_today_net(self):
        self.change("payment_status='cash_pending',paid_amount=0")
        data=self.summary();self.assertEqual(data['today_net'],0)
        self.assertIn(data['items'][0]['settlement_status'],('held','not_ready'))
        self.assertGreater(data['items'][0]['worker_net'],0)

    def test_diagnosis_only_completion_uses_decline_timestamp(self):
        self.change("booking_type='diagnosis',work_ended_at=NULL,work_declined_at='2026-10-08T20:00:00'")
        self.assertEqual(self.summary()['items'][0]['completion_date'],'2026-10-09')

    def test_legacy_completed_event_supplies_completion_evidence(self):
        self.change('work_ended_at=NULL')
        conn=get_db();conn.execute("INSERT INTO booking_events(booking_id,status,note,created_at) VALUES (?,'completed','Legacy completion','2026-10-08 20:00:00')",(self.bid,));conn.commit();conn.close()
        self.assertEqual(self.summary()['items'][0]['completion_date'],'2026-10-09')

    def test_unknown_completion_day_remains_explicit_and_not_today(self):
        self.change('work_ended_at=NULL')
        data=self.summary();self.assertIsNone(data['items'][0]['completion_date'])
        self.assertEqual(data['unknown_completion_dates'],1)
        self.assertEqual(data['today_net'],0)
        self.assertGreater(data['net'],0)

    def test_cancelled_and_unfinished_jobs_are_not_earned_work(self):
        for status in ('cancelled','confirmed'):
            self.change("status='"+status+"'")
            data=self.summary();self.assertEqual(data['items'],[]);self.assertEqual(data['net'],0)

    def test_timezone_aware_completion_is_converted_and_invalid_dates_are_unknown(self):
        self.assertEqual(app.completed_service_date('2026-10-08T23:00:00+03:00'),'2026-10-09')
        self.assertIsNone(app.completed_service_date('invalid'))
