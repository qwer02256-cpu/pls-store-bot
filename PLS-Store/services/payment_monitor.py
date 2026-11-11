"""Reconcile pending payments even when the host has no public callback URL."""
import asyncio
import logging

from services.db import db

log = logging.getLogger(__name__)


async def reconcile_pending(client, on_confirmed, after_id=0, limit=50):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, platega_id FROM payments WHERE status='PENDING' AND platega_id IS NOT NULL "
            "AND id>? ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()
    for row in rows:
        tx_id = row['platega_id']
        try:
            data = await client.get_status(tx_id)
            status = str(data.get('status') or '').upper()
            provider_id = data.get('transactionId') or data.get('id')
            if provider_id is not None and str(provider_id) != tx_id:
                raise ValueError('Transaction ID mismatch')
            if status == 'CONFIRMED':
                await on_confirmed(tx_id, data)
            elif status in {'CANCELED', 'CANCELLED', 'EXPIRED'}:
                with db() as conn:
                    conn.execute("UPDATE payments SET status=? WHERE id=? AND status='PENDING'",
                                 ('CANCELED' if status == 'CANCELLED' else status, row['id']))
        except ValueError as exc:
            log.warning('Payment settlement rejected for local payment %s: %s', row['id'], str(exc))
        except Exception as exc:
            # Keep pending on network/database errors; the next sweep retries it.
            log.warning('Payment reconciliation failed for local payment %s: %s', row['id'], type(exc).__name__)
    return rows[-1]['id'] if len(rows) == limit else 0


async def monitor_payments(client, on_confirmed, interval=30):
    cursor = 0
    while True:
        try:
            cursor = await reconcile_pending(client, on_confirmed, after_id=cursor)
        except Exception as exc:
            log.warning('Payment monitor failed: %s', type(exc).__name__)
            cursor = 0
        await asyncio.sleep(interval)
