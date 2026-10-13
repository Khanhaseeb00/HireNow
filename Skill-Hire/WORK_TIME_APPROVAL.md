# Work time and extra-time approval

New work timers snapshot the booked `scheduled_minutes` (or legacy booked hours) as `approved_work_minutes`. Arrival, payment readiness and diagnosis approval remain required. One worker cannot start two running timers. Both detail screens calibrate the elapsed display from the server UTC timestamp.

The worker requests 15, 30, 60 or 120 extra minutes with a reason. The hirer sees the extra minutes, maximum incremental charge at the frozen booking hourly rate, and total proposed time limit before approving or declining. There is no separate premium overtime rate in this change. A pending request cannot be replaced; each request has its own ID. Booking locks and matching request IDs prevent duplicate approval and stale approvals. Accepted schedule conflicts block extensions; later booking conflict checks include already approved extensions.

The timer keeps recording elapsed duration. Billing is capped at the approved minutes, with existing whole-minute/whole-rupee rounding. Approval can cover elapsed minutes already beyond the prior cap; this is explicitly disclosed before approval. This is a billing limit, not an automatic instruction that physically stops work. Worker and hirer should agree before continuing. Recorded and billable minutes are displayed separately on completion and on payment receipts. Diagnosis fees and worker commissions retain their separate accounting.

A timer already running at deployment has a null approved limit and preserves its prior hourly agreement. Completed historical bills are not rewritten. No real bookings, payment transactions or balances were modified by these tests.

Validation: 157 backend tests; 15 frontend scripts. Tests cover approval/decline, cap enforcement, frozen rates, invalid input, ownership, stale/duplicate decisions, concurrent approval, completion races, legacy timers, next-job overlap, two running timers, shared server time, cost disclosure and stale UI response handling. Live two-device tests and PostgreSQL concurrency remain pending in FLOW_CHECKLIST.md.
