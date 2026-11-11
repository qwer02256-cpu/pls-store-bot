"""
PLS Store — Telegram-магазин цифровых товаров
Python + aiogram 3 + SQLite + Platega
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    InputMediaPhoto,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    LinkPreviewOptions,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    BOT_TOKEN,
    ADMIN_ID,
    SUPPORT_USERNAME,
    REVIEWS_URL,
    BANNER_PATH,
    PLATEGA_MERCHANT_ID,
    PLATEGA_SECRET,
    PLATEGA_CALLBACK_BASE,
    PLATEGA_CALLBACK_PORT,
    ASSETS_DIR,
    BANNERS_DIR,
    require_config,
)
from services.db import (
    db,
    init_db,
    get_setting,
    set_setting,
    ensure_user,
    get_user,
    change_balance,
    add_purchase,
    parse_price_rub,
    get_products,
    get_product,
    get_faq,
    get_user_language,
    set_user_language,
)
from services.platega import PlategaClient, PlategaError
from services.emoji import PackEmojiMiddleware
from services.ui import (
    main_menu_kb,
    profile_kb,
    back_kb,
    topup_amounts_kb,
    profile_text,
    support_button,
    language_kb,
    btn,
    design_button,
)

# ─── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("pls-store")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ─── Platega client ─────────────────────────────────────────────────────────
platega: PlategaClient | None = None
if PLATEGA_MERCHANT_ID and PLATEGA_SECRET:
    try:
        platega = PlategaClient(PLATEGA_MERCHANT_ID, PLATEGA_SECRET)
        log.info("Platega client initialized")
    except Exception as e:
        log.error("Platega init failed: %s", e)

# ─── original seed / constants (preserved) ──────────────────────────────────
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
        "name": "Ch AutoDelivery",
        "price": "3490₽ / Навсегда",
        "description": ORIGINAL_CH_AUTODELIVERY_DESCRIPTION,
        "version": "4.5",
    },
]


def seed_if_empty():
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        if count == 0:
            for i, p in enumerate(SEED_PRODUCTS):
                conn.execute(
                    "INSERT INTO products (name, price, description, version, sort_order, price_rub) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (p["name"], p["price"], p["description"], p.get("version"), i, parse_price_rub(p["price"])),
                )
            log.info("Seed products inserted")


# ─── helpers ────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


I18N = {
    "ru": {
        "welcome": "<b>PLS Store</b>\nПлагины и дополнения для FunPayCardinal\n\nДобро пожаловать, {first_name}!",
        "license_title": "📜 <b>Лицензионное соглашение</b>\n\nПеред использованием магазина необходимо принять условия.",
        "license_accept": "Спасибо! Условия приняты.", "language_title": "<b>Выберите язык</b>",
        "language_saved": "Язык изменён на русский.", "back": "⬅️ Назад", "home": "🏠 Главное меню",
        "shop": "🛒 Магазин PLS Store", "no_products": "Пока товаров нет. Загляни позже или напиши в поддержку.",
        "choose_product": "Выбери интересующий товар:", "unavailable": "Этот товар больше недоступен.",
        "buy": "🛒 Купить за {price:.0f}₽", "manual": "📩 Оставить заявку (вручную)", "back_catalog": "⬅️ Назад в каталог",
        "purchases": "🛒 <b>Мои покупки</b>", "empty_purchases": "Пока пусто. Загляните в магазин!",
        "balance": "💰 Баланс", "spent": "💸 Всего потрачено", "purchase_count": "🛒 Покупок",
        "promo_enter": "🎟 Введите промокод:", "promo_bad": "Промокод не найден или неактивен.",
        "promo_used": "Вы уже использовали этот промокод.", "promo_limit": "Лимит использований исчерпан.",
        "promo_ok": "✅ Промокод применён! Начислено <b>{bonus:.0f}₽</b>.",
        "topup_title": "💳 Пополнение баланса", "topup_choose": "Выберите сумму или укажите свою.\nОплата через Platega (СБП / карты).\nСредства зачисляются автоматически после подтверждения.",
        "topup_custom": "Введите сумму пополнения в рублях (минимум {minimum}₽):", "number": "Введите число, например 500",
        "minimum": "Минимальная сумма: {amount:.0f}₽", "payment_unavailable": "⚠️ Платёжная система пока не настроена.",
        "payment_error": "Ошибка создания платежа: {error}", "pay": "💳 Перейти к оплате", "check": "🔄 Проверить статус",
        "to_profile": "⬅️ В профиль", "invoice": "💳 Счёт на <b>{amount:.0f}₽</b> создан.\nID: <code>{tx_id}</code>\n\nНажмите кнопку для оплаты. После успешной оплаты баланс пополнится автоматически.",
        "status": "Статус платежа <code>{tx_id}</code>: <b>{status}</b>", "faq_title": "❓ FAQ — частые вопросы",
        "faq": "🗓 <b>Как купить?</b>\nПрофиль → «Пополнить», затем Магазин → выберите товар → оплата с баланса.\n\n💲 <b>Чем платить?</b>\nСБП и доступные способы оплаты Platega. Баланс зачисляется автоматически.\n\n🚀 <b>Когда придёт товар?</b>\nПосле успешной оплаты с баланса товар появится в «Мои покупки».\n\n☑️ <b>Гарантия и замена?</b>\nПо вопросам замены и гарантии — в поддержку.\n\n🎧 <b>Поддержка</b>\nКнопка «Поддержка» в главном меню.",
        "documents": "<b>📄 Документы</b>\n━━━━━━━━━━━━━━━━━━━━\n\nПравила сервиса, обработка данных и актуальные цены.",
        "support": "💬 <b>Поддержка PLS Store</b>\n\nНажми кнопку ниже, чтобы написать продавцу.",
        "support_unset": "💬 Поддержка пока не настроена.", "write_support": "💬 Написать в поддержку",
        "reviews": "⭐ Открыть отзывы", "reviews_title": "⭐ Отзывы покупателей PLS Store", "reviews_unset": "⭐ Ссылка на отзывы пока не настроена.",
        "api_info": "⚡ <b>API</b>\n\nРаздел API пока в разработке.\nОбратитесь в поддержку за доступом.",
        "license_url": "📜 Условия лицензии", "license_accept_button": "✅ Принимаю условия",
    },
    "en": {
        "welcome": "<b>PLS Store</b>\nPlugins and add-ons for FunPayCardinal\n\nWelcome, {first_name}!",
        "license_title": "📜 <b>License agreement</b>\n\nPlease accept the terms before using the store.",
        "license_accept": "Thank you! Terms accepted.", "language_title": "<b>Select language</b>",
        "language_saved": "Language changed to English.", "back": "⬅️ Back", "home": "🏠 Main menu",
        "shop": "🛒 PLS Store", "no_products": "There are no products yet. Please check back later or contact support.",
        "choose_product": "Choose a product:", "unavailable": "This product is no longer available.",
        "buy": "🛒 Buy for {price:.0f}₽", "manual": "📩 Request manually", "back_catalog": "⬅️ Back to catalog",
        "purchases": "🛒 <b>My purchases</b>", "empty_purchases": "No purchases yet. Visit the shop!",
        "balance": "💰 Balance", "spent": "💸 Total spent", "purchase_count": "🛒 Purchases",
        "promo_enter": "🎟 Enter promo code:", "promo_bad": "Promo code not found or inactive.",
        "promo_used": "You have already used this promo code.", "promo_limit": "The usage limit has been reached.",
        "promo_ok": "✅ Promo code applied! <b>{bonus:.0f}₽</b> credited.",
        "topup_title": "💳 Add balance", "topup_choose": "Choose an amount or enter a custom one.\nPay via Platega (bank transfer / cards).\nFunds are credited automatically after confirmation.",
        "topup_custom": "Enter the top-up amount in RUB (minimum {minimum}₽):", "number": "Enter a number, e.g. 500",
        "minimum": "Minimum amount: {amount:.0f}₽", "payment_unavailable": "⚠️ The payment system is not configured yet.",
        "payment_error": "Payment creation error: {error}", "pay": "💳 Proceed to payment", "check": "🔄 Check status",
        "to_profile": "⬅️ Profile", "invoice": "💳 Invoice for <b>{amount:.0f}₽</b> created.\nID: <code>{tx_id}</code>\n\nUse the button to pay. Your balance will be credited automatically after successful payment.",
        "status": "Payment status <code>{tx_id}</code>: <b>{status}</b>", "faq_title": "❓ FAQ — frequently asked questions",
        "faq": "🗓 <b>How do I buy?</b>\nOpen Profile → Add balance, then Shop → choose a product → pay using your balance.\n\n💲 <b>How can I pay?</b>\nAvailable Platega payment methods. Your balance is credited automatically.\n\n🚀 <b>When will I receive the product?</b>\nAfter a successful balance payment, the product will appear in My purchases.\n\n☑️ <b>Warranty and replacement?</b>\nContact support for warranty and replacement questions.\n\n🎧 <b>Support</b>\nUse the Support button in the main menu.",
        "documents": "<b>📄 Documents</b>\n━━━━━━━━━━━━━━━━━━━━\n\nService rules, data processing information and current prices.",
        "support": "💬 <b>PLS Store Support</b>\n\nPress the button below to contact the seller.",
        "support_unset": "💬 Support has not been configured yet.", "write_support": "💬 Contact support",
        "reviews": "⭐ Open reviews", "reviews_title": "⭐ PLS Store customer reviews", "reviews_unset": "⭐ Reviews link has not been configured yet.",
        "api_info": "⚡ <b>API</b>\n\nThe API section is under development.\nContact support to request access.",
        "license_url": "📜 License terms", "license_accept_button": "✅ I accept the terms",
    }
}

I18N["uk"] = {'welcome': '<b>PLS Store</b>\nПлагіни та доповнення для FunPayCardinal\n\nЛаскаво просимо, {first_name}!',
 'license_title': '📜 <b>Ліцензійна угода</b>\n\nПрийміть умови, щоб користуватися магазином.',
 'license_accept': 'Дякуємо! Умови прийнято.',
 'language_title': '<b>Виберіть мову</b>',
 'language_saved': 'Мову змінено на українську.',
 'back': '⬅️ Назад',
 'home': '🏠 Головне меню',
 'shop': '🛒 Магазин PLS Store',
 'no_products': 'Товарів поки немає. Завітайте пізніше або напишіть у підтримку.',
 'choose_product': 'Виберіть товар:',
 'unavailable': 'Цей товар більше недоступний.',
 'buy': '🛒 Купити за {price:.0f}₽',
 'manual': '📩 Залишити заявку (вручну)',
 'back_catalog': '⬅️ Назад до каталогу',
 'purchases': '🛒 <b>Мої покупки</b>',
 'empty_purchases': 'Поки порожньо. Завітайте до магазину!',
 'balance': '💰 Баланс',
 'spent': '💸 Усього витрачено',
 'purchase_count': '🛒 Покупок',
 'promo_enter': '🎟 Введіть промокод:',
 'promo_bad': 'Промокод не знайдено або він неактивний.',
 'promo_used': 'Ви вже використали цей промокод.',
 'promo_limit': 'Ліміт використань вичерпано.',
 'promo_ok': '✅ Промокод застосовано! Нараховано <b>{bonus:.0f}₽</b>.',
 'topup_title': '💳 Поповнення балансу',
 'topup_choose': 'Виберіть суму або введіть свою.\n'
                 'Оплата через Platega (СБП / картки).\n'
                 'Кошти зараховуються автоматично після підтвердження.',
 'topup_custom': 'Введіть суму поповнення в рублях (мінімум {minimum}₽):',
 'number': 'Введіть число, наприклад 500',
 'minimum': 'Мінімальна сума: {amount:.0f}₽',
 'payment_unavailable': '⚠️ Платіжну систему ще не налаштовано.',
 'payment_error': 'Помилка створення платежу: {error}',
 'pay': '💳 Перейти до оплати',
 'check': '🔄 Перевірити статус',
 'to_profile': '⬅️ До профілю',
 'invoice': '💳 Рахунок на <b>{amount:.0f}₽</b> створено.\n'
            'ID: <code>{tx_id}</code>\n'
            '\n'
            'Натисніть кнопку оплати. Після успішної оплати баланс поповниться автоматично.',
 'status': 'Статус платежу <code>{tx_id}</code>: <b>{status}</b>',
 'faq_title': '❓ FAQ — часті запитання',
 'faq': '🗓 <b>Як купити?</b>\n'
        'Профіль → «Поповнити», потім Магазин → виберіть товар → оплата з балансу.\n'
        '\n'
        '💲 <b>Як платити?</b>\n'
        'СБП та доступні способи оплати Platega. Баланс зараховується автоматично.\n'
        '\n'
        '🚀 <b>Коли надійде товар?</b>\n'
        'Після успішної оплати з балансу товар з’явиться в «Мої покупки».\n'
        '\n'
        '☑️ <b>Гарантія та заміна?</b>\n'
        'З питань заміни та гарантії звертайтеся до підтримки.\n'
        '\n'
        '🎧 <b>Підтримка</b>\n'
        'Кнопка «Підтримка» в головному меню.',
 'documents': '<b>📄 Документи</b>\n━━━━━━━━━━━━━━━━━━━━\n\nПравила сервісу, обробка даних та актуальні ціни.',
 'support': '💬 <b>Підтримка PLS Store</b>\n\nНатисніть кнопку нижче, щоб написати продавцю.',
 'support_unset': '💬 Підтримку ще не налаштовано.',
 'write_support': '💬 Написати в підтримку',
 'reviews': '⭐ Відкрити відгуки',
 'reviews_title': '⭐ Відгуки покупців PLS Store',
 'reviews_unset': '⭐ Посилання на відгуки ще не налаштовано.',
 'api_info': '⚡ <b>API</b>\n\nРозділ API ще в розробці.\nЗверніться до підтримки для доступу.',
 'license_url': '📜 Умови ліцензії',
 'license_accept_button': '✅ Приймаю умови'}

def tr(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_language(user_id)
    template = I18N.get(lang, I18N["ru"]).get(key, I18N["ru"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def message_text_with_custom_emoji(message: Message) -> str:
    """Convert incoming Telegram custom-emoji entities to Bot API HTML tags."""
    value = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    replacements = []
    # Telegram entity offsets are UTF-16 code units, while Python indexes code points.
    for entity in entities:
        if entity.type != "custom_emoji" or not entity.custom_emoji_id:
            continue
        start_units, end_units = entity.offset, entity.offset + entity.length
        units = 0
        start_idx = end_idx = None
        for idx, char in enumerate(value):
            if units == start_units:
                start_idx = idx
            units += len(char.encode("utf-16-le")) // 2
            if units == end_units:
                end_idx = idx + 1
                break
        if start_idx is not None and end_idx is not None:
            emoji = html.escape(value[start_idx:end_idx])
            tag = f'<tg-emoji emoji-id="{entity.custom_emoji_id}">{emoji}</tg-emoji>'
            replacements.append((start_idx, end_idx, tag))
    for start_idx, end_idx, tag in sorted(replacements, reverse=True):
        value = value[:start_idx] + tag + value[end_idx:]
    return value


def plain_stored_text(value: str | None) -> str:
    """Fallback text for Telegram fields (e.g. inline keyboard labels) without entity support."""
    return re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r'\1', value or "", flags=re.DOTALL)


def safe_stored_html(value: str | None) -> str:
    """Escape stored text while allowing only our Telegram custom-emoji tags."""
    value = value or ""
    pattern = re.compile(r'<tg-emoji emoji-id="\d+">.*?</tg-emoji>', re.DOTALL)
    parts = []
    last = 0
    for match in pattern.finditer(value):
        parts.append(html.escape(value[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(html.escape(value[last:]))
    return "".join(parts)


def banner_path(key: str) -> Path | None:
    """Return path to section banner if exists."""
    name = get_setting(f"banner_{key}", "")
    if name:
        p = BANNERS_DIR / name
        if p.is_file():
            return p
    # Uploaded banners take precedence over bundled section defaults.
    if key in {"profile", "faq", "language", "shop"}:
        bundled = ASSETS_DIR / f"{key}.png"
        if bundled.is_file():
            return bundled
    if key == "home" and BANNER_PATH.is_file():
        return BANNER_PATH
    return None


async def safe_edit_or_send(target, text: str, reply_markup=None, photo: Path | None = None):
    """Update callback screens in place to avoid Telegram's deletion animation."""
    message = target if isinstance(target, Message) else target.message
    if not isinstance(target, Message):
        try:
            if photo and photo.is_file():
                await message.edit_media(
                    media=InputMediaPhoto(media=FSInputFile(photo), caption=text),
                    reply_markup=reply_markup,
                )
            elif message.photo and len(text) <= 1024:
                # Keep the current banner for text-only subsections.
                await message.edit_caption(caption=text, reply_markup=reply_markup)
            elif not message.photo:
                await message.edit_text(text, reply_markup=reply_markup, link_preview_options=NO_PREVIEW)
            else:
                # Telegram cannot turn a photo message into a text message.
                await message.answer(text, reply_markup=reply_markup, link_preview_options=NO_PREVIEW)
                await message.edit_reply_markup(reply_markup=None)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message.lower():
                return
            # Old or otherwise uneditable messages can still open a fresh screen.
    if photo and photo.is_file():
        await message.answer_photo(photo=FSInputFile(photo), caption=text, reply_markup=reply_markup)
    else:
        await message.answer(text, reply_markup=reply_markup, link_preview_options=NO_PREVIEW)


