"""UI helpers: keyboards and text builders with design settings."""
from __future__ import annotations

import html
import re
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.db import get_setting, ensure_user, get_user, get_user_language


def btn(key: str, default: str) -> str:
    """Return button text without HTML custom-emoji wrappers."""
    value = get_setting(key, default)
    # The custom emoji is rendered separately by icon_custom_emoji_id.
    # Remove its fallback character from the label to avoid showing it twice.
    clean = re.sub(r'<tg-emoji\s+emoji-id="\d+">.*?</tg-emoji>', '', value, flags=re.DOTALL).strip()
    return clean or default


def design_button(key: str, default: str, callback_data: str, style: str | None = None, text_override: str | None = None) -> InlineKeyboardButton:
    """Build a configured button with optional Premium Emoji icon and Bot API style."""
    emoji_id = get_setting(f"{key}_icon_custom_emoji_id", "").strip()
    kwargs = {"text": text_override if text_override is not None else btn(key, default), "callback_data": callback_data}
    if emoji_id.isdigit():
        kwargs["icon_custom_emoji_id"] = emoji_id
    if style in {"primary", "success", "danger"}:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def main_menu_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    # Layout and colors match the supplied reference: blue/green pairs,
    # followed by a full-width red support button.
    labels = {
        "ru": ("👤 Профиль", "🛒 Магазин", "🌐 Язык", "❓ FAQ", "🎧 Поддержка"),
        "en": ("👤 Profile", "🛒 Shop", "🌐 Language", "❓ FAQ", "🎧 Support"),
    }
    profile, shop, language, faq, support = labels.get(lang, labels["ru"])
    return InlineKeyboardMarkup(inline_keyboard=[
        [design_button("btn_profile", profile, "profile", "primary", profile), design_button("btn_shop", shop, "catalog", "success", shop)],
        [design_button("btn_language", language, "language", "primary", language), design_button("btn_faq", faq, "faq", "success", faq)],
        [design_button("btn_support", support, "support", "danger", support)],
    ])


def profile_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    labels = {
        "ru": ("💳 Пополнить баланс", "🛒 Мои покупки", "⚡ API", "🎟 Промокод", "🔗 Рефералка", "⬅️ Назад"),
        "en": ("💳 Add balance", "🛒 My purchases", "⚡ API", "🎟 Promo code", "🔗 Referrals", "⬅️ Back"),
    }
    topup, purchases, api, promo, referral, back = labels.get(lang, labels["ru"])
    return InlineKeyboardMarkup(inline_keyboard=[
        [design_button("btn_topup", topup, "topup", text_override=topup)],
        [design_button("btn_purchases", purchases, "mypurchases", text_override=purchases), design_button("btn_api", api, "api_info", text_override=api)],
        [design_button("btn_promo", promo, "promo", text_override=promo), design_button("btn_referral", referral, "referral", text_override=referral)],
        [design_button("btn_back", back, "home", text_override=back)],
    ])


def back_kb(to: str = "home", lang: str = "ru") -> InlineKeyboardMarkup:
    label = "⬅️ Back" if lang == "en" else "⬅️ Назад"
    return InlineKeyboardMarkup(inline_keyboard=[[design_button("btn_back", label, to, text_override=label)]])


def profile_text(user_id: int, tg_user, lang: str = "ru") -> str:
    u = ensure_user(user_id, tg_user.username, tg_user.first_name)
    name = html.escape(tg_user.full_name or u.get("first_name") or "—")
    uname = f"@{html.escape(tg_user.username)}" if tg_user.username else "—"
    if lang == "en":
        return (f"<b>👤 Profile</b>\n\n👤 {name}\n🆔 <code>{user_id}</code>\n"
                f"💰 Balance: <b>{u['balance']:.0f}₽</b>\n💸 Total spent: <b>{u['total_spent']:.0f}₽</b>\n"
                f"🛒 Purchases: <b>{u['purchases_count']}</b>\n🎁 Referrals: <b>0</b> · earned <b>{u['referral_earnings']:.0f}₽</b>\n")
    return (f"<b>{btn('profile_title', '👤 Профиль')}</b>\n\n👤 {name}\n🆔 <code>{user_id}</code>\n"
            f"💰 Баланс: <b>{u['balance']:.0f}₽</b>\n💸 Всего потрачено: <b>{u['total_spent']:.0f}₽</b>\n"
            f"🛒 Покупок: <b>{u['purchases_count']}</b>\n🎁 Рефералов: <b>0</b> · заработано <b>{u['referral_earnings']:.0f}₽</b>\n")


def topup_amounts_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    amounts = [100, 300, 500, 1000, 2000, 5000]
    rows = []
    row = []
    for a in amounts:
        row.append(InlineKeyboardButton(text=f"{a}₽", callback_data=f"topup:{a}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=("✏️ Custom amount" if lang == "en" else "✏️ Своя сумма"), callback_data="topup:custom")])
    rows.append([design_button("btn_back", ("⬅️ Back" if lang == "en" else "⬅️ Назад"), "profile", text_override=("⬅️ Back" if lang == "en" else "⬅️ Назад"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)
