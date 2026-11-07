import asyncio
import html
import logging
import os
import re
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
    BotCommand,
    LinkPreviewOptions,
)

# Отключаем превью ссылок (нижняя карточка Telegraph/YouTube и т.п.)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

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


ORIGINAL_CH_AUTODELIVERY_DESCRIPTION = (
    "Плагин для FunPayCardinal: склад заранее раскрученных Telegram-каналов "
    "и мгновенная выдача покупателю.\n\n"
    "• Создаёт приватные каналы через Telethon (вход по QR)\n"
    "• Накрутка подписчиков через SMM-панель API\n"
    "• Выдача по количеству в заказе (order.amount): купил 2 → получит 2 канала\n"
    "• Автопередача прав владельца по @username\n"
    "• Тарифы, лоты, папки Telegram, FAQ-автоответы, свой лог\n"
    "• On-demand создание, если склад пуст\n\n"
    "После оплаты продавец свяжется и передаст файл плагина + инструкцию."
)

SEED_PRODUCTS = [
    {
        "name": "Ch AutoDelivery — автовыдача TG-каналов",
        "price": "299 ₽",
        "description": ORIGINAL_CH_AUTODELIVERY_DESCRIPTION,
        "version": "v4.5",
    },
    {
        "name": "AutoSMM",
        "price": "349 ₽",
        "description": (
            "Плагин для FunPayCardinal для автоматизации работы с SMM-услугами, "
            "управления панелями, лотами и ценами. Подробности и инструкция — в статье товара."
        ),
        "version": "",
    },
]


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                description TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                image_path TEXT,
                article_url TEXT,
                video_url TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS license_accepts (
                user_id INTEGER PRIMARY KEY,
                accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Миграция существующей базы: добавляем поле изображения товара.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "image_path" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN image_path TEXT")
        if "article_url" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN article_url TEXT")
        if "video_url" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN video_url TEXT")
        if "sort_order" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        if "version" not in columns:
            conn.execute("ALTER TABLE products ADD COLUMN version TEXT")
        order_columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "status" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")

        # Добавляем стартовые товары, если их ещё нет. Существующие описания и настройки не затираем.
        for p in SEED_PRODUCTS:
            pattern = f"%{p['name'].split('—')[0].strip()}%"
            existing = conn.execute(
                "SELECT id FROM products WHERE lower(name) LIKE lower(?) LIMIT 1",
                (pattern,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE products SET price=? WHERE id=?",
                    (p["price"], existing["id"]),
                )
                if p.get("version"):
                    conn.execute(
                        "UPDATE products SET version=? WHERE id=?",
                        (p["version"], existing["id"]),
                    )
            else:
                conn.execute(
                    "INSERT INTO products (name, price, description, version) VALUES (?, ?, ?, ?)",
                    (p["name"], p["price"], p["description"], p.get("version", "")),
                )


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO bot_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛒 Каталог товаров", callback_data="catalog")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews")],
        [InlineKeyboardButton(text="📄 Документы и информация", callback_data="documents")],
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
            "SELECT id, name, price, description, version FROM products WHERE active=1 ORDER BY sort_order ASC, id DESC"
        ).fetchall()


def get_product(product_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT id, name, price, description, image_path, article_url, video_url, version "
            "FROM products WHERE id=? AND active=1",
            (product_id,),
        ).fetchone()


def build_product_caption(product, *, max_desc: int = 420) -> str:
    """
    Карточка товара в стиле компактного Telegram-объявления:
    название (+ версия) → цитата → статья → видео → цена → лицензия.
    Между блоками — пустые строки (как на маркетплейс-скринах).
    Лицензия — кликабельный текст без показа сырой ссылки.
    """
    name = (product["name"] or "").strip()
    version = ""
    try:
        version = (product["version"] or "").strip()
    except (KeyError, IndexError, TypeError):
        version = ""
    if version:
        ver_display = version if version.lower().startswith("v") else f"v{version}"
        title = f"{name} {ver_display}"
    else:
        title = name
    title = html.escape(title)

    full_description = (product["description"] or "").strip()
    compact = full_description
    if len(compact) > max_desc:
        compact = compact[: max_desc - 3].rsplit(" ", 1)[0] + "..."

    blocks: list[str] = [f"💎 <b>{title}</b>"]
    if compact:
        blocks.append(f"<blockquote>{html.escape(compact)}</blockquote>")

    article_url = (product["article_url"] or "").strip() if product["article_url"] else ""
    if article_url:
        safe = html.escape(article_url, quote=True)
        blocks.append(f"📄 <b>Статья:</b> <a href=\"{safe}\">Читать полное описание</a>")

    video_url = (product["video_url"] or "").strip() if product["video_url"] else ""
    if video_url:
        safe = html.escape(video_url, quote=True)
        blocks.append(f"🎥 <b>Видео-обзор:</b> <a href=\"{safe}\">Смотреть</a>")

    price = html.escape(product["price"] or "")
    blocks.append(f"💰 <b>Цена:</b> {price}")

    license_url = get_setting("license_url", "").strip()
    if license_url:
        safe_l = html.escape(license_url, quote=True)
        blocks.append(
            f"📜 <i>Условия лицензии, возврата и поддержки — "
            f"<a href=\"{safe_l}\">лицензионное соглашение</a></i>"
        )

    # Пустая строка между блоками — визуальные отступы как на скрине
    return "\n\n".join(blocks)


def get_order_count(user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM orders WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row["total"])


def has_accepted_license(user_id: int) -> bool:
    license_url = get_setting("license_url", "").strip()
    if not license_url:
        return True  # лицензия не настроена — вход свободный
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM license_accepts WHERE user_id=?", (user_id,)
        ).fetchone()
    return row is not None


def accept_license(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO license_accepts(user_id) VALUES (?) "
            "ON CONFLICT(user_id) DO UPDATE SET accepted_at=CURRENT_TIMESTAMP",
            (user_id,),
        )


