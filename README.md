# SMS Payment Verification System

A webhook server that receives SMS messages from an Android SMS forwarding app, parses Mobile Money payment confirmations, and automatically activates user subscriptions.

**📘 Full integration guide:** See [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md) for connecting your app + SMS forwarder.

## Architecture

```
Android SMS App → POST /webhook → Parse SMS → Match Transaction → Activate → Telegram Alert
                                          ↓
                                   SQLite Database
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your settings
```

### 3. Run the server

```bash
# Development
python app.py

# Production (via gunicorn)
gunicorn app:app --bind 0.0.0.0:5000 --workers 2
```

### 4. Test

```bash
pytest tests/ -v
```

## API Reference

### `POST /webhook` — Receive SMS

**Request:**
```json
{
  "sender": "0782123456",
  "message": "UGX 2,500 paid to Airtel Money. Ref: M2361ABC123."
}
```

**Success Response (200):**
```json
{
  "status": "success",
  "transaction_id": "M2361ABC123",
  "plan": "day",
  "plan_days": 1,
  "amount": 2500.0,
  "user_email": "user@example.com",
  "subscription_expires": "2026-03-09 10:30:00"
}
```

| Status | Condition |
|---|---|
| 400 | Invalid JSON, missing fields, cannot parse, invalid amount, duplicate |
| 404 | No matching pending transaction found |

### `POST /api/initiate-payment` — Start a payment

Header: `X-API-Key: your-api-key`

Body: `{"user_id": "uuid", "plan": "monthly", "phone": "+2567..."}`

### `GET /api/payment-status/<tx_ref>` — Poll for confirmation

### `GET /api/active-plans` — Get pricing

## Plan Pricing

| Plan | Price (UGX) | Duration |
|---|---|---|
| `day` | 2,500 | 1 day |
| `monthly` | 15,000 | 30 days |
| `quarterly` | 40,000 | 90 days |

## Deployment

### Render
Uses `render.yaml`. Connect the GitHub repo and deploy.

### Railway
Uses `railway.toml`. Just push to a Railway-connected repo.

### Replit
Uses `.replit` config.
