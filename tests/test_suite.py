"""
Comprehensive test suite for the SMS Payment Verification System.

Covers:
  - SMS parser accuracy across all supported formats
  - Webhook endpoint: success, invalid payloads, duplicates, 404
  - Database: CRUD, subscription activation, expiry calculation

Run with:
    pytest tests/ -v
"""

import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from database import (
    init_db,
    create_user,
    get_user,
    create_transaction,
    get_pending_by_manual_id,
    activate_subscription,
    PLAN_PRICES,
    PLAN_DURATION_DAYS,
)
from sms_parser import parse_sms


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    """Isolate each test with a fresh SQLite database."""
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
def test_user():
    unique_id = uuid.uuid4().hex[:8]
    email = f"test_{unique_id}@example.com"
    user_id = create_user(email, "+256782123456")
    return {"id": user_id, "email": email}


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
        r = parse_sms("UGX40000 paid to 0782123456. TxnId: PP1234XYZ789. Thank you.")
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

    def test_ugx_suffix(self):
        r = parse_sms("2,500 UGX received. Ref: M2361GHI789 from +256782123456")
        assert r.transaction_id == "M2361GHI789"
        assert r.amount == 2500.0

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


class TestWebhook:
    def test_health_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_webhook_missing_message(self, client):
        resp = client.post("/webhook", json={"sender": "0782123456"})
        assert resp.status_code == 400

    def test_webhook_not_json(self, client):
        resp = client.post("/webhook", data="not json")
        assert resp.status_code == 400

    def test_webhook_cannot_parse(self, client):
        resp = client.post("/webhook", json={"sender": "0782123456", "message": "Hello"})
        assert resp.status_code == 400

    def test_webhook_full_success_flow(self, client, test_user):
        manual_id = f"M2361E2E{uuid.uuid4().hex[:4].upper()}"
        create_transaction(user_id=test_user["id"], tx_ref="TX-E2E", amount=2500, plan="day", manual_transaction_id=manual_id)
        resp = client.post("/webhook", json={"sender": "0782123456", "message": f"UGX 2,500 paid. Ref: {manual_id}."})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["plan"] == "day"
        user = get_user(test_user["id"])
        assert user["is_subscribed"] == 1

    def test_webhook_duplicate(self, client, test_user):
        manual_id = f"M2361DUP{uuid.uuid4().hex[:4].upper()}"
        create_transaction(user_id=test_user["id"], tx_ref="TX-DUP", amount=2500, plan="day", manual_transaction_id=manual_id)
        msg = f"UGX 2,500 paid. Ref: {manual_id}."
        r1 = client.post("/webhook", json={"sender": "0782123456", "message": msg})
        assert r1.status_code == 200
        r2 = client.post("/webhook", json={"sender": "0782123456", "message": msg})
        assert r2.status_code == 400
        assert "duplicate" in r2.get_json()["error"].lower()

    def test_webhook_invalid_amount(self, client, test_user):
        manual_id = f"M2361BAD{uuid.uuid4().hex[:4].upper()}"
        create_transaction(user_id=test_user["id"], tx_ref="TX-BAD", amount=15000, plan="monthly", manual_transaction_id=manual_id)
        resp = client.post("/webhook", json={"sender": "0782123456", "message": f"UGX 9,999 paid. Ref: {manual_id}."})
        assert resp.status_code == 400

    def test_create_user_api(self, client):
        resp = client.post("/api/users", json={"email": "api-user@example.com"})
        assert resp.status_code == 201

    def test_get_user_api(self, client, test_user):
        resp = client.get(f"/api/users/{test_user['id']}")
        assert resp.status_code == 200
        assert resp.get_json()["email"] == test_user["email"]

    def test_create_transaction_api(self, client, test_user):
        resp = client.post("/api/transactions", json={"user_id": test_user["id"], "amount": 15000, "plan": "monthly"})
        assert resp.status_code == 201

    def test_subscription_expiry(self, test_user):
        from database import get_db
        user = activate_subscription(test_user["id"], "quarterly")
        assert user["subscription_expires"] is not None
        with get_db() as (conn, cur):
            cur.execute("SELECT julianday(subscription_expires) - julianday('now') AS days_left FROM users WHERE id = ?", (test_user["id"],))
            days_left = cur.fetchone()["days_left"]
            assert 88 <= days_left <= 91


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
