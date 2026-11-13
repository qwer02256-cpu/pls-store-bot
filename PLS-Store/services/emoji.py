"""Load published Telegram emoji IDs and apply them to outgoing HTML and buttons."""
from __future__ import annotations

import gzip
import html
import io
import json
import logging
import os
import re
from pathlib import Path

from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import InlineKeyboardMarkup

from services.db import get_setting, set_setting

log = logging.getLogger(__name__)
CACHE_KEY = "published_custom_emoji"
DEFAULT_PACK = "PLSstoreemoje"
FLAGS_PACK = "PLSStoreFlags"
# Semantic button icons can differ from the ordinary emoji used in their label.
BUTTON_EMOJI = {
    "profile": "👤", "catalog": "🛍", "catalog:plugins": "🧩",
    "language": "🌐", "faq": "❓", "support": "🎧", "documents": "📄",
    "topup": "💳", "mypurchases": "🛒", "api_info": "💻",
    "promo": "🎟", "referral": "👥", "home": "🏠",
    "lang:set:ru": "🇷🇺", "lang:set:uk": "🇺🇦", "lang:set:en": "🇬🇧",
    "topup:custom": "✏️",
}
ALIASES = {
    "🆔": "👤", "💲": "💰", "💸": "🧾", "💬": "🎧",
    "🗓": "📅", "☑": "✅",
    "📜": "📄",  # Use the published animated document when no scroll was uploaded.
}


def normalized(emoji: str) -> str:
    return re.sub(r"[\U0001f3fb-\U0001f3ff]", "", emoji.replace("\ufe0f", ""))