def license_gate_keyboard(license_url: str) -> InlineKeyboardMarkup:
    rows = []
    if license_url:
        rows.append([InlineKeyboardButton(text="📜 Открыть соглашение", url=license_url)])
    rows.extend([
        [InlineKeyboardButton(text="📄 Документы и информация", callback_data="documents")],
        [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="license:accept")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_license_gate(target, user_id: int) -> None:
    license_url = get_setting("license_url", "").strip()
    text = (
        "📜 <b>Лицензионное соглашение</b>\n\n"
        "Перед использованием бота необходимо принять условия лицензии, "
        "возврата и поддержки.\n\n"
        "Откройте соглашение по кнопке ниже, ознакомьтесь и нажмите "
        "«✅ Принимаю условия»."
    )
    markup = license_gate_keyboard(license_url)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await safe_edit_or_send(target, text, reply_markup=markup)


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
        await message.answer(text, reply_markup=reply_markup, link_preview_options=NO_PREVIEW)
        return

    try:
        await message.edit_text(text, reply_markup=reply_markup, link_preview_options=NO_PREVIEW)
    except Exception as exc:
        log.warning("edit_text не удался, отправляю новое сообщение: %s", exc)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(text, reply_markup=reply_markup, link_preview_options=NO_PREVIEW)


async def show_home(target, first_name: str):
    welcome_template = get_setting(
        "welcome_text",
        "<b>PLS Store</b>\nПлагины и дополнения для FunPayCardinal\n\nДобро пожаловать, {first_name}!",
    )
    safe_name = html.escape(first_name)
    try:
        text = welcome_template.replace("{first_name}", safe_name)
    except Exception:
        text = f"<b>PLS Store</b>\nДобро пожаловать, {safe_name}!"

    codeword = get_setting("platega_codeword", "Platega test").strip()
    if codeword:
        text += f"\\n\\n<code>{html.escape(codeword)}</code>"

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


dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not has_accepted_license(message.from_user.id):
        await show_license_gate(message, message.from_user.id)
        return
    await show_home(message, message.from_user.first_name or "покупатель")


@dp.callback_query(F.data == "home")
async def cb_home(callback: CallbackQuery):
    await callback.answer()
    if not has_accepted_license(callback.from_user.id):
        await show_license_gate(callback, callback.from_user.id)
        return
    await show_home(callback, callback.from_user.first_name or "покупатель")


@dp.callback_query(F.data == "license:accept")
async def cb_license_accept(callback: CallbackQuery):
    await callback.answer("Спасибо! Условия приняты.")
    accept_license(callback.from_user.id)
    await show_home(callback, callback.from_user.first_name or "покупатель")


async def render_catalog(target) -> None:
    products = get_products()
    if not products:
        text = (
            "🛒 <b>Каталог PLS Store</b>\n\n"
            "Пока товаров нет. Загляни позже или напиши в поддержку."
        )
        markup = back_menu()
    else:
        builder = InlineKeyboardBuilder()
        for product in products:
            ver = (product["version"] or "").strip() if product["version"] else ""
            if ver:
                ver_disp = ver if ver.lower().startswith("v") else f"v{ver}"
                title = f"{product['name']} {ver_disp} — {product['price']}"
            else:
                title = f"{product['name']} — {product['price']}"
            builder.button(text=title[:64], callback_data=f"product:{product['id']}")
        builder.button(text="⬅️ Главное меню", callback_data="home")
        builder.adjust(1)
        text = "🛒 <b>Каталог PLS Store</b>\n\nВыбери интересующий плагин:"
        markup = builder.as_markup()

    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await safe_edit_or_send(target, text, reply_markup=markup)


@dp.message(Command("catalog"))
async def cmd_catalog(message: Message):
    if not has_accepted_license(message.from_user.id):
        await show_license_gate(message, message.from_user.id)
        return
    await render_catalog(message)


@dp.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery):
    await callback.answer()
    if not has_accepted_license(callback.from_user.id):
        await show_license_gate(callback, callback.from_user.id)
        return
    await render_catalog(callback)


@dp.callback_query(F.data.startswith("product:"))
async def cb_product(callback: CallbackQuery):
    await callback.answer()
    if not has_accepted_license(callback.from_user.id):
        await show_license_gate(callback, callback.from_user.id)
        return
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

    text = build_product_caption(product)
    rows = [[InlineKeyboardButton(text="📩 Оставить заявку", callback_data=f"buy:{product_id}")]]
    rows.extend([
        [InlineKeyboardButton(text="⬅️ Назад в каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    image_path = product["image_path"]
    if image_path:
        image_file = Path(image_path)
        if not image_file.is_absolute():
            image_file = _BASE_DIR / image_file
        if image_file.is_file():
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(
                photo=FSInputFile(image_file),
                caption=text,
                reply_markup=keyboard,
            )
            return

    await safe_edit_or_send(callback, text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot):
    if not has_accepted_license(callback.from_user.id):
        await callback.answer()
        await show_license_gate(callback, callback.from_user.id)
        return
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


def profile_text(user) -> str:
    username = f"@{user.username}" if user.username else "не указан"
    orders = get_order_count(user.id)
    return (
        "👤 <b>Твой профиль</b>\n\n"
        f"Имя: {html.escape(user.full_name)}\n"
        f"Username: {html.escape(username)}\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Заявок на покупку: <b>{orders}</b>"
    )


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    if not has_accepted_license(message.from_user.id):
        await show_license_gate(message, message.from_user.id)
        return
    await message.answer(profile_text(message.from_user), reply_markup=back_menu())


@dp.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    if not has_accepted_license(callback.from_user.id):
        await show_license_gate(callback, callback.from_user.id)
        return
    await safe_edit_or_send(
        callback,
        profile_text(callback.from_user),
        reply_markup=back_menu(),
    )


@dp.callback_query(F.data == "reviews")
async def cb_reviews(callback: CallbackQuery):
    await callback.answer()
    if not has_accepted_license(callback.from_user.id):
        await show_license_gate(callback, callback.from_user.id)
        return
    reviews_url = get_setting("reviews_url", REVIEWS_URL)
    if reviews_url:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Открыть канал с отзывами", url=reviews_url)],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")],
        ])
        text = "⭐ <b>Отзывы покупателей</b>\n\nНажми кнопку ниже, чтобы открыть канал с отзывами."
    else:
        keyboard = back_menu()
        text = "⭐ Ссылка на канал с отзывами пока не настроена."
    await safe_edit_or_send(callback, text, reply_markup=keyboard)


def support_screen() -> tuple[str, InlineKeyboardMarkup]:
    support_username = get_setting("support_username", SUPPORT_USERNAME).lstrip("@")
    if support_username:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать в поддержку", url=f"https://t.me/{support_username}")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")],
        ])
        text = "💬 <b>Поддержка PLS Store</b>\n\nНажми кнопку ниже, чтобы написать продавцу."
    else:
        keyboard = back_menu()
        text = "💬 Поддержка пока не настроена. Скоро здесь появится контакт."
    return text, keyboard


