"""Atomic, idempotent settlement of a confirmed Platega top-up."""
from decimal import Decimal, InvalidOperation

from services.db import db


def money(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid payment amount") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Payment amount must be positive")
    try:
        rounded = amount.quantize(Decimal('0.01'))
    except InvalidOperation:
        raise ValueError("Payment amount is out of range") from None
    if amount != rounded:
        raise ValueError("Payment amount must have at most two decimal places")
    return amount


def currency_code(value) -> str:
    # ISO 4217 numeric RUB and the historical RUR spelling identify the same currency.
    code = str(value).strip().upper()
    return 'RUB' if code in {'RUB', 'RUR', '643'} else code


def settle_payment(tx_id: str, data: dict):
    """Return settlement details once; roll back balance, logs and status together."""
    if str(data.get('status', '')).upper() != 'CONFIRMED':
        raise ValueError('Payment is not confirmed')
    provider_id = data.get('transactionId') or data.get('id')
    if provider_id is not None and str(provider_id) != tx_id:
        raise ValueError('Transaction ID mismatch')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM payments WHERE platega_id=?', (tx_id,)).fetchone()
        if row is None or row['status'] == 'CONFIRMED':
            return None
        amount = money(row['amount'])
        details = data.get('paymentDetails') or data
        if not isinstance(details, dict):
            raise ValueError('Invalid payment details')
        # The provider total may include a payer-side surcharge. Credit only
        # the locally recorded invoice principal, never the surcharge.
        if 'amount' in details and money(details['amount']) < amount:
            raise ValueError('Payment amount is below invoice amount')
        if details.get('currency') and currency_code(details['currency']) != currency_code(row['currency']):
            raise ValueError('Payment currency mismatch')
        user_id = row['user_id']
        user = conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
        if user is None:
            raise ValueError('Payment user does not exist')
        balance = float((Decimal(str(user['balance'])) + amount).quantize(Decimal('0.01')))
        conn.execute('UPDATE users SET balance=? WHERE user_id=?', (balance, user_id))
        conn.execute('INSERT INTO balance_log(user_id, delta, reason, payment_id) VALUES (?, ?, ?, ?)',
                     (user_id, float(amount), f'Platega topup {tx_id}', row['id']))
        bonus = Decimal(0)
        referrer_id = user['referrer_id']
        if referrer_id and referrer_id != user_id:
            setting = conn.execute("SELECT value FROM bot_settings WHERE key='referral_percent'").fetchone()
            try:
                percent = Decimal(setting['value'] if setting else '10')
            except InvalidOperation:
                raise ValueError('Invalid referral percentage') from None
            if not percent.is_finite() or not 0 <= percent <= 100:
                raise ValueError('Invalid referral percentage')
            bonus = (amount * percent / 100).quantize(Decimal('0.01'))
            if bonus > 0:
                referrer = conn.execute('SELECT * FROM users WHERE user_id=?', (referrer_id,)).fetchone()
                if referrer is None:
                    raise ValueError('Referrer does not exist')
                ref_balance = float((Decimal(str(referrer['balance'])) + bonus).quantize(Decimal('0.01')))
                earnings = float((Decimal(str(referrer['referral_earnings'])) + bonus).quantize(Decimal('0.01')))
                conn.execute('UPDATE users SET balance=?, referral_earnings=? WHERE user_id=?',
                             (ref_balance, earnings, referrer_id))
                conn.execute('INSERT INTO balance_log(user_id, delta, reason, payment_id) VALUES (?, ?, ?, ?)',
                             (referrer_id, float(bonus), f'Referral {user_id} topup {tx_id}', row['id']))
        conn.execute("UPDATE payments SET status='CONFIRMED', confirmed_at=CURRENT_TIMESTAMP WHERE id=?", (row['id'],))
        return dict(user_id=user_id, amount=float(amount), balance=balance,
                    referrer_id=referrer_id, referral_bonus=float(bonus))
