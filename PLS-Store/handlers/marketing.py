"""Administrator broadcast preview/confirmation and per-product sale prices."""

import html
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import ADMIN_ID
from services.db import db, get_product
from services.banners import themed_reply as screen_reply
from services.pricing import percent, product_price, price_label, set_discount
from services import broadcasts

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t,
                    callback_data=d,
                    style=(
                        "danger"
                        if d.endswith(":cancel")
                        or d in ("adm:home", "adm:broadcasts", "adm:discounts")
                        else "success" if ":send" in d else "primary"
                    ),
                )
                for t, d in row
            ]
            for row in rows
        ]
    )


STATUS_LABELS = {
    "draft": "Черновик",
    "queued": "В очереди",
    "running": "Отправляется",
    "completed": "Завершена",
    "cancelled": "Остановлена",
}


class Marketing(StatesGroup):
    message = State()
    discount = State()


async def broadcast_card(target, bid):
    data = broadcasts.stats(bid, ADMIN_ID)
    if not data:
        await screen_reply(target, "Рассылка не найдена")
        return
    if data["status"] == "draft":
        with db() as conn:
            count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        await screen_reply(
            target,
            data["body"],
            reply_markup=kb(
                [
                    [(f"Отправить всем ({count})", f"cast:{bid}:send")],
                    [("Изменить текст", f"cast:{bid}:edit")],
                    [("Отмена", f"cast:{bid}:cancel")],
                ]
            ),
        )
        return
    counts = data["counts"]
    text = (
        f"📣 <b>Рассылка #{bid}</b>\nСтатус: {STATUS_LABELS.get(data['status'],data['status'])}\nПолучателей: {data['total']}\n"
        f"Доставлено: {counts.get('sent',0)}\nНе отправлено: {counts.get('pending',0)}\n"
        f"Отправляется: {counts.get('sending',0)}\nБот заблокирован: {counts.get('blocked',0)}\n"
        f"Ошибки: {counts.get('failed',0)}\nДоставка не подтверждена: {counts.get('unknown',0)}"
    )
    if data["status"] == "cancelled":
        text += "\n\nОставшиеся сообщения не будут отправлены. Уже отправленные не отзываются."
    if counts.get("unknown"):
        text += "\n\nНеопределённые доставки не повторяются автоматически, чтобы не отправить сообщение дважды."
    rows = [[("Обновить", f"cast:{bid}:view")]]
    if data["status"] in ("queued", "running"):
        rows.append([("Остановить", f"cast:{bid}:cancel")])
    rows.append([("← Рассылки", "adm:broadcasts")])
    await screen_reply(target, text, reply_markup=kb(rows))