@dp.message(Command("support"))
async def cmd_support(message: Message):
    if not has_accepted_license(message.from_user.id):
        await show_license_gate(message, message.from_user.id)
        return
    text, keyboard = support_screen()
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    await callback.answer()
    if not has_accepted_license(callback.from_user.id):
        await show_license_gate(callback, callback.from_user.id)
        return
    text, keyboard = support_screen()
    await safe_edit_or_send(callback, text, reply_markup=keyboard)



def documents_screen() -> tuple[str, InlineKeyboardMarkup]:
    privacy_url = get_setting("privacy_url", "").strip()
    terms_url = get_setting("terms_url", "").strip()
    tariffs_url = get_setting("tariffs_url", "").strip()
    support_username = get_setting("support_username", SUPPORT_USERNAME).strip().lstrip("@")
    support_email = get_setting("support_email", "plsstoresupport@gmail.com").strip()

    rows = []
    if privacy_url:
        rows.append([InlineKeyboardButton(text="📃 Политика конфиденциальности", url=privacy_url)])
    if terms_url:
        rows.append([InlineKeyboardButton(text="📜 Пользовательское соглашение", url=terms_url)])
    if tariffs_url:
        rows.append([InlineKeyboardButton(text="💰 Товары и тарифы", url=tariffs_url)])
    if support_username:
        rows.append([InlineKeyboardButton(text="💬 Поддержка в Telegram", url=f"https://t.me/{support_username}")])
    if support_email:
        rows.append([InlineKeyboardButton(text="✉️ Написать на почту", url=f"mailto:{support_email}")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="home")])

    missing = []
    if not privacy_url:
        missing.append("политика конфиденциальности")
    if not terms_url:
        missing.append("пользовательское соглашение")
    if not tariffs_url:
        missing.append("страница с тарифами")

    text = (
        "📄 <b>Документы и информация PLS STORE</b>\\n\\n"
        "Здесь собраны документы магазина, актуальные цены и контакты поддержки.\\n\\n"
        f"✉️ Почта: <code>{html.escape(support_email or 'не указана')}</code>"
    )
    if missing:
        text += "\\n\\n⚠️ <b>Не все документы настроены.</b> Администратору необходимо добавить: " + ", ".join(missing) + "."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "documents")
async def cb_documents(callback: CallbackQuery):
    await callback.answer()
    text, keyboard = documents_screen()
    await safe_edit_or_send(callback, text, reply_markup=keyboard)


@dp.message(Command("documents"))
async def cmd_documents(message: Message):
    text, keyboard = documents_screen()
    await message.answer(text, reply_markup=keyboard)


class ProductWizard(StatesGroup):
    add_name = State()
    add_price = State()
    add_description = State()
    add_image = State()
    add_article = State()
    edit_value = State()
    edit_image = State()
    set_banner = State()
    edit_welcome = State()
    edit_support = State()
    edit_reviews = State()
    edit_reviews_label = State()
    edit_license = State()
    edit_privacy = State()
    edit_terms = State()
    edit_tariffs = State()
    edit_email = State()
    edit_codeword = State()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def admin_home_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧩 Товары", callback_data="adm:products"),
         InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm:add")],
        [InlineKeyboardButton(text="📨 Заявки", callback_data="adm:orders")],
        [InlineKeyboardButton(text="🎨 Оформление и ссылки", callback_data="adm:settings")],
        [InlineKeyboardButton(text="🖼 Изменить баннер", callback_data="adm:banner")],
    ])


def admin_products_keyboard():
    with db() as conn:
        products = conn.execute(
            "SELECT id, name, active FROM products ORDER BY sort_order ASC, id DESC"
        ).fetchall()
    rows = []
    for p in products:
        mark = "🟢" if p["active"] else "⚪"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {p['name'][:42]}",
            callback_data=f"adm:p:{p['id']}"
        )])
    rows.extend([
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm:add")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="adm:home")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_product_keyboard(product_id: int, active: int, name: str = ""):
    toggle_text = "🙈 Скрыть товар" if active else "👁 Опубликовать товар"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"adm:edit:{product_id}:name"),
         InlineKeyboardButton(text="🏷 Версия", callback_data=f"adm:edit:{product_id}:version")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"adm:edit:{product_id}:price"),
         InlineKeyboardButton(text="📝 Описание", callback_data=f"adm:edit:{product_id}:description")],
        [InlineKeyboardButton(text="🖼 Фото", callback_data=f"adm:image:{product_id}"),
         InlineKeyboardButton(text="🔗 Ссылка на статью", callback_data=f"adm:edit:{product_id}:article_url")],
        [InlineKeyboardButton(text="🎥 Видеообзор", callback_data=f"adm:edit:{product_id}:video_url")],
        [InlineKeyboardButton(text="🗑 Убрать фото", callback_data=f"adm:removeimage:{product_id}")],
        [InlineKeyboardButton(text="👁 Предпросмотр", callback_data=f"adm:preview:{product_id}"),
         InlineKeyboardButton(text="📋 Копировать", callback_data=f"adm:copy:{product_id}")],
        [InlineKeyboardButton(text="⬆️ Выше", callback_data=f"adm:move:{product_id}:up"),
         InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"adm:move:{product_id}:down")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"adm:toggle:{product_id}")],
        *([[InlineKeyboardButton(text="♻️ Вернуть исходный текст Ch AutoDelivery", callback_data=f"adm:restore:{product_id}")]] if (name or "").lower().startswith("ch autodelivery") else []),
        [InlineKeyboardButton(text="🗑 Удалить навсегда", callback_data=f"adm:deleteconfirm:{product_id}")],
        [InlineKeyboardButton(text="⬅️ К списку товаров", callback_data="adm:products")],
    ])


