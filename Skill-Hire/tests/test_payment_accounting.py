import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_audit_flows as audit_flows
from test_smoke import app, get_db


class PaymentAccountingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = audit_flows.HireNowAuditFlows()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.booking_id = self.fixture.create_booking()
        self.order = f"order_initial_{self.booking_id}"
        self.balance_order = f"order_balance_{self.booking_id}"
        self.payment = f"pay_initial_{self.booking_id}"
        self.balance_payment = f"pay_balance_{self.booking_id}"
        self.provider = {self.order: [self.entity(self.order, self.payment, 200)]}
        conn = get_db()
        conn.execute("UPDATE bookings SET status='completed',payment_method='online',total_amount=300,paid_amount=200,payment_status='balance_due',razorpay_order_id=? WHERE id=?", (self.order, self.booking_id))
        app.sync_booking_financials(conn, self.booking_id)
        conn.commit(); conn.close()
        for mock in (
            patch.object(app.payments, 'verify_checkout_signature', return_value=True),
            patch.object(app.payments, 'verify_webhook_signature', return_value=True),
            patch.object(app.payments, 'fetch_order', side_effect=lambda oid: {'id': oid, 'currency': 'INR'}),
            patch.object(app.payments, 'fetch_order_payments', side_effect=lambda oid: copy.deepcopy(self.provider.get(oid, []))),
        ):
            mock.start(); self.addCleanup(mock.stop)

    def entity(self, order, payment, amount, refunded=0, status='captured'):
        return {'id': payment, 'order_id': order, 'amount': amount * 100,
                'amount_refunded': refunded * 100, 'currency': 'INR', 'status': status}

    def balance(self, amount=100):
        conn = get_db()
        adjustment = app.sync_payment_adjustment(conn, self.booking_id)
        conn.execute('UPDATE payment_adjustments SET provider_order_id=? WHERE id=?', (self.balance_order, adjustment['id']))
        conn.commit(); conn.close()
        self.provider[self.balance_order] = [self.entity(self.balance_order, self.balance_payment, amount)]

    def verify(self, balance=False):
        self.fixture.login('hirer', self.fixture.hirer)
        return self.client.post('/api/payments/verify-balance' if balance else '/api/payments/verify', json={
            'razorpay_order_id': self.balance_order if balance else self.order,
            'razorpay_payment_id': self.balance_payment if balance else self.payment,
            'razorpay_signature': 'mock-signature'})

    def webhook(self, order=None, client=None):
        return (client or self.client).post('/api/payments/webhook', json={
            'event': 'payment.captured', 'payload': {'payment': {'entity': {'order_id': order or self.order}}}})

    def booking(self):
        conn = get_db(); row = dict(conn.execute('SELECT * FROM bookings WHERE id=?', (self.booking_id,)).fetchone()); conn.close(); return row

    def test_initial_proof_replay_keeps_actual_receipt_and_balance(self):
        for _ in range(2):
            r = self.verify(); self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()['paid_amount'], 200)
            self.assertEqual(r.get_json()['payment_status'], 'balance_due')

    def test_balance_and_original_webhook_do_not_double_count_or_lose_balance(self):
        self.balance()
        self.assertEqual(self.webhook(self.balance_order).status_code, 200)
        for _ in range(2):
            self.assertEqual(self.verify(balance=True).get_json()['paid_amount'], 300)
            self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.booking()['paid_amount'], 300)
        self.assertEqual(self.booking()['payment_status'], 'paid')
        conn = get_db()
        self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM booking_captured_payments WHERE booking_id=?', (self.booking_id,)).fetchone()['n'], 2)
        conn.close()

    def test_partial_balance_uses_actual_capture_and_leaves_residual_due(self):
        self.balance(amount=50)
        r = self.verify(balance=True)
        self.assertEqual(r.get_json()['paid_amount'], 250)
        self.assertEqual(r.get_json()['payment_status'], 'balance_due')
        conn = get_db()
        row = conn.execute("SELECT amount FROM payment_adjustments WHERE booking_id=? AND status='pending'", (self.booking_id,)).fetchone()
        self.assertEqual(row['amount'], 50); conn.close()

    def test_authorization_is_not_a_captured_payment(self):
        self.provider[self.order][0]['status'] = 'authorized'
        r = self.verify(); self.assertEqual(r.status_code, 409)
        self.assertEqual(self.booking()['paid_amount'], 200)
        self.assertEqual(self.booking()['payment_status'], 'balance_due')

    def test_currency_or_order_mismatch_fails_closed(self):
        for field, value in [('currency', 'USD'), ('order_id', 'another_order')]:
            original = self.provider[self.order][0][field]
            self.provider[self.order][0][field] = value
            self.assertEqual(self.verify().status_code, 502)
            self.assertEqual(self.booking()['paid_amount'], 200)
            self.provider[self.order][0][field] = original

    def test_provider_outage_does_not_change_payment(self):
        with patch.object(app.payments, 'fetch_order', side_effect=app.payments.RazorpayAPIError('Provider unavailable')):
            self.assertEqual(self.webhook().status_code, 502)
        self.assertEqual(self.booking()['payment_status'], 'balance_due')

    def test_overpayment_and_refund_survive_late_capture_webhook(self):
        conn = get_db(); conn.execute('UPDATE bookings SET total_amount=150 WHERE id=?', (self.booking_id,)); conn.commit(); conn.close()
        self.assertEqual(self.verify().get_json()['payment_status'], 'refund_pending')
        self.provider[self.order][0]['amount_refunded'] = 5000
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.booking()['paid_amount'], 150)
        self.assertEqual(self.booking()['payment_status'], 'paid')
        # A stale provider snapshot must not undo an already verified refund.
        self.provider[self.order][0]['amount_refunded'] = 0
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.booking()['paid_amount'], 150)

    def test_legacy_recorded_refund_is_not_subtracted_twice(self):
        conn = get_db(); conn.execute('UPDATE bookings SET total_amount=150,paid_amount=150,payment_status=\'paid\' WHERE id=?', (self.booking_id,))
        conn.execute("INSERT INTO payment_adjustments(booking_id,adjustment_type,amount,status,provider_refund_id) VALUES (?,'refund',50,'resolved','legacy_refund')", (self.booking_id,))
        conn.commit(); conn.close()
        self.provider[self.order][0]['amount_refunded'] = 5000
        self.assertEqual(self.verify().get_json()['paid_amount'], 150)

    def test_cancelled_late_capture_is_refundable_not_paid(self):
        conn = get_db(); conn.execute("UPDATE bookings SET status='cancelled',paid_amount=0,payment_status='cancelled' WHERE id=?", (self.booking_id,)); conn.commit(); conn.close()
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.booking()['status'], 'cancelled')
        self.assertEqual(self.booking()['payment_status'], 'refund_pending')
        conn = get_db(); refund = conn.execute("SELECT amount FROM payment_adjustments WHERE booking_id=? AND adjustment_type='refund' AND status='pending'", (self.booking_id,)).fetchone(); conn.close()
        self.assertEqual(refund['amount'], 200)

    def test_fully_refunded_cancelled_booking_does_not_reopen_refund(self):
        conn = get_db(); conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (self.booking_id,)); conn.commit(); conn.close()
        self.provider[self.order][0] = self.entity(self.order, self.payment, 200, refunded=200, status='refunded')
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.booking()['paid_amount'], 0)
        self.assertEqual(self.booking()['payment_status'], 'cancelled')

    def test_concurrent_webhook_and_checkout_count_each_payment_once(self):
        self.balance()
        def post(index):
            client = app.app.test_client()
            if index == 0:
                return self.webhook(self.balance_order, client).status_code
            with client.session_transaction() as sess:
                sess['hirer_id'] = self.fixture.hirer
            return client.post('/api/payments/verify-balance',json={'razorpay_order_id':self.balance_order,'razorpay_payment_id':self.balance_payment,'razorpay_signature':'sig'}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(post, range(2))), [200, 200])
        self.assertEqual(self.verify(balance=True).get_json()['paid_amount'], 300)

    def test_hirer_worker_admin_amounts_match(self):
        self.assertEqual(self.verify().status_code, 200)
        summary = self.client.get(f'/api/bookings/{self.booking_id}/payment-summary').get_json()
        self.assertEqual(summary['paid_amount'], 200)
        self.assertEqual(summary['adjustment']['amount'], 100)
        self.fixture.login('worker', self.fixture.worker)
        worker = self.client.get('/api/worker/earnings-summary').get_json()['items'][0]
        self.assertEqual(worker['payment_collected'], 200)
        self.assertEqual(worker['settlement_status'], 'held')
        with self.client.session_transaction() as sess:
            sess.clear(); sess['admin_authenticated'] = True
        admin = self.client.get('/api/admin/finance').get_json()
        item = next(x for x in admin['items'] if x['booking_id'] == self.booking_id)
        self.assertEqual(item['payment_collected'], 200)

    def test_old_checkout_order_remains_linked_after_new_order(self):
        self.assertEqual(self.verify().status_code, 200)
        conn = get_db(); conn.execute('UPDATE bookings SET razorpay_order_id=? WHERE id=?', ('order_new', self.booking_id)); conn.commit(); conn.close()
        self.provider['order_new'] = []
        self.assertEqual(self.verify().get_json()['paid_amount'], 200)

    def test_wrong_hirer_and_invalid_signature_cannot_verify(self):
        with patch.object(app.payments, 'verify_checkout_signature', return_value=False):
            self.assertEqual(self.verify().status_code, 400)
        self.fixture.login('hirer', self.fixture.other_hirer)
        r = self.client.post('/api/payments/verify', json={'razorpay_order_id':self.order,'razorpay_payment_id':self.payment,'razorpay_signature':'sig'})
        self.assertEqual(r.status_code, 404)

    def test_existing_settlement_is_held_when_receipt_no_longer_covers_bill(self):
        self.verify()
        conn = get_db(); conn.execute("UPDATE booking_financials SET settlement_status='settled' WHERE booking_id=?", (self.booking_id,)); app.sync_booking_financials(conn, self.booking_id); conn.commit()
        self.assertEqual(conn.execute('SELECT settlement_status FROM booking_financials WHERE booking_id=?',(self.booking_id,)).fetchone()['settlement_status'],'held'); conn.close()

    def refund_adjustment(self):
        conn = get_db(); conn.execute('UPDATE bookings SET total_amount=150 WHERE id=?', (self.booking_id,)); conn.commit(); conn.close()
        self.verify()
        conn = get_db(); row = conn.execute("SELECT id FROM payment_adjustments WHERE booking_id=? AND adjustment_type='refund' AND status='pending'", (self.booking_id,)).fetchone(); conn.close()
        return row['id']

    def resolve_refund(self, adjustment_id):
        with self.client.session_transaction() as sess:
            sess.clear(); sess['admin_authenticated'] = True; sess['admin_csrf'] = 'token'
        return self.client.post(f'/api/admin/payment-adjustments/{adjustment_id}/resolve',
            json={'reference': f'rfnd_{self.booking_id}'}, headers={'X-CSRF-Token': 'token'})

    def test_admin_refund_needs_processed_provider_evidence(self):
        adjustment = self.refund_adjustment()
        with patch.object(app.payments, 'fetch_refund', return_value={'id':f'rfnd_{self.booking_id}','status':'pending','amount':5000,'payment_id':self.payment}):
            self.assertEqual(self.resolve_refund(adjustment).status_code, 409)
        self.assertEqual(self.booking()['paid_amount'], 200)

    def test_admin_processed_refund_is_applied_once_even_with_stale_provider_list(self):
        adjustment = self.refund_adjustment()
        with patch.object(app.payments, 'fetch_refund', return_value={'id':f'rfnd_{self.booking_id}','status':'processed','amount':5000,'payment_id':self.payment}), \
             patch.object(app.payments, 'fetch_payment', return_value=self.provider[self.order][0]):
            response = self.resolve_refund(adjustment)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.resolve_refund(adjustment).status_code, 409)
        self.assertEqual(self.booking()['paid_amount'], 150)
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.booking()['paid_amount'], 150)

    def test_processed_refund_with_wrong_payment_or_amount_is_rejected(self):
        adjustment = self.refund_adjustment()
        for amount, order in [(4000, self.order), (5000, 'order_other')]:
            refund = {'id':f'rfnd_{self.booking_id}','status':'processed','amount':amount,'payment_id':self.payment}
            payment = self.entity(order, self.payment, 200)
            with patch.object(app.payments, 'fetch_refund', return_value=refund), patch.object(app.payments, 'fetch_payment', return_value=payment):
                self.assertEqual(self.resolve_refund(adjustment).status_code, 409)
        self.assertEqual(self.booking()['paid_amount'], 200)

    def test_refund_processed_webhook_reconciles_without_adding_capture_again(self):
        self.refund_adjustment()
        self.provider[self.order][0]['amount_refunded'] = 5000
        with patch.object(app.payments, 'fetch_payment', return_value=self.provider[self.order][0]):
            response = self.client.post('/api/payments/webhook', json={'event':'refund.processed','payload':{'refund':{'entity':{'payment_id':self.payment}}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.booking()['paid_amount'], 150)

    def test_order_creation_archives_old_order_before_replacing_it(self):
        conn = get_db(); conn.execute("UPDATE bookings SET status='confirmed',paid_amount=0,payment_status='pending' WHERE id=?",(self.booking_id,)); conn.commit(); conn.close()
        self.fixture.login('hirer', self.fixture.hirer)
        with patch.object(app.payments,'fetch_order',return_value={'id':self.order,'currency':'INR','amount':20000,'status':'created'}), patch.object(app.payments,'fetch_order_payments',return_value=[]), patch.object(app.payments,'create_order',return_value={'id':'order_replacement','currency':'INR','amount':30000}):
            self.assertEqual(self.client.post(f'/api/bookings/{self.booking_id}/create-order').status_code,200)
        self.provider['order_replacement'] = []
        self.assertEqual(self.verify().get_json()['paid_amount'],200)
        self.assertEqual(self.booking()['payment_status'],'pending')

    def test_checkout_retry_reuses_initial_order(self):
        conn=get_db();conn.execute("UPDATE bookings SET status='confirmed',paid_amount=0,payment_status='pending' WHERE id=?",(self.booking_id,));conn.commit();conn.close()
        self.fixture.login('hirer', self.fixture.hirer)
        with patch.object(app.payments,'fetch_order',return_value={'id':self.order,'currency':'INR','amount':30000,'status':'created'}), patch.object(app.payments,'fetch_order_payments',return_value=[]), patch.object(app.payments,'create_order') as create:
            for _ in range(2):
                r=self.client.post(f'/api/bookings/{self.booking_id}/create-order')
                self.assertEqual(r.status_code,200,r.get_json());self.assertEqual(r.get_json()['order_id'],self.order)
            create.assert_not_called()

    def test_checkout_retry_reuses_balance_order(self):
        self.balance();self.fixture.login('hirer', self.fixture.hirer)
        with patch.object(app.payments,'fetch_order',return_value={'id':self.balance_order,'currency':'INR','amount':10000,'status':'attempted'}), patch.object(app.payments,'fetch_order_payments',return_value=[]), patch.object(app.payments,'create_order') as create:
            for _ in range(2):
                r=self.client.post(f'/api/bookings/{self.booking_id}/create-balance-order')
                self.assertEqual(r.status_code,200,r.get_json());self.assertEqual(r.get_json()['order_id'],self.balance_order)
            create.assert_not_called()

    def test_concurrent_checkout_creates_one_order(self):
        conn=get_db();conn.execute("UPDATE bookings SET status='confirmed',paid_amount=0,payment_status='pending',razorpay_order_id=NULL WHERE id=?",(self.booking_id,));conn.commit();conn.close()
        order={'id':'order_concurrent','currency':'INR','amount':30000,'status':'created'}
        def checkout(_):
            client=app.app.test_client()
            with client.session_transaction() as session:
                session['hirer_id']=self.fixture.hirer
            response=client.post(f'/api/bookings/{self.booking_id}/create-order')
            return response.status_code,response.get_json()
        with patch.object(app.payments,'fetch_order',return_value=order), patch.object(app.payments,'fetch_order_payments',return_value=[]), patch.object(app.payments,'create_order',return_value=order) as create:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(checkout,range(2)))
            for status,body in results:
                self.assertEqual(status,200,body);self.assertEqual(body['order_id'],order['id'])
            self.assertEqual(create.call_count,1)

    def test_pending_payment_blocks_new_initial_order(self):
        conn=get_db();conn.execute("UPDATE bookings SET status='confirmed',paid_amount=0,payment_status='pending' WHERE id=?",(self.booking_id,));conn.commit();conn.close()
        self.fixture.login('hirer',self.fixture.hirer)
        with patch.object(app.payments,'fetch_order',return_value={'id':self.order,'currency':'INR','amount':30000,'status':'attempted'}), patch.object(app.payments,'fetch_order_payments',return_value=[{'status':'authorized'}]), patch.object(app.payments,'create_order') as create:
            self.assertEqual(self.client.post(f'/api/bookings/{self.booking_id}/create-order').status_code,502)
            create.assert_not_called()

    def test_uncaptured_order_does_not_supply_proof_for_legacy_paid_flag(self):
        conn=get_db();conn.execute("UPDATE bookings SET status='confirmed',payment_status='paid' WHERE id=?",(self.booking_id,));conn.commit();conn.close()
        self.provider[self.order][0]['status']='authorized'
        self.fixture.login('hirer', self.fixture.hirer)
        response=self.client.post(f'/api/bookings/{self.booking_id}/sync-payment')
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.booking()['paid_amount'],0)
        self.assertEqual(self.booking()['payment_status'],'pending')

    def test_late_online_capture_cannot_overwrite_verified_cash_receipt(self):
        conn=get_db();conn.execute("UPDATE bookings SET payment_method='cash',payment_status='paid',paid_amount=300,cash_verified_at='2026-10-08T10:00:00' WHERE id=?",(self.booking_id,));conn.commit();conn.close()
        self.assertEqual(self.webhook().status_code,502)
        self.assertEqual(self.booking()['payment_method'],'cash')
        self.assertEqual(self.booking()['paid_amount'],300)
