import os
import uuid
import re
from datetime import datetime, timedelta
from firebase_functions import https_fn
from firebase_admin import initialize_app, firestore
import json

# Initialize Firebase
initialize_app()
db = firestore.client()

# ============================================================
# PLAN CONFIGURATION
# ============================================================
PLANS = {
    'day': {'price': 2500, 'days': 1, 'label': 'Day Pass'},
    'monthly': {'price': 15000, 'days': 30, 'label': 'Monthly VIP'},
    'quarterly': {'price': 40000, 'days': 90, 'label': 'Quarterly Pro'}
}

# ============================================================
# SMS PARSER
# ============================================================
def extract_transaction_id(text):
    patterns = [
        r'Ref[:\s]+([A-Z0-9\-]+)',
        r'TXN[:\s]+([A-Z0-9\-]+)',
        r'Transaction[:\s]+([A-Z0-9\-]+)',
        r'Reference[:\s]+([A-Z0-9\-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def extract_amount(text):
    match = re.search(r'UGX\s*([\d,]+\.?\d*)', text, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(',', ''))
    match = re.search(r'([\d,]+\.?\d*)\s*UGX', text, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(',', ''))
    return None

def normalize_phone(phone):
    phone = re.sub(r'[^0-9]', '', phone or '')
    if phone.startswith('0'):
        return '256' + phone[1:]
    elif not phone.startswith('256'):
        return '256' + phone
    return phone

# ============================================================
# WEBHOOK ENDPOINT
# ============================================================
@https_fn.on_request()
def webhook(req: https_fn.Request) -> https_fn.Response:
    """Main webhook endpoint - receives SMS from forwarder"""
    
    if req.method != 'POST':
        return https_fn.Response('Method not allowed', status=405)
    
    try:
        data = req.get_json()
        if not data:
            return https_fn.Response(json.dumps({'error': 'Invalid JSON'}), status=400, content_type='application/json')
        
        sender = data.get('sender', '')
        message = data.get('message', '')
        
        if not message:
            return https_fn.Response(json.dumps({'error': 'Missing message'}), status=400, content_type='application/json')
        
        tx_id = extract_transaction_id(message)
        amount = extract_amount(message)
        
        if not tx_id:
            return https_fn.Response(json.dumps({'error': 'Could not extract transaction ID'}), status=400, content_type='application/json')
        
        if amount is None:
            return https_fn.Response(json.dumps({'error': 'Could not extract amount'}), status=400, content_type='application/json')
        
        used_check = db.collection('transactions').where('manualTransactionId', '==', tx_id).where('status', '==', 'success').get()
        if len(used_check) > 0:
            return https_fn.Response(json.dumps({'error': 'Duplicate transaction'}), status=409, content_type='application/json')
        
        pending = db.collection('transactions').where('manualTransactionId', '==', tx_id).where('status', '==', 'pending').get()
        
        if len(pending) == 0:
            pending = db.collection('transactions').where('status', '==', 'pending').get()
            matched = None
            for tx in pending:
                if abs(tx.to_dict().get('amount', 0) - amount) < 0.01:
                    matched = tx
                    break
            if not matched:
                return https_fn.Response(json.dumps({'error': 'No matching pending transaction'}), status=404, content_type='application/json')
            pending_doc = matched
        else:
            pending_doc = pending[0]
        
        tx_data = pending_doc.to_dict()
        tx_id_doc = pending_doc.id
        user_id = tx_data.get('userId')
        
        plan = None
        for slug, config in PLANS.items():
            if abs(config['price'] - amount) < 0.01:
                plan = slug
                break
        
        if not plan:
            return https_fn.Response(json.dumps({'error': f'Amount {amount} does not match any plan'}), status=400, content_type='application/json')
        
        days = PLANS[plan]['days']
        expiry = datetime.utcnow() + timedelta(days=days)
        
        db.collection('transactions').document(tx_id_doc).update({
            'status': 'success',
            'amountReceived': amount,
            'smsReceivedAt': firestore.SERVER_TIMESTAMP,
            'verifiedAt': firestore.SERVER_TIMESTAMP,
            'plan': plan
        })
        
        if user_id:
            db.collection('users').document(user_id).update({
                'tier': plan,
                'isSubscribed': True,
                'subscriptionExpires': expiry
            })
        
        return https_fn.Response(json.dumps({
            'status': 'success',
            'message': 'Payment verified',
            'transaction_id': tx_id,
            'plan': plan,
            'plan_days': days,
            'amount': amount,
            'subscription_expires': expiry.isoformat()
        }), status=200, content_type='application/json')
        
    except Exception as e:
        return https_fn.Response(json.dumps({'error': str(e)}), status=500, content_type='application/json')


# ============================================================
# HEALTH CHECK
# ============================================================
@https_fn.on_request()
def health_check(req: https_fn.Request) -> https_fn.Response:
    return https_fn.Response(json.dumps({
        'status': 'ok',
        'service': 'sms-payment-verification',
        'timestamp': datetime.utcnow().isoformat()
    }), status=200, content_type='application/json')


# ============================================================
# INITIATE PAYMENT
# ============================================================
@https_fn.on_request()
def initiate_payment(req: https_fn.Request) -> https_fn.Response:
    if req.method != 'POST':
        return https_fn.Response('Method not allowed', status=405)
    
    try:
        data = req.get_json()
        if not data:
            return https_fn.Response(json.dumps({'error': 'Invalid JSON'}), status=400, content_type='application/json')
        
        user_id = data.get('userId')
        plan = data.get('plan')
        
        if not user_id or not plan:
            return https_fn.Response(json.dumps({'error': 'Missing userId or plan'}), status=400, content_type='application/json')
        
        if plan not in PLANS:
            return https_fn.Response(json.dumps({'error': 'Invalid plan'}), status=400, content_type='application/json')
        
        amount = PLANS[plan]['price']
        tx_ref = f"TX-{uuid.uuid4().hex[:10].upper()}"
        
        existing = db.collection('transactions').where('userId', '==', user_id).where('status', '==', 'pending').get()
        if len(existing) > 0:
            return https_fn.Response(json.dumps({'error': 'User already has a pending payment'}), status=409, content_type='application/json')
        
        tx_data = {
            'userId': user_id,
            'txRef': tx_ref,
            'amount': amount,
            'currency': 'UGX',
            'status': 'pending',
            'plan': plan,
            'manualTransactionId': None,
            'amountReceived': None,
            'createdAt': firestore.SERVER_TIMESTAMP
        }
        db.collection('transactions').add(tx_data)
        
        return https_fn.Response(json.dumps({
            'txRef': tx_ref,
            'amount': amount,
            'plan': plan,
            'planLabel': PLANS[plan]['label'],
            'merchantPhone': os.getenv('MERCHANT_PHONE', '0756408723'),
            'message': f'Please send UGX {amount:,} to the number above'
        }), status=201, content_type='application/json')
        
    except Exception as e:
        return https_fn.Response(json.dumps({'error': str(e)}), status=500, content_type='application/json')


# ============================================================
# GET PLANS
# ============================================================
@https_fn.on_request()
def get_plans(req: https_fn.Request) -> https_fn.Response:
    return https_fn.Response(json.dumps({
        'plans': [
            {
                'slug': slug,
                'label': config['label'],
                'price': config['price'],
                'days': config['days'],
                'formatted': f"UGX {config['price']:,}"
            }
            for slug, config in PLANS.items()
        ]
    }), status=200, content_type='application/json')