class PackEmojiMiddleware(BaseRequestMiddleware):
    def __init__(self):
        try:
            cached = json.loads(get_setting(CACHE_KEY, "{}"))
        except (ValueError, TypeError):
            cached = {}
        self.ids = {str(k): str(v) for k, v in cached.items() if str(v).isdigit()} if isinstance(cached, dict) else {}
        self._compile()

    def _compile(self):
        self.render_ids = dict(self.ids)
        for alias, source in ALIASES.items():
            if source in self.ids:
                self.render_ids.setdefault(normalized(alias), self.ids[source])
        alternatives = [re.escape(key) + "\ufe0f?" for key in sorted(self.render_ids, key=len, reverse=True)]
        self.pattern = re.compile("|".join(alternatives)) if alternatives else None

    async def _find_wave_file(self, bot, stickers):
        """Recognize our actual TGS even when assigned the wrong ordinary emoji."""
        path = Path(__file__).resolve().parents[1] / "emoji-pack" / "52_wave.tgs"
        if not path.is_file() or not hasattr(bot, "download"):
            return None
        expected = json.loads(gzip.decompress(path.read_bytes()))
        # New uploads are appended. Limit work if the file has not been uploaded yet.
        for sticker in reversed(stickers[-8:]):
            if not getattr(sticker, "is_animated", False) or not getattr(sticker, "file_id", None):
                continue
            emoji_id = str(getattr(sticker, "custom_emoji_id", ""))
            if not emoji_id.isdigit():
                continue
            try:
                buffer = io.BytesIO()
                await bot.download(sticker.file_id, destination=buffer, timeout=10)
                actual = json.loads(gzip.decompress(buffer.getvalue()))
                if all(actual.get(key) == expected.get(key) for key in ("w", "h", "fr", "op", "layers")):
                    return emoji_id
            except Exception as exc:
                log.warning("Could not identify greeting animation: %s", type(exc).__name__)
        return None

    async def refresh(self, bot) -> int:
        ids = {}
        wave_candidates = []
        names = [raw.strip().rstrip("/").rsplit("/", 1)[-1]
                 for raw in os.getenv("CUSTOM_EMOJI_PACKS", DEFAULT_PACK).split(",") if raw.strip()]
        if FLAGS_PACK not in names:
            names.append(FLAGS_PACK)
        for raw_name in names:
            name = raw_name.strip().rstrip("/").rsplit("/", 1)[-1]
            if not name:
                continue
            sticker_set = await bot.get_sticker_set(name=name, request_timeout=20)
            if sticker_set.sticker_type != "custom_emoji":
                raise ValueError(f"{name} is not a custom emoji set")
            if name != FLAGS_PACK:
                wave_candidates.extend(sticker_set.stickers)
            for sticker in sticker_set.stickers:
                if sticker.emoji and sticker.custom_emoji_id and str(sticker.custom_emoji_id).isdigit():
                    emoji = normalized(sticker.emoji)
                    is_flag = re.fullmatch(r"[🇦-🇿]{2}", emoji) is not None
                    if is_flag and getattr(sticker, "needs_repainting", False):
                        log.warning("Skipping recolored flag in emoji set %s", name)
                        continue
                    if is_flag and name == FLAGS_PACK:
                        ids[emoji] = str(sticker.custom_emoji_id)
                    else:
                        ids.setdefault(emoji, str(sticker.custom_emoji_id))
        if "👋" not in ids:
            wave_id = await self._find_wave_file(bot, wave_candidates)
            if wave_id:
                ids["👋"] = wave_id
        if not ids:
            raise ValueError("Published emoji set contains no usable custom emoji IDs")
        set_setting(CACHE_KEY, json.dumps(ids, ensure_ascii=False))
        self.ids = ids
        self._compile()
        return len(ids)

    def emoji_id(self, emoji: str) -> str | None:
        key = normalized(emoji)
        return self.ids.get(key) or self.ids.get(normalized(ALIASES.get(key, "")))

    def render_html(self, text: str) -> str:
        if not self.pattern:
            return text
        parts = re.split(r"(<[^>]*>)", text)
        protected = []
        for i, part in enumerate(parts):
            if part.startswith("<"):
                match = re.match(r"<(/?)([\w-]+)", part)
                if match and match[2].lower() in {"tg-emoji", "code", "pre"}:
                    if match[1]:
                        if protected:
                            protected.pop()
                    else:
                        protected.append(match[2].lower())
            elif not protected:
                parts[i] = self.pattern.sub(
                    lambda m: f'<tg-emoji emoji-id="{self.render_ids[normalized(m[0])]}">{html.escape(m[0])}</tg-emoji>', part
                )
        return "".join(parts)

    def keyboard(self, markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
        rows = []
        for row in markup.inline_keyboard:
            buttons = []
            for button in row:
                text = button.text
                # Back buttons share destination callbacks with forward navigation.
                is_back = text.startswith(("⬅", "«"))
                icon = self.emoji_id("⬅️") if is_back else self.emoji_id(BUTTON_EMOJI.get(button.callback_data, ""))
                leading = re.match(r"^([^\w\s]+)\s*", text)
                if not icon and leading:
                    icon = self.emoji_id(leading[1])
                if icon:
                    label = text[leading.end():] if leading else text
                    if is_back:
                        label = label.lstrip("« ")
                    # Telegram requires nonempty text even when an icon is supplied.
                    button = button.model_copy(update={"text": label or text, "icon_custom_emoji_id": icon})
                buttons.append(button)
            rows.append(buttons)
        return markup.model_copy(update={"inline_keyboard": rows})

    async def __call__(self, make_request, bot, method):
        updates = {}
        # Explicit entities carry UTF-16 offsets; leave those messages intact.
        for field, entities in (("text", "entities"), ("caption", "caption_entities")):
            value = getattr(method, field, None)
            mode = getattr(method, "parse_mode", None)
            if isinstance(mode, Default):
                mode = bot.default.parse_mode
            if isinstance(value, str) and mode == "HTML" and not getattr(method, entities, None):
                updates[field] = self.render_html(value)
        markup = getattr(method, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup):
            updates["reply_markup"] = self.keyboard(markup)
        media = getattr(method, "media", None)
        if media is not None:
            def render_media(item):
                caption = getattr(item, "caption", None)
                mode = getattr(item, "parse_mode", None)
                if isinstance(mode, Default):
                    mode = bot.default.parse_mode
                if caption and mode == "HTML" and not getattr(item, "caption_entities", None):
                    return item.model_copy(update={"caption": self.render_html(caption)})
                return item
            updates["media"] = [render_media(item) for item in media] if isinstance(media, list) else render_media(media)
        return await make_request(bot, method.model_copy(update=updates) if updates else method)
