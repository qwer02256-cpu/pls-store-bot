"""FunPay Cardinal: one confirmation reminder five hours after payment."""
from __future__ import annotations

from contextlib import contextmanager
import html
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time

NAME = 'ConfirmOrderReminder'
VERSION = '1.0.0'
DESCRIPTION = 'Одно напоминание подтвердить заказ через 5 часов после оплаты.'
CREDITS = '@uservkru'
UUID = 'e9d99427-78f0-4052-a731-52a116d119b2'
SETTINGS_PAGE = True
DELAY = 5 * 60 * 60
DB_PATH = Path('storage/plugins/ConfirmOrderReminder/state.db')
LOG = logging.getLogger(NAME)
_STOP = threading.Event()
_WAKE = threading.Event()
_LOCK = threading.RLock()
_THREAD = None
PREFIX = 'cfr:'
TEXT = ('Здравствуйте! 👋 Если вы уже получили заказ и у вас всё отлично работает, '
        'пожалуйста, подтвердите выполнение заказа, чтобы средства зачислились на мой баланс.\n\n'
        'Ссылка на заказ: {order_url}\n\nЗаранее спасибо! 💜')


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
        conn.executescript('''
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS reminders(seller INTEGER,order_id TEXT,paid_seen REAL,
            due REAL,state TEXT NOT NULL,attempts INTEGER DEFAULT 0,next_at REAL DEFAULT 0,
            PRIMARY KEY(seller,order_id));
        ''')
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('enabled','1')")
        # A crash may happen after FunPay accepted the message: never resend automatically.
        conn.execute("UPDATE reminders SET state='uncertain' WHERE state='sending'")


def _enabled():
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
    return row is not None and row['value'] == '1'


def _order_id(value):
    value = str(value or '').lstrip('#').upper()
    if not re.fullmatch('[A-Z0-9]{8}', value):
        raise ValueError('Некорректный номер заказа')
    return value


def _status(order):
    value = getattr(order, 'status', None)
    return str(getattr(value, 'name', value) or '').split('.')[-1].upper()


def _set_state(seller, order_id, state):
    with _db() as conn:
        conn.execute('UPDATE reminders SET state=? WHERE seller=? AND order_id=?', (state, seller, order_id))


def new_order_handler(cardinal, event):
    try:
        if _status(event.order) != 'PAID':
            return
        oid = _order_id(event.order.id)
        seller = int(cardinal.account.id)
        with _LOCK:
            if not _enabled():
                return
            now = time.time()
            with _db() as conn:
                # First observed payment wins: duplicate events cannot postpone the reminder.
                conn.execute('INSERT OR IGNORE INTO reminders(seller,order_id,paid_seen,due,state) VALUES (?,?,?,?,?)',
                             (seller, oid, now, now + DELAY, 'pending'))
        _WAKE.set()
    except Exception:
        LOG.warning('Не удалось запланировать напоминание; проверьте права записи ConfirmOrderReminder.')


def order_status_handler(cardinal, event):
    try:
        status = _status(event.order)
        if status in ('CLOSED', 'REFUNDED'):
            with _LOCK:
                _set_state(int(cardinal.account.id), _order_id(event.order.id), status.lower())
    except Exception:
        LOG.warning('Не удалось обновить статус напоминания; перед отправкой статус будет проверен повторно.')


def _process_one(cardinal):
    seller = int(cardinal.account.id)
    with _LOCK:
        if _STOP.is_set() or not _enabled():
            return False
        with _db() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE seller=? AND state='pending' AND due<=? AND next_at<=? "
                               'ORDER BY due LIMIT 1', (seller, time.time(), time.time())).fetchone()
        if row is None:
            return False
        oid = row['order_id']
        try:
            order = cardinal.account.get_order(oid)
            if str(getattr(order, 'seller_id', '')) != str(seller):
                _set_state(seller, oid, 'not_our_sale')
                return True
            status = _status(order)
            if status != 'PAID':
                _set_state(seller, oid, status.lower() if status in ('CLOSED','REFUNDED') else 'unknown_status')
                return True
            buyer_name = str(order.buyer_username)
            chat = cardinal.account.get_chat_by_name(buyer_name, make_request=True)
            if (chat is None or str(chat.name).casefold() != buyer_name.casefold()
                    or not cardinal.account.chat_id_private(chat.id)):
                raise ValueError('Не удалось определить чат покупателя')
        except Exception:
            attempts = row['attempts'] + 1
            with _db() as conn:
                conn.execute('UPDATE reminders SET attempts=?,next_at=?,state=? WHERE seller=? AND order_id=?',
                    (attempts, time.time() + 300, 'failed' if attempts >= 6 else 'pending', seller, oid))
            LOG.warning('Не удалось проверить заказ #%s для напоминания, попытка %s/6.', oid, attempts)
            return True
        if _STOP.is_set() or not _enabled():
            return False
        _set_state(seller, oid, 'sending')
        try:
            # Direct API method raises on failure. Cardinal.send_message can silently return None and retry.
            result = cardinal.account.send_message(chat.id, TEXT.format(order_url=f'https://funpay.com/orders/{oid}/'))
            if result is None:
                raise ValueError('FunPay не подтвердил отправку')
        except Exception:
            _set_state(seller, oid, 'uncertain')
            LOG.warning('Отправка напоминания #%s не подтверждена. Проверьте чат; автоповтора не будет.', oid)
            return True
        _set_state(seller, oid, 'sent')
        LOG.info('Напоминание подтвердить заказ #%s отправлено.', oid)
        return True