def build_product_caption(product, *, max_desc: int = 420, lang: str = "ru") -> str:
    name = safe_stored_html(product["name"])
    desc = (product["description"] or "").strip()
    if len(desc) > max_desc:
        desc = desc[: max_desc - 1] + "…"
    desc = safe_stored_html(desc)
    price = html.escape(product["price"] or "")
    lines = [f"💎 <b>{name}</b>\n", f"<blockquote>{desc}</blockquote>\n"]
    if product.get("article_url"):
        lines.append(f"📄 {'Article' if lang == 'en' else 'Статья'}:\n{html.escape(product['article_url'])}")
    if product.get("video_url"):
        lines.append(f"🎥 {'Video review' if lang == 'en' else 'Видео-обзор'}: {html.escape(product['video_url'])}")
    lines.append(f"💰 {'Price' if lang == 'en' else 'Цена'}: <b>{price}</b>")
    reviews_url = get_setting("reviews_url", REVIEWS_URL)
    reviews_label = get_setting("reviews_label", "Отзывы · Клик")
    if reviews_url:
        lines.append(f'💬 <a href="{html.escape(reviews_url)}">{html.escape(reviews_label)}</a>')
    license_text = get_setting("license_text", "")
    if license_text:
        lines.append(f"📜 {html.escape(license_text)}")
    return "\n".join(lines)