async def show_admin_product(target, product_id: int):
    with db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        if isinstance(target, CallbackQuery):
            await target.message.answer("Товар не найден.")
        else:
            await target.answer("Товар не найден.")
        return
    version = (p["version"] or "").strip() if p["version"] else ""
    text = (
        f"🧩 <b>{html.escape(p['name'])}</b>"
        f"{' · ' + html.escape(version) if version else ''}\n"
        f"ID: <code>{p['id']}</code> · Статус: {'🟢 опубликован' if p['active'] else '⚪ скрыт'}\n\n"
        f"Версия: <b>{html.escape(version) if version else 'не указана'}</b>\n"
        f"Цена: <b>{html.escape(p['price'])}</b>\n"
        f"Описание: {html.escape((p['description'] or '')[:450])}"
        f"{'…' if len(p['description'] or '') > 450 else ''}\n\n"
        f"Фото: {'добавлено' if p['image_path'] else 'нет'}\n"
        f"Статья: {'добавлена' if p['article_url'] else 'нет'}\n"
        f"Видеообзор: {'добавлен' if p['video_url'] else 'нет'}"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=admin_product_keyboard(product_id, p["active"], p["name"]))
    else:
        await target.answer(text, reply_markup=admin_product_keyboard(product_id, p["active"], p["name"]))


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Панель управления PLS Store</b>\n\nВыбери нужный раздел:", reply_markup=admin_home_keyboard())


@dp.callback_query(F.data == "adm:home")
async def adm_home(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("🛠 <b>Панель управления PLS Store</b>\n\nВыбери нужный раздел:", reply_markup=admin_home_keyboard())


@dp.callback_query(F.data == "adm:products")
async def adm_products(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("🧩 <b>Управление товарами</b>\n\n🟢 — опубликован, ⚪ — скрыт.", reply_markup=admin_products_keyboard())


@dp.callback_query(F.data.startswith("adm:p:"))
async def adm_product_open(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    try:
        product_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        return
    await show_admin_product(callback, product_id)


@dp.callback_query(F.data == "adm:add")
async def adm_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(ProductWizard.add_name)
    await callback.answer()
    await callback.message.answer("➕ <b>Новый товар</b>\n\nОтправь название товара.")


@dp.message(ProductWizard.add_name, F.text & ~F.text.startswith('/'))
async def adm_add_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Название не должно быть пустым. Отправь название ещё раз.")
        return
    await state.update_data(name=value)
    await state.set_state(ProductWizard.add_price)
    await message.answer("Теперь отправь цену. Например: <code>300 ₽</code> или <code>по запросу</code>.")


@dp.message(ProductWizard.add_price, F.text & ~F.text.startswith('/'))
async def adm_add_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Цена не должна быть пустой. Отправь её ещё раз.")
        return
    await state.update_data(price=value)
    await state.set_state(ProductWizard.add_description)
    await message.answer("Отправь описание товара. Можно использовать переносы строк.")


@dp.message(ProductWizard.add_description, F.text & ~F.text.startswith('/'))
async def adm_add_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Описание не должно быть пустым. Отправь его ещё раз.")
        return
    data = await state.get_data()
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO products (name, price, description) VALUES (?, ?, ?)",
            (data["name"], data["price"], value),
        )
        product_id = cursor.lastrowid
    await state.update_data(product_id=product_id)
    await state.set_state(ProductWizard.add_image)
    await message.answer(
        f"✅ Товар создан, ID <code>{product_id}</code>.\n\n"
        "Теперь отправь фотографию товара. Если фото пока нет — отправь <code>-</code>."
    )


@dp.message(ProductWizard.add_image, F.photo)
async def adm_add_image(message: Message, bot: Bot, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    folder = _BASE_DIR / "assets" / "products"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"product_{product_id}.jpg"
    try:
        f = await bot.get_file(message.photo[-1].file_id)
        await bot.download_file(f.file_path, destination=dest)
    except Exception:
        log.exception("Не удалось загрузить фото нового товара")
        await message.answer("Не получилось сохранить фото. Попробуй отправить его ещё раз или отправь '-' чтобы пропустить.")
        return
    with db() as conn:
        conn.execute("UPDATE products SET image_path=? WHERE id=?", (dest.relative_to(_BASE_DIR).as_posix(), product_id))
    await state.set_state(ProductWizard.add_article)
    await message.answer("🔗 Отправь ссылку на полную статью (https://...). Если статьи нет — отправь <code>-</code>.")


@dp.message(ProductWizard.add_image, F.text & ~F.text.startswith('/'))
async def adm_add_image_skip(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if (message.text or "").strip() != "-":
        await message.answer("Отправь фото или '-' чтобы пропустить.")
        return
    await state.set_state(ProductWizard.add_article)
    await message.answer("🔗 Отправь ссылку на полную статью (https://...). Если статьи нет — отправь <code>-</code>.")


@dp.message(ProductWizard.add_article, F.text & ~F.text.startswith('/'))
async def adm_add_article(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value != "-" and not (value.startswith("https://") or value.startswith("http://")):
        await message.answer("Нужна полная ссылка, начинающаяся с https://, или '-' чтобы пропустить.")
        return
    data = await state.get_data()
    with db() as conn:
        conn.execute("UPDATE products SET article_url=? WHERE id=?", (None if value == "-" else value, data["product_id"]))
    product_id = data["product_id"]
    await state.clear()
    await message.answer("🎉 <b>Товар полностью настроен!</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Открыть настройки товара", callback_data=f"adm:p:{product_id}")],
        [InlineKeyboardButton(text="🧩 К списку товаров", callback_data="adm:products")],
    ]))


@dp.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit_field(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[3] not in {"name", "price", "description", "article_url", "video_url", "version"}:
        await callback.answer("Некорректное поле", show_alert=True)
        return
    product_id, field = int(parts[2]), parts[3]
    await state.clear()
    await state.update_data(product_id=product_id, field=field)
    await state.set_state(ProductWizard.edit_value)
    labels = {
        "name": "название",
        "price": "цену",
        "description": "описание",
        "article_url": "ссылку на статью",
        "video_url": "ссылку на видеообзор",
        "version": "версию плагина (например: 4.5 или v4.5)",
    }
    await callback.answer()
    await callback.message.answer(
        f"✏️ Отправь новое значение для поля «{labels[field]}».\n"
        + ("Для удаления ссылки отправь '-'. " if field in {"article_url", "video_url"} else "")
        + ("Для удаления версии отправь '-'. " if field == "version" else "")
        + "Для отмены нажми /cancel."
    )


@dp.message(ProductWizard.edit_value, F.text & ~F.text.startswith('/'))
async def adm_edit_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    field, product_id = data["field"], data["product_id"]
    value = (message.text or "").strip()
    if not value:
        await message.answer("Пустое значение не подходит. Отправь текст ещё раз.")
        return
    if field in {"article_url", "video_url"}:
        if value == "-":
            value = None
        elif not (value.startswith("https://") or value.startswith("http://")):
            await message.answer("Ссылка должна начинаться с https:// или http://. Для удаления отправь '-'.")
            return
    if field == "version":
        if value == "-":
            value = None
        else:
            value = value.strip()
    with db() as conn:
        conn.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, product_id))
    await state.clear()
    await message.answer("✅ Изменения сохранены.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Вернуться к товару", callback_data=f"adm:p:{product_id}")],
        [InlineKeyboardButton(text="🧩 К списку товаров", callback_data="adm:products")],
    ]))


