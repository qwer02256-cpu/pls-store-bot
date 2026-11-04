import asyncio
import html
import logging
import os
import sqlite3
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("pls-store")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
REVIEWS_URL = os.getenv("REVIEWS_URL", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "data/store.db"))
_BASE_DIR = Path(__file__).resolve().parent
_banner_env = os.getenv("BANNER_PATH", "").strip()
BANNER_PATH = Path(_banner_env) if _banner_env else (_BASE_DIR / "assets" / "banner.png")
if not BANNER_PATH.is_absolute():
    BANNER_PATH = _BASE_DIR / BANNER_PATH

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError("ADMIN_ID должен быть числовым Telegram ID администратора")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                description TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                product_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛒 Каталог товаров", callback_data="catalog")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="support")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")]
    ])


def get_products():
    with db() as conn:
        return conn.execute(
            "SELECT id, name, price, description FROM products WHERE active=1 ORDER BY id DESC"
        ).fetchall()


def get_product(product_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT id, name, price, description FROM products WHERE id=? AND active=1",
            (product_id,),
        ).fetchone()


def get_order_count(user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM orders WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row["total"])


async def safe_edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Безопасно показывает текстовый экран.
    Если предыдущее сообщение — фото (главное меню), удаляем его и шлём новое.
    Иначе пробуем edit_text, при неудаче — удаляем и шлём новое.
    """
    message = callback.message
    has_photo = bool(getattr(message, "photo", None))

    if has_photo:
        try:
            await message.delete()
        except Exception:
            log.warning("Не удалось удалить фото-сообщение")
        await message.answer(text, reply_markup=reply_markup)
        return

    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception as exc:
        log.warning("edit_text не удался, отправляю новое сообщение: %s", exc)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(text, reply_markup=reply_markup)


async def show_home(target, first_name: str):
    text = (
        "<b>PLS Store</b>\n"
        "Плагины и дополнения для FunPayCardinal\n\n"
        f"Добро пожаловать, {html.escape(first_name)}!"
    )

    if isinstance(target, Message):
        message = target
    else:
        message = target.message
        try:
            await message.delete()
        except Exception:
            log.warning("Не удалось удалить предыдущее сообщение главного меню")

    if BANNER_PATH.is_file():
        await message.answer_photo(
            photo=FSInputFile(BANNER_PATH),
            caption=text,
            reply_markup=main_menu(),
        )
    else:
        log.warning("Баннер не найден: %s", BANNER_PATH)
        await message.answer(text, reply_markup=main_menu())


dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await show_home(message, message.from_user.first_name or "покупатель")


@dp.callback_query(F.data == "home")
async def cb_home(callback: CallbackQuery):
    await callback.answer()
    await show_home(callback, callback.from_user.first_name or "покупатель")


@dp.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery):
    await callback.answer()
    products = get_products()
    if not products:
        await safe_edit_or_send(
            callback,
            "🛒 <b>Каталог PLS Store</b>\n\n"
            "Пока товаров нет. Загляни позже или напиши в поддержку.",
            reply_markup=back_menu(),
        )
        return

    builder = InlineKeyboardBuilder()
    for product in products:
        title = f"{product['name']} — {product['price']}"
        builder.button(text=title[:60], callback_data=f"product:{product['id']}")
    builder.button(text="⬅️ Главное меню", callback_data="home")
    builder.adjust(1)

    await safe_edit_or_send(
        callback,
        "🛒 <b>Каталог PLS Store</b>\n\nВыбери интересующий плагин:",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data.startswith("product:"))
async def cb_product(callback: CallbackQuery):
    await callback.answer()
    try:
        product_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    product = get_product(product_id)
    if not product:
        await safe_edit_or_send(
            callback,
            "Этот товар больше недоступен.",
            reply_markup=back_menu(),
        )
        return

    text = (
        f"🧩 <b>{html.escape(product['name'])}</b>\n\n"
        f"{html.escape(product['description'])}\n\n"
        f"Цена: <b>{html.escape(product['price'])}</b>\n\n"
        "Покупка оформляется вручную. Нажми кнопку ниже, чтобы отправить заявку."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Оставить заявку", callback_data=f"buy:{product_id}")],
        [InlineKeyboardButton(text="⬅️ Назад в каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
    ])
    await safe_edit_or_send(callback, text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot):
    await callback.answer("Заявка отправлена!")
    try:
        product_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    product = get_product(product_id)
    if not product:
        await safe_edit_or_send(
            callback,
            "Этот товар больше недоступен.",
            reply_markup=back_menu(),
        )
        return

    user = callback.from_user
    username = f"@{user.username}" if user.username else "не указан"
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, username, product_id) VALUES (?, ?, ?)",
            (user.id, user.username, product_id),
        )
        order_id = cursor.lastrowid

    await safe_edit_or_send(
        callback,
        "✅ <b>Заявка отправлена!</b>\n\n"
        "Продавец получил уведомление и свяжется с тобой для оформления покупки.\n"
        "Оплата и передача товара согласовываются вручную.",
        reply_markup=back_menu(),
    )
    admin_text = (
        f"🔔 <b>Новая заявка №{order_id}</b>\n\n"
        f"Товар: <b>{html.escape(product['name'])}</b>\n"
        f"Цена: {html.escape(product['price'])}\n"
        f"Покупатель: {html.escape(user.full_name)}\n"
        f"Username: {html.escape(username)}\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        f"Ответить: <code>/reply {user.id} текст сообщения</code>"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text)
    except Exception:
        log.exception("Не удалось отправить уведомление администратору")


@dp.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    user = callback.from_user
    username = f"@{user.username}" if user.username else "не указан"
    orders = get_order_count(user.id)
    await safe_edit_or_send(
        callback,
        "👤 <b>Твой профиль</b>\n\n"
        f"Имя: {html.escape(user.full_name)}\n"
        f"Username: {html.escape(username)}\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Заявок на покупку: <b>{orders}</b>",
        reply_markup=back_menu(),
    )


@dp.callback_query(F.data == "reviews")
async def cb_reviews(callback: CallbackQuery):
    await callback.answer()
    if REVIEWS_URL:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Открыть канал с отзывами", url=REVIEWS_URL)],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")],
        ])
        text = "⭐ <b>Отзывы покупателей</b>\n\nНажми кнопку ниже, чтобы открыть канал с отзывами."
    else:
        keyboard = back_menu()
        text = "⭐ Ссылка на канал с отзывами пока не настроена."
    await safe_edit_or_send(callback, text, reply_markup=keyboard)


@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    await callback.answer()
    if SUPPORT_USERNAME:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать в поддержку", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")],
        ])
        text = "💬 <b>Поддержка PLS Store</b>\n\nНажми кнопку ниже, чтобы написать продавцу."
    else:
        keyboard = back_menu()
        text = "💬 Поддержка пока не настроена. Скоро здесь появится контакт."
    await safe_edit_or_send(callback, text, reply_markup=keyboard)


@dp.message(Command("addproduct"))
async def cmd_add_product(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    payload = (message.text or "").partition(" ")[2]
    parts = [part.strip() for part in payload.split("|", 2)]
    if len(parts) != 3 or not all(parts):
        await message.answer(
            "Формат:\n<code>/addproduct Название | Цена | Описание</code>"
        )
        return
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO products (name, price, description) VALUES (?, ?, ?)",
            tuple(parts),
        )
        product_id = cursor.lastrowid
    await message.answer(f"✅ Товар добавлен. ID: <code>{product_id}</code>")


@dp.message(Command("delproduct"))
async def cmd_del_product(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    payload = (message.text or "").partition(" ")[2].strip()
    if not payload.isdigit():
        await message.answer("Формат: <code>/delproduct ID</code>")
        return
    with db() as conn:
        cursor = conn.execute(
            "UPDATE products SET active=0 WHERE id=?", (int(payload),)
        )
    if cursor.rowcount:
        await message.answer("🗑 Товар скрыт из каталога.")
    else:
        await message.answer("Товар с таким ID не найден.")


@dp.message(Command("products"))
async def cmd_products(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    products = get_products()
    if not products:
        await message.answer("Каталог пока пуст.")
        return
    lines = ["<b>Товары в каталоге:</b>"]
    for p in products:
        lines.append(f"ID <code>{p['id']}</code> — {html.escape(p['name'])} — {html.escape(p['price'])}")
    await message.answer("\n".join(lines))


@dp.message(Command("reply"))
async def cmd_reply(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: <code>/reply TELEGRAM_ID текст</code>")
        return
    try:
        await bot.send_message(
            int(parts[1]),
            "💬 <b>Сообщение от поддержки PLS Store</b>\n\n" + html.escape(parts[2]),
        )
        await message.answer("✅ Сообщение отправлено.")
    except Exception as exc:
        log.warning("Не удалось отправить ответ: %s", exc)
        await message.answer("Не удалось отправить сообщение. Проверь Telegram ID и настройки бота.")


async def main():
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    log.info("PLS Store запускается")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
