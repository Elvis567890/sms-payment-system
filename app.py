"""
SMS Payment Verification System — Flask Webhook Server
=======================================================
🔒 SECURITY HARDENED — HMAC webhook verification, rate limiting,
   Talisman headers, input validation, API-key gating.
"""

import os, uuid, hmac, hashlib, time, json
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman

from database import (
    init_db, create_user, get_user, get_user_by_email,
    activate_subscription, create_transaction, get_transaction_by_ref,
    get_pending_by_manual_id, get_pending_by_user, get_recent_pending,
    is_manual_id_used, mark_transaction_success, mark_transaction_failed,
    count_recent_failed_webhooks, expire_stale_transactions,
    PLAN_PRICES, PLAN_DURATION_DAYS, plan_from_amount,
)
from sms_parser import parse_sms
from telegram_notifier import notify_payment_success, notify_admin_error

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY: raise RuntimeError("SECRET_KEY required")
if len(SECRET_KEY) < 32: raise RuntimeError("SECRET_KEY must be >= 32 chars")
API_KEY = os.getenv("API_KEY")
if not API_KEY: raise RuntimeError("API_KEY required")
if len(API_KEY) < 24: raise RuntimeError("API_KEY must be >= 24 chars")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", SECRET_KEY)
MERCHANT_PHONE = os.getenv("MERCHANT_PHONE", "")
MAX_SMS_LENGTH = int(os.getenv("MAX_SMS_LENGTH", "500"))
MAX_USER_EMAIL_LENGTH = int(os.getenv("MAX_USER_EMAIL_LENGTH", "254"))
MAX_PHONE_LENGTH = int(os.getenv("MAX_PHONE_LENGTH", "20"))
WEBHOOK_RATE_LIMIT = os.getenv("WEBHOOK_RATE_LIMIT", "30 per minute")
GLOBAL_RATE_LIMIT = os.getenv("GLOBAL_RATE_LIMIT", "60 per minute")
if not MERCHANT_PHONE: raise RuntimeError("MERCHANT_PHONE required")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

csp = {"default-src": "'none'", "frame-ancestors": "'none'"}
talisman = Talisman(app, content_security_policy=csp, force_https=os.getenv("APP_ENV")=="production", strict_transport_security=True, strict_transport_security_max_age=31536000, frame_options="DENY", referrer_policy="no-referrer", session_cookie_secure=True, session_cookie_http_only=True)

limiter = Limiter(get_remote_address, app=app, default_limits=[GLOBAL_RATE_LIMIT], storage_uri="memory://")