@router.callback_query(F.data == "adm:broadcasts")
async def broadcast_menu(call, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    await state.clear()
    with db() as conn:
        rows = conn.execute(
            "SELECT id,status FROM broadcasts WHERE admin_id=? ORDER BY id DESC LIMIT 10",
            (ADMIN_ID,),
        ).fetchall()
    buttons = (
        [[("Создать рассылку", "cast:new")]]
        + [
            [
                (
                    f"#{r['id']} · {STATUS_LABELS.get(r['status'],r['status'])}",
                    f"cast:{r['id']}:view",
                )
            ]
            for r in rows
        ]
        + [[("← Админ-панель", "adm:home")]]
    )
    await screen_reply(
        call,
        "📣 <b>Рассылки</b>\n\nСообщение будет отправлено всем зарегистрированным пользователям отдельным сообщением. Сначала проверьте текст в предпросмотре и подтвердите отправку.",
        reply_markup=kb(buttons),
    )


@router.callback_query(F.data == "cast:new")
async def compose(call, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    await state.clear()
    await state.set_state(Marketing.message)
    await screen_reply(
        call,
        "Отправьте текст рассылки (до 4096 символов). Можно использовать форматирование Telegram. На следующем экране будет предпросмотр и подтверждение.",
        reply_markup=kb([[("Отмена", "adm:broadcasts")]]),
    )


@router.message(Marketing.message)
async def receive_broadcast(message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text or len(message.text) > 4096:
        await screen_reply(message, "Отправьте текст длиной до 4096 символов.")
        return
    formatted = getattr(message, "html_text", None)
    body = formatted if isinstance(formatted, str) else html.escape(message.text)
    data = await state.get_data()
    bid = data.get("broadcast_id")
    if bid:
        with db() as conn:
            cur = conn.execute(
                "UPDATE broadcasts SET body=? WHERE id=? AND admin_id=? AND status='draft'",
                (body, bid, ADMIN_ID),
            )
            if not cur.rowcount:
                bid = None
    if not bid:
        bid = broadcasts.draft(ADMIN_ID, body)
    await state.clear()
    await broadcast_card(message, bid)


@router.callback_query(F.data.startswith("cast:"))
async def broadcast_action(call, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    try:
        _, raw, action = call.data.split(":")
        bid = int(raw)
    except (ValueError, TypeError):
        await call.answer()
        return
    data = broadcasts.stats(bid, ADMIN_ID)
    if not data:
        await call.answer("Рассылка не найдена", show_alert=True)
        return
    await call.answer()
    if action == "send":
        broadcasts.enqueue(bid, ADMIN_ID)
    elif action == "cancel":
        broadcasts.cancel(bid, ADMIN_ID)
    elif action == "edit" and data["status"] == "draft":
        await state.set_state(Marketing.message)
        await state.update_data(broadcast_id=bid)
        await screen_reply(
            call,
            "Отправьте новый текст рассылки.",
            reply_markup=kb([[("Отмена", "adm:broadcasts")]]),
        )
        return
    await state.clear()
    await broadcast_card(call, bid)


@router.callback_query(F.data == "adm:discounts")
async def discounts_menu(call, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    await state.clear()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM products ORDER BY sort_order,id LIMIT 80"
        ).fetchall()
    buttons = [
        [(f"{r['name'][:30]} · {price_label(r)}", f"sale:product:{r['id']}")]
        for r in rows
    ]
    await screen_reply(
        call,
        "🏷 <b>Скидки на товары</b>\n\nВыберите товар. Базовая цена сохраняется; скидка применяется к новым оформлениям покупки.",
        reply_markup=kb(buttons + [[("← Админ-панель", "adm:home")]]),
    )


async def discount_card(target, pid):
    product = get_product(pid)
    if not product:
        await screen_reply(target, "Товар не найден")
        return
    try:
        effective = product_price(product)
    except ValueError:
        await screen_reply(
            target,
            "Сначала установите положительную базовую цену в карточке товара.",
            reply_markup=kb([[("← Товары", "adm:products")]]),
        )
        return
    await screen_reply(
        target,
        f"🏷 <b>{html.escape(product['name'])}</b>\n\nБазовая цена: {html.escape(product['price'])}\nСкидка: {percent(product['discount_percent']):g}%\nЦена для покупателя: <b>{effective:.2f}₽</b>\n\nСтарые оформленные покупки и счета сохраняют свою цену.",
        reply_markup=kb(
            [
                [("Установить скидку", f"sale:set:{pid}")],
                [("Убрать скидку", f"sale:clear:{pid}")],
                [("← Скидки", "adm:discounts")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("sale:"))
async def discount_action(call, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    try:
        _, action, raw = call.data.split(":")
        pid = int(raw)
    except (ValueError, TypeError):
        await call.answer()
        return
    await call.answer()
    if not get_product(pid):
        await screen_reply(call, "Товар не найден")
        return
    if action == "set":
        await state.set_state(Marketing.discount)
        await state.update_data(discount_product_id=pid)
        await screen_reply(
            call,
            "Введите скидку от 0 до 99 процентов, например 10 или 15,5. Ноль отключает скидку.",
            reply_markup=kb([[("Отмена", "adm:discounts")]]),
        )
        return
    if action == "clear":
        set_discount(pid, 0, ADMIN_ID)
    await state.clear()
    await discount_card(call, pid)


@router.message(Marketing.discount)
async def receive_discount(message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    pid = data["discount_product_id"]
    try:
        set_discount(pid, message.text or "", ADMIN_ID)
    except ValueError as exc:
        await screen_reply(message, html.escape(str(exc)))
        return
    await state.clear()
    await discount_card(message, pid)
