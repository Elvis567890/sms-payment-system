# Tests — SMS Payment Verification System

## Run all tests

```bash
pip install pytest
pytest tests/ -v
```

## Manual curl Tests

### 1. Health check
```bash
curl http://localhost:5000/health
```

### 2. Create a test user
```bash
curl -X POST http://localhost:5000/api/users \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "phone": "+256782123456"}'
```

### 3. Create a pending transaction
```bash
curl -X POST http://localhost:5000/api/transactions \
  -H "Content-Type: application/json" \
  -d '{"user_id": "USER_ID", "amount": 2500, "plan": "day", "manual_transaction_id": "M2361TEST01"}'
```

### 4. Send an SMS webhook
```bash
curl -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -d '{"sender": "0782123456", "message": "UGX 2,500 paid. Ref: M2361TEST01."}'
```

### 5. Verify user activation
```bash
curl http://localhost:5000/api/users/USER_ID
```
