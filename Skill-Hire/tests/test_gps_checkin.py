import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import test_audit_flows as audit
from test_smoke import app, get_db


class GPSCheckInTests(unittest.TestCase):
    def setUp(self):
        self.f=audit.HireNowAuditFlows();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.bid=self.f.create_booking()
        conn=get_db();conn.execute("UPDATE bookings SET status='confirmed',payment_status='cash_pending' WHERE id=?",(self.bid,));conn.commit();conn.close()
        self.f.login('worker',self.f.worker)

    def post(self, status='confirmed', client=None, **changes):
        payload=dict(latitude=28.6,longitude=77.2,accuracy=10,gps_timestamp=time.time()*1000,expected_status=status)
        payload.update(changes)
        return (client or self.f.client).post(f'/api/worker/bookings/{self.bid}/check-in',json=payload)

    def state(self):
        conn=get_db();row=dict(conn.execute('SELECT * FROM bookings WHERE id=?',(self.bid,)).fetchone());conn.close();return row

    def test_invalid_coordinates_and_gps_metadata_do_not_advance(self):
        for changes in [dict(latitude=91),dict(longitude=-181),dict(latitude='abc'),dict(latitude=True),dict(latitude=None),dict(latitude=float('nan')),dict(longitude=float('inf')),dict(accuracy=None),dict(accuracy=101),dict(accuracy=-1),dict(accuracy=float('nan')),dict(gps_timestamp=time.time()*1000-121000),dict(gps_timestamp=time.time()*1000+31000)]:
            self.assertEqual(self.post(**changes).status_code,400,changes)
            self.assertEqual(self.state()['status'],'confirmed')

    def test_arrival_requires_proximity_but_travel_does_not(self):
        self.assertEqual(self.post(latitude=0,longitude=0).status_code,200)
        far=self.post('en_route',latitude=0,longitude=0)
        self.assertEqual(far.status_code,409)
        self.assertGreater(far.get_json()['distance_metres'],300)
        self.assertEqual(self.state()['status'],'en_route')
        near=self.post('en_route',latitude=28.601)
        self.assertEqual(near.status_code,200,near.get_json())
        self.assertLess(near.get_json()['distance_metres'],300)

    def test_duplicate_update_cannot_skip_arrival_or_add_events(self):
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.post().status_code,409)
        self.assertEqual(self.state()['status'],'en_route')
        self.assertEqual(self.post('en_route').status_code,200)
        self.assertEqual(self.post('en_route').status_code,409)
        conn=get_db();n=conn.execute("SELECT COUNT(*) AS n FROM booking_events WHERE booking_id=? AND status='checked_in'",(self.bid,)).fetchone()['n'];conn.close()
        self.assertEqual(n,1)

    def test_concurrent_travel_updates_do_not_skip_arrival(self):
        def run(_):
            client=app.app.test_client()
            with client.session_transaction() as sess:sess['worker_id']=self.f.worker
            return self.post(client=client).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:codes=list(executor.map(run,range(2)))
        self.assertEqual(sorted(codes),[200,409])
        self.assertEqual(self.state()['status'],'en_route')

    def test_missing_site_pin_has_owner_only_repair_and_is_immutable(self):
        conn=get_db();conn.execute('UPDATE bookings SET service_latitude=NULL,service_longitude=NULL WHERE id=?',(self.bid,));conn.commit();conn.close()
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.post('en_route').status_code,409)
        url=f'/api/bookings/{self.bid}/site-location';payload=dict(latitude=28.6,longitude=77.2)
        self.assertEqual(self.f.client.post(url,json=payload).status_code,401)
        self.f.login('hirer',self.f.other_hirer)
        self.assertEqual(self.f.client.post(url,json=payload).status_code,404)
        self.f.login('hirer',self.f.hirer)
        self.assertEqual(self.f.client.post(url,json=payload).status_code,200)
        self.assertEqual(self.f.client.post(url,json=payload).status_code,409)
        self.f.login('worker',self.f.worker)
        self.assertEqual(self.post('en_route').status_code,200)

    def test_cancelled_unpaid_and_unassigned_jobs_cannot_advance(self):
        self.f.login('worker',self.f.worker+100000)
        self.assertIn(self.post().status_code,(403,404))
        self.f.login('worker',self.f.worker)
        conn=get_db();conn.execute("UPDATE bookings SET payment_method='online',payment_status='pending' WHERE id=?",(self.bid,));conn.commit();conn.close()
        self.assertEqual(self.post().status_code,400)
        conn=get_db();conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(self.bid,));conn.commit();conn.close()
        self.assertEqual(self.post().status_code,409)

    def test_site_pin_repair_rejects_invalid_or_completed_jobs(self):
        self.f.login('hirer',self.f.hirer)
        url=f'/api/bookings/{self.bid}/site-location'
        self.assertEqual(self.f.client.post(url,json={'latitude':True,'longitude':0}).status_code,400)
        conn=get_db();conn.execute("UPDATE bookings SET status='completed',service_latitude=NULL,service_longitude=NULL WHERE id=?",(self.bid,));conn.commit();conn.close()
        self.assertEqual(self.f.client.post(url,json={'latitude':0,'longitude':0}).status_code,409)
