import unittest
from datetime import datetime, timedelta
import test_audit_flows as audit
from test_smoke import app, get_db


class BookingQuoteTests(unittest.TestCase):
    def setUp(self):
        self.f = audit.HireNowAuditFlows(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.login('hirer', self.f.hirer)

    def create(self, **changes):
        payload = dict(worker_id=self.f.worker, start_date='2099-01-02', start_time='10:00', hours=2, payment_method='cash')
        payload.update(changes)
        return self.f.client.post('/api/bookings', json=payload)

    def test_missing_time_and_overlong_end_are_rejected(self):
        for changes in [dict(start_time=None), dict(start_time='00:00',end_time='23:00'), dict(end_time='09:00'), dict(start_date=[]), dict(start_time={})]:
            self.assertEqual(self.create(**changes).status_code,400)

    def test_exact_fractional_duration_and_adjacent_booking(self):
        r=self.create(start_time='18:30',end_time='20:00')
        self.assertEqual(r.status_code,201,r.get_json())
        self.assertEqual(r.get_json()['total_amount'],150)
        self.assertEqual(r.get_json()['scheduled_minutes'],90)
        self.assertEqual(self.create(start_time='17:00',end_time='18:30').status_code,201)
        self.assertEqual(self.create(start_time='18:00',end_time='19:00').status_code,409)

    def test_changed_rate_cannot_change_final_bill(self):
        r=self.create(); bid=r.get_json()['id']
        conn=get_db()
        conn.execute('UPDATE workers SET daily_wage=1600 WHERE id=?',(self.f.worker,))
        conn.execute("UPDATE bookings SET status='in_progress',payment_status='cash_pending',work_started_at=? WHERE id=?",((datetime.utcnow()-timedelta(minutes=60,seconds=5)).isoformat(),bid))
        conn.commit();conn.close()
        self.f.login('worker',self.f.worker)
        result=self.f.client.post(f'/api/worker/bookings/{bid}/work-timer',json={'action':'stop'})
        self.assertEqual(result.status_code,200,result.get_json())
        self.assertEqual(result.get_json()['total_amount'],100)

    def test_availability_rechecked_on_acceptance(self):
        bid=self.create().get_json()['id']
        conn=get_db();conn.execute('INSERT INTO worker_unavailable_dates(worker_id,unavailable_date) VALUES (?,?)',(self.f.worker,'2099-01-02'));conn.commit();conn.close()
        self.f.login('worker',self.f.worker)
        self.assertEqual(self.f.client.post(f'/api/worker/bookings/{bid}/respond',json={'action':'accept'}).status_code,409)

    def test_stale_quote_requires_review(self):
        self.assertEqual(self.create(expected_hourly_rate=200).status_code,409)
        self.assertEqual(self.create(expected_hourly_rate=100).status_code,201)

    def test_legacy_estimate_is_frozen_once(self):
        bid=self.create().get_json()['id']
        conn=get_db();conn.execute('UPDATE bookings SET agreed_hourly_rate=NULL WHERE id=?',(bid,));conn.execute('UPDATE workers SET daily_wage=1600 WHERE id=?',(self.f.worker,));conn.commit();conn.close()
        app.ensure_schema_extensions()
        conn=get_db();row=conn.execute('SELECT agreed_hourly_rate,rate_snapshot_source FROM bookings WHERE id=?',(bid,)).fetchone();conn.close()
        self.assertEqual(row['agreed_hourly_rate'],100)
        self.assertEqual(row['rate_snapshot_source'],'legacy_estimate')