# ─── FSM ────────────────────────────────────────────────────────────────────
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
    # new
    topup_custom = State()
    design_edit = State()
    faq_add_q = State()
    faq_add_a = State()
    faq_edit = State()
    balance_adjust = State()
    promo_create = State()


# ─── license gate (preserved) ───────────────────────────────────────────────
def has_accepted_license(user_id: int) -> bool:
    with db() as conn:
        return bool(
            conn.execute("SELECT 1 FROM license_accepts WHERE user_id=?", (user_id,)).fetchone()
        )


def accept_license(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO license_accepts(user_id) VALUES (?) "
            "ON CONFLICT(user_id) DO UPDATE SET accepted_at=CURRENT_TIMESTAMP",
            (user_id,),
        )


def license_gate_keyboard(license_url: str, user_id: int) -> InlineKeyboardMarkup:
    rows = []
    if license_url:
        rows.append([InlineKeyboardButton(text=tr(user_id, "license_url"), url=license_url)])
    rows.append([InlineKeyboardButton(text=tr(user_id, "license_accept_button"), callback_data="license:accept")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_license_gate(target, user_id: int) -> None:
    license_url = get_setting("license_url", "")
    text = tr(user_id, "license_title")
    await safe_edit_or_send(target, text, reply_markup=license_gate_keyboard(license_url, user_id))


# ─── home / catalog / product ───────────────────────────────────────────────
async def show_home(target, first_name: str):
    lang = get_user_language(target.from_user.id if isinstance(target, CallbackQuery) else target.from_user.id)
    welcome_template = get_setting("welcome_text", I18N[lang]["welcome"]) if lang == "ru" else I18N[lang]["welcome"]
    safe_name = html.escape(first_name)
    try:
        text = welcome_template.replace("{first_name}", safe_name)
    except Exception:
        text = f"<b>PLS Store</b>\nДобро пожаловать, {safe_name}!"
    photo = banner_path("home")
    await safe_edit_or_send(target, text, reply_markup=main_menu_kb(lang), photo=photo)


async def render_catalog(target) -> None:
    user_id = target.from_user.id
    lang = get_user_language(user_id)
    labels = {
        "ru": ("Выберите категорию.", "Плагины для FunPayCardinal"),
        "uk": ("Виберіть категорію.", "Плагіни для FunPayCardinal"),
        "en": ("Choose a category.", "Plugins for FunPayCardinal"),
    }
    prompt, category = labels[lang]
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🧩 {category} ›", callback_data="catalog:plugins")],
        [design_button("btn_back", "« Back" if lang == "en" else "« Назад", "home", "danger")],
    ])
    text = f"<b>{tr(user_id, 'shop')}</b>\n━━━━━━━━━━━━━━━━━━━━\n\n{prompt}"
    await safe_edit_or_send(target, text, reply_markup=markup, photo=banner_path("shop"))


async def render_plugins(target) -> None:
    products = get_products()
    if not products:
        text = f"🛒 <b>{tr(target.from_user.id, 'shop')}</b>\n\n{tr(target.from_user.id, 'no_products')}"
        markup = InlineKeyboardMarkup(inline_keyboard=[[design_button("btn_back", tr(target.from_user.id, "back"), "catalog", "danger")]])
    else:
        builder = InlineKeyboardBuilder()
        for product in products:
            ver = (product["version"] or "").strip() if product["version"] else ""
            if ver:
                ver_disp = ver if ver.lower().startswith("v") else f"v{ver}"
                title = f"{plain_stored_text(product['name'])} {ver_disp} — {product['price']}"
            else:
                title = f"{plain_stored_text(product['name'])} — {product['price']}"
            builder.button(text=title[:64], callback_data=f"product:{product['id']}")
        builder.add(design_button("btn_back", tr(target.from_user.id, "back"), "catalog", "danger"))
        builder.adjust(1)
        text = f"🛒 <b>{tr(target.from_user.id, 'shop')}</b>\n\n{tr(target.from_user.id, 'choose_product')}"
        markup = builder.as_markup()

    photo = banner_path("shop")
    await safe_edit_or_send(target, text, reply_markup=markup, photo=photo)


# ─── dispatcher ─────────────────────────────────────────────────────────────
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    # referral: /start ref_123
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1][4:])
            if ref_id != message.from_user.id:
                u = get_user(message.from_user.id)
                if u and not u.get("referrer_id"):
                    with db() as conn:
                        conn.execute(
                            "UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL",
                            (ref_id, message.from_user.id),
                        )
        except ValueError:
            pass
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
    await callback.answer(tr(callback.from_user.id, "license_accept"))
    accept_license(callback.from_user.id)
    await show_home(callback, callback.from_user.first_name or "покупатель")


@dp.message(Command("catalog"))
@dp.callback_query(F.data == "catalog")
async def cb_catalog(event: Message | CallbackQuery):
    if isinstance(event, CallbackQuery):
        await event.answer()
        user = event.from_user
        target = event
    else:
        user = event.from_user
        target = event
    if not has_accepted_license(user.id):
        await show_license_gate(target, user.id)
        return
    await render_catalog(target)


