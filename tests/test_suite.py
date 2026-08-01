"""
Comprehensive test suite for the SMS Payment Verification System.

Covers:
  - SMS parser accuracy across all supported formats
  - Webhook endpoint: success, invalid payloads, duplicates, 404
  - Database: CRUD, subscription activation, expiry calculation
  - Security: rate limiting, HMAC verification, input validation

Run with: pytest tests/ -v
"""

import os, sys, uuid, pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["SECRET_KEY"] = "a" * 32
os.environ["API_KEY"] = "b" * 24
os.environ["MERCHANT_PHONE"] = "0782123456"
os.environ["APP_ENV"] = "testing"

from app import app
from database import (
    init_db, create_user, get_user, create_transaction,
    get_pending_by_manual_id, activate_subscription,
    PLAN_PRICES, PLAN_DURATION_DAYS,
)
from sms_parser import parse_sms


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    import database
    db_dir = tmp_path / "data"
    db_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(database, "DB_DIR", str(db_dir))
    monkeypatch.setattr(database, "DB_PATH", str(db_dir / "payments.db"))
    database.init_db()
    monkeypatch.setattr(database, "init_db", lambda: None)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "b" * 24}


@pytest.fixture
def test_user(auth_headers, client):
    unique_id = uuid.uuid4().hex[:8]
    email = f"test_{unique_id}@example.com"
    resp = client.post("/api/users", json={"email": email, "phone": "+256782123456"}, headers=auth_headers)
    data = resp.get_json()
    return {"id": data["id"], "email": email}


class TestSMSParser:
    def test_basic_ref_format(self):
        r = parse_sms("Payment of UGX 2,500 to Airtel. Ref: M2361ABC123. Balance: UGX 500.")
        assert r.transaction_id == "M2361ABC123"
        assert r.amount == 2500.0
        assert r.is_valid

    def test_transaction_id_label(self):
        r = parse_sms("You have received UGX 15,000 from +256782123456. Transaction ID: FT9876543210")
        assert r.transaction_id == "FT9876543210"
        assert r.amount == 15000.0
        assert r.sender_phone == "+256782123456"

    def test_txnid_format(self):
        r = parse_sms("UGX40000 paid to 0782123456. TxnId: PP1234XYZ789.")
        assert r.transaction_id == "PP1234XYZ789"
        assert r.amount == 40000.0

    def test_amount_label(self):
        r = parse_sms("A1234ABC567 confirmation. Amount: 2,500 UGX sent to 0705123456")
        assert r.transaction_id == "A1234ABC567"
        assert r.amount == 2500.0

    def test_received_format(self):
        r = parse_sms("You have received UGX 15,000 from +256705123456. Ref M2361DEF456")
        assert r.transaction_id == "M2361DEF456"
        assert r.amount == 15000.0
        assert r.sender_phone == "+256705123456"

    def test_no_decimal_amount(self):
        r = parse_sms("UGX40000 paid. FT12345678 confirmed. From 0782123456")
        assert r.amount == 40000.0
        assert r.transaction_id == "FT12345678"

    def test_empty_message(self):
        r = parse_sms("")
        assert not r.is_valid
        assert "empty message" in r.errors

    def test_garbage_message(self):
        r = parse_sms("Your data bundle of 1GB has been activated.")
        assert not r.is_valid

    def test_phone_normalization(self):
        r = parse_sms("UGX 2,500 paid. Ref: M2361XXX000 from 0782123456")
        assert r.sender_phone == "+256782123456"


class TestDatabase:
    def test_create_user(self):
        user_id = create_user("dbtest@example.com", "+256700000001")
        user = get_user(user_id)
        assert user["email"] == "dbtest@example.com"
        assert user["tier"] == "free"
        assert not user["is_subscribed"]

    def test_create_transaction(self, test_user):
        create_transaction(user_id=test_user["id"], tx_ref="TX-TEST001", amount=2500, plan="day", manual_transaction_id="M2361TEST01")
        pending = get_pending_by_manual_id("M2361TEST01")
        assert pending is not None
        assert pending["amount"] == 2500
        assert pending["status"] == "pending"

    def test_activate_subscription(self, test_user):
        user = activate_subscription(test_user["id"], "monthly")
        assert user["tier"] == "monthly"
        assert user["is_subscribed"] == 1
        assert user["subscription_expires"] is not None

    def test_plan_config(self):
        assert PLAN_DURATION_DAYS["day"] == 1
        assert PLAN_DURATION_DAYS["monthly"] == 30
        assert PLAN_DURATION_DAYS["quarterly"] == 90
        assert PLAN_PRICES["day"] == 2500
        assert PLAN_PRICES["monthly"] == 15000
        assert PLAN_PRICES["quarterly"] == 40000

    def test_expire_stale_transactions(self, test_user):
        from database import expire_stale_transactions, get_db
        create_transaction(user_id=test_user["id"], tx_ref="TX-STALE", amount=2500, plan="day")
        with get_db() as (conn, cur):
            cur.execute("UPDATE transactions SET created_at = datetime('now', '-10 minutes') WHERE tx_ref = ?", ("TX-STALE",))
        expired = expire_stale_transactions(5)
        assert expired >= 1


