# Hire Now flow verification checklist

Updated: 9 October 2026 (Asia/Riyadh). This records code and automated verification, not a certification of the live application.

| Area | Status | Evidence |
| --- | --- | --- |
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
```

Backend validation: 114 passing tests. Frontend validation: eight passing test scripts. Embedded scripts in the three affected templates pass `node --check`; `git diff --check` passes. Deployment and live end-to-end behavior are separate from saving code to main.
