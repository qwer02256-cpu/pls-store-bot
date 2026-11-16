"""FunPay Cardinal: five-star review replies with the purchased lot title."""
from __future__ import annotations

from contextlib import contextmanager
import html
import logging
from pathlib import Path
import re
import sqlite3
import string
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = 'AutoReviewReply'
VERSION = '1.0.0'
DESCRIPTION = 'Ответы на отзывы с 5 звёздами с названием купленного лота.'
CREDITS = '@uservkru'
UUID = 'd182fc9c-3d40-4a83-b6b4-61b61bc17452'
SETTINGS_PAGE = True
DEFAULT_TEMPLATE = 'Спасибо за заказ и отзыв! 💜\n\n🛍 {lot_title}'
DB_PATH = Path('storage/plugins/AutoReviewReply/state.db')
LOG = logging.getLogger('AutoReviewReply')
_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD = None
_LOCK = threading.RLock()
_WAITING = set()
_PREFIX = 'arr:'
_MAX_ATTEMPTS = 5
_MAX_LENGTH = 1000


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _init_db():
    with _db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
            seller INTEGER NOT NULL, order_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(seller, order_id))''')
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('enabled', '1')")
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('template', ?)", (DEFAULT_TEMPLATE,))


def _setting(key):
    with _db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else ('1' if key == 'enabled' else DEFAULT_TEMPLATE)


def _set_setting(key, value):
    with _db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))


def _validate_template(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 700:
        raise ValueError('Введите текст от 1 до 700 символов.')
    found = False
    try:
        for _, field, spec, conversion in string.Formatter().parse(text):
            if field is not None:
                if field != 'lot_title' or spec or conversion:
                    raise ValueError('Доступна только переменная {lot_title}, без дополнительных настроек.')
                found = True
    except ValueError as exc:
        raise ValueError('Используйте {lot_title} для названия лота. Проверьте фигурные скобки.') from exc
    if not found:
        raise ValueError('Добавьте {lot_title}, чтобы ответ содержал название купленного лота.')
    return text.strip()


def _reply_text(template, title):
    template = _validate_template(template)
    title = re.sub(r'\s+', ' ', str(title or '')).strip()
    if not title:
        raise ValueError('Название лота отсутствует')
    # Shorten only the title so the thanks and the user's template remain intact.
    copies = sum(field == 'lot_title' for _, field, _, _ in string.Formatter().parse(template))
    budget = (_MAX_LENGTH - len(template.format(lot_title=''))) // copies
    if budget < 2:
        raise ValueError('Слишком длинный шаблон')
    if len(title) > budget:
        title = title[:budget - 1].rstrip() + '…'
    return template.format(lot_title=title)


def _seller(cardinal):
    return int(cardinal.account.id)


def _enqueue(seller, order_id):
    with _db() as conn:
        conn.execute("""INSERT INTO jobs(seller, order_id) VALUES (?,?)
                     ON CONFLICT(seller, order_id) DO UPDATE SET state='pending', attempts=0, next_at=0
                     WHERE jobs.state='not_five_stars'""", (seller, order_id))
    _WAKE.set()


def new_message_handler(cardinal: Cardinal, event):
    message = getattr(event, 'message', None)
    # FunPay system events are authored by id 0. Buyer text must never trigger replies.
    if message is None or getattr(message, 'author_id', None) != 0:
        return
    kind = getattr(getattr(message, 'type', None), 'name', '')
    if kind not in ('NEW_FEEDBACK', 'FEEDBACK_CHANGED'):
        return
    match = re.search(r'#([A-Z0-9]{8})(?![A-Z0-9])', message.text or '')
    if not match:
        return
    try:
        if _setting('enabled') == '1':
            _enqueue(_seller(cardinal), match.group(1))
    except Exception:
        LOG.warning('Не удалось поставить отзыв в очередь; проверьте права записи AutoReviewReply.')


def _finish(seller, order_id, state):
    with _db() as conn:
        conn.execute('UPDATE jobs SET state=? WHERE seller=? AND order_id=?', (state, seller, order_id))


def _process_one(cardinal):
    with _LOCK:
        if _STOP.is_set() or _setting('enabled') != '1':
            return False
        seller = _seller(cardinal)
        with _db() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE seller=? AND state='pending' AND next_at<=? ORDER BY next_at, rowid LIMIT 1",
                               (seller, time.time())).fetchone()
        if job is None:
            return False
        order_id = job['order_id']
        try:
            order = cardinal.account.get_order(order_id)
            if str(getattr(order, 'seller_id', '')) != str(seller):
                _finish(seller, order_id, 'not_our_sale')
                return True
            review = getattr(order, 'review', None)
            if review is not None and getattr(review, 'reply', None):
                _finish(seller, order_id, 'answered')
                return True
            if review is None:
                raise ValueError('Отзыв пока недоступен')
            if getattr(review, 'stars', None) != 5:
                # A later FEEDBACK_CHANGED can retry after the buyer changes to 5 stars.
                _finish(seller, order_id, 'not_five_stars')
                return True
            title = getattr(order, 'short_description', None) or getattr(order, 'title', None)
            text = _reply_text(_setting('template'), title)
            if _STOP.is_set() or _setting('enabled') != '1':
                return False
            cardinal.account.send_review(order_id, text)
            confirmed = cardinal.account.get_order(order_id)
            if not getattr(getattr(confirmed, 'review', None), 'reply', None):
                raise ValueError('FunPay ещё не подтвердил ответ')
            _finish(seller, order_id, 'sent')
            LOG.info('Ответ на отзыв к заказу #%s отправлен.', order_id)
        except Exception:
            attempts = job['attempts'] + 1
            with _db() as conn:
                conn.execute('UPDATE jobs SET attempts=?, next_at=?, state=? WHERE seller=? AND order_id=?',
                             (attempts, time.time() + min(900, 60 * 2 ** (attempts - 1)),
                              'failed' if attempts >= _MAX_ATTEMPTS else 'pending', seller, order_id))
            # Never print raw HTTP exceptions, which can contain authentication data.
            LOG.warning('Не удалось ответить на отзыв #%s, попытка %s/%s. Проверьте соединение и доступ к заказу.',
                        order_id, attempts, _MAX_ATTEMPTS)
        return True


def _worker(cardinal):
    while not _STOP.is_set():
        _WAKE.clear()
        try:
            processed = _process_one(cardinal)
        except Exception:
            LOG.warning('AutoReviewReply: очередь недоступна; проверьте права записи и вход в FunPay.')
            processed = False
        # Pace API requests even if several reviews arrived at once.
        if processed:
            _STOP.wait(3)
        else:
            _WAKE.wait(5)


def _panel(cardinal, chat_id, notice=''):
    _WAITING.discard(chat_id)
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    enabled = _setting('enabled') == '1'
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Выключить' if enabled else 'Включить', callback_data=_PREFIX + 'toggle'))
    kb.add(InlineKeyboardButton('Изменить текст ответа', callback_data=_PREFIX + 'template'))
    kb.add(InlineKeyboardButton('Вернуть стандартный текст', callback_data=_PREFIX + 'reset'))
    template = html.escape(_setting('template'))
    cardinal.telegram.bot.send_message(chat_id,
        (html.escape(notice) + '\n\n' if notice else '') +
        '<b>Ответы на отзывы</b>\n\n' + ('Включены ✅' if enabled else 'Выключены') +
        '\nТолько отзывы с 5 звёздами. Уже существующие ответы не заменяются.\n\n'
        '<b>Ваш текст:</b>\n<pre>' + template + '</pre>\n\n'
        '{lot_title} заменяется названием купленного лота из заказа. '
        'Ответ появится под отзывом на FunPay, а не в чате покупателя.\n\n'
        'Пример:\n' + html.escape(_reply_text(_setting('template'), '100 Telegram Stars')),
        parse_mode='HTML', reply_markup=kb)


def _callback(cardinal, call):
    bot = cardinal.telegram.bot
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    action = call.data[len(_PREFIX):]
    _WAITING.discard(chat_id)
    if action == 'cancel':
        _panel(cardinal, chat_id, 'Ввод отменён.')
        return
    if action == 'toggle':
        with _LOCK:
            _set_setting('enabled', '0' if _setting('enabled') == '1' else '1')
        _WAKE.set()
    elif action == 'reset':
        with _LOCK:
            _set_setting('template', DEFAULT_TEMPLATE)
    elif action == 'template':
        _WAITING.add(chat_id)
        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
        cancel = InlineKeyboardMarkup()
        cancel.add(InlineKeyboardButton('Отменить ввод', callback_data=_PREFIX + 'cancel'))
        bot.send_message(chat_id, 'Отправьте новый текст ответа. Вместо названия лота вставьте {lot_title}.\n\n'
                         'Пример:\nСпасибо за заказ! 💜\n🛍 {lot_title}\n\n/cancel — отменить ввод.', reply_markup=cancel)
        return
    _panel(cardinal, chat_id)


def _receive_template(cardinal, message):
    chat_id = message.chat.id
    text = message.text or ''
    if text.strip().lower() == '/cancel':
        _WAITING.discard(chat_id)
        _panel(cardinal, chat_id, 'Ввод отменён.')
        return
    try:
        text = _validate_template(text)
    except ValueError as exc:
        cardinal.telegram.bot.send_message(chat_id, str(exc))
        return
    with _LOCK:
        _set_setting('template', text)
    _WAITING.discard(chat_id)
    _panel(cardinal, chat_id, 'Текст сохранён. Он будет использоваться для новых ответов.')


def init_cardinal(cardinal: Cardinal):
    global _THREAD
    _init_db()
    tg = cardinal.telegram
    cardinal.add_telegram_commands(UUID, [('reviewreply', 'Ответы на отзывы с названием лота', True)])
    tg.msg_handler(lambda m: _panel(cardinal, m.chat.id), commands=['reviewreply'])
    tg.cbq_handler(lambda q: _callback(cardinal, q), func=lambda q: q.data.startswith(_PREFIX))
    # Cardinal's standard plugin settings callback.
    from tg_bot import CBT
    tg.cbq_handler(lambda q: _panel(cardinal, q.message.chat.id),
                   func=lambda q: q.data.startswith(f'{CBT.PLUGIN_SETTINGS}:{UUID}'))
    tg.msg_handler(lambda m: _receive_template(cardinal, m),
                   func=lambda m: m.chat.id in _WAITING and bool(m.text))
    if _THREAD is None or not _THREAD.is_alive():
        _STOP.clear()
        _THREAD = threading.Thread(target=_worker, args=(cardinal,), daemon=True, name='review-replies')
        _THREAD.start()
    LOG.info('AutoReviewReply %s: ответы только на 5 звёзд; настройки /reviewreply.', VERSION)


def stop_plugin(*args):
    _STOP.set()
    _WAKE.set()


BIND_TO_PRE_INIT = [init_cardinal]
BIND_TO_NEW_MESSAGE = [new_message_handler]
BIND_TO_DELETE = [stop_plugin]