@dp.callback_query(F.data == "catalog:plugins")
async def cb_plugins(callback: CallbackQuery):
    await callback.answer()
    if not has_accepted_license(callback.from_user.id):
        await show_license_gate(callback, callback.from_user.id)
        return
    await render_plugins(callback)


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
    if not product or not product.get("active", 1):
        await safe_edit_or_send(callback, tr(callback.from_user.id, "unavailable"), reply_markup=back_kb("catalog", get_user_language(callback.from_user.id)))
        return

    text = build_product_caption(product, lang=get_user_language(callback.from_user.id))
    price_rub = parse_price_rub(product["price"], product)
    rows = [
        [InlineKeyboardButton(text=tr(callback.from_user.id, "buy", price=price_rub), callback_data=f"buybal:{product_id}")],
        [InlineKeyboardButton(text=tr(callback.from_user.id, "manual"), callback_data=f"buy:{product_id}")],
        [InlineKeyboardButton(text=tr(callback.from_user.id, "back_catalog"), callback_data="catalog:plugins")],
        [InlineKeyboardButton(text=tr(callback.from_user.id, "home"), callback_data="home")],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    image_path = product.get("image_path")
    photo = None
    if image_path:
        p = Path(image_path)
        if not p.is_absolute():
            p = ASSETS_DIR / p
        if p.is_file():
            photo = p
    await safe_edit_or_send(callback, text, reply_markup=keyboard, photo=photo)


# ─── buy from balance ───────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("buybal:"))
async def cb_buy_balance(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        product_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        return
    product = get_product(product_id)
    if not product or not product.get("active", 1):
        await callback.message.answer(tr(callback.from_user.id, "unavailable"))
        return
    user_id = callback.from_user.id
    ensure_user(user_id, callback.from_user.username, callback.from_user.first_name)
    price = parse_price_rub(product["price"], product)
    if price <= 0:
        await callback.message.answer("Product price is not set. Contact support." if get_user_language(callback.from_user.id) == "en" else "Цена товара не задана. Обратитесь в поддержку.")
        return
    u = get_user(user_id)
    bal = float(u["balance"]) if u else 0.0
    if bal < price:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=("💳 Add balance" if get_user_language(user_id) == "en" else "💳 Пополнить баланс"), callback_data="topup")],
            [InlineKeyboardButton(text=tr(user_id, "back"), callback_data=f"product:{product_id}")],
        ])
        await callback.message.answer(
            ((f"Insufficient funds.\n\nPrice: <b>{price:.0f}₽</b>\nYour balance: <b>{bal:.0f}₽</b>\n\nAdd funds and try again.") if get_user_language(user_id) == "en" else (f"Недостаточно средств.\n\nЦена: <b>{price:.0f}₽</b>\nВаш баланс: <b>{bal:.0f}₽</b>\n\nПополните баланс и повторите покупку.")),
            reply_markup=kb,
        )
        return

    # confirm
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(f"✅ Confirm charge {price:.0f}₽" if get_user_language(user_id) == "en" else f"✅ Подтвердить списание {price:.0f}₽"), callback_data=f"buyok:{product_id}")],
        [InlineKeyboardButton(text=("❌ Cancel" if get_user_language(user_id) == "en" else "❌ Отмена"), callback_data=f"product:{product_id}")],
    ])
    await callback.message.answer(
        ((f"Confirm purchase:\n\n📦 <b>{safe_stored_html(product['name'])}</b>\n💰 {price:.0f}₽\nBalance after purchase: <b>{bal - price:.0f}₽</b>") if get_user_language(user_id) == "en" else (f"Подтвердите покупку:\n\n📦 <b>{safe_stored_html(product['name'])}</b>\n💰 {price:.0f}₽\nБаланс после покупки: <b>{bal - price:.0f}₽</b>")),
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("buyok:"))
async def cb_buy_confirm(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        product_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        return
    product = get_product(product_id)
    if not product:
        await callback.message.answer(tr(callback.from_user.id, "unavailable"))
        return
    user_id = callback.from_user.id
    price = parse_price_rub(product["price"], product)
    try:
        new_bal = change_balance(user_id, -price, f"Покупка: {product['name']}")
    except ValueError:
        await callback.message.answer("Insufficient funds. Add balance and try again." if get_user_language(user_id) == "en" else "Недостаточно средств. Пополните баланс.")
        return
    purchase_id = add_purchase(user_id, product_id, price, product["name"])
    # also create order record for admin visibility
    with db() as conn:
        conn.execute(
            "INSERT INTO orders (user_id, username, product_id, status) VALUES (?, ?, ?, 'completed')",
            (user_id, callback.from_user.username, product_id),
        )
    await callback.message.answer(
        ((f"✅ <b>Purchase successful!</b>\n\nProduct: <b>{safe_stored_html(product['name'])}</b>\nCharged: {price:.0f}₽\nBalance: <b>{new_bal:.0f}₽</b>\n\nYour product is available in My purchases.\nThe seller has been notified and will contact you if needed.") if get_user_language(user_id) == "en" else (f"✅ <b>Покупка успешна!</b>\n\nТовар: <b>{safe_stored_html(product['name'])}</b>\nСписано: {price:.0f}₽\nБаланс: <b>{new_bal:.0f}₽</b>\n\nТовар доступен в разделе «Мои покупки».\nПродавец получил уведомление и свяжется при необходимости.")),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=("🛒 My purchases" if get_user_language(user_id) == "en" else "🛒 Мои покупки"), callback_data="mypurchases")],
            [InlineKeyboardButton(text=tr(callback.from_user.id, "home"), callback_data="home")],
        ]),
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✅ <b>Автопокупка #{purchase_id}</b>\n\n"
            f"Товар: {safe_stored_html(product['name'])}\n"
            f"Цена: {price:.0f}₽\n"
            f"Покупатель: {html.escape(callback.from_user.full_name)}\n"
            f"ID: <code>{user_id}</code>",
        )
    except Exception:
        log.exception("admin notify failed")


# ─── manual order (preserved) ───────────────────────────────────────────────
@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        product_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    product = get_product(product_id)
    if not product:
        await safe_edit_or_send(callback, "Товар недоступен.", reply_markup=back_kb("catalog"))
        return
    user = callback.from_user
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, username, product_id) VALUES (?, ?, ?)",
            (user.id, user.username, product_id),
        )
        order_id = cursor.lastrowid
    await safe_edit_or_send(
        callback,
        ("✅ <b>Request sent!</b>\n\nThe seller has been notified and will contact you to arrange the purchase.\nPayment and delivery will be arranged manually." if get_user_language(user.id) == "en" else "✅ <b>Заявка отправлена!</b>\n\nПродавец получил уведомление и свяжется с тобой для оформления покупки.\nОплата и передача товара согласовываются вручную."),
        reply_markup=back_kb("home", get_user_language(user.id)),
    )
    username = f"@{user.username}" if user.username else "без username"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Новая заявка №{order_id}</b>\n\n"
            f"Товар: <b>{safe_stored_html(product['name'])}</b>\n"
            f"Цена: {html.escape(product['price'])}\n"
            f"Покупатель: {html.escape(user.full_name)}\n"
            f"Username: {html.escape(username)}\n"
            f"Telegram ID: <code>{user.id}</code>\n\n"
            f"/reply {user.id} текст",
        )
    except Exception:
        log.exception("admin notify failed")


# ─── profile ────────────────────────────────────────────────────────────────
@dp.message(Command("profile"))
@dp.callback_query(F.data == "profile")
async def cb_profile(event: Message | CallbackQuery):
    if isinstance(event, CallbackQuery):
        await event.answer()
        user = event.from_user
        target = event
    else:
        user = event.from_user
        target = event
    if not has_accepted_license(user.id):
        await show_license_gate(target, user.id)
        return
    lang = get_user_language(user.id)
    text = profile_text(user.id, user, lang)
    photo = banner_path("profile")
    await safe_edit_or_send(target, text, reply_markup=profile_kb(lang), photo=photo)


@dp.callback_query(F.data == "mypurchases")
async def cb_mypurchases(callback: CallbackQuery):
    await callback.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (callback.from_user.id,),
        ).fetchall()
    if not rows:
        text = f"{tr(callback.from_user.id, 'purchases')}\n\n{tr(callback.from_user.id, 'empty_purchases')}"
    else:
        lines = [tr(callback.from_user.id, "purchases") + "\n"]
        for r in rows:
            lines.append(
                f"• {html.escape(r['product_name'] or 'Товар')} — {r['price']:.0f}₽\n"
                f"  <i>{r['created_at']}</i>"
            )
        text = "\n".join(lines)
    await safe_edit_or_send(callback, text, reply_markup=back_kb("profile"))


@dp.callback_query(F.data == "api_info")
async def cb_api_info(callback: CallbackQuery):
    await callback.answer()
    text = get_setting("api_info_text", tr(callback.from_user.id, "api_info"))
    if get_user_language(callback.from_user.id) == "en" and text.startswith("⚡ <b>API</b>"):
        text = tr(callback.from_user.id, "api_info")
    await safe_edit_or_send(callback, text, reply_markup=back_kb("profile"))


@dp.callback_query(F.data == "promo")
async def cb_promo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProductWizard.promo_create)  # reuse for input
    await callback.message.answer(
        tr(callback.from_user.id, "promo_enter"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="profile")]
        ]),
    )


