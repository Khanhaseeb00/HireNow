"""Captured payment records shared by checkout, webhooks and reconciliation.

Amounts are stored in paise. Booking totals remain integer rupees for the
existing UI contract; unsupported fractional-rupee receipts fail closed.
"""
import payments
from db import foreign_id_sql, for_update, is_postgres


class PaymentPending(payments.RazorpayAPIError):
    pass


def ensure_schema(conn):
    conn.execute(f"""CREATE TABLE IF NOT EXISTS booking_payment_orders (
        order_id TEXT PRIMARY KEY,
        booking_id {foreign_id_sql()} NOT NULL REFERENCES bookings(id)
    )""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS booking_captured_payments (
        payment_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES booking_payment_orders(order_id),
        booking_id {foreign_id_sql()} NOT NULL REFERENCES bookings(id),
        amount_paise INTEGER NOT NULL CHECK(amount_paise > 0),
        refunded_paise INTEGER NOT NULL DEFAULT 0 CHECK(refunded_paise >= 0 AND refunded_paise <= amount_paise)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_booking_payment_orders_booking ON booking_payment_orders(booking_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_booking_captured_payments_booking ON booking_captured_payments(booking_id)")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS booking_processed_refunds (
        refund_id TEXT PRIMARY KEY,
        booking_id {foreign_id_sql()} NOT NULL REFERENCES bookings(id),
        payment_id TEXT NOT NULL,
        amount_paise INTEGER NOT NULL CHECK(amount_paise > 0)
    )""")


def register_order(conn, booking_id, order_id):
    if not isinstance(order_id, str) or not order_id:
        raise payments.RazorpayAPIError("Invalid provider order ID")
    conn.execute("""INSERT INTO booking_payment_orders(order_id, booking_id)
        VALUES (?, ?) ON CONFLICT(order_id) DO NOTHING""", (order_id, booking_id))
    row = conn.execute("SELECT booking_id FROM booking_payment_orders WHERE order_id=?", (order_id,)).fetchone()
    if row["booking_id"] != booking_id:
        raise payments.RazorpayAPIError("Provider order belongs to a different booking")


def booking_for_order(conn, order_id, hirer_id=None):
    sql = """SELECT b.* FROM bookings b WHERE
        (b.razorpay_order_id=? OR b.id IN
         (SELECT booking_id FROM booking_payment_orders WHERE order_id=?) OR b.id IN
         (SELECT booking_id FROM payment_adjustments WHERE provider_order_id=?))"""
    params = [order_id, order_id, order_id]
    if hirer_id is not None:
        sql += " AND b.hirer_id=?"
        params.append(hirer_id)
    return conn.execute(sql, params).fetchone()


def payment_status(booking, received):
    if booking["status"] in ("cancelled", "rejected"):
        return "refund_pending" if received > 0 else "cancelled"
    total = int(booking["total_amount"] or 0)
    if received > total:
        return "refund_pending"
    if received == total:
        return "paid"
    if booking["status"] == "completed":
        return "balance_due"
    return "pending"


