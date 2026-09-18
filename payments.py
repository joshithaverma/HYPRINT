"""
payments.py — webhook signature verification and refund calls.

SECURITY NOTE ON THE WEBHOOK
============================
The webhook endpoint is reachable from the public internet through the
Cloudflare tunnel, which means anyone can POST to it. The ONLY thing
standing between an attacker and free printing is signature verification,
so it is deliberately strict:

  * HMAC-SHA256 over the RAW request body (not the re-serialized JSON —
    re-serializing changes byte order/whitespace and breaks the signature).
  * hmac.compare_digest for the comparison, to avoid timing side channels.
  * A signed timestamp with a max age, so a captured valid webhook can't be
    replayed days later.
  * Plus, at the DB layer, a UNIQUE constraint on payment_ref, so even a
    successfully replayed in-window webhook can't mark two jobs paid.

If KIOSK_WEBHOOK_SECRET is unset, verification fails closed (every webhook
is rejected) rather than open. An unset secret is a misconfiguration, not a
"skip security" flag.
"""

import hashlib
import hmac
import time

import requests

import config


class WebhookError(Exception):
    pass


def verify_webhook(raw_body: bytes, signature_header: str | None,
                   timestamp_header: str | None) -> None:
    """Raises WebhookError if the request is not authentic. Returns None if OK."""
    if not config.WEBHOOK_SECRET:
        raise WebhookError("Webhook secret is not configured on this kiosk.")

    if not signature_header:
        raise WebhookError("Missing signature header.")

    # Replay window check.
    if timestamp_header:
        try:
            ts = int(timestamp_header)
        except ValueError:
            raise WebhookError("Malformed timestamp header.")
        age = abs(time.time() - ts)
        if age > config.WEBHOOK_MAX_AGE_SECONDS:
            raise WebhookError("Webhook timestamp is outside the accepted window.")
        signed_payload = timestamp_header.encode() + b"." + raw_body
    else:
        signed_payload = raw_body

    expected = hmac.new(
        config.WEBHOOK_SECRET.encode(),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time compare — a plain == leaks how many leading bytes matched.
    if not hmac.compare_digest(expected, signature_header.strip()):
        raise WebhookError("Signature mismatch.")


def refund_payment(payment_ref: str, amount_rupees: float) -> tuple[str | None, str | None]:
    """
    Call the gateway's refund API. Returns (refund_ref, error_message).
    Exactly one of the two is non-None.

    This is a blocking network call — main.py runs it in a threadpool so it
    never stalls the asyncio event loop (and therefore never stalls the
    1-second printer watchdog).
    """
    if not (config.GATEWAY_KEY_ID and config.GATEWAY_KEY_SECRET):
        return None, "Payment gateway credentials are not configured."

    url = f"{config.GATEWAY_API_BASE}/v1/payments/{payment_ref}/refund"
    try:
        resp = requests.post(
            url,
            auth=(config.GATEWAY_KEY_ID, config.GATEWAY_KEY_SECRET),
            json={"amount": int(round(amount_rupees * 100))},   # gateways use paise
            timeout=20,
        )
    except requests.RequestException as e:
        return None, f"Refund request failed to reach the gateway: {e}"

    if resp.status_code not in (200, 201):
        return None, f"Gateway rejected the refund (HTTP {resp.status_code}): {resp.text[:200]}"

    try:
        return resp.json().get("id"), None
    except ValueError:
        return None, "Gateway returned an unreadable refund response."
