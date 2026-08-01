"""
SMS Parser — extracts transaction ID, amount, and sender phone
from Mobile Money SMS messages using regex patterns.

Supported formats: Airtel Money / MTN MoMo / bank alerts
common in Uganda and East Africa.
"""

import re
from dataclasses import dataclass, field


@dataclass
class ParsedSMS:
    transaction_id: str | None = None
    amount: float | None = None
    sender_phone: str | None = None
    raw: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.transaction_id is not None and self.amount is not None and not self.errors


# ── transaction-id patterns ──
RE_TX_ID = re.compile(
    r"(?:Ref[:\s]*|Transaction\s*ID[:\s]*|TxnId[:\s]*|FT\s*)"
    r"([A-Z]{1,3}\d{4,}[A-Z0-9]*)",
    re.IGNORECASE,
)

# Exclude codes that start with "UGX" (currency, not a tx id)
RE_TX_ID_LOOSE = re.compile(r"\b((?!UGX\d)[A-Z]{1,3}\d{4,}[A-Z0-9]{0,6})\b")


# ── amount patterns ──
RE_AMOUNT_UGX = re.compile(r"(?:UGX\s*)?([\d,]+(?:\.\d{1,2})?)\s*(?:UGX)?", re.IGNORECASE)
RE_AMOUNT_LABEL = re.compile(r"(?:Amount|Amt|AMOUNT)[:\s]*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
RE_RECEIVED_AMOUNT = re.compile(r"(?:received|sent|paid|payment\s+of)\s+(?:UGX\s*)?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
RE_UGX_PREFIX = re.compile(r"UGX\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)


# ── phone-number patterns ──
RE_PHONE = re.compile(r"(?:(?:\+?256)|0)(7\d{2})\s*(\d{6})")


def _parse_number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_sms(message: str) -> ParsedSMS:
    result = ParsedSMS(raw=message.strip())
    if not message or not message.strip():
        result.errors.append("empty message")
        return result

    # 1. Extract transaction ID
    m = RE_TX_ID.search(message)
    if m:
        result.transaction_id = m.group(1).strip().upper()
    else:
        m = RE_TX_ID_LOOSE.search(message)
        if m:
            result.transaction_id = m.group(1).strip().upper()

    if result.transaction_id and len(result.transaction_id) < 6:
        result.errors.append(f"transaction_id too short: {result.transaction_id}")
        result.transaction_id = None

    # 2. Extract amount
    amount_raw: str | None = None

    m = RE_UGX_PREFIX.search(message)
    if m:
        amount_raw = m.group(1)

    if amount_raw is None:
        m = RE_AMOUNT_LABEL.search(message)
        if m:
            amount_raw = m.group(1)

    if amount_raw is None:
        m = RE_RECEIVED_AMOUNT.search(message)
        if m:
            amount_raw = m.group(1)

    if amount_raw is None:
        m = RE_AMOUNT_UGX.search(message)
        if m:
            amount_raw = m.group(1)

    if amount_raw:
        result.amount = _parse_number(amount_raw)
        if result.amount is None:
            result.errors.append(f"could not parse amount: {amount_raw}")
        elif result.amount < 100:
            result.amount = None
            result.errors.append(f"amount too small (likely false positive): {amount_raw}")
    else:
        result.errors.append("no amount found in message")

    # 3. Extract sender phone
    m = RE_PHONE.search(message)
    if m:
        result.sender_phone = "+256" + m.group(1) + m.group(2)

    return result
