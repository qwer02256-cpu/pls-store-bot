import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.client.default import DefaultBotProperties
from aiogram.methods import EditMessageMedia, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, MessageEntity

from services.emoji import CACHE_KEY, PackEmojiMiddleware


class EmojiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ids = {'👤': '111', '🎧': '222', '⬅': '333', '🛍': '444', '🌐': '555', '💰': '666'}
        with patch('services.emoji.get_setting', return_value=json.dumps(self.ids)):
            self.mw = PackEmojiMiddleware()
        self.bot = SimpleNamespace(default=DefaultBotProperties(parse_mode='HTML'))

    def test_html_preserves_existing_entities_and_attributes(self):
        original = '<b>👤 Profile</b> <code>👤</code><pre>👤</pre><a href="https://example.org/👤">👤</a><tg-emoji emoji-id="999">👤</tg-emoji>'
        rendered = self.mw.render_html(original)
        self.assertEqual(rendered.count('emoji-id="111"'), 2)
        self.assertIn('<code>👤</code><pre>👤</pre>', rendered)
        self.assertIn('href="https://example.org/👤"', rendered)
        self.assertIn('<tg-emoji emoji-id="999">👤</tg-emoji>', rendered)
        self.assertIn('emoji-id="666"', self.mw.render_html('💲 Баланс'))

    def test_buttons_preserve_url_color_and_custom_label(self):
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='🎧 Support', url='https://t.me/support', style='danger'),
            InlineKeyboardButton(text='« Назад', callback_data='catalog', style='danger'),
            InlineKeyboardButton(text='🛒 Магазин', callback_data='catalog'),
            InlineKeyboardButton(text='🇺🇦 Українська', callback_data='lang:set:uk'),
        ]])
        result = self.mw.keyboard(markup).inline_keyboard[0]
        self.assertEqual(result[0].icon_custom_emoji_id, '222')
        self.assertEqual(result[0].text, 'Support')
        self.assertEqual(result[0].url, 'https://t.me/support')
        self.assertEqual(result[0].style, 'danger')
        self.assertEqual(result[1].icon_custom_emoji_id, '333')
        self.assertEqual(result[2].icon_custom_emoji_id, '444')
        self.assertEqual(result[3].text, '🇺🇦 Українська')
        self.assertEqual(markup.inline_keyboard[0][0].text, '🎧 Support')

    def test_screenshot_icons_and_colored_flag_ids(self):
        icons = ['🔒', '📜', '💵', '🎧', '🗓', '🚀', '☑', '✏', '🇷🇺', '🇺🇦', '🇬🇧']
        ids = {emoji: str(1000 + i) for i, emoji in enumerate(icons)}
        with patch('services.emoji.get_setting', return_value=json.dumps(ids)):
            middleware = PackEmojiMiddleware()
        for emoji in icons:
            self.assertIn(f'emoji-id="{ids[emoji]}"', middleware.render_html(emoji + ' Текст'))
        rows = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='🇷🇺 Русский ✓', callback_data='lang:set:ru'),
            InlineKeyboardButton(text='🇺🇦 Українська', callback_data='lang:set:uk'),
            InlineKeyboardButton(text='🇬🇧 English', callback_data='lang:set:en'),
            InlineKeyboardButton(text='✏️ Своя сумма', callback_data='topup:custom'),
        ]])
        result = middleware.keyboard(rows).inline_keyboard[0]
        for button, glyph in zip(result, ['🇷🇺', '🇺🇦', '🇬🇧', '✏']):
            self.assertEqual(button.icon_custom_emoji_id, ids[glyph])
            self.assertNotIn(glyph, button.text)
        self.assertEqual(result[0].text, 'Русский ✓')

    async def test_message_and_edit_media_are_transformed(self):
        request = AsyncMock(return_value='ok')
        original = SendMessage(chat_id=1, text='👤 Profile')
        self.assertEqual(await self.mw(request, self.bot, original), 'ok')
        self.assertIn('tg-emoji', request.call_args.args[1].text)
        self.assertEqual(original.text, '👤 Profile')
        media = EditMessageMedia(chat_id=1, message_id=2, media=InputMediaPhoto(media='file', caption='👤 Profile'))
        await self.mw(request, self.bot, media)
        self.assertIn('tg-emoji', request.call_args.args[1].media.caption)

    async def test_plain_text_and_explicit_entity_offsets_are_preserved(self):
        request = AsyncMock()
        for method in [SendMessage(chat_id=1, text='👤', parse_mode=None),
                       SendMessage(chat_id=1, text='👤', entities=[MessageEntity(type='bold', offset=0, length=2)])]:
            await self.mw(request, self.bot, method)
            self.assertEqual(request.call_args.args[1].text, '👤')

    async def test_refresh_uses_published_ids_and_caches_them(self):
        api = SimpleNamespace(get_sticker_set=AsyncMock(return_value=SimpleNamespace(sticker_type='custom_emoji', stickers=[
            SimpleNamespace(emoji='⬅️', custom_emoji_id='777'),
            SimpleNamespace(emoji='👤', custom_emoji_id='888')])) )
        with patch('services.emoji.set_setting') as save, patch.dict('os.environ', {'CUSTOM_EMOJI_PACKS':'https://t.me/addemoji/PLSstoreemoje'}):
            self.assertEqual(await self.mw.refresh(api), 2)
            save.assert_called_once()
            self.assertEqual(save.call_args.args[0], CACHE_KEY)
            self.assertEqual(self.mw.emoji_id('⬅️'), '777')
            api.get_sticker_set.assert_awaited_once_with(name='PLSstoreemoje', request_timeout=20)

    async def test_refresh_failure_preserves_cached_ids(self):
        api = SimpleNamespace(get_sticker_set=AsyncMock(side_effect=RuntimeError('unavailable')))
        with self.assertRaises(RuntimeError):
            await self.mw.refresh(api)
        self.assertEqual(self.mw.emoji_id('👤'), '111')

    def test_corrupt_cache_falls_back_to_standard_emoji(self):
        for value in ['bad json', '[]', '{}']:
            with patch('services.emoji.get_setting', return_value=value):
                middleware = PackEmojiMiddleware()
            self.assertEqual(middleware.render_html('👤 Профиль'), '👤 Профиль')


if __name__ == '__main__':
    unittest.main()
