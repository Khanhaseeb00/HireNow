import os
import time
import unittest
from unittest.mock import patch, Mock
from test_smoke import app, get_db
import test_audit_flows as audit_flows
import travel_tracking


class LiveTravelTests(unittest.TestCase):
    def setUp(self):
        self.fixture=audit_flows.HireNowAuditFlows();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.booking=self.fixture.create_booking();self.client=self.fixture.client
        conn=get_db();conn.execute("UPDATE bookings SET status='confirmed',payment_method='cash',payment_status='cash_pending' WHERE id=?",(self.booking,));conn.commit();conn.close()
        self.fixture.login('worker',self.fixture.worker)
        self.path=f'/api/worker/bookings/{self.booking}'
        self.read=f'/api/bookings/{self.booking}/travel-location'
        self.fix={'latitude':28.59,'longitude':77.2,'accuracy':20,'gps_timestamp':time.time()*1000,'speed':5}
        env=patch.dict(os.environ,{'TRAVEL_ROUTING_PROVIDER':'','TRAVEL_ROUTING_COORDINATES_ALLOWED':'','GOOGLE_ROUTES_API_KEY':''});env.start();self.addCleanup(env.stop)

    def start(self):
        response=self.client.post(self.path+'/start-travel',json=self.fix)
        self.assertEqual(response.status_code,200,response.get_json())
        return response.get_json()['sharing_token']

    def view(self):
        self.fixture.login('hirer',self.fixture.hirer)
        return self.client.get(self.read)

    def test_heading_requires_fresh_gps_and_records_initial_location(self):
        self.assertEqual(self.client.post(self.path+'/start-travel',json={}).status_code,400)
        for changes in ({'accuracy':1001},{'gps_timestamp':time.time()*1000-130000},{'latitude':True},{'speed':float('inf')}):
            self.assertEqual(self.client.post(self.path+'/start-travel',json=dict(self.fix,**changes)).status_code,400)
        self.start();response=self.view();self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['location']['latitude'],28.59);self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertNotIn('sharing_token',response.get_json())

    def test_live_read_is_scoped_to_assigned_hirer(self):
        self.start();self.fixture.login('hirer',self.fixture.other_hirer)
        self.assertEqual(self.client.get(self.read).status_code,404)
        self.fixture.login('worker',self.fixture.worker);self.assertEqual(self.client.get(self.read).status_code,401)

    def test_updates_require_active_sharing_token_and_monotonic_timestamp(self):
        token=self.start()
        newer=dict(self.fix,gps_timestamp=self.fix['gps_timestamp']+1,latitude=28.591,sharing_token=token)
        self.assertEqual(self.client.post(self.path+'/travel-location',json=newer).status_code,200)
        self.assertEqual(self.client.post(self.path+'/travel-location',json=dict(self.fix,sharing_token=token)).status_code,409)
        self.assertEqual(self.client.post(self.path+'/travel-location',json=dict(newer,sharing_token='incorrect')).status_code,403)
        self.assertEqual(self.view().get_json()['location']['latitude'],28.591)

    def test_stop_blocks_late_updates_and_resume_rotates_token(self):
        token=self.start();self.assertEqual(self.client.delete(self.path+'/travel-location').status_code,200)
        self.assertEqual(self.client.post(self.path+'/travel-location',json=dict(self.fix,sharing_token=token)).status_code,403)
        self.assertFalse(self.view().get_json()['sharing'])
        self.fixture.login('worker',self.fixture.worker);new=self.start();self.assertNotEqual(new,token)
        self.assertEqual(self.client.post(self.path+'/travel-location',json=dict(self.fix,sharing_token=token)).status_code,403)

    def test_stale_fix_has_no_eta_and_is_explicitly_marked(self):
        self.start();conn=get_db();conn.execute('UPDATE booking_travel_locations SET gps_timestamp=? WHERE booking_id=?',(time.time()*1000-180000,self.booking));conn.commit();conn.close()
        result=self.view().get_json();self.assertFalse(result['fresh']);self.assertIsNone(result['eta_minutes']);self.assertIn('stale',result['message'])

    def test_no_location_or_eta_is_exposed_after_arrival(self):
        token=self.start();conn=get_db();conn.execute("UPDATE bookings SET status='checked_in' WHERE id=?",(self.booking,));conn.commit();conn.close()
        self.assertEqual(self.client.post(self.path+'/travel-location',json=dict(self.fix,sharing_token=token)).status_code,409)
        result=self.view().get_json();self.assertFalse(result['sharing']);self.assertIsNone(result['location'])

    def test_rough_eta_uses_reported_speed_and_does_not_call_google(self):
        self.start()
        with patch.object(travel_tracking,'google_route') as provider:
            result=self.view().get_json();self.assertGreater(result['eta_minutes'],0);self.assertEqual(result['eta_basis'],'straight_line_current_speed');provider.assert_not_called()

    def test_unknown_speed_inaccurate_gps_or_cross_country_pin_has_no_fake_eta(self):
        self.fix['speed']=None;self.start();self.assertIsNone(self.view().get_json()['eta_minutes'])
        self.fixture.login('worker',self.fixture.worker);self.fix.update(speed=5,accuracy=800,gps_timestamp=time.time()*1000);self.start();self.assertIsNone(self.view().get_json()['eta_minutes'])
        conn=get_db();conn.execute('UPDATE bookings SET service_latitude=0,service_longitude=0 WHERE id=?',(self.booking,));conn.commit();conn.close()
        self.assertIsNone(self.view().get_json()['eta_minutes'])

    def test_configured_route_eta_is_cached_and_requires_operator_opt_in(self):
        self.start()
        with patch.dict(os.environ,{'TRAVEL_ROUTING_PROVIDER':'google','TRAVEL_ROUTING_COORDINATES_ALLOWED':'true','GOOGLE_ROUTES_API_KEY':'test'}),patch.object(travel_tracking,'google_route',return_value=(180,2000)) as provider:
            result=self.view().get_json();self.assertEqual(result['eta_minutes'],3);self.assertEqual(result['eta_basis'],'traffic_route')
            self.client.get(self.read);self.assertEqual(provider.call_count,1)

    def test_route_provider_rejection_does_not_break_location_sharing(self):
        self.start()
        with patch.dict(os.environ,{'TRAVEL_ROUTING_PROVIDER':'google','TRAVEL_ROUTING_COORDINATES_ALLOWED':'true','GOOGLE_ROUTES_API_KEY':'test'}),patch.object(travel_tracking,'google_route',return_value=None):
            result=self.view().get_json();self.assertTrue(result['fresh']);self.assertEqual(result['eta_basis'],'straight_line_current_speed')

    def test_google_protocol_validates_duration_and_does_not_send_without_opt_in(self):
        with patch.object(travel_tracking.requests,'post') as post:
            self.assertIsNone(travel_tracking.google_route(self.fix,{'service_latitude':28.6,'service_longitude':77.2}));post.assert_not_called()
        response=Mock();response.json.return_value={'routes':[{'duration':'123s','distanceMeters':2000}]}
        with patch.dict(os.environ,{'TRAVEL_ROUTING_PROVIDER':'google','TRAVEL_ROUTING_COORDINATES_ALLOWED':'true','GOOGLE_ROUTES_API_KEY':'test'}),patch.object(travel_tracking.requests,'post',return_value=response) as post:
            self.assertEqual(travel_tracking.google_route(self.fix,{'service_latitude':28.6,'service_longitude':77.2}),(123,2000))
            self.assertEqual(post.call_args.kwargs['json']['routingPreference'],'TRAFFIC_AWARE')

    def test_worker_cannot_start_two_simultaneous_journeys(self):
        conn=get_db();conn.execute("INSERT INTO bookings(hirer_id,worker_id,start_date,hours,total_amount,payment_method,status,payment_status) VALUES (?,?,?,?,0,'cash','en_route','cash_pending')",(self.fixture.hirer,self.fixture.worker,'2099-01-03',2));conn.commit();conn.close()
        self.assertEqual(self.client.post(self.path+'/start-travel',json=self.fix).status_code,409)
