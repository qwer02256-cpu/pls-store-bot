"""Persisted, rate-limited broadcasts. Uncertain deliveries are never auto-repeated."""

import asyncio
import logging
import time
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
)
from services.db import db

log = logging.getLogger(__name__)


def draft(admin_id, body):
    with db() as conn:
        return conn.execute(
            "INSERT INTO broadcasts(admin_id,body,status) VALUES(?,?,'draft')",
            (admin_id, body),
        ).lastrowid


def enqueue(bid, admin_id):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM broadcasts WHERE id=? AND admin_id=?", (bid, admin_id)
        ).fetchone()
        if not row:
            raise ValueError("Рассылка не найдена")
        if row["status"] != "draft":
            return False
        conn.execute(
            "INSERT INTO broadcast_recipients(broadcast_id,user_id,status) SELECT ?,user_id,'pending' FROM users",
            (bid,),
        )
        conn.execute("UPDATE broadcasts SET status='queued' WHERE id=?", (bid,))
        return True


def cancel(bid, admin_id):
    with db() as conn:
        conn.execute(
            "UPDATE broadcasts SET status='cancelled' WHERE id=? AND admin_id=? AND status IN ('draft','queued','running')",
            (bid, admin_id),
        )


def stats(bid, admin_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM broadcasts WHERE id=? AND admin_id=?", (bid, admin_id)
        ).fetchone()
        if not row:
            return None
        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status,count(*) n FROM broadcast_recipients WHERE broadcast_id=? GROUP BY status",
                (bid,),
            )
        }
        return dict(row, counts=counts, total=sum(counts.values()))


def recover():
    with db() as conn:
        # A process may stop after Telegram accepted a send but before SQLite recorded it.
        conn.execute(
            "UPDATE broadcast_recipients SET status='unknown',error='interrupted' WHERE status='sending'"
        )
        conn.execute("UPDATE broadcasts SET status='queued' WHERE status='running'")


def finish():
    with db() as conn:
        conn.execute(
            "UPDATE broadcasts SET status='completed',finished_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running') AND NOT EXISTS (SELECT 1 FROM broadcast_recipients r WHERE r.broadcast_id=broadcasts.id AND r.status IN ('pending','sending'))"
        )


async def process_one(bot, now=None):
    now = time.time() if now is None else now
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT r.*,b.body FROM broadcast_recipients r JOIN broadcasts b ON b.id=r.broadcast_id WHERE r.status='pending' AND r.next_attempt_at<=? AND b.status IN ('queued','running') ORDER BY b.id,r.user_id LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.commit()
            finish()
            return None
        bid, uid = row["broadcast_id"], row["user_id"]
        conn.execute(
            "UPDATE broadcast_recipients SET status='sending',attempts=attempts+1 WHERE broadcast_id=? AND user_id=?",
            (bid, uid),
        )
        conn.execute("UPDATE broadcasts SET status='running' WHERE id=?", (bid,))
    status = "sent"
    error = None
    retry = 0
    try:
        # A broadcast is a new message, not a replacement for the user's store menu.
        await bot.send_message(uid, row["body"], parse_mode="HTML")
    except TelegramRetryAfter as exc:
        status = "pending"
        retry = max(1, exc.retry_after)
        error = "rate_limit"
    except TelegramForbiddenError:
        status = "blocked"
        error = "forbidden"
    except TelegramBadRequest:
        status = "failed"
        error = "bad_request"
    except asyncio.CancelledError:
        # Leave sending for startup recovery; delivery may already have happened.
        raise
    except Exception as exc:
        status = "unknown"
        error = type(exc).__name__
    with db() as conn:
        conn.execute(
            "UPDATE broadcast_recipients SET status=?,error=?,next_attempt_at=? WHERE broadcast_id=? AND user_id=?",
            (status, error, now + retry + 1 if retry else 0, bid, uid),
        )
    finish()
    return retry


async def worker(bot):
    recover()
    while True:
        try:
            retry = await process_one(bot)
            await asyncio.sleep(max(0.08, retry or 0) if retry is not None else 1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Broadcast queue error: %s", type(exc).__name__)
            await asyncio.sleep(5)
