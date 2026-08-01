"""
SMS Payment Verification System — Flask Webhook Server
=======================================================

Receives forwarded SMS messages from Android, parses Mobile Money
payment confirmations, and activates user subscriptions automatically.

Endpoints:
  POST /webhook          — Main SMS webhook (receives forwarded SMS)
  GET  /health           — Health check
  POST /api/transactions — Create a pending transaction (manual testing)
  GET  /api/transactions — List recent pending transactions
  POST /api/users        — Create a test user
  GET  /api/users/<id>   — Get user details
  POST /api/initiate-payment  — Your app calls this to start a payment
  GET  /api/payment-status/<tx_ref>  — Your app polls this for status
"""

import os
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from database import (
    init_db,
    create_user,
    get_user,
    get_user_by_email,
    activate_subscription,
    create_transaction,
    get_transaction_by_ref,
    get_pending_by_manual_id,
    get_pending_by_user,
    get_recent_pending,
    is_manual_id_used,
    mark_transaction_success,
    mark_transaction_failed,
    get_recent_pending,
    PLAN_PRICES,
    PLAN_DURATION_DAYS,
    plan_from_amount,
)
from sms_parser import parse_sms
from telegram_notifier import notify_payment_success, notify_admin_error

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")

# API key for your main app to call this server securely
API_KEY = os.getenv("API_KEY", "change-me-to-a-secret-shared-between-apps")
MERCHANT_PHONE = os.getenv("MERCHANT_PHONE", "")  # the phone number users send money to


