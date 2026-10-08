"""
Razorpay integration.

Reads credentials from environment variables — never hardcode keys:
    export RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
    export RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
    export RAZORPAY_WEBHOOK_SECRET=xxxxxxxxxxxxxxxx   # set in Razorpay dashboard too

Get test-mode keys free from the Razorpay dashboard (Settings > API Keys)
without any business verification — good enough for all of this to work
end-to-end before you're a registered business.

Docs this follows:
  - Orders API:      https://razorpay.com/docs/api/orders/
  - Checkout verify: https://razorpay.com/docs/payments/server-integration/python/payment-gateway/build-integration/#3-verify-payment-signature
  - Webhooks:        https://razorpay.com/docs/webhooks/
"""
import os
import hmac
import hashlib
import requests

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

API_BASE = "https://api.razorpay.com/v1"
ORDERS_URL = f"{API_BASE}/orders"


class RazorpayConfigError(Exception):
    pass


class RazorpayAPIError(Exception):
    pass


def _check_configured():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RazorpayConfigError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. "
            "Export them (test-mode keys from the Razorpay dashboard work fine)."
        )


def create_order(amount_rupees: int, receipt: str, notes: dict | None = None) -> dict:
    """
    Creates a Razorpay order. Amount must be sent to Razorpay in paise
    (smallest currency unit), so ₹2,100 becomes 210000.

    Returns the order dict Razorpay sends back, e.g.:
      {"id": "order_XXXXX", "amount": 210000, "currency": "INR", "status": "created", ...}
    """
    _check_configured()
    payload = {
        "amount": int(amount_rupees) * 100,
        "currency": "INR",
        "receipt": receipt,
        "notes": notes or {},
    }
    resp = requests.post(
        ORDERS_URL,
        json=payload,
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RazorpayAPIError(f"Razorpay order creation failed ({resp.status_code}): {resp.text}")
    return resp.json()


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Called after Razorpay Checkout succeeds on the frontend. The frontend
    hands you back razorpay_order_id, razorpay_payment_id and
    razorpay_signature — verify them server-side before trusting the
    payment. This is pure HMAC math, no network call, so it's instant and
    fully testable offline.
    """
    if not RAZORPAY_KEY_SECRET:
        raise RazorpayConfigError(
            "RAZORPAY_KEY_SECRET is not set. Export it (test-mode key from the "
            "Razorpay dashboard works fine)."
        )
    body = f"{order_id}|{payment_id}"
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """
    Verifies the X-Razorpay-Signature header on an incoming webhook call.
    raw_body must be the exact, unparsed request body bytes — verifying
    against a re-serialized JSON object will fail even with the right data,
    because whitespace/key order can differ.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        raise RazorpayConfigError("RAZORPAY_WEBHOOK_SECRET is not set.")
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _fetch(resource):
    _check_configured()
    try:
        resp = requests.get(f"{API_BASE}/{resource}",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=10)
        if resp.status_code >= 400:
            raise RazorpayAPIError(f"Razorpay fetch failed ({resp.status_code})")
        result = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RazorpayAPIError("Unable to fetch payment provider state. Please retry sync.") from exc
    if not isinstance(result, dict):
        raise RazorpayAPIError("Invalid payment provider response")
    return result


def fetch_order(order_id: str) -> dict:
    return _fetch(f"orders/{order_id}")


def fetch_order_payments(order_id: str) -> list[dict]:
    result = _fetch(f"orders/{order_id}/payments")
    items = result.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise RazorpayAPIError("Invalid provider payment list")
    return items


def fetch_payment(payment_id: str) -> dict:
    return _fetch(f"payments/{payment_id}")


def fetch_refund(refund_id: str) -> dict:
    """Read existing refund evidence; this never initiates a refund."""
    return _fetch(f"refunds/{refund_id}")