@dp.message(ProductWizard.promo_create, F.text)
async def promo_apply(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    await state.clear()
    with db() as conn:
        promo = conn.execute(
            "SELECT * FROM promos WHERE upper(code)=? AND active=1", (code,)
        ).fetchone()
        if not promo:
            await message.answer(tr(message.from_user.id, "promo_bad"), reply_markup=back_kb("profile", get_user_language(message.from_user.id)))
            return
        used = conn.execute(
            "SELECT 1 FROM promo_uses WHERE promo_id=? AND user_id=?",
            (promo["id"], message.from_user.id),
        ).fetchone()
        if used:
            await message.answer(tr(message.from_user.id, "promo_used"), reply_markup=back_kb("profile", get_user_language(message.from_user.id)))
            return
        if promo["max_uses"] is not None and promo["used_count"] >= promo["max_uses"]:
            await message.answer(tr(message.from_user.id, "promo_limit"), reply_markup=back_kb("profile", get_user_language(message.from_user.id)))
            return
        # apply
        if promo["discount_type"] == "fixed":
            bonus = float(promo["value"])
        else:
            # percent of nothing specific — give fixed bonus based on value as amount
            bonus = float(promo["value"])
        change_balance(message.from_user.id, bonus, f"Промокод {code}")
        conn.execute(
            "INSERT INTO promo_uses (promo_id, user_id) VALUES (?, ?)",
            (promo["id"], message.from_user.id),
        )
        conn.execute(
            "UPDATE promos SET used_count = used_count + 1 WHERE id=?",
            (promo["id"],),
        )
    await message.answer(
        tr(message.from_user.id, "promo_ok", bonus=bonus),
        reply_markup=back_kb("profile"),
    )


@dp.callback_query(F.data == "referral")
async def cb_referral(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    pct = get_setting("referral_percent", "10")
    with db() as conn:
        ref_count = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id=?", (callback.from_user.id,)
        ).fetchone()["c"]
        u = get_user(callback.from_user.id)
        earned = u["referral_earnings"] if u else 0
    if get_user_language(callback.from_user.id) == "en":
        text = (f"🔗 <b>Referral program</b>\n\nYour link:\n<code>{link}</code>\n\n"
                f"You earn <b>{pct}%</b> from your referrals' top-ups.\nInvited: <b>{ref_count}</b>\nEarned: <b>{earned:.0f}₽</b>")
    else:
        text = (f"🔗 <b>Реферальная система</b>\n\nВаша ссылка:\n<code>{link}</code>\n\n"
                f"Вы получаете <b>{pct}%</b> от пополнений приглашённых.\nПриглашено: <b>{ref_count}</b>\nЗаработано: <b>{earned:.0f}₽</b>")
    await safe_edit_or_send(callback, text, reply_markup=back_kb("profile", get_user_language(callback.from_user.id)))


# ─── topup / Platega ────────────────────────────────────────────────────────
@dp.callback_query(F.data == "topup")
async def cb_topup(callback: CallbackQuery):
    await callback.answer()
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    text = f"<b>{tr(callback.from_user.id, 'topup_title')}</b>\n\n{tr(callback.from_user.id, 'topup_choose')}"
    photo = banner_path("topup")
    await safe_edit_or_send(callback, text, reply_markup=topup_amounts_kb(get_user_language(callback.from_user.id)), photo=photo)


@dp.callback_query(F.data.startswith("topup:"))
async def cb_topup_amount(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    part = callback.data.split(":", 1)[1]
    if part == "custom":
        await state.set_state(ProductWizard.topup_custom)
        await callback.message.answer(
            tr(callback.from_user.id, "topup_custom", minimum=get_setting('min_topup', '100'))
        )
        return
    try:
        amount = float(part)
    except ValueError:
        return
    await create_topup(callback.message, callback.from_user, amount, bot)


@dp.message(ProductWizard.topup_custom, F.text)
async def topup_custom_msg(message: Message, state: FSMContext, bot: Bot):
    try:
        amount = float((message.text or "").replace(",", ".").strip())
    except ValueError:
        await message.answer(tr(message.from_user.id, "number"))
        return
    min_a = float(get_setting("min_topup", "100"))
    if amount < min_a:
        await message.answer(tr(message.from_user.id, "minimum", amount=min_a))
        return
    await state.clear()
    await create_topup(message, message.from_user, amount, bot)


async def create_topup(message: Message, user, amount: float, bot: Bot):
    ensure_user(user.id, user.username, user.first_name)
    if not platega:
        await message.answer(
            tr(user.id, "payment_unavailable")
        )
        return
    payload = f"topup:{user.id}:{amount}"
    try:
        me = await bot.me()
        ret = f"https://t.me/{me.username}"
        data = await platega.create_payment(
            amount=amount,
            description=f"Пополнение PLS Store #{user.id}",
            payload=payload,
            return_url=ret,
            failed_url=ret,
            payment_method=2,
            metadata={
                "userId": str(user.id),
                "userName": f"@{user.username}" if user.username else str(user.id),
            },
        )
    except PlategaError as e:
        log.exception("create payment failed")
        await message.answer(tr(user.id, "payment_error", error=e))
        return

    tx_id = data.get("transactionId") or data.get("id")
    redirect = data.get("redirect") or data.get("url")
    with db() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, amount, platega_id, status, payload, redirect_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, amount, tx_id, data.get("status", "PENDING"), payload, redirect),
        )
    rows = []
    if redirect:
        rows.append([InlineKeyboardButton(text=tr(user.id, "pay"), url=redirect)])
    rows.append([InlineKeyboardButton(text=tr(user.id, "check"), callback_data=f"paystatus:{tx_id}")])
    rows.append([InlineKeyboardButton(text=tr(user.id, "to_profile"), callback_data="profile")])
    await message.answer(
        tr(user.id, "invoice", amount=amount, tx_id=tx_id),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("paystatus:"))
async def cb_pay_status(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    tx_id = callback.data.split(":", 1)[1]
    if not platega:
        await callback.message.answer(tr(callback.from_user.id, "payment_unavailable"))
        return
    try:
        data = await platega.get_status(tx_id)
    except PlategaError as e:
        await callback.message.answer(f"Ошибка: {e}")
        return
    status = (data.get("status") or "").upper()
    await callback.message.answer(tr(callback.from_user.id, "status", tx_id=tx_id, status=status))
    if status == "CONFIRMED":
        await process_confirmed_payment(tx_id, data, bot)


async def process_confirmed_payment(tx_id: str, data: dict, bot: Bot) -> bool:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM payments WHERE platega_id=? AND status != 'CONFIRMED'",
            (tx_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        amount = float(row["amount"])
        user_id = row["user_id"]
        pay_id = row["id"]
        conn.execute(
            "UPDATE payments SET status='CONFIRMED', confirmed_at=CURRENT_TIMESTAMP WHERE platega_id=?",
            (tx_id,),
        )
        conn.commit()
    try:
        new_bal = change_balance(user_id, amount, f"Platega topup {tx_id}", payment_id=pay_id)
    except Exception:
        log.exception("balance credit failed")
        return False
    try:
        await bot.send_message(
            user_id,
            (f"✅ Balance topped up by <b>{amount:.0f}₽</b>.\nCurrent balance: <b>{new_bal:.0f}₽</b>" if get_user_language(user_id) == "en" else f"✅ Баланс пополнен на <b>{amount:.0f}₽</b>.\nТекущий баланс: <b>{new_bal:.0f}₽</b>"),
        )
    except Exception:
        pass
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
            try:
                await bot.send_message(
                    u["referrer_id"],
                    f"🎁 Реферальный бонус +{bonus:.0f}₽ (пополнение пользователя {user_id})",
                )
            except Exception:
                pass
    return True


# ─── FAQ / documents ────────────────────────────────────────────────────────
DOCUMENTS = [
    ("🔒 Политика конфиденциальности", "https://telegra.ph/Politika-konfidencialnosti-PLS-STORE-11-07"),
    ("📜 Пользовательское соглашение", "https://telegra.ph/Polzovatelskoe-soglashenie-PLS-STORE-11-07"),
    ("💵 Товары и тарифы", "https://telegra.ph/Tovary-i-tarify-PLS-STORE-11-07"),
]


def documents_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, url=url)]
        for title, url in DOCUMENTS
    ]
    rows.extend([
        [support_button(style=None)],
        [design_button("btn_back", "⬅️ Назад", "faq", "danger")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "language")
async def cb_language(callback: CallbackQuery):
    await callback.answer()
    lang = get_user_language(callback.from_user.id)
    kb = language_kb(lang)
    await safe_edit_or_send(callback, tr(callback.from_user.id, "language_title"), reply_markup=kb, photo=banner_path("language"))


@dp.callback_query(F.data.startswith("lang:set:"))
async def cb_language_set(callback: CallbackQuery):
    lang = callback.data.rsplit(":", 1)[-1]
    if lang not in ("ru", "uk", "en"):
        await callback.answer("Invalid language", show_alert=True)
        return
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    set_user_language(callback.from_user.id, lang)
    await callback.answer(I18N[lang]["language_saved"], show_alert=True)
    await show_home(callback, callback.from_user.first_name or "customer")


@dp.callback_query(F.data == "faq")
async def cb_faq(callback: CallbackQuery):
    await callback.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [design_button("btn_documents", ("📄 Documents" if get_user_language(callback.from_user.id) == "en" else ("📄 Документи" if get_user_language(callback.from_user.id) == "uk" else "📄 Документы")), "documents", "primary")],
        [design_button("btn_back", ("« Back" if get_user_language(callback.from_user.id) == "en" else "« Назад"), "home", "danger")],
    ])
    await safe_edit_or_send(callback, f"<b>{tr(callback.from_user.id, 'faq_title')}</b>\n━━━━━━━━━━━━━━━━━━━━\n\n{tr(callback.from_user.id, 'faq')}", reply_markup=markup, photo=banner_path("faq"))


