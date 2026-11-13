"""Edit the current store screen without Telegram's message-deletion animation."""

import asyncio
import logging
import re
from types import SimpleNamespace
from weakref import WeakValueDictionary
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    Message,
    FSInputFile,
    InputMediaPhoto,
    LinkPreviewOptions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from services.db import db, get_user_language

log = logging.getLogger(__name__)
_locks = WeakValueDictionary()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _ids(message):
    chat = getattr(getattr(message, "chat", None), "id", None)
    mid = getattr(message, "message_id", None)
    return (
        chat if isinstance(chat, int) else None,
        mid if isinstance(mid, int) else None,
    )


def _kind(message):
    photos = getattr(message, "photo", None)
    if isinstance(photos, (list, tuple)) and photos:
        return "photo"
    if isinstance(getattr(message, "text", None), str):
        return "text"
    return "unknown"


def _ref(chat, mid):
    return SimpleNamespace(chat=SimpleNamespace(id=chat), message_id=mid)


async def _edit(call, chat, mid):
    try:
        return await call()
    except TelegramBadRequest as exc:
        if "message is not modified" in exc.message.lower():
            return _ref(chat, mid)
        raise


async def _disable(bot, chat, mid):
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat, message_id=mid, reply_markup=None
        )
    except Exception:
        pass


def _back(chat_id):
    label = {"ru": "← Назад", "en": "← Back", "uk": "← Назад"}[
        get_user_language(chat_id)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data="home", style="danger")]
        ]
    )


async def _render(bot, chat, hint, text, markup, photo, kwargs, send, force_new=False):
    if chat is None:
        return (await send())[-1]
    lock = _locks.setdefault(chat, asyncio.Lock())
    async with lock:
        with db() as conn:
            old = [
                dict(r)
                for r in conn.execute(
                    "SELECT message_id,kind FROM chat_screens WHERE chat_id=? ORDER BY message_id",
                    (chat,),
                )
            ]
        if not old and hint:
            old = [dict(message_id=hint[0], kind=hint[1])]
        elif hint:
            for row in old:
                if row["message_id"] == hint[0] and row["kind"] == "unknown":
                    row["kind"] = hint[1]
        active = []
        result = None
        has_photo = photo is not None and photo.is_file()
        text_options = {
            k: v
            for k, v in kwargs.items()
            if k in ("parse_mode", "entities", "link_preview_options")
        }
        text_options.setdefault("link_preview_options", NO_PREVIEW)
        caption_options = {
            k: v for k, v in kwargs.items() if k in ("parse_mode", "caption_entities")
        }

        async def edit_text(mid):
            return await _edit(
                lambda: bot.edit_message_text(
                    chat_id=chat,
                    message_id=mid,
                    text=text,
                    reply_markup=markup,
                    **text_options
                ),
                chat,
                mid,
            )

        async def edit_photo(mid, caption):
            if has_photo:
                media = InputMediaPhoto(media=FSInputFile(photo), caption=caption)
                return await _edit(
                    lambda: bot.edit_message_media(
                        chat_id=chat,
                        message_id=mid,
                        media=media,
                        reply_markup=markup if caption else None,
                    ),
                    chat,
                    mid,
                )
            return await _edit(
                lambda: bot.edit_message_caption(
                    chat_id=chat,
                    message_id=mid,
                    caption=caption,
                    reply_markup=markup if caption else None,
                    **caption_options
                ),
                chat,
                mid,
            )

        async def split_photo(mid, previous_kind):
            # Send the long text first, so a failed send leaves the old caption usable.
            content = await bot.send_message(
                chat, text, reply_markup=markup, **text_options
            )
            _, content_id = _ids(content)
            pair = (
                [(mid, "photo"), (content_id, "text")] if content_id is not None else []
            )
            try:
                await edit_photo(mid, "")
            except Exception as exc:
                log.warning("Banner update deferred: %s", type(exc).__name__)
                if previous_kind == "text":
                    await _disable(bot, chat, mid)
                    pair = [(content_id, "text")] if content_id is not None else []
            return content, pair

        try:
            if old and not force_new:
                current = old[-1]
                photos = [r for r in old if r["kind"] == "photo"]
                texts = [r for r in old if r["kind"] == "text"]
                # Once a long caption needs a companion text, reuse both messages.
                # Collapsing the pair would require deletion and trigger the effect.
                if photos and texts:
                    banner = photos[-1]
                    content = texts[-1]
                    await edit_photo(banner["message_id"], "")
                    result = await edit_text(content["message_id"])
                    active = [
                        (banner["message_id"], "photo"),
                        (content["message_id"], "text"),
                    ]
                elif has_photo or current["kind"] == "photo":
                    mid = current["message_id"]
                    if len(text) <= 1024:
                        result = await edit_photo(mid, text)
                        active = [(mid, "photo")]
                    else:
                        result, active = await split_photo(mid, current["kind"])
                else:
                    mid = current["message_id"]
                    try:
                        result = await edit_text(mid)
                        active = [(mid, "text")]
                    except TelegramBadRequest as exc:
                        if (
                            current["kind"] != "unknown"
                            or "no text" not in exc.message.lower()
                        ):
                            raise
                        if len(text) <= 1024:
                            result = await edit_photo(mid, text)
                            active = [(mid, "photo")]
                        else:
                            result, active = await split_photo(mid, "photo")
        except (TelegramBadRequest, TelegramForbiddenError):
            # If Telegram refuses to edit an old/deleted screen, open a new one.
            # Disable old buttons instead of deleting any messages.
            result = None
            active = []
        if result is None:
            messages = await send()
            result = messages[-1]
            active = [
                (_ids(m)[1], _kind(m))
                for m in messages
                if _ids(m)[0] == chat and _ids(m)[1] is not None
            ]
        if not active:
            return result
        active_ids = {mid for mid, _ in active}
        for previous in old:
            if previous["message_id"] not in active_ids:
                await _disable(bot, chat, previous["message_id"])
        with db() as conn:
            conn.execute("DELETE FROM chat_screens WHERE chat_id=?", (chat,))
            conn.executemany(
                "INSERT INTO chat_screens(chat_id,message_id,kind) VALUES(?,?,?)",
                [(chat, mid, kind) for mid, kind in active],
            )
        return result