@dp.callback_query(F.data.startswith("adm:image:"))
async def adm_image_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    await state.clear()
    await state.update_data(product_id=product_id)
    await state.set_state(ProductWizard.edit_image)
    await callback.answer()
    await callback.message.answer(f"🖼 Отправь новое фото для товара ID <code>{product_id}</code>.")


@dp.message(ProductWizard.edit_image, F.photo)
async def adm_edit_image(message: Message, bot: Bot, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    folder = _BASE_DIR / "assets" / "products"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"product_{product_id}.jpg"
    try:
        f = await bot.get_file(message.photo[-1].file_id)
        await bot.download_file(f.file_path, destination=dest)
    except Exception:
        log.exception("Не удалось заменить фото товара")
        await message.answer("Не удалось сохранить фото. Попробуй ещё раз.")
        return
    with db() as conn:
        conn.execute("UPDATE products SET image_path=? WHERE id=?", (dest.relative_to(_BASE_DIR).as_posix(), product_id))
    await state.clear()
    await message.answer("✅ Фото товара обновлено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Вернуться к товару", callback_data=f"adm:p:{product_id}")]
    ]))


@dp.callback_query(F.data.startswith("adm:removeimage:"))
async def adm_remove_image(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    with db() as conn:
        conn.execute("UPDATE products SET image_path=NULL WHERE id=?", (product_id,))
    await callback.answer("Фото удалено")
    await show_admin_product(callback, product_id)


@dp.callback_query(F.data.startswith("adm:toggle:"))
async def adm_toggle_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    with db() as conn:
        conn.execute("UPDATE products SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (product_id,))
    await callback.answer("Статус изменён")
    await show_admin_product(callback, product_id)




@dp.callback_query(F.data.startswith("adm:restore:"))
async def adm_restore_description_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    with db() as conn:
        p = conn.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
    if not p or not (p["name"] or "").lower().startswith("ch autodelivery"):
        await callback.answer("Восстановление доступно только для Ch AutoDelivery", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        "♻️ Восстановить исходный текст описания Ch AutoDelivery?\n"
        "Текущий текст описания будет заменён исходным шаблоном.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да, восстановить", callback_data=f"adm:restoreyes:{product_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"adm:p:{product_id}")],
        ])
    )


@dp.callback_query(F.data.startswith("adm:restoreyes:"))
async def adm_restore_description(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    with db() as conn:
        p = conn.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
        if not p or not (p["name"] or "").lower().startswith("ch autodelivery"):
            await callback.answer("Товар не найден или это не Ch AutoDelivery", show_alert=True)
            return
        conn.execute("UPDATE products SET description=? WHERE id=?", (ORIGINAL_CH_AUTODELIVERY_DESCRIPTION, product_id))
    await callback.answer("Исходный текст восстановлен")
    await show_admin_product(callback, product_id)


@dp.callback_query(F.data.startswith("adm:deleteconfirm:"))
async def adm_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    with db() as conn:
        p = conn.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        f"⚠️ <b>Удалить товар навсегда?</b>\n\n"
        f"<b>{html.escape(p['name'])}</b> (ID <code>{product_id}</code>)\n\n"
        "Запись товара будет удалена из базы. "
        "Заявки по этому товару останутся (в списке будет «Товар удалён»).\n"
        "Чтобы только спрятать товар из каталога — используй «Скрыть товар».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить навсегда", callback_data=f"adm:deleteyes:{product_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"adm:p:{product_id}")],
        ])
    )


@dp.callback_query(F.data.startswith("adm:deleteyes:"))
async def adm_delete_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    image_path = None
    with db() as conn:
        row = conn.execute("SELECT image_path FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            await callback.answer("Товар не найден", show_alert=True)
            return
        image_path = row["image_path"]
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))
    # Удаляем файл фото, если он лежит в assets/products
    if image_path:
        try:
            img = Path(image_path)
            if not img.is_absolute():
                img = _BASE_DIR / img
            if img.is_file() and "products" in img.parts:
                img.unlink(missing_ok=True)
        except Exception:
            log.warning("Не удалось удалить файл фото товара ID %s: %s", product_id, image_path)
    await callback.answer("Товар удалён")
    await callback.message.answer(
        f"🗑 Товар ID <code>{product_id}</code> удалён навсегда.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧩 К списку товаров", callback_data="adm:products")],
            [InlineKeyboardButton(text="🛠 В админ-панель", callback_data="adm:home")],
        ])
    )


