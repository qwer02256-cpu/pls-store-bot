"""Keep one current store screen per chat; document deliveries remain available."""

import asyncio
import logging
from weakref import WeakValueDictionary
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    Message,
    FSInputFile,
    LinkPreviewOptions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from services.db import db, get_user_language

log = logging.getLogger(__name__)
_locks = WeakValueDictionary()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _ids(message):
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    return (
        chat_id if isinstance(chat_id, int) else None,
        message_id if isinstance(message_id, int) else None,
    )


async def _delete(bot, chat_id, mid):
    try:
        await bot.delete_message(chat_id, mid)
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        # Old/deleted messages cannot always be removed. Disable stale buttons.
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=mid, reply_markup=None
            )
        except Exception:
            pass
        return True
    except Exception as exc:
        # Retain the ID and retry after a transient Telegram/network failure.
        log.warning("Screen cleanup deferred: %s", type(exc).__name__)
        return False


async def _render(bot, chat_id, source_id, send):
    if chat_id is None:
        return (await send())[-1]
    lock = _locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        messages = await send()
        new_ids = {
            _ids(m)[1]
            for m in messages
            if _ids(m)[0] == chat_id and _ids(m)[1] is not None
        }
        if not new_ids:
            return messages[-1]
        # Record the replacement before removing anything. If sending fails,
        # the old screen and its buttons are still usable.
        with db() as conn:
            old = {
                r["message_id"]
                for r in conn.execute(
                    "SELECT message_id FROM chat_screens WHERE chat_id=?", (chat_id,)
                )
            }
            if source_id is not None:
                old.add(source_id)
            conn.executemany(
                "INSERT OR IGNORE INTO chat_screens(chat_id,message_id) VALUES(?,?)",
                [(chat_id, mid) for mid in old | new_ids],
            )
        for mid in sorted(old - new_ids):
            if await _delete(bot, chat_id, mid):
                with db() as conn:
                    conn.execute(
                        "DELETE FROM chat_screens WHERE chat_id=? AND message_id=?",
                        (chat_id, mid),
                    )
        return messages[-1]


def _back(chat_id):
    label = {"ru": "← Назад", "en": "← Back", "uk": "← Назад"}[
        get_user_language(chat_id)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data="home", style="danger")]
        ]
    )


async def screen_reply(target, text, reply_markup=None, photo=None, **kwargs):
    message = (
        target
        if isinstance(target, Message) or not hasattr(target, "message")
        else target.message
    )
    chat_id, source_id = _ids(message)
    bot = getattr(message, "bot", None)
    if reply_markup is None:
        current = getattr(message, "reply_markup", None)
        if isinstance(current, InlineKeyboardMarkup):
            reply_markup = current
        elif chat_id is not None:
            reply_markup = _back(chat_id)

    async def send():
        if photo and photo.is_file():
            if len(text) <= 1024:
                return [
                    await message.answer_photo(
                        photo=FSInputFile(photo),
                        caption=text,
                        reply_markup=reply_markup,
                        **kwargs
                    )
                ]
            banner = await message.answer_photo(photo=FSInputFile(photo))
            try:
                content = await message.answer(
                    text,
                    reply_markup=reply_markup,
                    **dict({"link_preview_options": NO_PREVIEW}, **kwargs)
                )
            except Exception:
                if bot is not None and chat_id is not None:
                    _, mid = _ids(banner)
                    if mid is not None:
                        await _delete(bot, chat_id, mid)
                raise
            return [banner, content]
        options = dict(kwargs)
        options.setdefault("link_preview_options", NO_PREVIEW)
        return [await message.answer(text, reply_markup=reply_markup, **options)]

    return await _render(bot, chat_id, source_id, send)


async def screen_send(bot, chat_id, text, **kwargs):
    if kwargs.get("reply_markup") is None:
        kwargs["reply_markup"] = _back(chat_id)

    async def send():
        return [await bot.send_message(chat_id, text, **kwargs)]

    return await _render(bot, chat_id, None, send)
