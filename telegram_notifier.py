"""
Telegram notification module — sends structured admin alerts
when a payment is verified and a subscription is activated.
"""

import os
import requests
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""


def _send_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] not configured — skipping notification")
        return False
    try:
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if resp.status_code == 200:
            print("[telegram] notification sent successfully")
            return True
        else:
            print(f"[telegram] failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[telegram] error: {e}")
        return False


def notify_payment_success(user_email: str, user_phone: str | None, plan: str, amount: float, transaction_id: str, sms_sender: str, expires_at: str) -> bool:
    plan_label = {"day": "1 Day", "monthly": "30 Days", "quarterly": "90 Days"}.get(plan, plan)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (
        "💰 *Payment Verified*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📧 User:     `{user_email}`\n"
        f"📱 Phone:    `{user_phone or 'N/A'}`\n"
        f"📦 Plan:     *{plan_label}*\n"
        f"💵 Amount:   UGX {amount:,.0f}\n"
        f"🆔 Tx Ref:   `{transaction_id}`\n"
        f"📨 SMS From: `{sms_sender}`\n"
        f"⏳ Expires:  `{expires_at}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now}"
    )
    return _send_message(text)


def notify_admin_error(error_message: str) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"⚠️ *Payment System Alert*\n━━━━━━━━━━━━━━━━━━\n{error_message}\n━━━━━━━━━━━━━━━━━━\n🕐 {now}"
    return _send_message(text)
