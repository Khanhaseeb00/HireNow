import unittest
from unittest.mock import Mock, patch

import payments


class PaymentProviderErrorTests(unittest.TestCase):
    def setUp(self):
        credentials = patch.multiple(payments, RAZORPAY_KEY_ID='test', RAZORPAY_KEY_SECRET='test')
        credentials.start()
        self.addCleanup(credentials.stop)

    def response(self, body, status=200):
        response = Mock(status_code=status, text='private provider details')
        response.json.return_value = body
        return response

    def test_valid_order_preserves_expected_checkout_fields(self):
        order = {'id': 'order_test', 'amount': 12300, 'currency': 'INR'}
        with patch.object(payments.requests, 'post', return_value=self.response(order)) as post:
            self.assertEqual(payments.create_order(123, 'booking_test'), order)
            self.assertEqual(post.call_args.kwargs['json']['amount'], 12300)
            self.assertEqual(post.call_count, 1)

    def test_timeout_and_connection_failure_are_controlled_without_post_retry(self):
        for failure in (payments.requests.Timeout, payments.requests.ConnectionError):
            with self.subTest(failure=failure), patch.object(payments.requests, 'post', side_effect=failure('private failure')) as post:
                with self.assertRaisesRegex(payments.RazorpayAPIError, 'Check payment status'):
                    payments.create_order(123, 'booking_test')
                self.assertEqual(post.call_count, 1)

    def test_http_error_does_not_leak_provider_body(self):
        with patch.object(payments.requests, 'post', return_value=self.response({}, 503)):
            with self.assertRaises(payments.RazorpayAPIError) as error:
                payments.create_order(123, 'booking_test')
            self.assertNotIn('private', str(error.exception))

    def test_unreadable_json_is_controlled(self):
        response = self.response(None)
        response.json.side_effect = ValueError('private response')
        with patch.object(payments.requests, 'post', return_value=response):
            with self.assertRaisesRegex(payments.RazorpayAPIError, 'unreadable'):
                payments.create_order(123, 'booking_test')

    def test_invalid_order_cannot_reach_checkout(self):
        valid = {'id': 'order_test', 'amount': 12300, 'currency': 'INR'}
        for body in (None, [], {}, dict(valid, id=''), dict(valid, amount=123),
                     dict(valid, amount='12300'), dict(valid, amount=True), dict(valid, currency='USD')):
            with self.subTest(body=body), patch.object(payments.requests, 'post', return_value=self.response(body)):
                with self.assertRaisesRegex(payments.RazorpayAPIError, 'invalid checkout order'):
                    payments.create_order(123, 'booking_test')