@dp.callback_query(F.data.startswith("adm:copy:"))
async def adm_copy_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    with db() as conn:
        p = conn.execute(
            "SELECT name, price, description, image_path, article_url, video_url, version FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
        if not p:
            await callback.answer("Товар не найден", show_alert=True)
            return
        cur = conn.execute(
            "INSERT INTO products (name, price, description, image_path, article_url, video_url, version, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                p["name"] + " (копия)",
                p["price"],
                p["description"],
                p["image_path"],
                p["article_url"],
                p["video_url"],
                p["version"],
            ),
        )
        new_id = cur.lastrowid
    await callback.answer("Копия создана")
    await callback.message.answer(f"📋 Создана скрытая копия товара ID <code>{new_id}</code>.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настроить копию", callback_data=f"adm:p:{new_id}")],
        [InlineKeyboardButton(text="🧩 К списку товаров", callback_data="adm:products")],
    ]))


@dp.callback_query(F.data.startswith("adm:preview:"))
async def adm_preview_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    product_id = int(callback.data.split(":")[2])
    with db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    text = build_product_caption(p)
    rows = [[InlineKeyboardButton(text="📩 Оставить заявку (пример)", callback_data="adm:previewnoop")]]
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    image = p["image_path"]
    if image:
        image_file = Path(image)
        if not image_file.is_absolute():
            image_file = _BASE_DIR / image_file
        if image_file.is_file():
            await callback.message.answer_photo(FSInputFile(image_file), caption=text, reply_markup=markup)
            await callback.answer()
            return
    await callback.message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    await callback.answer()


@dp.callback_query(F.data == "adm:previewnoop")
async def adm_preview_noop(callback: CallbackQuery):
    await callback.answer("Это только предпросмотр", show_alert=True)



@dp.callback_query(F.data.startswith("adm:move:"))
async def adm_move_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, raw_id, direction = callback.data.split(":")
    product_id = int(raw_id)
    with db() as conn:
        items = conn.execute("SELECT id FROM products ORDER BY sort_order ASC, id DESC").fetchall()
        ids = [row["id"] for row in items]
        if product_id not in ids:
            await callback.answer("Товар не найден", show_alert=True)
            return
        old_index = ids.index(product_id)
        new_index = old_index - 1 if direction == "up" else old_index + 1
        if not 0 <= new_index < len(ids):
            await callback.answer("Дальше переместить нельзя")
            return
        ids[old_index], ids[new_index] = ids[new_index], ids[old_index]
        for order, item_id in enumerate(ids):
            conn.execute("UPDATE products SET sort_order=? WHERE id=?", (order, item_id))
    await callback.answer("Порядок изменён")
    await show_admin_product(callback, product_id)


@dp.callback_query(F.data.startswith("adm:orderdone:"))
async def adm_order_done(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":")[2])
    with db() as conn:
        cur = conn.execute("UPDATE orders SET status='completed' WHERE id=?", (order_id,))
    await callback.answer("Заявка закрыта" if cur.rowcount else "Заявка не найдена")
    await callback.message.answer(
        f"Заявка №{order_id} закрыта." if cur.rowcount else "Заявка не найдена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨 Обновить список заявок", callback_data="adm:orders")],
            [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="adm:home")],
        ])
    )


@dp.callback_query(F.data == "adm:orders")
async def adm_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    with db() as conn:
        rows = conn.execute("""
            SELECT o.id, o.user_id, o.username, o.created_at, o.status, p.name
            FROM orders o LEFT JOIN products p ON p.id=o.product_id
            ORDER BY o.id DESC LIMIT 15
        """).fetchall()
    order_buttons = []
    if not rows:
        text = "📨 <b>Заявки</b>\n\nПока заявок нет."
    else:
        lines = ["📨 <b>Последние заявки</b>\n"]
        for o in rows:
            uname = "@" + o["username"] if o["username"] else "без username"
            lines.append(
                f"№<code>{o['id']}</code> · {html.escape(o['status'])}\n"
                f"{html.escape(o['name'] or 'Товар удалён')} · {html.escape(uname)}\n"
                f"ID: <code>{o['user_id']}</code> · {html.escape(o['created_at'])}\n"
                f"/reply {o['user_id']} текст\n"
            )
            if o["status"] != "completed":
                order_buttons.append([InlineKeyboardButton(text=f"✅ Закрыть заявку №{o['id']}", callback_data=f"adm:orderdone:{o['id']}")])
        text = "\n".join(lines)
    await callback.answer()
    order_buttons.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="adm:home")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=order_buttons))




@dp.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    welcome = get_setting("welcome_text", "Стандартное приветствие")
    support = get_setting("support_username", SUPPORT_USERNAME) or "не задан"
    reviews = get_setting("reviews_url", REVIEWS_URL) or "не задана"
    reviews_label = get_setting("reviews_label", "Отзывы (350+) · Клик") or "не задано"
    license_url = get_setting("license_url", "") or "не задана"
    privacy_url = get_setting("privacy_url", "") or "не задана"
    terms_url = get_setting("terms_url", "") or "не задана"
    tariffs_url = get_setting("tariffs_url", "") or "не задана"
    support_email = get_setting("support_email", "plsstoresupport@gmail.com") or "не задана"
    codeword = get_setting("platega_codeword", "Platega test") or "выключено"
    await callback.answer()
    await callback.message.edit_text(
        "🎨 <b>Оформление и ссылки</b>\n\n"
        f"Приветствие: {'изменено' if welcome != 'Стандартное приветствие' else 'стандартное'}\n"
        f"Поддержка: <code>{html.escape(support)}</code>\n"
        f"Отзывы: <code>{html.escape(reviews)}</code>\n"
        f"Подпись отзывов: <code>{html.escape(reviews_label)}</code>\n"
        f"Лицензионное соглашение: <code>{html.escape(license_url)}</code>\n\n"
        "Выбери, что хочешь изменить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Текст приветствия", callback_data="adm:setting:welcome")],
            [InlineKeyboardButton(text="💬 Username поддержки", callback_data="adm:setting:support")],
            [InlineKeyboardButton(text="⭐ Ссылка на отзывы", callback_data="adm:setting:reviews")],
            [InlineKeyboardButton(text="🔢 Подпись отзывов", callback_data="adm:setting:reviews_label")],
            [InlineKeyboardButton(text="📜 Лицензионное соглашение", callback_data="adm:setting:license")],
            [InlineKeyboardButton(text="📃 Политика конфиденциальности", callback_data="adm:setting:privacy")],
            [InlineKeyboardButton(text="📜 Пользовательское соглашение", callback_data="adm:setting:terms")],
            [InlineKeyboardButton(text="💰 Ссылка на тарифы", callback_data="adm:setting:tariffs")],
            [InlineKeyboardButton(text="✉️ Почта поддержки", callback_data="adm:setting:email")],
            [InlineKeyboardButton(text="🧪 Кодовое слово Platega", callback_data="adm:setting:codeword")],
            [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="adm:home")],
        ])
    )


