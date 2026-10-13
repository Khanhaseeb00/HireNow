import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from test_smoke import app, get_db
import test_audit_flows as audit_flows


class StartTravelTests(unittest.TestCase):
    def setUp(self):
        self.fixture=audit_flows.HireNowAuditFlows();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.booking=self.fixture.create_booking();self.client=self.fixture.client
        conn=get_db();conn.execute("UPDATE bookings SET status='confirmed',payment_method='cash',payment_status='cash_pending' WHERE id=?",(self.booking,));conn.commit();conn.close()
        self.fixture.login('worker',self.fixture.worker)
        self.path=f'/api/worker/bookings/{self.booking}/start-travel'

    def test_start_travel_needs_no_gps_and_replay_does_not_duplicate_events(self):
        first=self.client.post(self.path,json={});second=self.client.post(self.path,json={})
        self.assertEqual(first.status_code,200);self.assertEqual(first.get_json()['status'],'en_route');self.assertTrue(second.get_json()['replayed'])
        conn=get_db();events=conn.execute("SELECT latitude,longitude FROM booking_events WHERE booking_id=? AND status='en_route'",(self.booking,)).fetchall();conn.close()
        self.assertEqual(len(events),1);self.assertIsNone(events[0]['latitude']);self.assertIsNone(events[0]['longitude'])

    def test_arrival_still_requires_accurate_gps_near_site(self):
        self.client.post(self.path,json={})
        endpoint=f'/api/worker/bookings/{self.booking}/check-in'
        data={'latitude':28.6,'longitude':77.2,'accuracy':500,'gps_timestamp':time.time()*1000,'expected_status':'en_route'}
        self.assertEqual(self.client.post(endpoint,json=data).status_code,400)
        data.update(accuracy=10,latitude=1,longitude=2)
        self.assertEqual(self.client.post(endpoint,json=data).status_code,409)
        self.assertEqual(self.client.post(f'/api/worker/bookings/{self.booking}/work-timer',json={'action':'start'}).status_code,409)
        data.update(latitude=28.6,longitude=77.2)
        result=self.client.post(endpoint,json=data);self.assertEqual(result.status_code,200,result.get_json());self.assertEqual(result.get_json()['status'],'checked_in')

    def test_unpaid_online_booking_cannot_start_travel(self):
        conn=get_db();conn.execute("UPDATE bookings SET payment_method='online',payment_status='pending' WHERE id=?",(self.booking,));conn.commit();conn.close()
        self.assertEqual(self.client.post(self.path,json={}).status_code,409)

    def test_closed_or_unaccepted_booking_cannot_start_travel(self):
        for status in ('requested','cancelled','rejected','completed','in_progress','checked_in'):
            conn=get_db();conn.execute('UPDATE bookings SET status=? WHERE id=?',(status,self.booking));conn.commit();conn.close()
            self.assertEqual(self.client.post(self.path,json={}).status_code,409)

    def test_hirer_or_unassigned_worker_cannot_start_travel(self):
        self.fixture.login('hirer',self.fixture.hirer);self.assertEqual(self.client.post(self.path,json={}).status_code,401)
        self.fixture.login('worker',self.fixture.worker);self.assertEqual(self.client.post('/api/worker/bookings/9999999/start-travel',json={}).status_code,404)

    def test_concurrent_start_travel_records_one_transition(self):
        def start(_):
            client=app.app.test_client()
            with client.session_transaction() as session:session['worker_id']=self.fixture.worker
            return client.post(self.path,json={})
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(start,range(2)))
        self.assertTrue(all(r.status_code==200 for r in results));self.assertEqual(sum(not r.get_json()['replayed'] for r in results),1)
