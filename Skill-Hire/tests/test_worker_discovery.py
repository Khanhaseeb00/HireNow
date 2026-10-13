import unittest
from test_smoke import app


class WorkerDiscoveryRateTests(unittest.TestCase):
    def test_public_rate_matches_booking_rate_not_unreviewed_hourly_field(self):
        worker = {'daily_wage': 801.04, 'hourly_wage': 1, 'skill': 'Painter', 'service_location_configured': 1}
        public = app.public_worker_payload(worker)
        self.assertEqual(public['booking_hourly_rate'], app.derived_hourly_rate(worker))
        self.assertEqual(public['booking_hourly_rate'], 100.13)
        self.assertNotEqual(public['booking_hourly_rate'], public['hourly_wage'])