@dp.callback_query(F.data.startswith("adm:setting:"))
async def adm_setting_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    key = callback.data.split(":")[2]
    state_map = {
        "welcome": (ProductWizard.edit_welcome, "Отправь новый текст приветствия. Можно использовать {first_name} для имени покупателя. HTML-теги: <b>, <i>, <u>, <code>."),
        "support": (ProductWizard.edit_support, "Отправь username поддержки без @ или с @. Чтобы очистить — отправь '-'."),
        "reviews": (ProductWizard.edit_reviews, "Отправь полную ссылку на канал отзывов (https://...). Чтобы очистить — отправь '-'."),
        "reviews_label": (ProductWizard.edit_reviews_label, "Отправь подпись для отзывов, например: 350+ · Клик. Чтобы очистить — отправь '-'."),
        "license": (ProductWizard.edit_license, "Отправь ссылку на лицензионное соглашение (https://...). Чтобы убрать — отправь '-'."),
        "privacy": (ProductWizard.edit_privacy, "Отправь ссылку на политику конфиденциальности (https://...). Чтобы убрать — отправь '-'."),
        "terms": (ProductWizard.edit_terms, "Отправь ссылку на пользовательское соглашение (https://...). Чтобы убрать — отправь '-'."),
        "tariffs": (ProductWizard.edit_tariffs, "Отправь ссылку на страницу с товарами и тарифами (https://...). Чтобы убрать — отправь '-'."),
        "email": (ProductWizard.edit_email, "Отправь email поддержки или '-' чтобы очистить."),
        "codeword": (ProductWizard.edit_codeword, "Отправь кодовое слово, которое временно показывать в главном меню. Чтобы отключить — отправь '-'."),
    }
    if key not in state_map:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    new_state, prompt = state_map[key]
    await state.clear()
    await state.set_state(new_state)
    await callback.answer()
    await callback.message.answer(prompt)


@dp.message(ProductWizard.edit_welcome, F.text & ~F.text.startswith('/'))
async def adm_setting_welcome_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not value or len(value) > 900:
        await message.answer("Текст должен быть от 1 до 900 символов. Попробуй ещё раз.")
        return
    set_setting("welcome_text", value)
    await state.clear()
    await message.answer("✅ Приветствие сохранено. Проверь его командой /start.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")],
        [InlineKeyboardButton(text="🛠 В админ-панель", callback_data="adm:home")],
    ]))


@dp.message(ProductWizard.edit_support, F.text & ~F.text.startswith('/'))
async def adm_setting_support_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip().lstrip("@")
    if value == "-":
        value = ""
    elif not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        await message.answer("Укажи корректный Telegram username (5–32 символа) или '-'.")
        return
    set_setting("support_username", value)
    await state.clear()
    await message.answer("✅ Контакт поддержки сохранён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")]
    ]))


@dp.message(ProductWizard.edit_reviews, F.text & ~F.text.startswith('/'))
async def adm_setting_reviews_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    elif not (value.startswith("https://") or value.startswith("http://")):
        await message.answer("Ссылка должна начинаться с https:// или http://, либо отправь '-'.")
        return
    set_setting("reviews_url", value)
    await state.clear()
    await message.answer("✅ Ссылка на отзывы сохранена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")]
    ]))


@dp.message(ProductWizard.edit_reviews_label, F.text & ~F.text.startswith('/'))
async def adm_setting_reviews_label_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    elif len(value) > 60:
        await message.answer("Подпись должна быть не длиннее 60 символов.")
        return
    set_setting("reviews_label", value)
    await state.clear()
    await message.answer("✅ Подпись отзывов сохранена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")]
    ]))


@dp.message(ProductWizard.edit_license, F.text & ~F.text.startswith('/'))
async def adm_setting_license_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    elif not (value.startswith("https://") or value.startswith("http://")):
        await message.answer("Ссылка должна начинаться с https:// или http://, либо отправь '-'.")
        return
    set_setting("license_url", value)
    await state.clear()
    await message.answer("✅ Ссылка на лицензионное соглашение сохранена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")]
    ]))


async def _save_document_url(message: Message, state: FSMContext, key: str, label: str):
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    elif not (value.startswith("https://") or value.startswith("http://")):
        await message.answer("Ссылка должна начинаться с https:// или http://, либо отправь '-'.")
        return
    set_setting(key, value)
    await state.clear()
    await message.answer(f"✅ {label} сохранена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")],
        [InlineKeyboardButton(text="🛠 В админ-панель", callback_data="adm:home")],
    ]))