def _paise(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise payments.RazorpayAPIError(f"Invalid provider {field}")
    if value % 100:
        raise payments.RazorpayAPIError("Fractional-rupee payments require manual reconciliation")
    return value


def record_processed_refund(conn, booking_id, refund_id, expected_rupees):
    refund = payments.fetch_refund(refund_id)
    if refund.get("id") != refund_id or refund.get("status") != "processed":
        raise payments.RazorpayAPIError("Refund must be processed by the payment provider")
    amount = _paise(refund.get("amount"), "refund amount")
    if amount != expected_rupees * 100:
        raise payments.RazorpayAPIError("Refund amount does not match this adjustment")
    payment = payments.fetch_payment(refund.get("payment_id"))
    owner = booking_for_order(conn, payment.get("order_id"))
    if not owner or owner["id"] != booking_id or payment.get("id") != refund.get("payment_id") or payment.get("currency") != "INR" or payment.get("status") not in ("captured", "refunded"):
        raise payments.RazorpayAPIError("Refund payment does not belong to this booking")
    existing = conn.execute("SELECT refund_id FROM booking_processed_refunds WHERE refund_id=?", (refund_id,)).fetchone()
    if existing:
        raise payments.RazorpayAPIError("This provider refund has already been recorded")
    conn.execute("INSERT INTO booking_processed_refunds(refund_id,booking_id,payment_id,amount_paise) VALUES (?,?,?,?)",
                 (refund_id, booking_id, payment["id"], amount))


def reconcile(conn, booking, required_order=None, required_payment=None):
    # All financial writers lock the booking first. SQLite needs an explicit
    # write transaction; PostgreSQL uses the same booking row lock everywhere.
    if not is_postgres() and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    booking = conn.execute(for_update("SELECT * FROM bookings WHERE id=?"), (booking["id"],)).fetchone()
    order_ids = {r["order_id"] for r in conn.execute(
        "SELECT order_id FROM booking_payment_orders WHERE booking_id=?", (booking["id"],)).fetchall()}
    if booking["razorpay_order_id"]:
        order_ids.add(booking["razorpay_order_id"])
    order_ids.update(r["provider_order_id"] for r in conn.execute(
        "SELECT provider_order_id FROM payment_adjustments WHERE booking_id=? AND provider_order_id IS NOT NULL",
        (booking["id"],)).fetchall())
    if not order_ids:
        return {"changed": False, "payment_status": booking["payment_status"], "provider_paid_amount": int(booking["paid_amount"] or 0)}
    snapshots = {}
    proof_found = False
    for order_id in sorted(order_ids):
        register_order(conn, booking["id"], order_id)
        order = payments.fetch_order(order_id)
        if order.get("id") != order_id or order.get("currency") != "INR":
            raise payments.RazorpayAPIError("Provider order or currency mismatch")
        for payment in payments.fetch_order_payments(order_id):
            if payment.get("status") not in ("captured", "refunded"):
                continue
            pid = payment.get("id")
            if not isinstance(pid, str) or not pid or payment.get("order_id") != order_id or payment.get("currency") != "INR":
                raise payments.RazorpayAPIError("Provider payment or currency mismatch")
            amount = _paise(payment.get("amount"), "payment amount")
            refunded = _paise(payment.get("amount_refunded", 0), "refunded amount")
            recorded = conn.execute("SELECT COALESCE(SUM(amount_paise),0) AS amount FROM booking_processed_refunds WHERE payment_id=? AND booking_id=?",
                                    (pid, booking["id"])).fetchone()["amount"]
            refunded = max(refunded, recorded)
            if amount <= 0 or refunded > amount:
                raise payments.RazorpayAPIError("Invalid provider payment amounts")
            if payment.get("status") == "refunded" and refunded != amount:
                raise payments.RazorpayAPIError("Incomplete provider refund details")
            if pid in snapshots and snapshots[pid] != (order_id, amount, refunded):
                raise payments.RazorpayAPIError("Conflicting provider payment records")
            snapshots[pid] = (order_id, amount, refunded)
            if order_id == required_order and pid == required_payment:
                proof_found = True
    if required_payment and not proof_found:
        raise PaymentPending("Payment is not captured yet. Retry payment sync after capture.")
    if snapshots and booking["cash_verified_at"]:
        raise payments.RazorpayAPIError("Cash was already verified for this booking. A late online capture requires admin review.")
    for pid, (order_id, amount, refunded) in snapshots.items():
        existing = conn.execute("SELECT * FROM booking_captured_payments WHERE payment_id=?", (pid,)).fetchone()
        if existing and (existing["booking_id"] != booking["id"] or existing["order_id"] != order_id or existing["amount_paise"] != amount):
            raise payments.RazorpayAPIError("Provider payment record does not match this booking")
        conn.execute("""INSERT INTO booking_captured_payments(payment_id, order_id, booking_id, amount_paise, refunded_paise)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(payment_id) DO UPDATE SET
            refunded_paise=CASE WHEN booking_captured_payments.refunded_paise > excluded.refunded_paise
                THEN booking_captured_payments.refunded_paise ELSE excluded.refunded_paise END""",
            (pid, order_id, booking["id"], amount, refunded))
    amounts = conn.execute("""SELECT COALESCE(SUM(amount_paise),0) AS captured,
        COALESCE(SUM(refunded_paise),0) AS refunded FROM booking_captured_payments WHERE booking_id=?""",
        (booking["id"],)).fetchone()
    # Preserve refunds already recorded by Admin on legacy bookings. Those
    # references and provider amount_refunded represent the same refund, so
    # do not subtract both. New records use the provider snapshot below.
    legacy_refunds = conn.execute("""SELECT COALESCE(SUM(amount),0) AS amount FROM payment_adjustments
        WHERE booking_id=? AND adjustment_type='refund' AND status='resolved'
          AND provider_refund_id IS NOT NULL""", (booking["id"],)).fetchone()["amount"] * 100
    received = max(0, amounts["captured"] - max(amounts["refunded"], legacy_refunds)) // 100
    if not amounts["captured"] and booking["payment_method"] == "cash":
        # An uncaptured checkout never changes the hirer's cash selection.
        return {"changed": False, "payment_status": booking["payment_status"], "provider_paid_amount": int(booking["paid_amount"] or 0)}
    status = payment_status(booking, received)
    pid = booking["payment_id"] or required_payment or next(iter(snapshots), None)
    changed = (booking["payment_status"] != status or int(booking["paid_amount"] or 0) != received
               or booking["payment_method"] != "online" or booking["payment_id"] != pid)
    if changed:
        conn.execute("UPDATE bookings SET payment_method='online', payment_status=?, paid_amount=?, payment_id=? WHERE id=?",
                     (status, received, pid, booking["id"]))
        conn.execute("INSERT INTO booking_events(booking_id,status,note) VALUES (?,?,?)",
                     (booking["id"], booking["status"], f"Provider payment reconciled: ₹{received} net received; {status}."))
    # Resolve paid balance orders using actual captured amounts, never the
    # mutable adjustment amount. An underpayment leaves the residual open.
    for order_id in order_ids:
        captured = sum(v[1] for v in snapshots.values() if v[0] == order_id)
        if captured:
            conn.execute("""UPDATE payment_adjustments SET status='resolved', provider_payment_id=?
                WHERE booking_id=? AND provider_order_id=? AND adjustment_type='balance_due' AND status='pending'""",
                (next(pid for pid,v in snapshots.items() if v[0] == order_id), booking["id"], order_id))
    return {"changed": changed, "payment_status": status, "provider_paid_amount": received, "payment_id": pid}