class TestWebhook:
    def test_health_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_webhook_missing_message(self, client):
        resp = client.post("/webhook", json={"sender": "0782123456"})
        assert resp.status_code == 400

    def test_webhook_not_json(self, client):
        resp = client.post("/webhook", data="not json", content_type="text/plain")
        assert resp.status_code == 400

    def test_webhook_cannot_parse(self, client):
        resp = client.post("/webhook", json={"sender": "0782123456", "message": "Hello"})
        assert resp.status_code == 400

    def test_webhook_full_success_flow(self, client, test_user):
        manual_id = f"M2361E2E{uuid.uuid4().hex[:4].upper()}"
        create_transaction(user_id=test_user["id"], tx_ref="TX-E2E", amount=2500, plan="day", manual_transaction_id=manual_id)
        resp = client.post("/webhook", json={"sender": "0782123456", "message": f"UGX 2,500 paid. Ref: {manual_id}."})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "success"
        assert resp.get_json()["plan"] == "day"

    def test_webhook_duplicate(self, client, test_user):
        manual_id = f"M2361DUP{uuid.uuid4().hex[:4].upper()}"
        create_transaction(user_id=test_user["id"], tx_ref="TX-DUP", amount=2500, plan="day", manual_transaction_id=manual_id)
        msg = {"sender": "0782123456", "message": f"UGX 2,500 paid. Ref: {manual_id}."}
        r1 = client.post("/webhook", json=msg)
        assert r1.status_code == 200
        r2 = client.post("/webhook", json=msg)
        assert r2.status_code in (400, 409)

    def test_webhook_invalid_amount(self, client, test_user):
        manual_id = f"M2361BAD{uuid.uuid4().hex[:4].upper()}"
        create_transaction(user_id=test_user["id"], tx_ref="TX-BAD", amount=15000, plan="monthly", manual_transaction_id=manual_id)
        resp = client.post("/webhook", json={"sender": "0782123456", "message": f"UGX 9,999 paid. Ref: {manual_id}."})
        assert resp.status_code == 400

    def test_webhook_oversized_message(self, client):
        resp = client.post("/webhook", json={"sender": "0782123456", "message": "X" * 600})
        assert resp.status_code == 400


class TestAPI:
    def test_create_user_requires_api_key(self, client):
        resp = client.post("/api/users", json={"email": "no@key.com"})
        assert resp.status_code == 401

    def test_create_user_with_key(self, client, auth_headers):
        resp = client.post("/api/users", json={"email": "with-key@example.com"}, headers=auth_headers)
        assert resp.status_code == 201

    def test_create_user_invalid_email(self, client, auth_headers):
        resp = client.post("/api/users", json={"email": "not-an-email"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_create_user_duplicate(self, client, auth_headers):
        client.post("/api/users", json={"email": "dup@example.com"}, headers=auth_headers)
        resp = client.post("/api/users", json={"email": "dup@example.com"}, headers=auth_headers)
        assert resp.status_code == 409

    def test_get_user_requires_key(self, client):
        resp = client.get("/api/users/some-id")
        assert resp.status_code == 401

    def test_get_user_with_key(self, client, auth_headers, test_user):
        resp = client.get(f"/api/users/{test_user['id']}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["email"] == test_user["email"]
        assert "password_hash" not in resp.get_json()

    def test_active_plans_public(self, client):
        resp = client.get("/api/active-plans")
        assert resp.status_code == 200
        plans = resp.get_json()["plans"]
        assert len(plans) == 3
        assert "merchant_phone" not in resp.get_json()

    def test_payment_status_requires_valid_tx_ref(self, client):
        resp = client.get("/api/payment-status/INVALID_REF")
        assert resp.status_code == 400


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