def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key or not constant_time_compare(key, API_KEY):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def verify_webhook_signature() -> bool:
    sig_header = request.headers.get("X-Webhook-Signature")
    ts_header = request.headers.get("X-Webhook-Timestamp")
    if not sig_header or not ts_header:
        app.logger.warning("[security] webhook without signature — allowed for backwards compat")
        return True
    try:
        ts = int(ts_header)
        if abs(int(time.time()) - ts) > 300:
            app.logger.warning("[security] webhook timestamp too old")
            return False
    except (ValueError, TypeError):
        return False
    raw_body = request.get_data(as_text=True)
    payload = f"{ts_header}.{raw_body}"
    expected = hmac.new(WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not constant_time_compare(expected, sig_header):
        app.logger.warning("[security] webhook signature mismatch")
        return False
    return True

def validate_email(email: str) -> bool:
    if not email or len(email) > MAX_USER_EMAIL_LENGTH: return False
    if "@" not in email or " " in email: return False
    local, _, domain = email.partition("@")
    return bool(local) and bool(domain) and "." in domain

def sanitize_phone(phone: str | None) -> str | None:
    if not phone: return None
    allowed = set("+0123456789")
    cleaned = "".join(c for c in phone if c in allowed)
    return cleaned[:MAX_PHONE_LENGTH] if cleaned else None

with app.app_context():
    init_db()

@app.route("/")
def index():
    """API Root Endpoint - Fixes the 404 error when visiting the base URL"""
    return jsonify({
        "service": "SMS Payment Verification API",
        "status": "operational",
        "version": "1.0",
        "endpoints": [
            {"path": "/", "method": "GET", "description": "API root (this response)"},
            {"path": "/health", "method": "GET", "description": "Health check"},
            {"path": "/webhook", "method": "POST", "description": "Mobile Money webhook receiver"},
            {"path": "/api/active-plans", "method": "GET", "description": "List available subscription plans"},
            {"path": "/api/initiate-payment", "method": "POST", "description": "Create a payment request"},
            {"path": "/api/payment-status/<tx_ref>", "method": "GET", "description": "Check payment status"},
            {"path": "/api/users", "method": "POST", "description": "Create a user"},
            {"path": "/api/users/<user_id>", "method": "GET", "description": "Get user info"},
            {"path": "/api/transactions", "method": "POST", "description": "Create a transaction"},
            {"path": "/api/transactions", "method": "GET", "description": "List transactions"}
        ],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    })

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "sms-payment-verification", "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})

@app.route("/webhook", methods=["POST"])
@limiter.limit(WEBHOOK_RATE_LIMIT)
def sms_webhook():
    if not verify_webhook_signature():
        return jsonify({"error": "invalid webhook signature"}), 403
    
    # === ULTIMATE DATA EXTRACTION ===
    data = None
    if request.is_json:
        data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict()
    if not data:
        data = request.args.to_dict()

    message_raw = None
    if data:
        possible_keys = ['message', 'text', 'body', 'sms', 'content', '%sms%', 'msg']
        for key in possible_keys:
            val = data.get(key)
            if val and isinstance(val, str):
                message_raw = val.strip()
                break
    
    if not message_raw:
        raw_body = request.get_data(as_text=True)
        if raw_body:
            try:
                json_data = json.loads(raw_body)
                for key in possible_keys:
                    val = json_data.get(key)
                    if val and isinstance(val, str):
                        message_raw = val.strip()
                        break
            except:
                pass
            if not message_raw:
                message_raw = raw_body.strip()

    # ============================================
    # 🔥 DEBUG: LOG WHAT THE SERVER RECEIVED 🔥
    app.logger.error(f"============== DEBUG ==============")
    app.logger.error(f"Received message_raw: '{message_raw}'")
    app.logger.error(f"===================================")
    # ============================================

    if not message_raw:
        return jsonify({"error": "Could not extract SMS text from the request body"}), 400

    if len(message_raw) > MAX_SMS_LENGTH:
        return jsonify({"error": f"message exceeds {MAX_SMS_LENGTH} chars"}), 400

    sender_raw = ""
    if data:
        sender_raw = data.get('sender') or data.get('phone') or data.get('number') or data.get('from') or ""
    
    sender = sanitize_phone(sender_raw)
    message = message_raw
    
    parsed = parse_sms(message)
    app.logger.info(f"[webhook] tx_id={parsed.transaction_id} amount={parsed.amount}")
    
    if not parsed.transaction_id:
        return jsonify({"error": "could not extract transaction ID", "details": parsed.errors}), 400
    if parsed.amount is None:
        return jsonify({"error": "could not extract amount", "details": parsed.errors}), 400
    
    if is_manual_id_used(parsed.transaction_id):
        app.logger.warning(f"[security] duplicate: {parsed.transaction_id}")
        return jsonify({"error": "duplicate transaction", "transaction_id": parsed.transaction_id}), 409
    
    failed_count = count_recent_failed_webhooks(sender or "unknown", 5)
    if failed_count > 20:
        app.logger.warning(f"[security] {failed_count} failed webhooks from {sender}")
        return jsonify({"error": "too many failed attempts"}), 429
    
    pending = get_pending_by_manual_id(parsed.transaction_id)
    if pending is None:
        pending_by_amount = [tx for tx in get_recent_pending(50) if abs(tx["amount"] - parsed.amount) < 0.01]
        if not pending_by_amount:
            return jsonify({"error": "no matching pending transaction", "transaction_id": parsed.transaction_id, "amount": parsed.amount}), 404
        pending = pending_by_amount[0]
    
    plan_slug = plan_from_amount(parsed.amount)
    if plan_slug is None:
        valid_prices = ", ".join(f"UGX {p:,}" for p in PLAN_PRICES.values())
        return jsonify({"error": f"amount UGX {parsed.amount:,.0f} doesn't match any plan", "valid_prices": valid_prices}), 400
    
    if pending["plan"] != plan_slug:
        app.logger.warning(f"[webhook] plan mismatch: tx={pending['plan']} sms={plan_slug}")
    
    sms_phone = parsed.sender_phone or sender
    mark_transaction_success(tx_id=pending["id"], manual_transaction_id=parsed.transaction_id, phone=sms_phone, sms_raw=message)
    user = activate_subscription(pending["user_id"], plan_slug)
    expires_at = user.get("subscription_expires", "unknown")
    
    notify_payment_success(user_email=user["email"], user_phone=sms_phone, plan=plan_slug, amount=parsed.amount, transaction_id=parsed.transaction_id, sms_sender=sender, expires_at=expires_at)
    
    return jsonify({"status": "success", "message": "Payment verified", "transaction_id": parsed.transaction_id, "plan": plan_slug, "plan_days": PLAN_DURATION_DAYS[plan_slug], "amount": parsed.amount, "user_email": user["email"], "subscription_expires": expires_at, "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}), 200

@app.route("/api/transactions", methods=["POST"])
@require_api_key
def api_create_transaction():
    data = request.get_json(silent=True)
    if not data: return jsonify({"error": "invalid JSON"}), 400
    user_id, amount, plan = data.get("user_id"), data.get("amount"), data.get("plan")
    if not user_id or amount is None or not plan: return jsonify({"error": "missing user_id, amount, or plan"}), 400
    if plan not in PLAN_PRICES: return jsonify({"error": f"invalid plan: {plan}", "valid": list(PLAN_PRICES)}), 400
    if get_user(user_id) is None: return jsonify({"error": "user not found"}), 404
    tx_ref = f"TX-{uuid.uuid4().hex[:12].upper()}"
    phone = sanitize_phone(data.get("phone"))
    tx_id = create_transaction(user_id=user_id, tx_ref=tx_ref, amount=float(amount), plan=plan, manual_transaction_id=data.get("manual_transaction_id"), phone=phone)
    return jsonify({"id": tx_id, "tx_ref": tx_ref, "status": "pending"}), 201

@app.route("/api/transactions", methods=["GET"])
@require_api_key
def api_list_transactions():
    status_filter = request.args.get("status", "pending")
    limit = min(int(request.args.get("limit", 20)), 100)
    from database import get_db
    with get_db() as (conn, cur):
        if status_filter == "all": cur.execute("SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?", (limit,))
        else: cur.execute("SELECT * FROM transactions WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status_filter, limit))
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"transactions": rows, "count": len(rows)})

@app.route("/api/users", methods=["POST"])
@require_api_key
def api_create_user():
    data = request.get_json(silent=True)
    if not data: return jsonify({"error": "invalid JSON"}), 400
    email = (data.get("email") or "").strip().lower()
    if not validate_email(email): return jsonify({"error": "valid email is required"}), 400
    if get_user_by_email(email): return jsonify({"error": "email already exists"}), 409
    phone = sanitize_phone(data.get("phone"))
    user_id = create_user(email=email, phone=phone, password_hash=data.get("password_hash"))
    return jsonify({"id": user_id, "email": email}), 201

@app.route("/api/users/<user_id>", methods=["GET"])
@require_api_key
def api_get_user(user_id):
    user = get_user(user_id)
    if user is None: return jsonify({"error": "user not found"}), 404
    safe_user = {k: v for k, v in user.items() if k != "password_hash"}
    return jsonify(safe_user)

@app.route("/api/initiate-payment", methods=["POST"])
@require_api_key
def api_initiate_payment():
    data = request.get_json(silent=True)
    if not data: return jsonify({"error": "invalid JSON"}), 400
    user_id, plan = data.get("user_id"), data.get("plan")
    user_phone = sanitize_phone(data.get("phone"))
    if not user_id or not plan: return jsonify({"error": "missing user_id or plan"}), 400
    if plan not in PLAN_PRICES: return jsonify({"error": f"invalid plan: {plan}", "valid": list(PLAN_PRICES)}), 400
    existing = get_pending_by_user(user_id)
    if existing: return jsonify({"error": "user already has a pending payment", "existing_tx_ref": existing[0]["tx_ref"]}), 409
    user = get_user(user_id)
    if user is None: return jsonify({"error": "user not found"}), 404
    amount = PLAN_PRICES[plan]
    tx_ref = f"TX-{uuid.uuid4().hex[:12].upper()}"
    plan_labels = {"day": "1 Day", "monthly": "30 Days", "quarterly": "90 Days"}
    create_transaction(user_id=user_id, tx_ref=tx_ref, amount=float(amount), plan=plan, phone=user_phone)
    return jsonify({"tx_ref": tx_ref, "amount": amount, "plan": plan, "plan_label": plan_labels[plan], "merchant_phone": MERCHANT_PHONE, "merchant_name": os.getenv("MERCHANT_NAME", "Our Service"), "instructions": f"Send exactly UGX {amount:,} to {MERCHANT_PHONE} via Mobile Money.", "poll_url": f"/api/payment-status/{tx_ref}"}), 201

@app.route("/api/payment-status/<tx_ref>", methods=["GET"])
def api_payment_status(tx_ref):
    if not tx_ref or len(tx_ref) > 30 or not tx_ref.startswith("TX-"): return jsonify({"error": "invalid tx_ref"}), 400
    tx = get_transaction_by_ref(tx_ref)
    if tx is None: return jsonify({"error": "not found"}), 404
    if tx["status"] == "success":
        user = get_user(tx["user_id"])
        return jsonify({"status": "success", "tx_ref": tx_ref, "plan": tx["plan"], "amount": tx["amount"], "user_email": user["email"] if user else "unknown", "subscription_expires": user["subscription_expires"] if user else None, "message": "Payment confirmed!"})
    if tx["status"] == "failed": return jsonify({"status": "failed", "tx_ref": tx_ref, "message": "Payment verification failed."})
    return jsonify({"status": "pending", "tx_ref": tx_ref, "message": "Waiting for payment confirmation..."})

@app.route("/api/active-plans", methods=["GET"])
def api_active_plans():
    plans = []
    labels = {"day": "1 Day", "monthly": "30 Days", "quarterly": "90 Days"}
    for slug, price in PLAN_PRICES.items():
        plans.append({"slug": slug, "label": labels[slug], "price_ugx": price, "formatted": f"UGX {price:,}", "days": PLAN_DURATION_DAYS[slug]})
    return jsonify({"plans": plans})

@app.errorhandler(429)
def ratelimit_handler(e): return jsonify({"error": "rate limit exceeded"}), 429
@app.errorhandler(404)
def not_found(_e): return jsonify({"error": "not found"}), 404
@app.errorhandler(500)
def server_error(_e):
    app.logger.error("[error] internal server error")
    return jsonify({"error": "internal server error"}), 500

with app.app_context():
    stale = expire_stale_transactions(30)
    if stale: app.logger.info(f"[startup] expired {stale} stale pending transactions")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("APP_ENV", "production") == "development"
    print(f"\n🚀 Starting on port {port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