def _worker(cardinal):
    while not _STOP.is_set():
        _WAKE.clear()
        try:
            processed = _process_one(cardinal)
        except Exception:
            processed = False
            LOG.warning('Не удалось обработать очередь напоминаний.')
        if processed:
            _STOP.wait(3)
        else:
            _WAKE.wait(30)


def _panel(cardinal, chat_id):
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton as Button
    seller = int(cardinal.account.id)
    with _db() as conn:
        rows = conn.execute("SELECT order_id,due FROM reminders WHERE seller=? AND state='pending' ORDER BY due LIMIT 8", (seller,)).fetchall()
        counts = conn.execute('SELECT state,count(*) AS n FROM reminders WHERE seller=? GROUP BY state', (seller,)).fetchall()
    labels = {'pending':'Ожидают','sent':'Отправлены','uncertain':'Проверить отправку вручную',
              'failed':'Ошибки проверки','cancelled':'Отменены','closed':'Подтверждены','refunded':'Возвраты'}
    text = '<b>Напомнить подтвердить заказ</b>\n\n' + ('Включено ✅' if _enabled() else 'Выключено')
    text += '\nОдно сообщение через 5 часов после получения события оплаты, для любых товаров.\n'
    text += '\n'.join(labels.get(row['state'], row['state']) + ': ' + str(row['n']) for row in counts)
    kb = InlineKeyboardMarkup()
    kb.add(Button('Выключить и отменить ожидающие' if _enabled() else 'Включить для новых оплат', callback_data=PREFIX+'toggle'))
    for row in rows:
        minutes = max(0, int((row['due']-time.time())/60))
        kb.add(Button(f'Отменить #{row["order_id"]} · через {minutes//60} ч {minutes%60} мин', callback_data=PREFIX+'cancel:'+row['order_id']))
    kb.add(Button('Обновить список', callback_data=PREFIX+'refresh'))
    text += '\n\nПеред отправкой проверяется статус FunPay. Подтверждённые и возвращённые заказы пропускаются.'
    text += '\n\n<b>Текст покупателю:</b>\n' + html.escape(TEXT.format(order_url='https://funpay.com/orders/НОМЕР_ЗАКАЗА/'))
    cardinal.telegram.bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=kb)


def _callback(cardinal, call):
    cardinal.telegram.bot.answer_callback_query(call.id)
    action = call.data[len(PREFIX):]
    with _LOCK:
        if action == 'toggle':
            enabled = not _enabled()
            with _db() as conn:
                conn.execute("UPDATE settings SET value=? WHERE key='enabled'", ('1' if enabled else '0',))
                if not enabled:
                    conn.execute("UPDATE reminders SET state='cancelled' WHERE state='pending' AND seller=?", (int(cardinal.account.id),))
        elif action.startswith('cancel:'):
            _set_state(int(cardinal.account.id), _order_id(action.split(':',1)[1]), 'cancelled')
    _WAKE.set()
    _panel(cardinal, call.message.chat.id)


def init_cardinal(cardinal):
    global _THREAD
    _init_db()
    cardinal.add_telegram_commands(UUID, [('confirmreminder','Напомнить покупателю подтвердить заказ',True)])
    tg = cardinal.telegram
    tg.msg_handler(lambda message: _panel(cardinal,message.chat.id), commands=['confirmreminder'])
    tg.cbq_handler(lambda call: _callback(cardinal,call), func=lambda call: call.data.startswith(PREFIX))
    from tg_bot import CBT
    settings = getattr(CBT,'PLUGIN_SETTINGS','43')
    tg.cbq_handler(lambda call: _panel(cardinal,call.message.chat.id), func=lambda call: call.data.startswith(f'{settings}:{UUID}'))
    if _THREAD is None or not _THREAD.is_alive():
        _STOP.clear()
        _THREAD = threading.Thread(target=_worker,args=(cardinal,),daemon=True,name='confirm-order-reminders')
        _THREAD.start()


def stop_plugin(*args):
    _STOP.set()
    _WAKE.set()


BIND_TO_PRE_INIT = [init_cardinal]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_ORDER_STATUS_CHANGED = [order_status_handler]
BIND_TO_DELETE = [stop_plugin]
