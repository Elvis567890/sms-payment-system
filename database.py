"""
Database layer — SQLite schema, connection helpers, and query functions.

🔒 Hardened: parameterized queries, WAL + foreign keys, stale tx expiry,
   failed webhook counting for abuse detection, restricted file perms.
"""

import sqlite3, os, uuid
from contextlib import contextmanager

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "payments.db")
os.makedirs(DB_DIR, exist_ok=True)
os.chmod(DB_DIR, 0o700)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn, conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as (conn, cur):
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, phone TEXT,
                password_hash TEXT,
                tier TEXT NOT NULL DEFAULT 'free' CHECK(tier IN ('free','day','monthly','quarterly')),
                is_subscribed INTEGER NOT NULL DEFAULT 0,
                subscription_expires TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                tx_ref TEXT UNIQUE NOT NULL, amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'UGX',
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','success','failed')),
                plan TEXT NOT NULL CHECK(plan IN ('day','monthly','quarterly')),
                manual_transaction_id TEXT, phone TEXT, sms_raw TEXT,
                processed_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS failed_webhooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);
            CREATE INDEX IF NOT EXISTS idx_tx_manual_id ON transactions(manual_transaction_id);
            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_failed_webhooks ON failed_webhooks(sender, timestamp);
        """)
    print(f"✓ Database ready at {DB_PATH}")

PLAN_PRICES = {"day": 2500, "monthly": 15000, "quarterly": 40000}
PLAN_DURATION_DAYS = {"day": 1, "monthly": 30, "quarterly": 90}

def plan_from_amount(amount: float) -> str | None:
    for slug, price in PLAN_PRICES.items():
        if abs(price - amount) < 0.01: return slug
    return None

def create_user(email, phone=None, password_hash=None):
    user_id = str(uuid.uuid4())
    with get_db() as (conn, cur):
        cur.execute("INSERT INTO users (id, email, phone, password_hash) VALUES (?,?,?,?)", (user_id, email, phone, password_hash))
    return user_id

def get_user(user_id):
    with get_db() as (conn, cur):
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
    return dict(row) if row else None

def get_user_by_email(email):
    with get_db() as (conn, cur):
        cur.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
    return dict(row) if row else None

def activate_subscription(user_id, plan):
    days = PLAN_DURATION_DAYS[plan]
    with get_db() as (conn, cur):
        cur.execute("UPDATE users SET tier = ?, is_subscribed = 1, subscription_expires = datetime('now', '+' || ? || ' days') WHERE id = ?", (plan, days, user_id))
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return dict(cur.fetchone())

def create_transaction(user_id, tx_ref, amount, plan, currency="UGX", manual_transaction_id=None, phone=None):
    tx_id = str(uuid.uuid4())
    with get_db() as (conn, cur):
        cur.execute("INSERT INTO transactions (id, user_id, tx_ref, amount, currency, plan, manual_transaction_id, phone) VALUES (?,?,?,?,?,?,?,?)", (tx_id, user_id, tx_ref, amount, currency, plan, manual_transaction_id, phone))
    return tx_id

def get_transaction_by_ref(tx_ref):
    with get_db() as (conn, cur):
        cur.execute("SELECT * FROM transactions WHERE tx_ref = ?", (tx_ref,))
        row = cur.fetchone()
    return dict(row) if row else None

def get_pending_by_manual_id(manual_transaction_id):
    with get_db() as (conn, cur):
        cur.execute("SELECT * FROM transactions WHERE manual_transaction_id = ? AND status = 'pending'", (manual_transaction_id,))
        row = cur.fetchone()
    return dict(row) if row else None

def get_pending_by_user(user_id):
    with get_db() as (conn, cur):
        cur.execute("SELECT * FROM transactions WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC", (user_id,))
        return [dict(r) for r in cur.fetchall()]

def is_manual_id_used(manual_transaction_id):
    with get_db() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE manual_transaction_id = ? AND status = 'success'", (manual_transaction_id,))
        return cur.fetchone()["cnt"] > 0

def mark_transaction_success(tx_id, manual_transaction_id, phone, sms_raw):
    with get_db() as (conn, cur):
        cur.execute("UPDATE transactions SET status = 'success', manual_transaction_id = ?, phone = ?, sms_raw = ?, processed_at = datetime('now') WHERE id = ?", (manual_transaction_id, phone, sms_raw, tx_id))

def mark_transaction_failed(tx_id):
    with get_db() as (conn, cur):
        cur.execute("UPDATE transactions SET status = 'failed', processed_at = datetime('now') WHERE id = ?", (tx_id,))

def get_recent_pending(limit=20):
    with get_db() as (conn, cur):
        cur.execute("SELECT * FROM transactions WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

def count_recent_failed_webhooks(sender, minutes=5):
    with get_db() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS cnt FROM failed_webhooks WHERE sender = ? AND timestamp >= datetime('now', '-' || ? || ' minutes')", (sender, minutes))
        return cur.fetchone()["cnt"]

def expire_stale_transactions(timeout_minutes=30):
    with get_db() as (conn, cur):
        cur.execute("UPDATE transactions SET status = 'failed', processed_at = datetime('now') WHERE status = 'pending' AND created_at < datetime('now', '-' || ? || ' minutes')", (timeout_minutes,))
        return cur.rowcount