@dp.callback_query(F.data == "documents")
async def cb_documents(callback: CallbackQuery):
    await callback.answer()
    lang = get_user_language(callback.from_user.id)
    titles = [
        (("🔒 Privacy Policy" if lang == "en" else ("🔒 Політика конфіденційності" if lang == "uk" else "🔒 Политика конфиденциальности")), DOCUMENTS[0][1]),
        (("📜 User Agreement" if lang == "en" else ("📜 Угода користувача" if lang == "uk" else "📜 Пользовательское соглашение")), DOCUMENTS[1][1]),
        (("💵 Products and Pricing" if lang == "en" else ("💵 Товари й тарифи" if lang == "uk" else "💵 Товары и тарифы")), DOCUMENTS[2][1]),
    ]
    rows = [[InlineKeyboardButton(text=title, url=url)] for title, url in titles]
    rows += [[support_button(lang, style=None)],
             [design_button("btn_back", tr(callback.from_user.id, "back"), "faq", "danger")]]
    await safe_edit_or_send(callback, tr(callback.from_user.id, "documents"), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), photo=banner_path("faq"))


# ─── support / reviews ──────────────────────────────────────────────────────
def support_screen(user_id: int):
    support_username = get_setting("support_username", SUPPORT_USERNAME).lstrip("@")
    if support_username:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr(user_id, "write_support"), url=f"https://t.me/{support_username}")],
            [InlineKeyboardButton(text=tr(user_id, "home"), callback_data="home")],
        ])
        text = tr(user_id, "support")
    else:
        keyboard = back_kb("home", get_user_language(user_id))
        text = tr(user_id, "support_unset")
    return text, keyboard


@dp.message(Command("support"))
@dp.callback_query(F.data == "support")
async def cb_support(event: Message | CallbackQuery):
    if isinstance(event, CallbackQuery):
        await event.answer()
        target = event
    else:
        target = event
    text, keyboard = support_screen(event.from_user.id)
    await safe_edit_or_send(target, text, reply_markup=keyboard)


@dp.callback_query(F.data == "reviews")
async def cb_reviews(callback: CallbackQuery):
    await callback.answer()
    url = get_setting("reviews_url", REVIEWS_URL)
    if url:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr(callback.from_user.id, "reviews"), url=url)],
            [InlineKeyboardButton(text=tr(callback.from_user.id, "back"), callback_data="home")],
        ])
        text = tr(callback.from_user.id, "reviews_title")
    else:
        kb = back_kb("home")
        text = tr(callback.from_user.id, "reviews_unset")
    await safe_edit_or_send(callback, text, reply_markup=kb)


# ─── admin panel (extended) ─────────────────────────────────────────────────
def admin_home_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧩 Товары", callback_data="adm:products"),
            InlineKeyboardButton(text="➕ Добавить", callback_data="adm:add"),
        ],
        [InlineKeyboardButton(text="📨 Заявки", callback_data="adm:orders")],
        [
            InlineKeyboardButton(text="💳 Платежи", callback_data="adm:payments"),
            InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users"),
        ],
        [
            InlineKeyboardButton(text="💰 Балансы", callback_data="adm:balances"),
            InlineKeyboardButton(text="🛒 Покупки", callback_data="adm:purchases"),
        ],
        [
            InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm:promos"),
            InlineKeyboardButton(text="❓ FAQ", callback_data="adm:faq"),
        ],
        [InlineKeyboardButton(text="🎨 Дизайн", callback_data="adm:design")],
        [InlineKeyboardButton(text="🖼 Баннеры", callback_data="adm:banners")],
        [InlineKeyboardButton(text="⚙️ Оформление и ссылки", callback_data="adm:settings")],
    ])


@dp.message(Command("syncemojis"))
async def cmd_sync_emojis(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    middleware = next((item for item in bot.session.middleware if isinstance(item, PackEmojiMiddleware)), None)
    if middleware is None:
        await message.answer("Модуль эмодзи не подключён. Перезапустите бота.")
        return
    try:
        count = await middleware.refresh(bot)
    except Exception as exc:
        log.warning("Emoji refresh failed: %s", type(exc).__name__)
        await message.answer("Не удалось загрузить набор эмодзи. Проверьте CUSTOM_EMOJI_PACKS и доступ бота к Telegram.")
        return
    await message.answer(f"✅ Набор обновлён: {count} эмодзи. Откройте главное меню заново.")


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Админ-панель PLS Store</b>", reply_markup=admin_home_keyboard())


@dp.callback_query(F.data == "adm:home")
async def adm_home(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    await safe_edit_or_send(callback, "🛠 <b>Админ-панель PLS Store</b>", reply_markup=admin_home_keyboard())


# --- design settings ---
DESIGN_KEYS = [
    ("welcome_text", "Приветствие ({first_name})"),
    ("btn_profile", "Кнопка Профиль"),
    ("btn_shop", "Кнопка Магазин"),
    ("btn_language", "Кнопка Язык"),
    ("btn_faq", "Кнопка FAQ"),
    ("btn_support", "Кнопка Поддержка"),
    ("btn_topup", "Кнопка Пополнить"),
    ("btn_purchases", "Кнопка Мои покупки"),
    ("btn_api", "Кнопка API"),
    ("btn_promo", "Кнопка Промокод"),
    ("btn_referral", "Кнопка Рефералка"),
    ("btn_back", "Кнопка Назад"),
    ("profile_title", "Заголовок профиля"),
    ("shop_title", "Заголовок магазина"),
    ("faq_title", "Заголовок FAQ"),
    ("topup_title", "Заголовок пополнения"),
    ("referral_percent", "Реф. %"),
    ("min_topup", "Мин. пополнение"),
    ("api_info_text", "Текст раздела API"),
]


@dp.callback_query(F.data == "adm:design")
async def adm_design(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    builder = InlineKeyboardBuilder()
    for key, label in DESIGN_KEYS:
        builder.button(text=label, callback_data=f"adm:des:{key}")
    builder.button(text="⬅️ Назад", callback_data="adm:home")
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "🎨 <b>Настройка дизайна</b>\n\nВыберите параметр для изменения.\n"
        "Текущие значения подставляются из базы и сохраняются после перезапуска.",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data.startswith("adm:des:"))
async def adm_design_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    key = callback.data.split(":", 2)[2]
    await state.set_state(ProductWizard.design_edit)
    await state.update_data(design_key=key)
    current = get_setting(key, "")
    await callback.answer()
    await callback.message.answer(
        (f"Текущее значение <code>{key}</code>:\n\n{html.escape(current)}\n\n"
         "Отправьте текст сообщением. Для кнопок можно добавить Premium Emoji: "
         "он будет отображаться отдельной иконкой слева от названия (или /cancel).")
    )


@dp.message(ProductWizard.design_edit, F.text)
async def adm_design_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("design_key")
    if not key:
        await state.clear()
        return
    stored_value = message_text_with_custom_emoji(message)
    set_setting(key, stored_value)
    if key.startswith("btn_"):
        # Telegram button icons use a separate Bot API field, not a <tg-emoji> HTML tag.
        custom_emoji_id = next(
            (str(entity.custom_emoji_id) for entity in (message.entities or [])
             if entity.type == "custom_emoji" and entity.custom_emoji_id),
            "",
        )
        set_setting(f"{key}_icon_custom_emoji_id", custom_emoji_id)
    await state.clear()
    await message.answer(
        f"✅ Сохранено: <code>{key}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎨 К дизайну", callback_data="adm:design")],
            [InlineKeyboardButton(text="🛠 Админка", callback_data="adm:home")],
        ]),
    )


# --- banners ---
BANNER_KEYS = ["home", "profile", "shop", "faq", "topup", "language"]