def require_api_key(f):
    """Decorator to protect endpoints called by your main app."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key or key != API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ── init DB on startup ───────────────────────────────────────────────────────
with app.app_context():
    init_db()


# ═══════════════════════════════════════════════════════
#  WEBHOOK — the core endpoint
# ═══════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def sms_webhook():
    """
    Receive an SMS forwarded from the Android app.

    Expected JSON:
    {
        "sender": "0782123456",
        "message": "UGX 2,500 paid to ... Ref: M2361ABC123"
    }
    """
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "invalid JSON body"}), 400

    sender = (data.get("sender") or "").strip()
    message = (data.get("message") or "").strip()

    if not message:
        return jsonify({"error": "missing 'message' field"}), 400

    parsed = parse_sms(message)
    print(f"[webhook] parsed → tx_id={parsed.transaction_id} amount={parsed.amount} phone={parsed.sender_phone} errors={parsed.errors}")

    if not parsed.transaction_id:
        return jsonify({"error": "could not extract transaction ID from SMS", "details": parsed.errors}), 400

    if parsed.amount is None:
        return jsonify({"error": "could not extract amount from SMS", "details": parsed.errors}), 400

    if is_manual_id_used(parsed.transaction_id):
        return jsonify({"error": "duplicate transaction", "message": f"Transaction ID {parsed.transaction_id} has already been processed.", "transaction_id": parsed.transaction_id}), 400

    pending = get_pending_by_manual_id(parsed.transaction_id)
    if pending is None:
        pending_by_amount = [
            tx for tx in get_recent_pending(50)
            if abs(tx["amount"] - parsed.amount) < 0.01
        ]
        if not pending_by_amount:
            return jsonify({"error": "no matching pending transaction found", "transaction_id": parsed.transaction_id, "amount": parsed.amount}), 404
        pending = pending_by_amount[0]
        print(f"[webhook] matched by amount only → tx_ref={pending['tx_ref']}")

    plan_slug = plan_from_amount(parsed.amount)
    if plan_slug is None:
        valid_prices = ", ".join(f"UGX {p:,}" for p in PLAN_PRICES.values())
        return jsonify({"error": f"amount UGX {parsed.amount:,.0f} does not match any plan price", "valid_prices": valid_prices}), 400

    if pending["plan"] != plan_slug:
        print(f"[webhook] ⚠ plan mismatch: tx={pending['plan']} sms={plan_slug} — using SMS plan")

    sms_phone = parsed.sender_phone or sender
    mark_transaction_success(tx_id=pending["id"], manual_transaction_id=parsed.transaction_id, phone=sms_phone, sms_raw=message)

    user = activate_subscription(pending["user_id"], plan_slug)
    expires_at = user.get("subscription_expires", "unknown")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    notify_payment_success(user_email=user["email"], user_phone=sms_phone, plan=plan_slug, amount=parsed.amount, transaction_id=parsed.transaction_id, sms_sender=sender, expires_at=expires_at)

    return jsonify({"status": "success", "message": "Payment verified and subscription activated", "transaction_id": parsed.transaction_id, "plan": plan_slug, "plan_days": PLAN_DURATION_DAYS[plan_slug], "amount": parsed.amount, "user_email": user["email"], "subscription_expires": expires_at, "processed_at": now_utc}), 200


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "sms-payment-verification", "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})


@app.route("/api/transactions", methods=["POST"])
def api_create_transaction():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid JSON"}), 400
    user_id = data.get("user_id")
    amount = data.get("amount")
    plan = data.get("plan")
    if not user_id or amount is None or not plan:
        return jsonify({"error": "missing user_id, amount, or plan"}), 400
    if plan not in PLAN_PRICES:
        return jsonify({"error": f"invalid plan: {plan}", "valid": list(PLAN_PRICES)}), 400
    if get_user(user_id) is None:
        return jsonify({"error": "user not found"}), 404
    tx_ref = f"TX-{uuid.uuid4().hex[:12].upper()}"
    tx_id = create_transaction(user_id=user_id, tx_ref=tx_ref, amount=float(amount), plan=plan, manual_transaction_id=data.get("manual_transaction_id"), phone=data.get("phone"))
    return jsonify({"id": tx_id, "tx_ref": tx_ref, "status": "pending"}), 201


@app.route("/api/transactions", methods=["GET"])
def api_list_transactions():
    status_filter = request.args.get("status", "pending")
    limit = min(int(request.args.get("limit", 20)), 100)
    with __import__("database").get_db() as (conn, cur):
        if status_filter == "all":
            cur.execute("SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            cur.execute("SELECT * FROM transactions WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status_filter, limit))
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"transactions": rows, "count": len(rows)})


@app.route("/api/users", methods=["POST"])
def api_create_user():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid JSON"}), 400
    email = data.get("email")
    if not email:
        return jsonify({"error": "email is required"}), 400
    if get_user_by_email(email):
        return jsonify({"error": "email already exists"}), 409
    user_id = create_user(email=email, phone=data.get("phone"), password_hash=data.get("password_hash"))
    return jsonify({"id": user_id, "email": email}), 201


@app.route("/api/users/<user_id>", methods=["GET"])
def api_get_user(user_id):
    user = get_user(user_id)
    if user is None:
        return jsonify({"error": "user not found"}), 404
    return jsonify(user)


@app.route("/api/initiate-payment", methods=["POST"])
@require_api_key
def api_initiate_payment():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid JSON"}), 400
    user_id = data.get("user_id")
    plan = data.get("plan")
    user_phone = data.get("phone")
    if not user_id or not plan:
        return jsonify({"error": "missing user_id or plan"}), 400
    if plan not in PLAN_PRICES:
        return jsonify({"error": f"invalid plan: {plan}", "valid": list(PLAN_PRICES)}), 400
    existing = get_pending_by_user(user_id)
    if existing:
        return jsonify({"error": "user already has a pending payment", "existing_tx_ref": existing[0]["tx_ref"]}), 409
    user = get_user(user_id)
    if user is None:
        return jsonify({"error": "user not found"}), 404
    amount = PLAN_PRICES[plan]
    tx_ref = f"TX-{uuid.uuid4().hex[:12].upper()}"
    plan_labels = {"day": "1 Day", "monthly": "30 Days", "quarterly": "90 Days"}
    tx_id = create_transaction(user_id=user_id, tx_ref=tx_ref, amount=float(amount), plan=plan, phone=user_phone)
    return jsonify({"tx_ref": tx_ref, "tx_id": tx_id, "amount": amount, "plan": plan, "plan_label": plan_labels[plan], "merchant_phone": MERCHANT_PHONE, "merchant_name": os.getenv("MERCHANT_NAME", "Our Service"), "instructions": f"Send exactly UGX {amount:,} to {MERCHANT_PHONE} via Mobile Money. Your {plan_labels[plan]} plan will activate automatically once the payment is confirmed.", "poll_url": f"/api/payment-status/{tx_ref}"}), 201


@app.route("/api/payment-status/<tx_ref>", methods=["GET"])
def api_payment_status(tx_ref):
    tx = get_transaction_by_ref(tx_ref)
    if tx is None:
        return jsonify({"error": "transaction not found"}), 404
    if tx["status"] == "success":
        user = get_user(tx["user_id"])
        return jsonify({"status": "success", "tx_ref": tx_ref, "plan": tx["plan"], "amount": tx["amount"], "user_email": user["email"] if user else "unknown", "subscription_expires": user["subscription_expires"] if user else None, "message": "Payment confirmed! Your plan is now active."})
    elif tx["status"] == "failed":
        return jsonify({"status": "failed", "tx_ref": tx_ref, "message": "Payment verification failed. Please try again."})
    else:
        return jsonify({"status": "pending", "tx_ref": tx_ref, "message": "Waiting for payment confirmation..."})


@app.route("/api/active-plans", methods=["GET"])
def api_active_plans():
    plans = []
    labels = {"day": "1 Day", "monthly": "30 Days", "quarterly": "90 Days"}
    for slug, price in PLAN_PRICES.items():
        plans.append({"slug": slug, "label": labels[slug], "price_ugx": price, "formatted": f"UGX {price:,}", "days": PLAN_DURATION_DAYS[slug], "merchant_phone": MERCHANT_PHONE})
    return jsonify({"plans": plans, "merchant_phone": MERCHANT_PHONE})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("APP_ENV", "production") == "development"
    print(f"\n🚀 SMS Payment Verification System starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
