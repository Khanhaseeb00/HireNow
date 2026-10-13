# Hire Now flow verification checklist

Updated: 13 October 2026 (Asia/Riyadh). This records code and automated verification, not a certification of the live application.

| Area | Status | Evidence |
| --- | --- | --- |
| Worker discovery city/skill, online/verified and hourly budget filters | Complete in code | `tests/worker_discovery.test.js` |
| Discovery hourly rates match booking billing, completed-job labels and city recommendations | Complete in code | `tests/test_worker_discovery.py`; `tests/worker_discovery.test.js` |
| Journey GPS recorded separately from strict arrival GPS accuracy/location verification | Complete in code | `tests/test_start_travel.py`; `tests/worker_journey.test.js` |
| Live worker GPS: assigned-hirer map, fresh/stale labels and sharing stop/resume | Complete in code | `tests/test_live_travel.py`; `tests/live_travel_ui.test.js` |
| ETA: explicitly labelled speed estimate and opt-in Google traffic route support | Complete in code; routing setup pending | `tests/test_live_travel.py`; `tests/live_travel_ui.test.js` |
| Native/background mobile GPS and Google route ETA production configuration | Needs testing/setup | Browser requires open worker portal; Google coordinate sharing is off by default |
| Booking issues: participant reports/replies, shared status and reporter reopen | Complete in code | `tests/test_booking_issues.py`; `tests/booking_issues.test.js` |
| Admin issue review: booking/messages context, CSRF, optimistic updates and history | Complete in code | `tests/test_booking_issues.py`; `tests/booking_issues.test.js` |
| New timer billing limit, hirer overtime approval, actual/billable minutes and frozen rate | Complete in code | `tests/test_overtime.py`; `tests/work_overtime.test.js` |
| Existing running timers retain the prior agreement | Complete in code | `tests/test_overtime.py` |
| Live overtime approval, capped receipts and two-device timer display | Needs testing | Automated checks use isolated SQLite and mocked DOM |
| Shared job progress, UTC timer and agreed-rate bill estimate on hirer/worker details | Complete in code | `tests/booking_tracking.test.js` |
| Active status polling, safe refresh and assigned-worker detail access | Complete in code | `tests/booking_tracking.test.js`; `tests/test_booking_tracking.py` |
| Booking review before submission, edit preservation and explicit confirmation | Complete in code | `tests/booking_review.test.js` |
| Booking retry key, replay and overlapping requests | Complete in code | Backend suite; `tests/booking_retry.test.js` |
| Authoritative payment receipts, balance and refund accounting | Complete in code | Backend suite including payment accounting tests |
| Initial/balance checkout order reuse and concurrent request guard | Complete in code | Backend payment accounting tests |
| Checkout provider timeout and invalid response handling | Complete in code | `tests/test_payment_provider_errors.py` |
| SDK failure, double-click and verification recovery for three checkout flows | Complete in code | `tests/checkout_recovery.test.js` |
| Balance-due status sync access after reload | Complete in code | Hirer template; shared sync endpoint |
| Hirer booking load error/retry and late response guards | Complete in code | `tests/flow_mutation_safety.test.js` |
| Hirer cancellation double-click and uncertain-response refresh | Complete in code | `tests/flow_mutation_safety.test.js` |
| Worker API timeout, unreadable responses and timer action recovery | Complete in code | `tests/worker_action_recovery.test.js` |
| Worker jobs retry, stale response and session isolation | Complete in code | `tests/worker_jobs_loading.test.js` |
| Worker cash OTP and safe detail rendering | Complete in code | Backend suite; `tests/worker_job_details.test.js` |
| Worker payout save guard, account input clearing and stale payout reads | Complete in code | `tests/flow_mutation_safety.test.js` |
| Worker earnings response guards across sessions | Complete in code | `tests/flow_mutation_safety.test.js` |
| Hirer/worker logout failure and confirmed logout behavior | Complete in code | `tests/flow_mutation_safety.test.js` |
| Independent admin section loading/retry and session handling | Complete in code | `tests/admin_loading.test.js` |
| Admin write guard through dashboard refresh, including settlement/refund records | Complete in code | `tests/flow_mutation_safety.test.js`; admin mutation wrapper |
| Live browser layout, GPS permissions and navigation | Needs testing | Automated frontend checks use actual extracted functions with mocked DOM/provider APIs |
| Razorpay sandbox capture, authorization, webhook/refund and interrupted checkout | Needs testing | No real provider transaction initiated by these checks |
| PostgreSQL locking and simultaneous sessions | Needs testing | Current backend suite uses isolated SQLite |
| Provider order accepted immediately before response loss/server crash | Integration work required | Local locking/order reuse does not guarantee exactly-once creation across external provider/DB failures |
| Production bank payout integration | Integration work required | Current payout account endpoint retains only masked last four digits; production provider tokenization/encryption is required |

Validation commands from `Skill-Hire`:

```sh
python -W ignore -m unittest discover -s tests -q
node tests/worker_job_details.test.js
node tests/worker_action_recovery.test.js
node tests/worker_jobs_loading.test.js
node tests/checkout_recovery.test.js
node tests/hirer_safe_rendering.test.js
node tests/admin_loading.test.js
node tests/booking_retry.test.js
node tests/flow_mutation_safety.test.js
node tests/worker_discovery.test.js
node tests/booking_review.test.js
node tests/booking_tracking.test.js
node tests/booking_issues.test.js
node tests/worker_journey.test.js
node tests/live_travel_ui.test.js
node tests/work_overtime.test.js
```

Backend validation: 157 passing tests. Frontend validation: fifteen passing test scripts. Embedded scripts in the three affected templates pass `node --check`; `git diff --check` passes. Deployment and live end-to-end behavior are separate from saving code to main.