async def screen_reply(target, text, reply_markup=None, photo=None, **kwargs):
    is_callback = not isinstance(target, Message) and hasattr(target, "message")
    message = target.message if is_callback else target
    chat, mid = _ids(message)
    bot = getattr(message, "bot", None)
    if reply_markup is None:
        current = getattr(message, "reply_markup", None)
        if isinstance(current, InlineKeyboardMarkup):
            reply_markup = current
        elif chat is not None:
            reply_markup = _back(chat)
    source_is_bot = (
        getattr(getattr(message, "from_user", None), "is_bot", False) is True
    )
    hint = (
        (mid, _kind(message))
        if mid is not None and (is_callback or source_is_bot)
        else None
    )
    command_text = getattr(message, "text", None)
    force_new = (
        not is_callback
        and not source_is_bot
        and isinstance(command_text, str)
        and re.match(r"^/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?(?:\s|$)", command_text)
        is not None
    )
    options = dict(kwargs)
    options.setdefault("link_preview_options", NO_PREVIEW)

    async def send():
        if photo and photo.is_file():
            photo_options = {
                k: v
                for k, v in kwargs.items()
                if k not in ("link_preview_options", "disable_web_page_preview")
            }
            if len(text) <= 1024:
                return [
                    await message.answer_photo(
                        photo=FSInputFile(photo),
                        caption=text,
                        reply_markup=reply_markup,
                        **photo_options
                    )
                ]
            banner = await message.answer_photo(photo=FSInputFile(photo))
            content = await message.answer(text, reply_markup=reply_markup, **options)
            return [banner, content]
        return [await message.answer(text, reply_markup=reply_markup, **options)]

    return await _render(
        bot, chat, hint, text, reply_markup, photo, kwargs, send, force_new=force_new
    )


async def screen_send(bot, chat_id, text, photo=None, **kwargs):
    options = dict(kwargs)
    markup = options.pop("reply_markup", None) or _back(chat_id)
    options.setdefault("link_preview_options", NO_PREVIEW)

    async def send():
        if photo and photo.is_file():
            photo_options = {k: v for k, v in options.items() if k not in ("link_preview_options", "disable_web_page_preview")}
            if len(text) <= 1024:
                return [await bot.send_photo(chat_id, photo=FSInputFile(photo), caption=text, reply_markup=markup, **photo_options)]
            banner = await bot.send_photo(chat_id, photo=FSInputFile(photo))
            content = await bot.send_message(chat_id, text, reply_markup=markup, **options)
            return [banner, content]
        return [await bot.send_message(chat_id, text, reply_markup=markup, **options)]

    return await _render(bot, chat_id, None, text, markup, photo, options, send)