@dp.message(ProductWizard.edit_privacy, F.text & ~F.text.startswith('/'))
async def adm_setting_privacy_save(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await _save_document_url(message, state, "privacy_url", "Ссылка на политику")


@dp.message(ProductWizard.edit_terms, F.text & ~F.text.startswith('/'))
async def adm_setting_terms_save(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await _save_document_url(message, state, "terms_url", "Ссылка на пользовательское соглашение")


@dp.message(ProductWizard.edit_tariffs, F.text & ~F.text.startswith('/'))
async def adm_setting_tariffs_save(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await _save_document_url(message, state, "tariffs_url", "Ссылка на тарифы")


@dp.message(ProductWizard.edit_email, F.text & ~F.text.startswith('/'))
async def adm_setting_email_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    elif not re.fullmatch(r"[^\\s@]+@[^\\s@]+\\.[^\\s@]+", value):
        await message.answer("Укажи корректный адрес электронной почты или '-'.")
        return
    set_setting("support_email", value)
    await state.clear()
    await message.answer("✅ Почта поддержки сохранена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")]
    ]))


@dp.message(ProductWizard.edit_codeword, F.text & ~F.text.startswith('/'))
async def adm_setting_codeword_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    value = (message.text or "").strip()
    if len(value) > 80:
        await message.answer("Кодовое слово должно быть не длиннее 80 символов.")
        return
    set_setting("platega_codeword", "" if value == "-" else value)
    await state.clear()
    await message.answer("✅ Кодовое слово обновлено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 К настройкам оформления", callback_data="adm:settings")]
    ]))


@dp.callback_query(F.data == "adm:banner")
async def adm_banner_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(ProductWizard.set_banner)
    await callback.answer()
    await callback.message.answer("🖼 Отправь новое фото баннера главного меню. Оно заменит текущий banner.png.")


@dp.message(ProductWizard.set_banner, F.photo)
async def adm_banner_save(message: Message, bot: Bot, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    BANNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = await bot.get_file(message.photo[-1].file_id)
        await bot.download_file(f.file_path, destination=BANNER_PATH)
    except Exception:
        log.exception("Не удалось сохранить главный баннер")
        await message.answer("Не удалось сохранить баннер. Попробуй ещё раз.")
        return
    await state.clear()
    await message.answer("✅ Главный баннер обновлён.")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Действие отменено. Открыть панель: /admin")


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


# ID товара, для которого администратор ожидает фотографию.
_pending_product_images: dict[int, int] = {}


@dp.message(Command("setproductimage"))
async def cmd_prepare_product_image(message: Message):
    """Позволяет сначала отправить команду, а затем отдельным сообщением фото."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    payload = (message.text or "").partition(" ")[2].strip()
    if not payload.isdigit():
        await message.answer("Формат: <code>/setproductimage ID</code>\nНапример: <code>/setproductimage 1</code>")
        return

    product_id = int(payload)
    with db() as conn:
        product = conn.execute(
            "SELECT id FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
    if not product:
        await message.answer("Товар с таким ID не найден. Посмотреть товары: /products")
        return

    _pending_product_images[message.from_user.id] = product_id
    await message.answer(
        f"📷 Теперь отправь следующим сообщением фотографию для товара ID <code>{product_id}</code>. "
        "Фото отправляй обычным сообщением, без подписи."
    )


@dp.message(F.photo)
async def cmd_set_product_image(message: Message, bot: Bot):
    """Сохраняет фото по команде в подписи или после отдельной команды."""
    if message.from_user.id != ADMIN_ID:
        return

    caption = (message.caption or "").strip().split()
    product_id = None

    # Вариант 1: фото сразу с подписью /setproductimage ID.
    if len(caption) == 2 and caption[0].split("@")[0].lower() == "/setproductimage" and caption[1].isdigit():
        product_id = int(caption[1])
    else:
        # Вариант 2: сначала команда /setproductimage ID, затем отдельное фото.
        product_id = _pending_product_images.pop(message.from_user.id, None)

    if product_id is None:
        return
    with db() as conn:
        product = conn.execute(
            "SELECT id FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
    if not product:
        await message.answer("Товар с таким ID не найден. Посмотреть ID можно командой /products.")
        return

    products_dir = _BASE_DIR / "assets" / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    destination = products_dir / f"product_{product_id}.jpg"
    try:
        photo_file = await bot.get_file(message.photo[-1].file_id)
        await bot.download_file(photo_file.file_path, destination=destination)
    except Exception as exc:
        log.exception("Не удалось сохранить изображение товара ID %s", product_id)
        await message.answer(f"❌ Не удалось сохранить фото: {html.escape(str(exc))}")
        return

    relative_path = destination.relative_to(_BASE_DIR).as_posix()
    with db() as conn:
        conn.execute(
            "UPDATE products SET image_path=? WHERE id=?", (relative_path, product_id)
        )
    await message.answer(
        f"✅ Изображение сохранено для товара ID <code>{product_id}</code>. "
        "Открой карточку товара в каталоге, чтобы проверить."
    )


@dp.message(Command("delproduct"))
async def cmd_del_product(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    payload = (message.text or "").partition(" ")[2].strip()
    if not payload.isdigit():
        await message.answer(
            "Формат: <code>/delproduct ID</code>\n"
            "Удаляет товар навсегда. Чтобы только скрыть — используй кнопку «Скрыть» в /admin."
        )
        return
    product_id = int(payload)
    image_path = None
    with db() as conn:
        row = conn.execute("SELECT image_path FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            await message.answer("Товар с таким ID не найден.")
            return
        image_path = row["image_path"]
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))
    if image_path:
        try:
            img = Path(image_path)
            if not img.is_absolute():
                img = _BASE_DIR / img
            if img.is_file() and "products" in img.parts:
                img.unlink(missing_ok=True)
        except Exception:
            log.warning("Не удалось удалить файл фото товара ID %s", product_id)
    await message.answer(f"🗑 Товар ID <code>{product_id}</code> удалён навсегда.")


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


async def setup_bot_commands(bot: Bot) -> None:
    """Меню команд слева внизу в Telegram (кнопка рядом с полем ввода)."""
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="catalog", description="Каталог товаров"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="support", description="Поддержка"),
        BotCommand(command="admin", description="Админ-панель (только для админа)"),
    ])


async def main():
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await setup_bot_commands(bot)
        log.info("Меню команд бота установлено")
    except Exception:
        log.exception("Не удалось установить меню команд")
    log.info("PLS Store запускается")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