@dp.callback_query(F.data == "adm:banners")
async def adm_banners(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    rows = []
    for k in BANNER_KEYS:
        cur = get_setting(f"banner_{k}", "—")
        rows.append([InlineKeyboardButton(
            text=f"🖼 {k}: {cur}",
            callback_data=f"adm:ban:{k}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")])
    await safe_edit_or_send(
        callback,
        "🖼 <b>Баннеры разделов</b>\n\nВыберите раздел и отправьте новое фото.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("adm:ban:"))
async def adm_banner_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    key = callback.data.split(":")[2]
    await state.set_state(ProductWizard.set_banner)
    await state.update_data(banner_key=key)
    await callback.answer()
    await callback.message.answer(f"Отправьте фото для баннера «{key}» (или /cancel):")


@dp.message(ProductWizard.set_banner, F.photo)
async def adm_banner_save(message: Message, bot: Bot, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("banner_key", "home")
    photo = message.photo[-1]
    filename = f"{key}_{photo.file_unique_id}.jpg"
    dest = BANNERS_DIR / filename
    await bot.download(photo, destination=dest)
    set_setting(f"banner_{key}", filename)
    # also update main banner if home
    if key == "home":
        try:
            import shutil
            shutil.copy(dest, BANNER_PATH)
        except Exception:
            pass
    await state.clear()
    await message.answer(
        f"✅ Баннер «{key}» обновлён.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🖼 К баннерам", callback_data="adm:banners")],
            [InlineKeyboardButton(text="🛠 Админка", callback_data="adm:home")],
        ]),
    )


# --- FAQ admin ---
@dp.callback_query(F.data == "adm:faq")
async def adm_faq(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    items = get_faq(active_only=False)
    builder = InlineKeyboardBuilder()
    for item in items:
        flag = "✅" if item["active"] else "🚫"
        builder.button(text=f"{flag} {item['question'][:40]}", callback_data=f"adm:faqi:{item['id']}")
    builder.button(text="➕ Добавить вопрос", callback_data="adm:faqadd")
    builder.button(text="⬅️ Назад", callback_data="adm:home")
    builder.adjust(1)
    await safe_edit_or_send(callback, "❓ <b>Управление FAQ</b>", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "adm:faqadd")
async def adm_faq_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(ProductWizard.faq_add_q)
    await callback.answer()
    await callback.message.answer("Отправьте вопрос. Можно вставлять Premium Emoji прямо в текст:")


@dp.message(ProductWizard.faq_add_q, F.text)
async def adm_faq_add_q(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(faq_q=message_text_with_custom_emoji(message))
    await state.set_state(ProductWizard.faq_add_a)
    await message.answer("Теперь отправьте ответ. Premium Emoji также поддерживаются:")


@dp.message(ProductWizard.faq_add_a, F.text)
async def adm_faq_add_a(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    q = data.get("faq_q", "")
    a = message_text_with_custom_emoji(message)
    with db() as conn:
        conn.execute("INSERT INTO faq (question, answer, sort_order) VALUES (?, ?, 999)", (q, a))
    await state.clear()
    await message.answer(
        "✅ Вопрос добавлен.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❓ К FAQ", callback_data="adm:faq")],
        ]),
    )


@dp.callback_query(F.data.startswith("adm:faqi:"))
async def adm_faq_item(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    fid = int(callback.data.split(":")[2])
    with db() as conn:
        row = conn.execute("SELECT * FROM faq WHERE id=?", (fid,)).fetchone()
        if not row:
            await callback.answer("Не найден", show_alert=True)
            return
        new_active = 0 if row["active"] else 1
        conn.execute("UPDATE faq SET active=? WHERE id=?", (new_active, fid))
    await callback.answer("Статус изменён")
    await adm_faq(callback)


# --- payments / users / balances / purchases (read-only lists) ---
@dp.callback_query(F.data == "adm:payments")
async def adm_payments(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM payments ORDER BY id DESC LIMIT 15"
        ).fetchall()
    if not rows:
        text = "💳 Платежей пока нет."
    else:
        lines = ["💳 <b>Последние платежи</b>\n"]
        for r in rows:
            lines.append(
                f"#{r['id']} user <code>{r['user_id']}</code> "
                f"{r['amount']:.0f}₽ · {r['status']}\n"
                f"<code>{r['platega_id'] or '—'}</code> · {r['created_at']}"
            )
        text = "\n".join(lines)
    await safe_edit_or_send(callback, text, reply_markup=back_kb("adm:home"))


@dp.callback_query(F.data == "adm:users")
async def adm_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    if not rows:
        text = "👥 Пользователей пока нет."
    else:
        lines = ["👥 <b>Пользователи</b>\n"]
        for r in rows:
            un = f"@{r['username']}" if r["username"] else "—"
            lines.append(
                f"<code>{r['user_id']}</code> {html.escape(un)} · "
                f"баланс {r['balance']:.0f}₽ · покупок {r['purchases_count']}"
            )
        text = "\n".join(lines)
    await safe_edit_or_send(callback, text, reply_markup=back_kb("adm:home"))


@dp.callback_query(F.data == "adm:purchases")
async def adm_purchases(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM purchases ORDER BY id DESC LIMIT 15"
        ).fetchall()
    if not rows:
        text = "🛒 Покупок пока нет."
    else:
        lines = ["🛒 <b>Последние покупки</b>\n"]
        for r in rows:
            lines.append(
                f"#{r['id']} user <code>{r['user_id']}</code> · "
                f"{html.escape(r['product_name'] or '')} · {r['price']:.0f}₽\n"
                f"{r['created_at']}"
            )
        text = "\n".join(lines)
    await safe_edit_or_send(callback, text, reply_markup=back_kb("adm:home"))


@dp.callback_query(F.data == "adm:balances")
async def adm_balances(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await state.set_state(ProductWizard.balance_adjust)
    await callback.message.answer(
        "💰 <b>Корректировка баланса</b>\n\n"
        "Отправьте в формате:\n"
        "<code>USER_ID +100 причина</code>\n"
        "или\n"
        "<code>USER_ID -50 причина</code>\n\n"
        "/cancel для отмены."
    )


@dp.message(ProductWizard.balance_adjust, F.text)
async def adm_balance_do(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Формат: USER_ID ±сумма [причина]")
        return
    try:
        uid = int(parts[0])
        delta = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("Неверный формат числа.")
        return
    reason = parts[2] if len(parts) > 2 else "Ручная корректировка админом"
    try:
        new_bal = change_balance(uid, delta, reason, admin_id=message.from_user.id)
    except ValueError as e:
        await message.answer(str(e))
        return
    await state.clear()
    await message.answer(
        f"✅ Баланс user <code>{uid}</code> изменён на {delta:+.0f}₽.\n"
        f"Новый баланс: <b>{new_bal:.0f}₽</b>",
        reply_markup=back_kb("adm:home"),
    )
    try:
        await bot.send_message(
            uid,
            f"💰 Ваш баланс изменён администратором на {delta:+.0f}₽.\n"
            f"Текущий баланс: <b>{new_bal:.0f}₽</b>\n"
            f"Причина: {html.escape(reason)}",
        )
    except Exception:
        pass


@dp.callback_query(F.data == "adm:promos")
async def adm_promos(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    with db() as conn:
        rows = conn.execute("SELECT * FROM promos ORDER BY id DESC LIMIT 20").fetchall()
    lines = ["🎟 <b>Промокоды</b>\n"]
    if not rows:
        lines.append("Пока нет. Создайте: /addpromo CODE 100")
    else:
        for r in rows:
            lines.append(
                f"<code>{r['code']}</code> · {r['value']:.0f} "
                f"({'%' if r['discount_type']=='percent' else '₽'}) · "
                f"used {r['used_count']}/{r['max_uses'] or '∞'} · "
                f"{'✅' if r['active'] else '🚫'}"
            )
    lines.append("\nСоздать: <code>/addpromo CODE СУММА [лимит]</code>")
    await safe_edit_or_send(callback, "\n".join(lines), reply_markup=back_kb("adm:home"))


@dp.message(Command("addpromo"))
async def cmd_addpromo(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Формат: /addpromo CODE 100 [max_uses]")
        return
    code = parts[1].upper()
    try:
        value = float(parts[2])
    except ValueError:
        await message.answer("Сумма должна быть числом")
        return
    max_uses = int(parts[3]) if len(parts) > 3 else None
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO promos (code, discount_type, value, max_uses) VALUES (?, 'fixed', ?, ?)",
                (code, value, max_uses),
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
            return
    await message.answer(f"✅ Промокод <code>{code}</code> на {value:.0f}₽ создан.")


# ─── original product admin (abbreviated but functional) ────────────────────
@dp.callback_query(F.data == "adm:products")
async def adm_products(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    with db() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY sort_order, id").fetchall()
    builder = InlineKeyboardBuilder()
    for p in products:
        flag = "✅" if p["active"] else "🚫"
        builder.button(
            text=f"{flag} {plain_stored_text(p['name'])[:40]} — {p['price']}",
            callback_data=f"adm:p:{p['id']}",
        )
    builder.button(text="⬅️ Назад", callback_data="adm:home")
    builder.adjust(1)
    await safe_edit_or_send(callback, "🧩 <b>Товары</b>", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("adm:p:"))
async def adm_product_open(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    pid = int(callback.data.split(":")[2])
    product = get_product(pid)
    if not product:
        await callback.answer("Не найден", show_alert=True)
        return
    await callback.answer()
    text = (
        f"🧩 <b>{safe_stored_html(product['name'])}</b>\n"
        f"Цена: {html.escape(product['price'])}\n"
        f"Активен: {'да' if product['active'] else 'нет'}\n"
        f"ID: {product['id']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Название", callback_data=f"adm:edit:{pid}:name"),
            InlineKeyboardButton(text="💰 Цена", callback_data=f"adm:edit:{pid}:price"),
        ],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"adm:edit:{pid}:description")],
        [
            InlineKeyboardButton(
                text="👁 Скрыть" if product["active"] else "👁 Показать",
                callback_data=f"adm:toggle:{pid}",
            ),
        ],
        [InlineKeyboardButton(text="⬅️ К товарам", callback_data="adm:products")],
    ])
    await safe_edit_or_send(callback, text, reply_markup=kb)


@dp.callback_query(F.data.startswith("adm:toggle:"))
async def adm_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    pid = int(callback.data.split(":")[2])
    with db() as conn:
        row = conn.execute("SELECT active FROM products WHERE id=?", (pid,)).fetchone()
        if row:
            conn.execute("UPDATE products SET active=? WHERE id=?", (0 if row["active"] else 1, pid))
    await callback.answer("Готово")
    callback.data = f"adm:p:{pid}"
    await adm_product_open(callback)


@dp.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit_field(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    pid, field = int(parts[2]), parts[3]
    await state.update_data(product_id=pid, field=field)
    await state.set_state(ProductWizard.edit_value)
    await callback.answer()
    await callback.message.answer(f"Отправьте новое значение для «{field}» (Premium Emoji поддерживаются, или /cancel):")


@dp.message(ProductWizard.edit_value, F.text)
async def adm_edit_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    pid, field = data["product_id"], data["field"]
    value = message_text_with_custom_emoji(message)
    allowed = {"name", "price", "description", "article_url", "video_url", "version"}
    if field not in allowed:
        await state.clear()
        return
    with db() as conn:
        conn.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, pid))
        if field == "price":
            conn.execute(
                "UPDATE products SET price_rub=? WHERE id=?",
                (parse_price_rub(value), pid),
            )
    await state.clear()
    await message.answer(
        "✅ Сохранено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ К товару", callback_data=f"adm:p:{pid}")],
        ]),
    )


