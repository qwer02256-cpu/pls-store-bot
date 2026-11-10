"""Payment and balance handlers for Platega + internal balance."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from services.db import (
    db, ensure_user, change_balance, get_setting, parse_price_rub, add_purchase, get_product
)
from services.ui import topup_amounts_kb, profile_kb, back_kb, btn
from services.platega import PlategaClient, PlategaError
from config import PLATEGA_MERCHANT_ID, PLATEGA_SECRET, PLATEGA_CALLBACK_BASE, ADMIN_ID

log = logging.getLogger("pls-store.payments")
router = Router()

platega: Optional[PlategaClient] = None
if PLATEGA_MERCHANT_ID and PLATEGA_SECRET:
    try:
        platega = PlategaClient(PLATEGA_MERCHANT_ID, PLATEGA_SECRET)
    except Exception as e:
        log.error("Platega init failed: %s", e)


class TopupStates(StatesGroup):
    custom_amount = State()


@router.callback_query(F.data == "topup")
async def cb_topup(callback: CallbackQuery):
    await callback.answer()
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    text = (
        f"<b>{btn('topup_title', '💳 Пополнение баланса')}</b>\n\n"
        "Выберите сумму или укажите свою.\n"
        "Оплата через Platega (СБП / карты).\n"
        "Средства зачисляются автоматически после подтверждения."
    )
    await callback.message.answer(text, reply_markup=topup_amounts_kb())


@router.callback_query(F.data.startswith("topup:"))
async def cb_topup_amount(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    part = callback.data.split(":", 1)[1]
    if part == "custom":
        await state.set_state(TopupStates.custom_amount)
        await callback.message.answer(
            "Введите сумму пополнения в рублях (минимум "
            f"{get_setting('min_topup', '100')}₽):"
        )
        return
    try:
        amount = float(part)
    except ValueError:
        return
    await create_topup(callback, amount, bot)


@router.message(TopupStates.custom_amount)
async def topup_custom(message: Message, state: FSMContext, bot: Bot):
    try:
        amount = float((message.text or "").replace(",", ".").strip())
    except ValueError:
        await message.answer("Введите число, например 500")
        return
    min_a = float(get_setting("min_topup", "100"))
    if amount < min_a:
        await message.answer(f"Минимальная сумма: {min_a:.0f}₽")
        return
    await state.clear()
    # create fake callback-like flow
    class Fake:
        from_user = message.from_user
        message = message
        async def answer(self, *a, **k): pass
    await create_topup(Fake(), amount, bot)


async def create_topup(target, amount: float, bot: Bot):
    user = target.from_user
    ensure_user(user.id, user.username, user.first_name)
    if not platega:
        await target.message.answer(
            "⚠️ Платёжная система пока не настроена.\n"
            "Администратор должен указать PLATEGA_MERCHANT_ID и PLATEGA_SECRET."
        )
        return

    payload = f"topup:{user.id}:{amount}"
    try:
        data = await platega.create_payment(
            amount=amount,
            description=f"Пополнение PLS Store #{user.id}",
            payload=payload,
            return_url=f"https://t.me/{(await bot.me()).username}",
            failed_url=f"https://t.me/{(await bot.me()).username}",
            payment_method=2,  # SBP
            metadata={
                "userId": str(user.id),
                "userName": f"@{user.username}" if user.username else str(user.id),
            },
        )
    except PlategaError as e:
        log.exception("create payment failed")
        await target.message.answer(f"Ошибка создания платежа: {e}")
        return

    tx_id = data.get("transactionId") or data.get("id")
    redirect = data.get("redirect") or data.get("url")
    with db() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, amount, platega_id, status, payload, redirect_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, amount, tx_id, data.get("status", "PENDING"), payload, redirect),
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=redirect)] if redirect else [],
        [InlineKeyboardButton(text="🔄 Проверить статус", callback_data=f"paystatus:{tx_id}")],
        [InlineKeyboardButton(text="⬅️ В профиль", callback_data="profile")],
    ])
    await target.message.answer(
        f"💳 Счёт на <b>{amount:.0f}₽</b> создан.\n"
        f"ID: <code>{tx_id}</code>\n\n"
        "Нажмите кнопку ниже для оплаты. После успешной оплаты баланс пополнится автоматически.",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("paystatus:"))
async def cb_pay_status(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    tx_id = callback.data.split(":", 1)[1]
    if not platega:
        await callback.message.answer("Platega не настроена")
        return
    try:
        data = await platega.get_status(tx_id)
    except PlategaError as e:
        await callback.message.answer(f"Ошибка: {e}")
        return
    status = (data.get("status") or "").upper()
    await callback.message.answer(f"Статус платежа <code>{tx_id}</code>: <b>{status}</b>")
    if status == "CONFIRMED":
        await process_confirmed_payment(tx_id, data, bot)


async def process_confirmed_payment(tx_id: str, data: dict, bot: Bot) -> bool:
    """Idempotent credit. Returns True if newly credited."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM payments WHERE platega_id=? AND status != 'CONFIRMED'",
            (tx_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False  # already processed or unknown
        amount = float(row["amount"])
        user_id = row["user_id"]
        conn.execute(
            "UPDATE payments SET status='CONFIRMED', confirmed_at=CURRENT_TIMESTAMP WHERE platega_id=?",
            (tx_id,),
        )
        conn.commit()
    try:
        new_bal = change_balance(user_id, amount, f"Platega topup {tx_id}", payment_id=row["id"])
    except Exception:
        log.exception("balance credit failed")
        return False
    try:
        await bot.send_message(
            user_id,
            f"✅ Баланс пополнен на <b>{amount:.0f}₽</b>.\n"
            f"Текущий баланс: <b>{new_bal:.0f}₽</b>",
        )
    except Exception:
        pass
    # referral bonus
    u = ensure_user(user_id)
    if u.get("referrer_id"):
        pct = float(get_setting("referral_percent", "10")) / 100.0
        bonus = round(amount * pct, 2)
        if bonus > 0:
            change_balance(u["referrer_id"], bonus, f"Реферал {user_id} topup")
            with db() as conn:
                conn.execute(
                    "UPDATE users SET referral_earnings = referral_earnings + ? WHERE user_id=?",
                    (bonus, u["referrer_id"]),
                )
    return True


# Buy from balance
@router.callback_query(F.data.startswith("buybal:"))
async def cb_buy_balance(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        product_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        return
    product = get_product(product_id)  # need to expose from original
    if not product:
        await callback.message.answer("Товар недоступен")
        return
    # placeholder - get_product is in main
    await callback.message.answer("Покупка с баланса — см. основной обработчик.")
