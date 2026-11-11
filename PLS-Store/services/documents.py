"""Shared document links for first entry and the FAQ."""
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton

from services.db import get_setting

DEFAULT_URLS = {
    'privacy': 'https://telegra.ph/Politika-konfidencialnosti-PLS-STORE-11-07',
    'agreement': 'https://telegra.ph/Polzovatelskoe-soglashenie-PLS-STORE-11-07',
    'terms': 'https://telegra.ph/Tovary-i-tarify-PLS-STORE-11-07',
}
LABELS = {
    'ru': ('🔒 Политика конфиденциальности', '📜 Пользовательское соглашение', '💵 Товары и тарифы'),
    'uk': ('🔒 Політика конфіденційності', '📜 Угода користувача', '💵 Товари й тарифи'),
    'en': ('🔒 Privacy Policy', '📜 User Agreement', '💵 Products and Pricing'),
}


def valid_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except (ValueError, TypeError):
        return False


def document_rows(lang='ru', license_url=''):
    rows = []
    for key, title in zip(DEFAULT_URLS, LABELS.get(lang, LABELS['ru'])):
        custom = get_setting(f'document_{key}_url', '').strip()
        if key == 'agreement' and not custom:
            custom = license_url.strip() or get_setting('license_url', '').strip()
        url = custom if valid_url(custom) else DEFAULT_URLS[key]
        rows.append([InlineKeyboardButton(text=title, url=url)])
    return rows