@dp.callback_query(F.data == "adm:add")
async def adm_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(ProductWizard.add_name)
    await callback.answer()
    await callback.message.answer("Отправьте название товара (Premium Emoji поддерживаются):")


@dp.message(ProductWizard.add_name, F.text)
async def adm_add_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(name=message_text_with_custom_emoji(message))
    await state.set_state(ProductWizard.add_price)
    await message.answer("Отправьте цену (например: 3490₽):")


@dp.message(ProductWizard.add_price, F.text)
async def adm_add_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(price=message.text)
    await state.set_state(ProductWizard.add_description)
    await message.answer("Отправьте описание товара. Premium Emoji поддерживаются:")


@dp.message(ProductWizard.add_description, F.text)
async def adm_add_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    price = data["price"]
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO products (name, price, description, price_rub) VALUES (?, ?, ?, ?)",
            (data["name"], price, message_text_with_custom_emoji(message), parse_price_rub(price)),
        )
        pid = cur.lastrowid
    await state.clear()
    await message.answer(
        f"✅ Товар создан (ID {pid}).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Настроить", callback_data=f"adm:p:{pid}")],
            [InlineKeyboardButton(text="🧩 К товарам", callback_data="adm:products")],
        ]),
    )


@dp.callback_query(F.data == "adm:orders")
async def adm_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT o.*, p.name AS product_name FROM orders o "
            "LEFT JOIN products p ON p.id=o.product_id "
            "ORDER BY o.id DESC LIMIT 15"
        ).fetchall()
    if not rows:
        text = "📨 Заявок пока нет."
    else:
        lines = ["📨 <b>Последние заявки</b>\n"]
        for o in rows:
            lines.append(
                f"#{o['id']} · {html.escape(o['product_name'] or '?')} · "
                f"<code>{o['user_id']}</code> · {o['status']}\n"
                f"{o['created_at']}"
            )
        text = "\n".join(lines)
    await safe_edit_or_send(callback, text, reply_markup=back_kb("adm:home"))


@dp.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    support = get_setting("support_username", SUPPORT_USERNAME) or "не задан"
    reviews = get_setting("reviews_url", REVIEWS_URL) or "не задан"
    text = (
        "⚙️ <b>Оформление и ссылки</b>\n\n"
        f"Поддержка: @{html.escape(support)}\n"
        f"Отзывы: {html.escape(reviews)}\n\n"
        "Для смены приветствия и кнопок используйте «🎨 Дизайн»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 Дизайн", callback_data="adm:design")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")],
    ])
    await safe_edit_or_send(callback, text, reply_markup=kb)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@dp.message(Command("reply"))
async def cmd_reply(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: /reply TELEGRAM_ID текст")
        return
    try:
        await bot.send_message(
            int(parts[1]),
            "💬 <b>Сообщение от поддержки PLS Store</b>\n\n" + html.escape(parts[2]),
        )
        await message.answer("✅ Отправлено.")
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")


# ─── Platega callback HTTP server ───────────────────────────────────────────
async def platega_callback_handler(request: web.Request):
    bot: Bot = request.app["bot"]
    if not platega:
        return web.Response(status=503, text="Platega not configured")
    headers = {k: v for k, v in request.headers.items()}
    if not platega.verify_callback_headers(headers):
        log.warning("Platega callback: invalid headers")
        return web.Response(status=403, text="Forbidden")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")
    status = (data.get("status") or "").upper()
    tx_id = data.get("id") or data.get("transactionId")
    log.info("Platega callback: %s status=%s", tx_id, status)
    if status == "CONFIRMED" and tx_id:
        await process_confirmed_payment(str(tx_id), data, bot)
    elif status in ("CANCELED", "CANCELLED") and tx_id:
        with db() as conn:
            conn.execute(
                "UPDATE payments SET status='CANCELED' WHERE platega_id=? AND status='PENDING'",
                (str(tx_id),),
            )
    return web.Response(status=200, text="OK")


async def start_callback_server(bot: Bot):
    if not PLATEGA_CALLBACK_BASE:
        log.warning("PLATEGA_CALLBACK_BASE not set — callback server disabled")
        return None
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/platega/callback", platega_callback_handler)
    app.router.add_get("/platega/callback", platega_callback_handler)  # health
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PLATEGA_CALLBACK_PORT)
    await site.start()
    log.info(
        "Platega callback server on :%s  → %s/platega/callback",
        PLATEGA_CALLBACK_PORT,
        PLATEGA_CALLBACK_BASE,
    )
    return runner


async def setup_bot_commands(bot: Bot):
    public_commands = [
        BotCommand(command="start", description="Главное меню / Main menu"),
        BotCommand(command="catalog", description="Магазин / Shop"),
        BotCommand(command="profile", description="Профиль / Profile"),
        BotCommand(command="support", description="Поддержка / Support"),
    ]
    await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
    if ADMIN_ID:
        await bot.set_my_commands(public_commands + [
            BotCommand(command="admin", description="Админ-панель"),
            BotCommand(command="addpromo", description="Создать промокод"),
            BotCommand(command="reply", description="Ответить пользователю"),
            BotCommand(command="syncemojis", description="Обновить анимированные эмодзи"),
        ], scope=BotCommandScopeChat(chat_id=ADMIN_ID))


async def main():
    require_config()
    init_db()
    seed_if_empty()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    emoji_middleware = PackEmojiMiddleware()
    bot.session.middleware(emoji_middleware)
    try:
        await emoji_middleware.refresh(bot)
    except Exception as exc:
        log.warning("Emoji set loading failed (%s); using cached icons or standard emoji", type(exc).__name__)
    try:
        await setup_bot_commands(bot)
    except Exception:
        log.exception("commands setup failed")
    runner = await start_callback_server(bot)
    log.info("PLS Store starting…")
    try:
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()
        if platega:
            await platega.aclose()


if __name__ == "__main__":
    asyncio.run(main())
