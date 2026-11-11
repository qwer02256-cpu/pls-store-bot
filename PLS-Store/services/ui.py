"""UI helpers: keyboards and text builders with design settings."""
from __future__ import annotations

import html
import re
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.db import get_setting, ensure_user, get_user


def btn(key: str, default: str) -> str:
    """Return button text without HTML custom-emoji wrappers."""
    value = get_setting(key, default)
    # The custom emoji is rendered separately by icon_custom_emoji_id.
    # Remove its fallback character from the label to avoid showing it twice.
    clean = re.sub(r'<tg-emoji\s+emoji-id="\d+">.*?</tg-emoji>', '', value, flags=re.DOTALL).strip()
    return clean or default


def design_button(key: str, default: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    """Build a configured button with optional Premium Emoji icon and Bot API style."""
    emoji_id = get_setting(f"{key}_icon_custom_emoji_id", "").strip()
    kwargs = {"text": btn(key, default), "callback_data": callback_data}
    if emoji_id.isdigit():
        kwargs["icon_custom_emoji_id"] = emoji_id
    if style in {"primary", "success", "danger"}:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def main_menu_kb() -> InlineKeyboardMarkup:
    # Layout and colors match the supplied reference: blue/green pairs,
    # followed by a full-width red support button.
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            design_button("btn_profile", "👤 Профиль", "profile", "primary"),
            design_button("btn_shop", "🛒 Магазин", "catalog", "success"),
        ],
        [
            design_button("btn_language", "🌐 Язык", "language", "primary"),
            design_button("btn_faq", "❓ FAQ", "faq", "success"),
        ],
        [
            design_button("btn_support", "🎧 Поддержка", "support", "danger"),
        ],
    ])


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [design_button("btn_topup", "💳 Пополнить баланс", "topup")],
        [
            design_button("btn_purchases", "🛒 Мои покупки", "mypurchases"),
            design_button("btn_api", "⚡ API", "api_info"),
        ],
        [
            design_button("btn_promo", "🎟 Промокод", "promo"),
            design_button("btn_referral", "🔗 Рефералка", "referral"),
        ],
        [design_button("btn_back", "⬅️ Назад", "home")],
    ])


def back_kb(to: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [design_button("btn_back", "⬅️ Назад", to)]
    ])


def profile_text(user_id: int, tg_user) -> str:
    u = ensure_user(user_id, tg_user.username, tg_user.first_name)
    name = html.escape(tg_user.full_name or u.get("first_name") or "—")
    uname = f"@{html.escape(tg_user.username)}" if tg_user.username else "—"
    return (
        f"<b>{btn('profile_title', '👤 Профиль')}</b>\n\n"
        f"👤 {name}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"💰 Баланс: <b>{u['balance']:.0f}₽</b>\n"
        f"💸 Всего потрачено: <b>{u['total_spent']:.0f}₽</b>\n"
        f"🛒 Покупок: <b>{u['purchases_count']}</b>\n"
        f"🎁 Рефералов: <b>0</b> · заработано <b>{u['referral_earnings']:.0f}₽</b>\n"
    )


def topup_amounts_kb() -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton(text="✏️ Своя сумма", callback_data="topup:custom")])
    rows.append([design_button("btn_back", "⬅️ Назад", "profile")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
