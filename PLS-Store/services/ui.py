"""UI helpers: keyboards and text builders with design settings."""
from __future__ import annotations

import html
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.db import get_setting, ensure_user, get_user


def btn(key: str, default: str) -> str:
    return get_setting(key, default)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=btn("btn_profile", "👤 Профиль"), callback_data="profile"),
            InlineKeyboardButton(text=btn("btn_shop", "🛒 Магазин"), callback_data="catalog"),
        ],
        [
            InlineKeyboardButton(text=btn("btn_faq", "❓ FAQ"), callback_data="faq"),
            InlineKeyboardButton(text=btn("btn_support", "🎧 Поддержка"), callback_data="support"),
        ],
    ])


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn("btn_topup", "💳 Пополнить баланс"), callback_data="topup")],
        [
            InlineKeyboardButton(text=btn("btn_purchases", "🛒 Мои покупки"), callback_data="mypurchases"),
            InlineKeyboardButton(text=btn("btn_api", "⚡ API"), callback_data="api_info"),
        ],
        [
            InlineKeyboardButton(text=btn("btn_promo", "🎟 Промокод"), callback_data="promo"),
            InlineKeyboardButton(text=btn("btn_referral", "🔗 Рефералка"), callback_data="referral"),
        ],
        [InlineKeyboardButton(text=btn("btn_back", "⬅️ Назад"), callback_data="home")],
    ])


def back_kb(to: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn("btn_back", "⬅️ Назад"), callback_data=to)]
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
    rows.append([InlineKeyboardButton(text=btn("btn_back", "⬅️ Назад"), callback_data="profile")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
