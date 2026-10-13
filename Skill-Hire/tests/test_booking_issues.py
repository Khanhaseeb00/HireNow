import unittest
from concurrent.futures import ThreadPoolExecutor
from test_smoke import app, get_db
import test_audit_flows as audit_flows


class BookingIssuesTests(unittest.TestCase):
    def setUp(self):
        self.fixture=audit_flows.HireNowAuditFlows();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.client=self.fixture.client;self.booking=self.fixture.create_booking()
        self.base=f'/api/bookings/{self.booking}/issues'
        self.description='The worker has not arrived. Please help with this booking.'

    def report(self):
        self.fixture.login('hirer',self.fixture.hirer)
        response=self.client.post(self.base,json={'category':'worker_no_show','description':self.description})
        self.assertIn(response.status_code,(200,201),response.get_json())
        return response.get_json()['issue']

    def admin(self):
        with self.client.session_transaction() as session:
            session.clear();session['admin_authenticated']=True;session['admin_csrf']='test-csrf';session['admin_username']='audit-admin'

    def review(self,issue,status='resolved',note='Reviewed the case and agreed a resolution with both parties.'):
        self.admin()
        return self.client.post(f"/api/admin/issues/{issue['id']}",headers={'X-CSRF-Token':'test-csrf'},json={'status':status,'resolution':note,'expected_updated_at':issue['updated_at']})

    def test_report_replay_and_shared_visibility(self):
        issue=self.report();again=self.report();self.assertEqual(issue['id'],again['id'])
        self.fixture.login('worker',self.fixture.worker)
        response=self.client.get(f'/api/worker/bookings/{self.booking}/issues')
        self.assertEqual(response.status_code,200);self.assertEqual(response.get_json()[0]['reporter_role'],'hirer')
        self.assertNotIn('reporter_id',response.get_json()[0])
        conn=get_db();self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM booking_issues WHERE booking_id=?',(self.booking,)).fetchone()['n'],1);conn.close()

    def test_other_hirer_and_wrong_role_cannot_read_or_report(self):
        self.report();self.fixture.login('hirer',self.fixture.other_hirer)
        self.assertEqual(self.client.get(self.base).status_code,404)
        self.assertEqual(self.client.post(self.base,json={'category':'other','description':self.description}).status_code,404)
        self.fixture.login('worker',self.fixture.worker)
        self.assertEqual(self.client.get(self.base).status_code,401)

    def test_validate_category_description_and_json(self):
        for body in ({'category':'cash_not_received','description':self.description},{'category':'other','description':'short'}, {'category':'other','description':'x'*2001},[],{'category':[],'description':self.description}):
            self.fixture.login('hirer',self.fixture.hirer)
            self.assertEqual(self.client.post(self.base,json=body).status_code,400)

    def test_reply_replay_mismatch_and_peer_visibility(self):
        issue=self.report();path=self.base+f"/{issue['id']}/reply";key='reply_key_1234567890'
        body={'body':'The address is correct; please contact me.','request_key':key}
        first=self.client.post(path,json=body);self.assertEqual(first.status_code,200)
        repeat=self.client.post(path,json=body);self.assertTrue(repeat.get_json()['replayed'])
        self.assertEqual(self.client.post(path,json=dict(body,body='Changed message')).status_code,409)
        self.assertEqual(len(repeat.get_json()['issue']['events']),2)

    def test_admin_auth_csrf_and_resolution_validation(self):
        issue=self.report();path=f"/api/admin/issues/{issue['id']}"
        self.assertEqual(self.client.get('/api/admin/issues').status_code,401)
        self.admin();self.assertEqual(self.client.post(path,json={}).status_code,403)
        self.assertEqual(self.review(issue,note='short').status_code,400)
        self.assertEqual(self.review(issue,status='invalid').status_code,400)

    def test_admin_resolution_visible_without_changing_booking_or_money(self):
        issue=self.report();conn=get_db();before=dict(conn.execute('SELECT status,payment_status,total_amount,paid_amount FROM bookings WHERE id=?',(self.booking,)).fetchone());conn.close()
        result=self.review(issue);self.assertEqual(result.status_code,200,result.get_json())
        self.fixture.login('worker',self.fixture.worker)
        row=self.client.get(f'/api/worker/bookings/{self.booking}/issues').get_json()[0]
        self.assertEqual(row['status'],'resolved');self.assertIn('agreed a resolution',row['resolution'])
        conn=get_db();after=dict(conn.execute('SELECT status,payment_status,total_amount,paid_amount FROM bookings WHERE id=?',(self.booking,)).fetchone());conn.close();self.assertEqual(before,after)

    def test_stale_admin_review_cannot_overwrite_new_reply(self):
        issue=self.report()
        self.client.post(self.base+f"/{issue['id']}/reply",json={'body':'More details for the admin','request_key':'reply_stale_12345678'})
        self.assertEqual(self.review(issue).status_code,409)

    def test_reporter_reply_reopens_resolved_issue_and_preserves_history(self):
        issue=self.report();self.assertEqual(self.review(issue).status_code,200)
        self.fixture.login('hirer',self.fixture.hirer)
        result=self.client.post(self.base+f"/{issue['id']}/reply",json={'body':'The agreed resolution has not happened yet.','request_key':'reopen_key_123456789'})
        self.assertEqual(result.status_code,200);self.assertEqual(result.get_json()['issue']['status'],'open')
        self.assertEqual(result.get_json()['issue']['events'][-1]['event_type'],'reopened');self.assertTrue(result.get_json()['issue']['resolution'])

    def test_admin_detail_includes_booking_messages_and_safe_booking(self):
        issue=self.report();conn=get_db();conn.execute('INSERT INTO messages(booking_id,sender_role,sender_id,body) VALUES (?,?,?,?)',(self.booking,'hirer',self.fixture.hirer,'Please call before arrival'));conn.commit();conn.close()
        self.admin();result=self.client.get(f"/api/admin/issues/{issue['id']}")
        self.assertEqual(result.status_code,200);self.assertEqual(result.get_json()['messages'][0]['body'],'Please call before arrival')
        self.assertNotIn('cash_otp_hash',result.get_json()['booking'])

    def test_resolution_retry_does_not_duplicate_events(self):
        issue=self.report()
        self.assertEqual(self.review(issue).status_code,200)
        retry=self.review(issue)
        self.assertEqual(retry.status_code,200);self.assertTrue(retry.get_json()['replayed'])
        self.assertEqual(len(retry.get_json()['issue']['events']),2)

    def test_foreign_hirer_cannot_reply_and_invalid_issue_is_rejected(self):
        issue=self.report();path=self.base+f"/{issue['id']}/reply"
        self.fixture.login('hirer',self.fixture.other_hirer)
        body={'body':'Trying to reply','request_key':'ownership_key_1234567'}
        self.assertEqual(self.client.post(path,json=body).status_code,404)
        self.fixture.login('hirer',self.fixture.hirer)
        self.assertEqual(self.client.post(self.base+'/99999999/reply',json=body).status_code,404)

    def test_concurrent_reports_create_one_thread(self):
        def report(_):
            client=app.app.test_client()
            with client.session_transaction() as session:session['hirer_id']=self.fixture.hirer
            return client.post(self.base,json={'category':'worker_no_show','description':self.description})
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(report,range(2)))
        self.assertEqual(sorted(r.status_code for r in results),[200,201])
        self.assertEqual(results[0].get_json()['issue']['id'],results[1].get_json()['issue']['id'])
