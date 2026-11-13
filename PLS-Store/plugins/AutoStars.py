# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import TYPE_CHECKING, Optional, List, Dict, Any
import os
import json
import logging
import requests
import re as _re
import html as _html
import shutil
import datetime

from telebot.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
from telebot.apihelper import ApiTelegramException

try:
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
except Exception:
    from FunPayAPI.updater.events import NewMessageEvent
    NewOrderEvent = None

try:
    import FunPayAPI
except Exception:
    FunPayAPI = None

import tg_bot.CBT as CBT

if TYPE_CHECKING:
    from cardinal import Cardinal


# PLS_LICENSE_GUARD_V1: only key, FunPay account id and product leave this client.
import threading as _pls_threading
import time as _pls_time
from pathlib import Path as _PlsPath

_PLS_ENDPOINT = "__PLS_LICENSE_ENDPOINT__"
_PLS_LOCK = _pls_threading.Lock()
_PLS_CHECKED = 0.0
_PLS_ACCOUNT = None

def _pls_require_license(cardinal):
    global _PLS_CHECKED, _PLS_ACCOUNT
    fid = str(getattr(cardinal.account, "id", "") or "")
    if not fid.isascii() or not fid.isdigit() or int(fid) <= 0:
        raise RuntimeError("AutoStars: FunPay ID ещё недоступен. Дождитесь входа Cardinal в FunPay.")
    with _PLS_LOCK:
        if fid == _PLS_ACCOUNT and _pls_time.monotonic() - _PLS_CHECKED < 60:
            return
        try:
            settings = json.loads(_PlsPath("storage/plugins/AutoStars/license.json").read_text(encoding="utf-8"))
            key = settings["key"]
            if not isinstance(key, str) or not key.startswith("PLS-"):
                raise ValueError()
            if not _PLS_ENDPOINT.startswith("https://"):
                raise ValueError()
            # No plugin config, Fragment credentials, mnemonic or wallet information.
            response = requests.post(_PLS_ENDPOINT,
                json={"key": key, "funpay_id": fid, "product": "autostars"},
                timeout=(5, 10), allow_redirects=False)
            if response.status_code != 200 or response.json().get("valid") is not True:
                raise ValueError()
        except Exception:
            _PLS_CHECKED = 0.0
            _PLS_ACCOUNT = None
            raise RuntimeError("AutoStars: лицензия не подтверждена. Проверьте ключ, доступ к PLS Store и привязку FunPay. Продажа остановлена.") from None
        _PLS_ACCOUNT = fid
        _PLS_CHECKED = _pls_time.monotonic()

# ========================================================================
# 1. НАСТРОЙКИ ЛОГИРОВАНИЯ
# ========================================================================
logger = logging.getLogger("AutoStars-Plugin")
LOG_TAG = "[AutoStars]"

class _AnsiColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:   "\033[90m",
        logging.INFO:    "\033[36m",
        logging.WARNING: "\033[33m",
        logging.ERROR:   "\033[31m",
        logging.CRITICAL:"\033[41m",
    }
    RESET = "\033[0m"

    def format(self, record):
        base = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        reset = self.RESET if color else ""
        return f"{color}{base}{reset}"

if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(_AnsiColorFormatter("%(asctime)s %(levelname)s " + LOG_TAG + " %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

def _log(level: str, msg: str):
    if level == "info":
        logger.info(f"{msg}")
    elif level == "warn":
        logger.warning(f"{msg}")
    elif level == "error":
        logger.error(f"{msg}")
    else:
        logger.debug(f"{msg}")


# ========================================================================
# 2. МЕТАДАННЫЕ ПЛАГИНА И КОНСТАНТЫ
# ========================================================================
NAME        = "AutoStars"
VERSION     = "2.1"
DESCRIPTION = "Плагин по продаже звезд."
CREDITS     = "@uservkru"
UUID        = "fa0c2f3a-7a85-4c09-a3b2-9f3a9b8f8a75"
# True  -> на стандартной карточке плагина появится кнопка «⚙️ Настройки» (открывает панель)
# False -> карточка без неё, панель открывается только командой /autostars
SETTINGS_PAGE = True

# Константы для API фрагмента
FRAGMENT_BASE          = os.getenv("FRAGMENT_BASE", "https://api.fragment-api.com/v1")
FRAGMENT_AUTH_URL      = os.getenv("FRAGMENT_AUTH_URL", f"{FRAGMENT_BASE}/auth/authenticate/")
FRAGMENT_WALLET_URL    = os.getenv("FRAGMENT_WALLET_URL", f"{FRAGMENT_BASE}/misc/wallet/")
FRAGMENT_USER_URL      = os.getenv("AUTOSTARS_FRAGMENT_USER_URL", f"{FRAGMENT_BASE}/misc/user/")
FRAGMENT_ORDER_STARS   = os.getenv("FRAGMENT_ORDER_STARS", f"{FRAGMENT_BASE}/order/stars/")

# Настройки FunPay
STARS_CATEGORY_ID = int(os.getenv("AUTOSTARS_CATEGORY_ID", "2418"))
MIN_BALANCE_TON   = float(os.getenv("AUTOSTARS_MIN_BALANCE_TON", "5.0"))

PLUGIN_FOLDER  = "storage/plugins/AutoStars"
SETTINGS_FILE  = os.path.join(PLUGIN_FOLDER, "settings.json")
_OLD_SETTINGS_FILE = os.path.join("storage/plugins/AutoStars-Lucky", "settings.json")
os.makedirs(PLUGIN_FOLDER, exist_ok=True)
if not os.path.exists(SETTINGS_FILE):
    # переносим настройки (токен, лоты и т.д.) из старой папки плагина, если она есть
    if os.path.exists(_OLD_SETTINGS_FILE):
        try:
            shutil.copy(_OLD_SETTINGS_FILE, SETTINGS_FILE)
        except Exception:
            pass
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=4, ensure_ascii=False)


# ========================================================================
# 3. РАБОТА С КОНФИГУРАЦИЕЙ И ШАБЛОНАМИ
# ========================================================================
def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Load settings error: {e}")
        return {}

def _save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Save settings error: {e}")

def _default_templates() -> dict:
    return {
        "purchase_created": "Спасибо за покупку {qty}⭐!\nНапишите ваш Telegram-тег одной строкой в формате @username.\nПример: @username",
        "username_received": "Принял тег: @{username}. Проверяю…",
        "username_invalid": "❌ Некорректный или несуществующий тег.\nОтправьте верный Telegram-тег в формате @username (5–32, латиница/цифры/подчёркивание).\nПример: @username",
        "username_valid": "✅ Тег принят: @{username}. Количество: {qty}⭐.",
        "sending": "Отправляю {qty}⭐ на @{username}…",
        "sent": "✅ Готово: отправлено {qty}⭐ на @{username}. {order_url}",
        "failed": "❌ Не удалось отправить звёзды: {reason}",
    }

def _fmt_tpl(tpl: str, **kw) -> str:
    try:
        return tpl.format(**kw)
    except Exception:
        return tpl

def _tpl(chat_id: Any, key: str, **kw) -> str:
    cfg_owner = _get_cfg_for_orders(chat_id)
    tpls = (cfg_owner.get("templates") if isinstance(cfg_owner, dict) else None) or {}
    if not tpls:
        cfg_local = _get_cfg(chat_id)
        tpls = (cfg_local.get("templates") if isinstance(cfg_local, dict) else None) or {}
    default = _default_templates().get(key, "")
    raw = tpls.get(key, default)
    return _fmt_tpl(raw, **kw)

def _get_cfg(chat_id: Any, persist: bool = True) -> dict:
    data = _load_settings()
    key = str(chat_id)
    cfg = data.get(key) or {}
    _before = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)
    cfg.setdefault("plugin_enabled", True)
    cfg.setdefault("lots_active", False)
    cfg.setdefault("auto_refund", False)
    cfg.setdefault("auto_deactivate", True)
    cfg.setdefault("manual_refund_enabled", False)
    cfg.setdefault("manual_refund_priority", True)
    cfg.setdefault("fragment_jwt", None)
    cfg.setdefault("wallet_version", None)
    cfg.setdefault("balance_ton", None)
    cfg.setdefault("last_wallet_raw", None)
    cfg.setdefault("templates", _default_templates())
    cfg.setdefault("category_id", STARS_CATEGORY_ID)
    cfg["category_id"] = STARS_CATEGORY_ID

    cfg.setdefault("min_balance_ton", MIN_BALANCE_TON)
    cfg.setdefault("notify_threshold_ton", 10.0)
    cfg.setdefault("last_notify_bal", 0.0)

    cfg.setdefault("star_lots", [])

    cfg.setdefault("ton_buy_price", 125.0)
    cfg.setdefault("fragment_rate", 0.115)
    cfg.setdefault("stats", {})

    cfg.setdefault("presale_enabled", False)
    cfg.setdefault("presale_text", "👋 Привет! Да, я тут. Выдача звёзд работает в автоматическом режиме 24/7. Можете оформлять заказ!")

    # --- ПРОКСИ ---
    cfg.setdefault("proxy_mode", "none") # "none", "cardinal", "custom"
    cfg.setdefault("custom_proxy", "")

    if persist and json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str) != _before:
        data[key] = cfg
        _save_settings(data)
    return cfg

def _owner_key(chat_id: Any) -> Optional[str]:
    """Ключ настроек продавца (у кого привязан токен). chat_id покупателя -> настройки админа."""
    data = _load_settings()
    own = data.get(str(chat_id))
    if isinstance(own, dict) and own.get("fragment_jwt"):
        return str(chat_id)
    for k, v in data.items():
        if isinstance(v, dict) and v.get("fragment_jwt"):
            return str(k)
    return None

def _get_cfg_for_orders(chat_id: Any) -> dict:
    owner = _owner_key(chat_id)
    if owner is not None:
        return _get_cfg(owner)
    # токена нет ни у кого — отдаём настройки по умолчанию, не создавая записей на каждого покупателя
    return _get_cfg(chat_id, persist=False)

def _set_cfg(chat_id: Any, **updates) -> dict:
    data = _load_settings()
    key = str(chat_id)
    cfg = data.get(key) or {}
    cfg.update(updates)
    data[key] = cfg
    _save_settings(data)
    return cfg

def _record_sale(cardinal: "Cardinal", chat_id: Any, qty: int, order_id: Optional[str] = None):
    chat_id = _owner_key(chat_id) or chat_id
    cfg = _get_cfg(chat_id)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    stats = cfg.get("stats", {})
    day_stat = stats.get(today, {"stars": 0, "cost_rub": 0.0, "revenue_rub": 0.0})

    ton_buy_price = float(cfg.get("ton_buy_price", 125.0))
    fragment_rate = float(cfg.get("fragment_rate", 0.115))

    cost_in_ton = (qty / 50.0) * fragment_rate
    cost_in_rub = cost_in_ton * ton_buy_price
    revenue = 0.0

    if order_id:
        try:
            order_obj = cardinal.account.get_order(order_id)
            if order_obj:
                revenue = float(order_obj.price)
        except Exception as e:
            logger.warning(f"Не удалось получить цену заказа {order_id} для статистики: {e}")

    day_stat["stars"] += qty
    day_stat["cost_rub"] += cost_in_rub
    day_stat["revenue_rub"] += revenue
    stats[today] = day_stat
    _set_cfg(chat_id, stats=stats)

# ========================================================================
# 4. ФУНКЦИИ ВЗАИМОДЕЙСТВИЯ С API ФРАГМЕНТА
# ========================================================================
def _get_proxies_for_chat(chat_id: Any) -> Optional[dict]:
    if not chat_id: return None
    cfg = _get_cfg_for_orders(chat_id)
    mode = cfg.get("proxy_mode", "none")
    if mode == "custom":
        p = cfg.get("custom_proxy")
        if p: return {"http": p, "https": p}
    elif mode == "cardinal":
        cref = globals().get("_CARDINAL_REF")
        if cref:
            p = getattr(cref, "proxy", None)
            if not p and hasattr(cref, "account"):
                p = getattr(cref.account, "proxy", None)
            if isinstance(p, dict) and p: return p
            if isinstance(p, str) and p: return {"http": p, "https": p}
    return None

def _check_username_exists(username: str, jwt: Optional[str], chat_id: Any = None) -> bool:
    if not username:
        return False
    uname = username.lstrip("@").strip()
    urls = [f"{(FRAGMENT_USER_URL or '').rstrip('/')}/{uname}/"]
    headers_with_jwt = {"Accept": "application/json"}
    if jwt:
        headers_with_jwt["Authorization"] = f"JWT {jwt}"

    proxies = _get_proxies_for_chat(chat_id)

    for url in urls:
        try:
            r = requests.get(url, headers=headers_with_jwt, timeout=8, proxies=proxies)
            if r.status_code == 200:
                try:
                    data = r.json()
                    if isinstance(data, dict) and (data.get("username") or data.get("user") or data.get("id")):
                        return True
                except Exception:
                    pass
            r2 = requests.get(url, headers={"Accept": "application/json"}, timeout=8, proxies=proxies)
            if r2.status_code == 200:
                try:
                    data = r2.json()
                    if isinstance(data, dict) and (data.get("username") or data.get("user") or data.get("id")):
                        return True
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"_check_username_exists {url} failed: {e}")
    return False

def _extract_wallet_info(data: dict) -> tuple[Optional[str], Optional[float]]:
    if not isinstance(data, dict):
        return None, None
    ver = None
    bal = None
    for key in ("wallet_version", "walletVersion", "version"):
        if key in data and isinstance(data[key], (str, int, float)):
            ver = str(data[key]); break
    for key in ("balance_ton", "balanceTon", "balance", "ton_balance"):
        if key in data:
            try:
                bal = float(data[key]); break
            except Exception:
                pass
    if bal is None:
        for outer in ("wallet", "ton"):
            node = data.get(outer)
            if isinstance(node, dict) and "balance" in node:
                try:
                    bal = float(node["balance"]); break
                except Exception:
                    pass
    if bal is None:
        for key in ("nanoton", "nanoTon", "nanotons", "balance_nano", "balanceNano"):
            if key in data:
                try:
                    v = float(data[key])
                    bal = v / 1e9 if v > 1e6 else v
                    break
                except Exception:
                    pass
    return ver, bal

def _check_fragment_wallet(jwt: str, chat_id: Any = None) -> tuple[Optional[str], Optional[float], Optional[dict]]:
    proxies = _get_proxies_for_chat(chat_id)
    try:
        r = requests.get(
            FRAGMENT_WALLET_URL,
            headers={"Accept": "application/json", "Authorization": f"JWT {jwt}"},
            timeout=20,
            proxies=proxies
        ); r.raise_for_status()
        data = r.json()
        ver, bal = _extract_wallet_info(data if isinstance(data, dict) else {})
        return ver, bal, data if isinstance(data, dict) else {"raw": data}
    except Exception as e:
        logger.warning(f"Fragment wallet check failed: {e}")
        return None, None, None

def _order_stars(jwt: str, username: str, quantity: int, show_sender: bool = False, webhook_url: Optional[str] = None, chat_id: Any = None) -> dict:
    _pls_require_license(_CARDINAL_REF)
    proxies = _get_proxies_for_chat(chat_id)
    try:
        u = username.lstrip("@").strip()
        payload = {"username": u, "quantity": quantity, "show_sender": bool(show_sender)}
        if webhook_url:
            payload["webhook_url"] = webhook_url

        _log("info", f"SEND start: {quantity}⭐ → @{u}")
        r = requests.post(
            FRAGMENT_ORDER_STARS,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"JWT {jwt}"
            },
            timeout=120,
            proxies=proxies
        )

        resp_json = None
        ct = (r.headers.get("Content-Type") or "").lower()
        if "application/json" in ct:
            try:
                resp_json = r.json()
            except Exception:
                resp_json = None

        ok_flags = set()
        if isinstance(resp_json, dict):
            for k in ("ok", "success", "sent", "purchased", "done"):
                v = resp_json.get(k)
                if isinstance(v, bool) and v:
                    ok_flags.add(k)
            status_val = str(resp_json.get("status", "")).lower()
            if status_val in {"ok", "success", "completed", "done"}:
                ok_flags.add("status")
            if any(k in resp_json for k in ("tx", "transaction", "order_id", "orderId")):
                ok_flags.add("tx")

        ok = bool(ok_flags)
        if resp_json is None:
            ok = False

        body_text = (r.text or "")[:400]
        _log("info" if ok else "error", f"SEND result: ok={ok} status={r.status_code} flags={','.join(sorted(ok_flags)) or '-'} body={body_text}")

        return {
            "ok": ok,
            "status": r.status_code,
            "text": r.text,
            "json": resp_json
        }
    except Exception as e:
        _log("error", f"SEND exception: {e}")
        return {"ok": False, "status": 0, "text": str(e), "json": None}

def _authenticate_fragment(api_key: str, phone_number: str, version: str, mnemonics: List[str], chat_id: Any = None) -> tuple[Optional[str], Optional[dict], int]:
    proxies = _get_proxies_for_chat(chat_id)
    try:
        payload = {"api_key": api_key, "phone_number": phone_number, "version": version, "mnemonics": mnemonics}
        r = requests.post(FRAGMENT_AUTH_URL, json=payload,
                          headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=40, proxies=proxies)
        try: data = r.json()
        except Exception: data = {"raw": r.text}
        if r.status_code >= 400:
            return None, data, r.status_code
        token_candidates = []
        if isinstance(data, dict):
            for key in ("token","jwt","access","authorization","Authorization","auth","detail"):
                val = data.get(key)
                if isinstance(val, str) and len(val) > 10:
                    token_candidates.append(val)
            for subkey in ("data","result","payload"):
                sub = data.get(subkey)
                if isinstance(sub, dict):
                    for key in ("token","jwt","access"):
                        val = sub.get(key)
                        if isinstance(val, str) and len(val) > 10:
                            token_candidates.append(val)
        jwt = token_candidates[0] if token_candidates else None
        return jwt, data, r.status_code
    except Exception as e:
        logger.warning(f"Authenticate failed: {e}")
        return None, {"error": str(e)}, 0

def _is_too_many_attempts(raw_resp: Any) -> tuple[bool, Optional[int]]:
    text = ""
    try:
        if isinstance(raw_resp, dict):
            parts = []
            for k in ("non_field_errors", "errors", "detail", "message", "error"):
                v = raw_resp.get(k)
                if isinstance(v, list):
                    parts.extend([str(x) for x in v])
                elif isinstance(v, (str, int, float)):
                    parts.append(str(v))
            text = " ".join(parts) if parts else json.dumps(raw_resp, ensure_ascii=False)
        else:
            text = str(raw_resp)
    except Exception:
        text = str(raw_resp)
    low = text.lower()
    if "too many login attempts" in low:
        m = _re.search(r"in\s+(\d+)\s+seconds", text, _re.I)
        sec = int(m.group(1)) if m else None
        return True, sec
    return False, None

# ========================================================================
# 5. ИНТЕРФЕЙС И КЛАВИАТУРЫ (TELEGRAM)
# ========================================================================
def _state_on(v: bool) -> str:
    return "🟢 Включено" if v else "🔴 Выключено"

def _safe_edit(bot, chat_id: Any, msg_id: int, text: str, kb=None):
    try:
        bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except ApiTelegramException as e:
        if "message is not modified" in str(e).lower():
            return
        raise

def _about_text() -> str:
    return (
        f"🧩 <b>Плагин:</b> {NAME}\n"
        f"📦 <b>Версия:</b> <code>{VERSION}</code>\n"
        f"👤 <b>Автор:</b> {CREDITS}\n\n"
        "Выберите раздел ниже."
    )

HELP_TEXT = """
<b>Инструкция и помощь</b>

<b>Что это?</b>
Панель управления продажей звёзд (Telegram Stars) для FunPay: токен Fragment, баланс, лоты, автопокупка и возвраты.

<b>Важно</b>
Категория FunPay для звёзд фиксирована: <code>2418</code>.

<b>Подключение токена (JWT)</b>
1) Откройте <i>Настройки → Токен → Создать токен</i>.
2) Введите <b>API-ключ</b> из <code>fragment-api.com/dashboard</code>.
3) Укажите <b>Телефон</b> (без «+», только цифры).
4) Выберите <b>Версию кошелька</b> (W5/V4R2).
5) Вставьте <b>24 слова</b> мнемофразы.
6) Подтвердите вход в официальном Telegram. После подтверждения токен выдастся автоматически.

<b>Сообщения (шаблоны)</b>
В шаблонах доступны плейсхолдеры:
• <code>{qty}</code> — количество звёзд;  • <code>{username}</code> — ник покупателя;
• <code>{order_id}</code> — номер заказа;   • <code>{order_url}</code> — ссылка на заказ;
• <code>{reason}</code> — краткая причина ошибки при отправке.

<b>Продажа звёзд</b>
1) Создайте лоты в категории <code>2418</code> (50/333/1000 и т.д.).
2) В разделе «⭐️ Звёзды (лоты)» добавьте пары <code>кол-во → LOT_ID</code> и включите нужные.
3) Включите «Лоты».
4) Покупатель присылает свой <b>@username</b>.
5) Плагин покупает через Fragment и присылает подтверждение (ссылка на заказ на FunPay).

<b>Возвраты</b>
• <b>Автовозврат</b> — при неудачной покупке пытается автоматически вернуть средства.
• <b>Команда !бэк</b> — ручной возврат по запросу покупателя.
"""

def _settings_text(chat_id: Any) -> str:
    cfg = _get_cfg(chat_id)
    prio = "выше автовозврата" if cfg.get("manual_refund_priority", True) else "ниже автовозврата"
    token_state = "привязан ✅" if cfg.get("fragment_jwt") else "не создан ❌"
    wallet_ver  = cfg.get("wallet_version") or "—"
    balance_ton = cfg.get("balance_ton")
    balance_txt = f"{balance_ton} TON" if balance_ton is not None else "—"
    lot_count   = len(cfg.get("star_lots") or [])
    reason = cfg.get("last_auto_deact_reason")
    state_txt, _ = _lots_state_summary(cfg)
    lots_line = f"• Лоты: <b>{state_txt}</b>"

    proxy_mode = cfg.get("proxy_mode", "none")
    if proxy_mode == "none": mode_str = "Выключен"
    elif proxy_mode == "cardinal": mode_str = "От бота (Cardinal)"
    else: mode_str = "Свой (Custom)"

    if (state_txt != "🟢 Включены") and reason:
        lots_line += f" <i>(авто-выкл: {reason})</i>"
    return (
        f"<b>Текущие настройки</b>\n\n"
        f"• Плагин: <b>{_state_on(cfg.get('plugin_enabled', True))}</b>\n"
        f"{lots_line}\n"
        f"• Автовозврат: <b>{_state_on(cfg.get('auto_refund', False))}</b>\n"
        f"• Автодеактивация: <b>{_state_on(cfg.get('auto_deactivate', True))}</b>\n"
        f"• Ручной возврат (!бэк): <b>{_state_on(cfg.get('manual_refund_enabled', False))}</b> (<i>{prio}</i>)\n"
        f"• Порог автодеактивации: <code>{cfg.get('min_balance_ton', MIN_BALANCE_TON)} TON</code>\n"
        f"• Порог уведомлений: <code>{cfg.get('notify_threshold_ton', 10.0)} TON</code>\n"
        f"• Прокси: <b>{mode_str}</b>\n"
        f"• Токен (JWT): <b>{token_state}</b>\n"
        f"• Баланс: <code>{balance_txt}</code>\n"
        f"• Категория (FunPay): <code>{STARS_CATEGORY_ID}</code>\n"
        f"• ⭐ Звёздных лотов: <b>{lot_count}</b>\n"
        "\nВыберите действие:"
    )

def _token_text(chat_id: Any) -> str:
    cfg = _get_cfg(chat_id)
    token_state = "Токен привязан ✅" if cfg.get("fragment_jwt") else "Пока не создан токен ❌"
    balance_ton = cfg.get("balance_ton")
    balance_txt = f"{balance_ton} TON" if balance_ton is not None else "—"
    return (
        f"<b>Токен (JWT)</b>\n\n"
        f"• Состояние: <b>{token_state}</b>\n"
        f"• Баланс: <code>{balance_txt}</code>\n\n"
        "Создайте токен, следуя шагам ниже."
    )

def _stars_text(chat_id: Any) -> str:
    cfg = _get_cfg(chat_id)
    items = cfg.get("star_lots") or []
    if not items:
        body = "Пока нет лотов со звёздами.\nНажмите «➕ Добавить лот»."
    else:
        rows = []
        for it in sorted(items, key=lambda x: (int(x.get('qty', 0)), int(x.get('lot_id', 0)))):
            rows.append(f"• <b>{it.get('qty')}</b> ⭐ → LOT <code>{it.get('lot_id')}</code> — " +
                        ("🟢 активен" if it.get('active') else "🔴 выключен"))
        body = "\n".join(rows)
    return "<b>⭐ Звёзды (лоты)</b>\n\n" + body

def _finance_text(chat_id: Any) -> str:
    cfg = _get_cfg(chat_id)
    ton_price = cfg.get("ton_buy_price", 125.0)
    frag_rate = cfg.get("fragment_rate", 0.115)
    stats = cfg.get("stats", {})
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    day_stat = stats.get(today, {"stars": 0, "cost_rub": 0.0, "revenue_rub": 0.0})
    profit = day_stat["revenue_rub"] - day_stat["cost_rub"]

    return (
        "📊 <b>Финансы и Статистика (за сегодня)</b>\n\n"
        f"📈 <b>Продано звёзд:</b> <code>{day_stat['stars']} ⭐</code>\n"
        f"💵 <b>Выручка (с FunPay):</b> <code>{round(day_stat['revenue_rub'], 2)} ₽</code>\n"
        f"📉 <b>Себестоимость:</b> <code>{round(day_stat['cost_rub'], 2)} ₽</code>\n"
        f"💰 <b>Чистая прибыль:</b> <b><code>{round(profit, 2)} ₽</code></b>\n\n"
        "⚙️ <b>Текущие параметры для просчёта:</b>\n"
        f"• Вы покупаете 1 TON за: <code>{ton_price} ₽</code>\n"
        f"• Fragment берет за 50⭐: <code>{frag_rate} TON</code>\n\n"
        "<i>* Прибыль считается по параметрам, установленным в момент конкретной продажи. Изменение параметров повлияет только на будущие заказы.</i>"
    )

def _presale_text(chat_id: Any) -> str:
    cfg = _get_cfg(chat_id)
    enabled = cfg.get("presale_enabled", False)
    text = _html.escape(str(cfg.get("presale_text", "👋 Привет! Да, я тут. Выдача звёзд работает в автоматическом режиме 24/7. Можете оформлять заказ!")))
    state = "🟢 Включен" if enabled else "🔴 Выключен"

    return (
        f"💬 <b>Автоответчик (Пре-сейл)</b>\n\n"
        f"Статус: <b>{state}</b>\n\n"
        f"Эта функция автоматически отвечает клиентам, которые перешли из лота со звёздами и сразу задали короткий вопрос (Тут?, Работаешь?, Автовыдача? и т.д.).\n\n"
        f"<b>Текущий текст ответа:</b>\n<code>{text}</code>"
    )

def _proxy_text(chat_id: Any) -> str:
    cfg = _get_cfg(chat_id)
    mode = cfg.get("proxy_mode", "none")
    mode_str = "Выключен" if mode == "none" else ("От бота (Cardinal)" if mode == "cardinal" else "Свой (Custom)")
    custom = _html.escape(str(cfg.get("custom_proxy") or "Не задан"))

    return (
        "🌐 <b>Настройки Прокси (Fragment)</b>\n\n"
        f"Режим: <b>{mode_str}</b>\n"
        f"Ваш прокси: <code>{custom}</code>\n\n"
        "<i>Формат прокси: http://user:pass@ip:port или http://ip:port</i>\n"
        "Используется для обхода блокировок 429 от Fragment."
    )

# Кнопки Telegram
def _home_kb() -> InlineKeyboardMarkup:
    kb = K()
    kb.row(B("⚙️ Настройки", callback_data=CBT_SETTINGS),
           B("📖 Инструкция", callback_data=CBT_HELP))
    kb.add(B("◀️ Назад", callback_data=CBT_BACK_PLUGINS))
    return kb

def _help_kb() -> InlineKeyboardMarkup:
    kb = K()
    kb.add(B("🏠 Домой", callback_data=CBT_HOME))
    return kb

def _settings_kb(chat_id: Any) -> InlineKeyboardMarkup:
    cfg = _get_cfg(chat_id)
    kb = K()
    kb.row(B(f"Плагин: {_state_on(cfg.get('plugin_enabled', True))}", callback_data=CBT_TOGGLE_PLUGIN))
    state_txt, _ = _lots_state_summary(cfg)
    kb.row(B(f"Лоты: {state_txt}", callback_data=CBT_TOGGLE_LOTS))
    kb.row(
        B(("🟢 Включить автовозврат" if not cfg.get("auto_refund", False) else "🟡 Выключить автовозврат"), callback_data=CBT_TOGGLE_REFUND),
        B(("🟢 Включить автодеакт." if not cfg.get("auto_deactivate", True) else "🟡 Выключить автодеакт."), callback_data=CBT_TOGGLE_DEACT)
    )
    kb.row(
        B(("🟢 Включить !бэк" if not cfg.get("manual_refund_enabled", False) else "🟡 Выключить !бэк"), callback_data=CBT_TOGGLE_MANUAL_REFUND),
        B(("⬆️ Приоритет !бэк: ВЫШЕ" if cfg.get("manual_refund_priority", True) else "⬇️ Приоритет !бэк: НИЖЕ"), callback_data=CBT_TOGGLE_BACK_PRIORITY)
    )
    kb.row(B("🔐 Токен", callback_data=CBT_TOKEN), B("🌐 Прокси", callback_data=CBT_PROXY_MENU))

    kb.row(
        B(f"🔋 Автодеакт.: {cfg.get('min_balance_ton', MIN_BALANCE_TON)}", callback_data=CBT_SET_MIN_BAL),
        B(f"🔔 Уведомл.: {cfg.get('notify_threshold_ton', 10.0)}", callback_data=CBT_SET_NOTIFY_BAL)
    )

    kb.row(B("⭐ Звёзды (лоты)", callback_data=CBT_STARS))
    kb.row(B("🧩 Сообщения", callback_data=CBT_MESSAGES))
    kb.row(B("📊 Финансы и Статистика", callback_data=CBT_FINANCE))
    kb.row(B("💬 Автоответчик (Пре-сейл)", callback_data=CBT_PRESALE_MENU))
    kb.add(B("🏠 Домой", callback_data=CBT_HOME))
    return kb

def _token_kb() -> InlineKeyboardMarkup:
    kb = K()
    kb.add(B("🧩 Создать токен", callback_data=CBT_CREATE_JWT))
    kb.row(B("♻️ Пересоздать токен", callback_data=CBT_CREATE_JWT),
           B("🗑 Удалить токен", callback_data=CBT_DEL_JWT))
    kb.add(B("🏠 Домой", callback_data=CBT_HOME))
    kb.add(B("◀️ Назад", callback_data=CBT_SETTINGS))
    return kb

def _stars_kb(chat_id: Any) -> InlineKeyboardMarkup:
    cfg = _get_cfg(chat_id)
    kb = K()
    for it in (cfg.get("star_lots") or [])[:10]:
        lot_id = it.get("lot_id"); qty = it.get("qty")
        state = "🟢 ON" if it.get("active") else "🔴 OFF"
        kb.row(B(f"{qty}⭐ LOT {lot_id} {state}", callback_data=f"{CBT_STAR_TOGGLE_P}{lot_id}"),
               B("🗑", callback_data=f"{CBT_STAR_DEL_P}{lot_id}"))
    kb.row(B("➕ Добавить лот", callback_data=CBT_STAR_ADD))
    kb.row(B("⚡ Включить все", callback_data=CBT_STAR_ACT_ALL), B("💤 Выключить все", callback_data=CBT_STAR_DEACT_ALL))
    kb.add(B("🏠 Домой", callback_data=CBT_HOME))
    kb.add(B("◀️ Назад", callback_data=CBT_BACK_PLUGINS))
    return kb

def _finance_kb() -> InlineKeyboardMarkup:
    kb = K()
    kb.row(B("✏️ Изменить цену 1 TON (₽)", callback_data=CBT_SET_TON_PRICE))
    kb.row(B("✏️ Изменить цену 50⭐ (TON)", callback_data=CBT_SET_FRAG_RATE))
    kb.add(B("◀️ Назад в настройки", callback_data=CBT_SETTINGS))
    return kb

def _presale_kb(chat_id: Any) -> InlineKeyboardMarkup:
    cfg = _get_cfg(chat_id)
    enabled = cfg.get("presale_enabled", False)
    btn_state = "🔴 Выключить" if enabled else "🟢 Включить"

    kb = K()
    kb.row(B(btn_state, callback_data=CBT_TOGGLE_PRESALE))
    kb.row(B("✏️ Изменить текст", callback_data=CBT_SET_PRESALE_TEXT))
    kb.add(B("◀️ Назад в настройки", callback_data=CBT_SETTINGS))
    return kb

def _proxy_kb(chat_id: Any) -> InlineKeyboardMarkup:
    kb = K()
    cfg = _get_cfg(chat_id)
    mode = cfg.get("proxy_mode", "none")

    if mode == "none":
        next_mode = "cardinal"; btn_txt = "Включить: От бота (Cardinal)"
    elif mode == "cardinal":
        next_mode = "custom"; btn_txt = "Включить: Свой прокси"
    else:
        next_mode = "none"; btn_txt = "🔴 Выключить прокси"

    kb.row(B(f"🔄 Режим: {btn_txt}", callback_data=f"{CBT_SET_PROXY_MODE}:{next_mode}"))
    kb.row(B("✏️ Задать свой прокси", callback_data=CBT_SET_CUSTOM_PROXY))
    kb.row(B("🔌 Проверить соединение", callback_data=CBT_CHECK_PROXY))
    kb.add(B("◀️ Назад в настройки", callback_data=CBT_SETTINGS))
    return kb

def _send_panel(bot, chat_id: Any, text: str, kb=None):
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"send_panel failed: {e}")

def _open_finance(bot, call):
    chat_id = call.message.chat.id
    _safe_edit(bot, chat_id, call.message.id, _finance_text(chat_id), _finance_kb())
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _open_presale(bot, call):
    chat_id = call.message.chat.id
    _safe_edit(bot, chat_id, call.message.id, _presale_text(chat_id), _presale_kb(chat_id))
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _open_proxy(bot, call):
    chat_id = call.message.chat.id
    _safe_edit(bot, chat_id, call.message.id, _proxy_text(chat_id), _proxy_kb(chat_id))
    try: bot.answer_callback_query(call.id)
    except Exception: pass

# ========================================================================
# 6. ЛОГИКА FUNPAY (ЛОТЫ, ВОЗВРАТЫ, ОЧЕРЕДЬ)
# ========================================================================
def _normalize_wallet_version(s: str) -> str:
    if not s: return "W5"
    t = s.strip().upper().replace(" ", "").replace(".", "")
    if t in ("W5", "V5R1"): return "W5"
    if t in ("V4R2",): return "V4R2"
    if "V4" in t and "R2" in t: return "V4R2"
    if "5" in t and ("R1" in t or "V5" in t or "W5" in t): return "W5"
    return "W5"

def _extract_qty_from_title(title: str) -> Optional[int]:
    if not title: return None
    nums = [int(x) for x in _re.findall(r"\d+", title)]
    for n in nums:
        if n >= 50: return n
    return None

def _extract_username_from_text(text: str) -> Optional[str]:
    if not text: return None
    m = _re.search(r"@?([A-Za-z0-9_]{5,})", text)
    if m: return m.group(1)
    return None

def _get_my_lots_by_category(cardinal: "Cardinal", category_id: int) -> Dict[int, Any]:
    lots: Dict[int, Any] = {}
    try:
        cardinal.update_lots_and_categories()
        if FunPayAPI is None: raise RuntimeError("FunPayAPI module not available")
        subcat = cardinal.account.get_subcategory(FunPayAPI.types.SubCategoryTypes.COMMON, int(category_id))
        lots = cardinal.tg_profile.get_sorted_lots(2).get(subcat, {}) or {}
    except Exception as e:
        logger.warning(f"_get_my_lots_by_category failed: {e}")
    return lots

def _is_stars_lot(cardinal: "Cardinal", lot_id: int) -> bool:
    try:
        fields = cardinal.account.get_lot_fields(int(lot_id))
        if not fields: return False
        sub = getattr(fields, "subcategory", None) or getattr(fields, "subcat", None)
        cid = (getattr(sub, "id", None) if sub else None)
        if cid is None:
            cid = getattr(fields, "subcategory_id", None) or getattr(fields, "category_id", None)
        if cid is None:
            try:
                cid = (getattr(fields, "fields", None) or {}).get("node_id")
            except Exception:
                cid = None
        return int(cid) == int(STARS_CATEGORY_ID)
    except Exception: return False

def _order_is_stars(order: Any) -> bool:
    try:
        cand = (getattr(order, "subcategory_id", None) or getattr(order, "category_id", None)
            or getattr(getattr(order, "subcategory", None), "id", None) or getattr(getattr(order, "category", None), "id", None))
        return cand is None or int(cand) == int(STARS_CATEGORY_ID)
    except Exception: return True

def _activate_lot(cardinal: "Cardinal", lot_id: int) -> bool:
    try:
        if not _is_stars_lot(cardinal, lot_id): return False
        fields = cardinal.account.get_lot_fields(int(lot_id))
        if not fields: return False
        if not getattr(fields, "active", False):
            fields.active = True
            cardinal.account.save_lot(fields)
        return True
    except Exception: return False

def _deactivate_lot(cardinal: "Cardinal", lot_id: int) -> bool:
    try:
        if not _is_stars_lot(cardinal, lot_id): return False
        fields = cardinal.account.get_lot_fields(int(lot_id))
        if not fields: return False
        if getattr(fields, "active", False):
            fields.active = False
            cardinal.account.save_lot(fields)
        return True
    except Exception: return False

def _apply_star_lots_state(cardinal: "Cardinal", star_lots: List[dict], enabled: bool) -> Dict[str, List[int]]:
    report = {"ok": [], "skip": [], "err": []}
    for it in star_lots or []:
        lot_id = it.get("lot_id")
        if not lot_id: continue
        try:
            if not _is_stars_lot(cardinal, int(lot_id)):
                report["skip"].append(int(lot_id))
                continue
            ok = _activate_lot(cardinal, lot_id) if enabled else _deactivate_lot(cardinal, lot_id)
            (report["ok"] if ok else report["skip"]).append(int(lot_id))
        except Exception: report["err"].append(int(lot_id))
    return report

def _apply_category_state(cardinal: "Cardinal", category_id: int, enabled: bool) -> Dict[str, List[int]]:
    category_id = int(STARS_CATEGORY_ID)
    report = {"ok": [], "skip": [], "err": []}
    lots = _get_my_lots_by_category(cardinal, category_id)
    for lot_id, _ in (lots or {}).items():
        try:
            ok = _activate_lot(cardinal, lot_id) if enabled else _deactivate_lot(cardinal, lot_id)
            (report["ok"] if ok else report["skip"]).append(int(lot_id))
        except Exception: report["err"].append(int(lot_id))
    return report

def _parse_fragment_error_text(response_text: str, status_code: int = 0) -> str:
    fallback = "Ошибка обработки заказа."
    try: data = json.loads(response_text)
    except Exception: data = None
    if status_code == 429: return "Слишком много запросов. Попробуйте ещё раз через минуту."
    if status_code in (500, 502, 503, 504): return "Сервис Fragment временно недоступен. Повторите позже."
    if status_code in (401, 403): return "Нужна повторная авторизация продавца."
    if isinstance(data, dict):
        if "username" in data: return "Неверный Telegram-тег (проверьте @username)."
        if "quantity" in data: return "Минимальное количество для покупки — 50 ⭐."
        for k in ("detail", "message", "error"):
            if data.get(k):
                msg = str(data[k])
                if "not enough" in msg.lower() or "balance" in msg.lower(): return "Недостаточно средств на кошельке Fragment."
                if "version" in msg.lower(): return "Неверная версия кошелька у продавца."
                if "username" in msg.lower(): return "Пользователь с таким @username не найден."
                return msg[:200]
        if isinstance(data.get("errors"), list):
            joined = " | ".join(str(x.get("error") or x) for x in data["errors"][:3])
            if "balance" in joined.lower(): return "Недостаточно средств на кошельке Fragment."
            return (joined or fallback)[:200]
    return fallback

def _auto_refund_order(cardinal: "Cardinal", order_id: Any, chat_id: Any, reason: str) -> bool:
    try:
        cardinal.account.refund(order_id)
        _safe_send(cardinal, chat_id, "✅ Средства успешно возвращены.")
        logger.warning(f"[REFUND] Заказ {order_id}: возврат выполнен. Причина: {reason}")
        return True
    except Exception as e:
        logger.error(f"[REFUND] Не удалось вернуть: {e}")
        _safe_send(cardinal, chat_id, "❌ Не удалось оформить возврат автоматически. Свяжитесь с админом.")
        return False

def _maybe_auto_deactivate(cardinal: "Cardinal", cfg: dict, chat_id: Optional[Any] = None):
    jwt = cfg.get("fragment_jwt")
    ver, bal, _raw = _check_fragment_wallet(jwt, chat_id=chat_id) if jwt else (None, None, None)
    if bal is None: return

    owner = _owner_key(chat_id) if chat_id is not None else None
    if owner is not None:
        _set_cfg(owner, balance_ton=round(bal, 6), **({"wallet_version": ver} if ver else {}))

    admin_tg_id = None
    for k, v in _load_settings().items():
        if isinstance(v, dict) and v.get("fragment_jwt") == jwt:
            try: admin_tg_id = int(k); break
            except Exception: pass

    notify_thr = float(cfg.get("notify_threshold_ton", 10.0))
    last_notify = cfg.get("last_notify_bal", 0.0)

    if admin_tg_id:
        if bal <= notify_thr:
            if last_notify == 0.0 or bal <= last_notify - 1.0 or last_notify > notify_thr:
                try:
                    cardinal.telegram.bot.send_message(
                        admin_tg_id,
                        f"⚠️ <b>КРИТИЧЕСКИЙ ОСТАТОК TON!</b>\n\nНа кошельке осталось всего <b>{bal} TON</b> (порог: {notify_thr} TON).\nСРОЧНО пополни кошелек, звезды заканчиваются!",
                        parse_mode="HTML"
                    )
                    _set_cfg(admin_tg_id, last_notify_bal=bal)
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление в TG: {e}")
        else:
            if last_notify != 0.0:
                _set_cfg(admin_tg_id, last_notify_bal=0.0)

    thr = float(cfg.get("min_balance_ton") or MIN_BALANCE_TON)
    if bal < thr and cfg.get("auto_deactivate", False):
        cat_id = STARS_CATEGORY_ID
        _apply_category_state(cardinal, cat_id, False)
        items = cfg.get("star_lots") or []
        for it in items: it["active"] = False
        _set_cfg(admin_tg_id if admin_tg_id is not None else (owner or "__orders__"), lots_active=False, star_lots=items, last_auto_deact_reason=f"Баланс {bal} < порога {thr}")

        if admin_tg_id:
            try:
                cardinal.telegram.bot.send_message(
                    admin_tg_id,
                    f"🛑 <b>ЛОТЫ ОТКЛЮЧЕНЫ!</b>\nБаланс ({bal} TON) упал ниже минимального порога автодеактивации ({thr} TON).",
                    parse_mode="HTML"
                )
            except: pass

_pending_orders: Dict[str, List[Dict[str, Any]]] = {}
_prompted_orders: Dict[str, set] = {}

def _mark_prompted(chat_id: Any, order_id: Optional[Any]) -> None:
    if order_id is None: return
    _prompted_orders.setdefault(str(chat_id), set()).add(str(order_id))

def _was_prompted(chat_id: Any, order_id: Optional[Any]) -> bool:
    if order_id is None: return False
    return str(order_id) in _prompted_orders.get(str(chat_id), set())

def _unmark_prompted(chat_id: Any, order_id: Optional[Any]) -> None:
    if order_id is None: return
    s = _prompted_orders.get(str(chat_id))
    if s: s.discard(str(order_id))

def _q(chat_id: Any) -> List[Dict[str, Any]]:
    return _pending_orders.setdefault(str(chat_id), [])

def _current(chat_id: Any) -> Optional[Dict[str, Any]]:
    q = _q(chat_id)
    return q[0] if q else None

def _push(chat_id: Any, item: Dict[str, Any]) -> None:
    q = _q(chat_id)
    oid = item.get("order_id")
    if oid and any(str(x.get("order_id")) == str(oid) for x in q):
        for x in q:
            if str(x.get("order_id")) == str(oid):
                for k, v in item.items():
                    if v is not None: x[k] = v
                x.setdefault("prompted", False)
                break
        return
    item.setdefault("prompted", False)
    q.append(item)

def _pop_current(chat_id: Any) -> Optional[Dict[str, Any]]:
    q = _q(chat_id)
    item = q.pop(0) if q else None
    if item and item.get("order_id"): _unmark_prompted(chat_id, item.get("order_id"))
    return item

def _update_current(chat_id: Any, **updates) -> None:
    cur = _current(chat_id)
    if cur is not None: cur.update(updates)

def _has_queue(chat_id: Any) -> bool:
    return bool(_q(chat_id))

def _safe_send(c: "Cardinal", chat_id, text: str):
    try: c.send_message(chat_id, text)
    except Exception: pass

# ========================================================================
# 7. ОБРАБОТЧИКИ СОБЫТИЙ (TELEGRAM CALLBACKS И МЕНЮ)
# ========================================================================
CBT_HOME       = f"{UUID}:home"
CBT_SETTINGS   = f"{UUID}:settings"
CBT_HELP       = f"{UUID}:help"
CBT_FSM_CANCEL = f"{UUID}:fsm_cancel"
CBT_EDIT_PLUGIN     = getattr(CBT, "EDIT_PLUGIN", "41")
CBT_PLUGIN_SETTINGS = getattr(CBT, "PLUGIN_SETTINGS", "43")
# «Назад» ведёт на стандартную карточку плагина Cardinal
CBT_BACK_PLUGINS = f"{CBT_EDIT_PLUGIN}:{UUID}:0"
CBT_TOGGLE_PLUGIN = f"{UUID}:toggle_plugin"

CBT_TOGGLE_LOTS   = f"{UUID}:toggle_lots"
CBT_TOGGLE_REFUND = f"{UUID}:toggle_refund"
CBT_TOGGLE_DEACT  = f"{UUID}:toggle_deact"
CBT_SET_MIN_BAL   = f"{UUID}:set_min_balance"
CBT_SET_NOTIFY_BAL = f"{UUID}:set_notify_bal"
CBT_TOGGLE_MANUAL_REFUND = f"{UUID}:toggle_manual_refund"
CBT_TOGGLE_BACK_PRIORITY = f"{UUID}:toggle_back_priority"

CBT_TOKEN         = f"{UUID}:token"
CBT_CREATE_JWT    = f"{UUID}:create_jwt"
CBT_JWT_CONFIRMED = f"{UUID}:jwt_confirmed"
CBT_JWT_RESEND    = f"{UUID}:jwt_resend"
CBT_MESSAGES      = f"{UUID}:msgs"
CBT_MSG_EDIT_P    = f"{UUID}:msg_edit:"
CBT_MSG_RESET_P   = f"{UUID}:msg_reset:"
CBT_SET_JWT       = f"{UUID}:set_jwt"
CBT_DEL_JWT       = f"{UUID}:del_jwt"

CBT_STARS         = f"{UUID}:stars"
CBT_STAR_ADD      = f"{UUID}:star_add"
CBT_STAR_ACT_ALL  = f"{UUID}:star_act_all"
CBT_STAR_DEACT_ALL= f"{UUID}:star_deact_all"
CBT_STAR_TOGGLE_P = f"{UUID}:star_toggle:"
CBT_STAR_DEL_P    = f"{UUID}:star_del:"

CBT_CONFIRM_SEND    = f"{UUID}:confirm_send"
CBT_CHANGE_USERNAME = f"{UUID}:change_username"
CBT_CANCEL_FLOW     = f"{UUID}:cancel_flow"

CBT_FINANCE       = f"{UUID}:finance"
CBT_SET_TON_PRICE = f"{UUID}:set_ton_price"
CBT_SET_FRAG_RATE = f"{UUID}:set_frag_rate"

CBT_PRESALE_MENU     = f"{UUID}:presale_menu"
CBT_TOGGLE_PRESALE   = f"{UUID}:toggle_presale"
CBT_SET_PRESALE_TEXT = f"{UUID}:set_presale_text"

CBT_PROXY_MENU       = f"{UUID}:proxy_menu"
CBT_SET_PROXY_MODE   = f"{UUID}:set_proxy_mode"
CBT_SET_CUSTOM_PROXY = f"{UUID}:set_custom_proxy"
CBT_CHECK_PROXY      = f"{UUID}:check_proxy"

_fsm: dict[int, dict] = {}

def _lots_state_summary(cfg: dict) -> tuple[str, Optional[bool]]:
    items = cfg.get("star_lots") or []
    if items:
        total = len(items)
        on = sum(1 for it in items if it.get("active"))
        if on == 0: return ("🔴 Выключены", False)
        if on == total: return ("🟢 Включены", True)
        return ("🟡 Частично", None)
    return ("🟢 Включены" if cfg.get("lots_active") else "🔴 Выключены", bool(cfg.get("lots_active")))

_MSG_TITLES = {
    "purchase_created": "После покупки (просим ник)",
    "username_received": "Ник получен (уведомление)",
    "username_invalid": "Ник неверный/не найден",
    "username_valid": "Ник верный (подтверждение)",
    "sending": "Отправка звёзд (процесс)",
    "sent": "Отправлено успешно",
    "failed": "Не удалось отправить",
}

def _messages_text(chat_id: Any) -> str:
    tpls = _get_cfg(chat_id).get("templates") or _default_templates()
    pend = _current(chat_id)
    if pend:
        oid = pend.get("order_id") or "ABC123"
        qty = int(pend.get("qty", 50)) or 150
        uname = (pend.get("candidate") or "@username")
        order_url = f"https://funpay.com/orders/{oid}/"
    else:
        oid = "ABC123"; qty = 150; uname = "@username"; order_url = "https://funpay.com/orders/ABC123/"

    lines = [
        "<b>Кастомные сообщения</b>", "",
        "Плейсхолдеры (что это и зачем):",
        "• {qty} — количество звёзд в заказе",
        "• {username} — ник покупателя (с @), подставится автоматически",
        "• {order_id} — номер заказа на FunPay",
        "• {order_url} — ссылка на страницу заказа",
        "• {reason} — краткая причина ошибки при неудачной отправке", "",
        "Текущие значения (пример):",
        _html.escape(f"qty={qty} username={uname} order_id={oid} order_url={order_url}"), "",
        "Выберите шаблон ниже, чтобы изменить:"
    ]
    for key, title in _MSG_TITLES.items():
        preview = _html.escape((tpls.get(key) or "").replace("\n", " ")[:70])
        lines.append(f"• <b>{title}</b>\n{preview}")
    return "\n".join(lines)

def _messages_kb(chat_id: Any) -> InlineKeyboardMarkup:
    kb = K()
    for key, title in list(_MSG_TITLES.items()):
        kb.row(B(f"✏️ {title}", callback_data=f"{CBT_MSG_EDIT_P}{key}"), B("♻️", callback_data=f"{CBT_MSG_RESET_P}{key}"))
    kb.add(B("◀️ Назад", callback_data=CBT_SETTINGS))
    return kb

def _open_messages(bot, call):
    chat_id = call.message.chat.id
    _safe_edit(bot, chat_id, call.message.id, _messages_text(chat_id), _messages_kb(chat_id))
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _msg_edit_start(bot, call):
    chat_id = call.message.chat.id
    key = call.data.split(":")[-1]
    _fsm[chat_id] = {"step": "msg_edit_value", "msg_key": key}
    pend = _current(chat_id)
    if pend:
        oid = pend.get("order_id") or "ABC123"
        qty = int(pend.get("qty", 50)) or 150
        uname = (pend.get("candidate") or "@username")
        order_url = f"https://funpay.com/orders/{oid}/"
    else:
        oid = "ABC123"; qty = 150; uname = "@username"; order_url = "https://funpay.com/orders/ABC123/"
    cfg = _get_cfg(chat_id)
    tpls = cfg.get("templates") or _default_templates()
    cur_text = tpls.get(key, _default_templates().get(key, ""))
    title = _MSG_TITLES.get(key, key)
    text_block = (
        f"Изменение: {_html.escape(title)}\n\n"
        "Доступные плейсхолдеры:\n"
        "{qty} {username} {order_id} {order_url} {reason}\n"
        "Текущие значения (пример):\n"
        + _html.escape(f"qty={qty} username={uname} order_id={oid} order_url={order_url}") + "\n\n"
        "Текущий текст шаблона:\n"
        f"{_html.escape(str(cur_text))}\n\n"
        "Пришлите новый текст (или /cancel)."
    )
    m = bot.send_message(chat_id, text_block, parse_mode="HTML", reply_markup=_kb_cancel_fsm())
    st = _fsm.get(chat_id, {})
    st["prompt_msg_id"] = getattr(m, "message_id", None)
    _fsm[chat_id] = st

def _msg_reset(bot, call):
    chat_id = call.message.chat.id
    key = call.data.split(":")[-1]
    cfg = _get_cfg(chat_id)
    tpls = cfg.get("templates") or {}
    defaults = _default_templates()
    if key in defaults:
        tpls[key] = defaults[key]
        _set_cfg(chat_id, templates=tpls)
    try: bot.answer_callback_query(call.id, "Сброшено по умолчанию")
    except Exception: pass
    _open_messages(bot, call)

def _kb_cancel_fsm() -> InlineKeyboardMarkup:
    kb = K()
    kb.add(B("❌ Отменить ввод", callback_data=CBT_FSM_CANCEL))
    return kb

def _fsm_cancel(cardinal: "Cardinal", call):
    chat_id = call.message.chat.id
    _fsm.pop(chat_id, None)
    try: cardinal.telegram.bot.answer_callback_query(call.id, "Отменено.")
    except Exception: pass
    cardinal.telegram.bot.send_message(chat_id, "❌ Отменено.")

def _toggle_plugin(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id)
    new_state = not bool(cfg.get("plugin_enabled", True))
    _set_cfg(chat_id, plugin_enabled=new_state)
    try: bot.answer_callback_query(call.id, "Плагин включён." if new_state else "Плагин выключен.")
    except Exception: pass
    _open_settings(bot, call)

def _open_home(bot, call):
    _safe_edit(bot, call.message.chat.id, call.message.id, _about_text(), _home_kb())
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _open_settings(bot, call):
    chat_id = call.message.chat.id
    _safe_edit(bot, chat_id, call.message.id, _settings_text(chat_id), _settings_kb(chat_id))
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _open_help(bot, call):
    _safe_edit(bot, call.message.chat.id, call.message.id, HELP_TEXT, _help_kb())
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _open_token(bot, call):
    chat_id = call.message.chat.id
    _safe_edit(bot, chat_id, call.message.id, _token_text(chat_id), _token_kb())
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _open_stars(bot, call):
    chat_id = call.message.chat.id
    _safe_edit(bot, chat_id, call.message.id, _stars_text(chat_id), _stars_kb(chat_id))
    try: bot.answer_callback_query(call.id)
    except Exception: pass

def _star_add(bot, call):
    chat_id = call.message.chat.id
    _fsm[chat_id] = {"step": "star_add_qty"}
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    bot.send_message(chat_id, "Введите <b>количество звёзд</b> (целое число от 50 до 1_000_000):", parse_mode="HTML", reply_markup=_kb_cancel_fsm())

def _star_act_all(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id); items = cfg.get("star_lots") or []
    if not items:
        bot.answer_callback_query(call.id, "Список пуст.", show_alert=True); return
    if _CARDINAL_REF is not None: _apply_star_lots_state(_CARDINAL_REF, items, True)
    for it in items: it["active"] = True
    _set_cfg(chat_id, star_lots=items)
    _open_stars(bot, call)

def _star_deact_all(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id); items = cfg.get("star_lots") or []
    if not items:
        bot.answer_callback_query(call.id, "Список пуст.", show_alert=True); return
    if _CARDINAL_REF is not None: _apply_star_lots_state(_CARDINAL_REF, items, False)
    for it in items: it["active"] = False
    _set_cfg(chat_id, star_lots=items)
    _open_stars(bot, call)

def _star_toggle(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id); lot_id = int(call.data.split(":")[-1])
    items = cfg.get("star_lots") or []
    found = None
    for it in items:
        if int(it.get("lot_id", 0)) == lot_id: found = it; break
    if not found: bot.answer_callback_query(call.id, "Лот не найден.", show_alert=True); return
    enabled = not bool(found.get("active"))
    if _CARDINAL_REF is not None:
        if enabled: _activate_lot(_CARDINAL_REF, lot_id)
        else:       _deactivate_lot(_CARDINAL_REF, lot_id)
    found["active"] = enabled
    _set_cfg(chat_id, star_lots=items)
    _open_stars(bot, call)

def _star_delete(bot, call):
    chat_id = call.message.chat.id
    lot_id = int(call.data.split(":")[-1])
    cfg = _get_cfg(chat_id)
    items = [x for x in (cfg.get("star_lots") or []) if int(x.get("lot_id", 0)) != lot_id]
    _set_cfg(chat_id, star_lots=items)
    _open_stars(bot, call)

def _toggle_lots(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id)
    if not cfg.get("plugin_enabled", True):
        bot.answer_callback_query(call.id, "Плагин выключен. Сначала включите его в настройках.", show_alert=True); return
    current = bool(cfg.get("lots_active", False))
    desired = not current
    if _CARDINAL_REF is not None:
        star_lots = cfg.get("star_lots") or []
        if star_lots:
            _apply_star_lots_state(_CARDINAL_REF, star_lots, desired)
            for it in star_lots: it["active"] = bool(desired)
            _set_cfg(chat_id, star_lots=star_lots)
        else:
            _apply_category_state(_CARDINAL_REF, STARS_CATEGORY_ID, desired)
    _set_cfg(chat_id, lots_active=desired)
    bot.answer_callback_query(call.id, ("Лоты включены." if desired else "Лоты выключены."), show_alert=False)
    _open_settings(bot, call)

def _toggle_refund(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id)
    cfg = _set_cfg(chat_id, auto_refund = not bool(cfg.get("auto_refund", False)))
    bot.answer_callback_query(call.id, ("Автовозврат включён." if cfg["auto_refund"] else "Автовозврат выключен."), show_alert=False)
    _open_settings(bot, call)

def _toggle_deact(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id)
    cfg = _set_cfg(chat_id, auto_deactivate = not bool(cfg.get("auto_deactivate", True)))
    bot.answer_callback_query(call.id, ("Автодеактивация включена." if cfg["auto_deactivate"] else "Автодеактивация выключена."), show_alert=False)
    _open_settings(bot, call)

def _toggle_manual_refund(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id)
    new_state = not bool(cfg.get("manual_refund_enabled", False))
    _set_cfg(chat_id, manual_refund_enabled=new_state)
    try: bot.answer_callback_query(call.id, "Команда !бэк включена." if new_state else "Команда !бэк выключена.")
    except Exception: pass
    _open_settings(bot, call)

def _toggle_back_priority(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id)
    new_state = not bool(cfg.get("manual_refund_priority", True))
    _set_cfg(chat_id, manual_refund_priority=new_state)
    try:
        txt = "Приоритет !бэк: ВЫШЕ автовозврата" if new_state else "Приоритет !бэк: НИЖЕ автовозврата"
        bot.answer_callback_query(call.id, txt)
    except Exception: pass
    _open_settings(bot, call)

def _ask_set_min_balance(bot, call):
    chat_id = call.message.chat.id
    _fsm[chat_id] = {"step": "set_min_balance"}
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    cur = _get_cfg(chat_id).get("min_balance_ton", MIN_BALANCE_TON)
    bot.send_message(chat_id, f"Введите новый порог баланса для деактивации в TON (сейчас {cur}). Можно с точкой или запятой. Пример: 5.5\n(или /cancel)", reply_markup=_kb_cancel_fsm())

def _ask_set_notify_balance(bot, call):
    chat_id = call.message.chat.id
    _fsm[chat_id] = {"step": "set_notify_bal"}
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    cur = _get_cfg(chat_id).get("notify_threshold_ton", 10.0)
    bot.send_message(chat_id, f"Введите порог баланса для отправки тревожного уведомления в TON (сейчас {cur}). Пример: 10.0\n(или /cancel)", reply_markup=_kb_cancel_fsm())

def _ask_price(bot, call, mode):
    chat_id = call.message.chat.id
    _fsm[chat_id] = {"step": mode}
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    if mode == "ton_price":
        text = "Введите стоимость покупки 1 TON в рублях (например, 125 или 123.5):\n(или /cancel)"
    else:
        text = "Введите актуальную стоимость 50⭐ на Fragment в TON (например, 0.115):\n(или /cancel)"
    bot.send_message(chat_id, text, reply_markup=_kb_cancel_fsm())

def _toggle_presale(bot, call):
    chat_id = call.message.chat.id
    cfg = _get_cfg(chat_id)
    new_state = not bool(cfg.get("presale_enabled", False))
    _set_cfg(chat_id, presale_enabled=new_state)
    try: bot.answer_callback_query(call.id, "Автоответчик включён." if new_state else "Автоответчик выключен.")
    except Exception: pass
    _open_presale(bot, call)

def _ask_presale_text(bot, call):
    chat_id = call.message.chat.id
    _fsm[chat_id] = {"step": "presale_text"}
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    bot.send_message(chat_id, "Введите новый текст для Автоответчика (Пре-сейл):\n(или /cancel)", reply_markup=_kb_cancel_fsm())

def _set_proxy_mode(bot, call):
    chat_id = call.message.chat.id
    new_mode = call.data.split(":")[-1]
    _set_cfg(chat_id, proxy_mode=new_mode)
    try: bot.answer_callback_query(call.id, f"Режим прокси изменен!")
    except Exception: pass
    _open_proxy(bot, call)

def _ask_custom_proxy(bot, call):
    chat_id = call.message.chat.id
    _fsm[chat_id] = {"step": "custom_proxy"}
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    bot.send_message(chat_id, "Введите свой прокси в формате:\n<code>http://user:pass@ip:port</code>\nили <code>socks5://user:pass@ip:port</code>\n\n(или /cancel)", parse_mode="HTML", reply_markup=_kb_cancel_fsm())

def _check_proxy_cmd(cardinal, call):
    chat_id = call.message.chat.id
    try: cardinal.telegram.bot.answer_callback_query(call.id, "Проверяю соединение...")
    except: pass

    proxies = _get_proxies_for_chat(chat_id)

    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=10)
        ip = r.json().get("ip", "неизвестно")

        r_frag = requests.get(FRAGMENT_BASE + "/misc/wallet/", proxies=proxies, timeout=10)
        frag_status = r_frag.status_code

        msg = f"✅ <b>Соединение успешно!</b>\n\n🌐 Внешний IP: <code>{ip}</code>\n🔗 Ответ от Fragment API: <code>{frag_status}</code>"
    except Exception as e:
        msg = f"❌ <b>Ошибка соединения:</b>\n<code>{str(e)}</code>"

    cardinal.telegram.bot.send_message(chat_id, msg, parse_mode="HTML")

def _cancel_cmd(cardinal: "Cardinal", chat_id: Any):
    _fsm.pop(chat_id, None)
    if _has_queue(chat_id): _pop_current(chat_id)
    _safe_send(cardinal, chat_id, "❌ Отменено. Текущий шаг сброшен.")
    if _has_queue(chat_id):
        nxt = _current(chat_id)
        qty = int(nxt.get("qty", 50))
        _safe_send(cardinal, chat_id, f"Следующий заказ в очереди: {qty}⭐.\nПришлите тег в формате @username одной строкой.")

def _select_wallet_version(bot, call):
    chat_id = call.message.chat.id
    data = call.data.split(":")[-1] if call and call.data else "W5"
    ver = _normalize_wallet_version(data)
    st = _fsm.get(chat_id) or {}
    if st.get("step") not in ("jwt_phone", "jwt_wallet_ver"): st["step"] = "jwt_wallet_ver"
    st["wallet_version"] = ver; st["step"] = "jwt_seed"; _fsm[chat_id] = st
    try: bot.answer_callback_query(call.id, f"Версия выбрана: {ver}")
    except Exception: pass
    bot.send_message(chat_id, "Введите 24 слова мнемофразы одной строкой (или /cancel):")

def _del_jwt(bot, call):
    chat_id = call.message.chat.id
    _set_cfg(chat_id, fragment_jwt=None, wallet_version=None, balance_ton=None, last_wallet_raw=None)
    try: bot.answer_callback_query(call.id, "Токен удалён.")
    except Exception: pass
    _open_token(bot, call)

def _start_create_jwt(bot, call):
    chat_id = call.message.chat.id
    _fsm[chat_id] = {"step": "jwt_api_key"}
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    bot.send_message(chat_id, "Введите API-ключ Fragment (или /cancel). Его можно взять в dashboard: https://fragment-api.com/dashboard", reply_markup=_kb_cancel_fsm())

def _handle_fsm(message: Message, cardinal: Cardinal):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    state = _fsm.get(chat_id) or {}

    if state.get("step") == "jwt_api_key":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        state["api_key"] = text; state["step"] = "jwt_phone"; _fsm[chat_id] = state
        cardinal.telegram.bot.send_message(chat_id, "Укажите телефон (без «+», только цифры), или /cancel:", reply_markup=_kb_cancel_fsm()); return

    if state.get("step") == "msg_edit_value":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            try:
                pmid = (_fsm.get(chat_id) or {}).get("prompt_msg_id")
                if pmid: cardinal.telegram.bot.delete_message(chat_id, pmid)
            except Exception: pass
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        key = state.get("msg_key")
        cfg = _get_cfg(chat_id)
        tpls = cfg.get("templates") or _default_templates()
        tpls[key] = text
        _set_cfg(chat_id, templates=tpls)
        try:
            pmid = state.get("prompt_msg_id")
            if pmid: cardinal.telegram.bot.delete_message(chat_id, pmid)
            cardinal.telegram.bot.delete_message(chat_id, message.message_id)
        except Exception: pass
        _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "✅ Шаблон обновлён.")
        _send_panel(cardinal.telegram.bot, chat_id, _messages_text(chat_id), _messages_kb(chat_id))
        return

    if state.get("step") == "jwt_phone":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        state["phone"] = text; state["step"] = "jwt_wallet_ver"; _fsm[chat_id] = state
        kb = K(); kb.row(B("W5 (V5R1)", callback_data=f"{UUID}:ver:W5"), B("V4R2", callback_data=f"{UUID}:ver:V4R2"))
        cardinal.telegram.bot.send_message(chat_id, "Укажите версию кошелька (W5 или V4R2). Можно нажать кнопку или ввести текстом. По умолчанию W5.", reply_markup=kb); return

    if state.get("step") == "jwt_wallet_ver":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        ver = _normalize_wallet_version(text); state["wallet_version"] = ver; state["step"] = "jwt_seed"; _fsm[chat_id] = state
        cardinal.telegram.bot.send_message(chat_id, "Вставьте 24 слова мнемофразы одной строкой (или /cancel):", reply_markup=_kb_cancel_fsm()); return

    if state.get("step") == "jwt_seed":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        words = [w.strip() for w in text.replace("\n", " ").split(" ") if w.strip()]
        if len(words) != 24:
            cardinal.telegram.bot.send_message(chat_id, f"⚠️ Должно быть ровно 24 слова (сейчас {len(words)}). Повторите ввод или /cancel."); return
        state["mnemonic"] = words; _fsm[chat_id] = state
        try: cardinal.telegram.bot.delete_message(chat_id, message.message_id)
        except Exception: pass
        api_key = state.get("api_key"); phone = state.get("phone"); wallet_ver = state.get("wallet_version") or "W5"
        jwt, raw, sc = _authenticate_fragment(api_key=api_key, phone_number=phone, version=wallet_ver, mnemonics=words, chat_id=chat_id)
        if jwt:
            _set_cfg(chat_id, fragment_jwt=jwt); cardinal.telegram.bot.send_message(chat_id, "✅ Успешно: токен создан и привязан.")
            ver, bal, resp = _check_fragment_wallet(jwt, chat_id=chat_id)
            if ver is not None or bal is not None or resp is not None:
                _set_cfg(chat_id, wallet_version=ver, balance_ton=(round(bal, 6) if isinstance(bal, (int, float)) else None), last_wallet_raw=resp)
            _fsm.pop(chat_id, None); return
        is_tma, wait_sec = _is_too_many_attempts(raw)
        if sc == 400 and is_tma: cardinal.telegram.bot.send_message(chat_id, f"Подождите {wait_sec or 'несколько'} секунд, затем подтвредите вход и подождите ответа в плагине. ", parse_mode="HTML")
        else: cardinal.telegram.bot.send_message(chat_id, f"Пока токен не выдан (статус {sc}). Подтвердите вход в Telegram и нажмите «Я подтвердил вход».", parse_mode="HTML")
        return

    if state.get("step") == "star_add_qty":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        try:
            qty = int(text.strip())
            if qty < 50 or qty > 1_000_000: raise ValueError
        except Exception:
            cardinal.telegram.bot.send_message(chat_id, "⚠️ Введите целое число от 50 до 1 000 000, либо /cancel."); return
        state["new_qty"] = qty; state["step"] = "star_add_lotid"; _fsm[chat_id] = state
        cardinal.telegram.bot.send_message(chat_id, "Теперь введите <b>LOT_ID</b> (целое положительное), или /cancel:", parse_mode="HTML", reply_markup=_kb_cancel_fsm())
        return

    if state.get("step") == "star_add_lotid":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        try:
            lot_id = int(text.strip())
            if lot_id <= 0: raise ValueError
        except Exception:
            cardinal.telegram.bot.send_message(chat_id, "⚠️ Введите положительное целое (LOT_ID), либо /cancel."); return
        qty = int(state.get("new_qty"))
        cfg = _get_cfg(chat_id); items = cfg.get("star_lots") or []
        updated = False
        for it in items:
            if int(it.get("lot_id", 0)) == lot_id:
                it["qty"] = qty; it["active"] = True; updated = True; break
        if not updated: items.append({"qty": qty, "lot_id": lot_id, "active": True})
        _set_cfg(chat_id, star_lots=items); _fsm.pop(chat_id, None)
        if _CARDINAL_REF is not None: _activate_lot(_CARDINAL_REF, lot_id)
        cardinal.telegram.bot.send_message(chat_id, f"✅ Добавлено: {qty} ⭐ (LOT {lot_id}). Управляйте в «⭐ Звёзды»."); return

    if state.get("step") == "set_min_balance":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        t = text.replace(",", ".").strip()
        try:
            val = float(t)
            if val < 0: raise ValueError
        except Exception:
            cardinal.telegram.bot.send_message(chat_id, "⚠️ Введите неотрицательное число. Пример: 4.2"); return
        _set_cfg(chat_id, min_balance_ton=val); _fsm.pop(chat_id, None)
        cardinal.telegram.bot.send_message(chat_id, f"✅ Порог деактивации сохранён: {val} TON")
        return

    if state.get("step") == "set_notify_bal":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        t = text.replace(",", ".").strip()
        try:
            val = float(t)
            if val < 0: raise ValueError
        except Exception:
            cardinal.telegram.bot.send_message(chat_id, "⚠️ Введите неотрицательное число. Пример: 10.0"); return
        _set_cfg(chat_id, notify_threshold_ton=val); _fsm.pop(chat_id, None)
        cardinal.telegram.bot.send_message(chat_id, f"✅ Порог уведомлений сохранён: {val} TON")
        return

    if state.get("step") in ["ton_price", "frag_rate"]:
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        t = text.replace(",", ".").strip()
        try:
            val = float(t)
            if val <= 0: raise ValueError
        except Exception:
            cardinal.telegram.bot.send_message(chat_id, "⚠️ Введите корректное положительное число."); return
        if state.get("step") == "ton_price":
            _set_cfg(chat_id, ton_buy_price=val)
            cardinal.telegram.bot.send_message(chat_id, f"✅ Цена покупки 1 TON сохранена: {val} ₽")
        else:
            _set_cfg(chat_id, fragment_rate=val)
            cardinal.telegram.bot.send_message(chat_id, f"✅ Цена 50⭐ на Fragment сохранена: {val} TON")
        _fsm.pop(chat_id, None)
        return

    if state.get("step") == "presale_text":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        _set_cfg(chat_id, presale_text=text)
        cardinal.telegram.bot.send_message(chat_id, "✅ Текст автоответчика изменён!")
        _fsm.pop(chat_id, None)
        _send_panel(cardinal.telegram.bot, chat_id, _presale_text(chat_id), _presale_kb(chat_id))
        return

    if state.get("step") == "custom_proxy":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            _fsm.pop(chat_id, None); cardinal.telegram.bot.send_message(chat_id, "❌ Отменено."); return
        _set_cfg(chat_id, custom_proxy=text.strip())
        cardinal.telegram.bot.send_message(chat_id, "✅ Свой прокси успешно сохранен!")
        _fsm.pop(chat_id, None)
        _send_panel(cardinal.telegram.bot, chat_id, _proxy_text(chat_id), _proxy_kb(chat_id))
        return

def _jwt_confirmed(bot, call):
    chat_id = call.message.chat.id
    state = _fsm.get(chat_id) or {}
    api_key = state.get("api_key"); phone = state.get("phone"); wallet_ver = state.get("wallet_version") or "W5"; mnemonic = state.get("mnemonic")
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    if not (api_key and phone and wallet_ver and mnemonic):
        bot.send_message(chat_id, "⚠️ Не хватает данных для создания токена. Попробуйте ещё раз."); _fsm.pop(chat_id, None); _open_token(bot, call); return
    jwt, raw, sc = _authenticate_fragment(api_key=api_key, phone_number=phone, version=wallet_ver, mnemonics=mnemonic, chat_id=chat_id)
    if jwt:
        _set_cfg(chat_id, fragment_jwt=jwt); bot.send_message(chat_id, "✅ Успешно: токен создан и привязан.")
        ver, bal, resp = _check_fragment_wallet(jwt, chat_id=chat_id)
        if ver is not None or bal is not None or resp is not None:
            _set_cfg(chat_id, wallet_version=ver, balance_ton=(round(bal, 6) if isinstance(bal, (int, float)) else None), last_wallet_raw=resp)
        _fsm.pop(chat_id, None)
    else:
        is_tma, wait_sec = _is_too_many_attempts(raw)
        if sc == 400 and is_tma:
            bot.send_message(chat_id, f"Слишком много попыток входа. Подождите {wait_sec or 'несколько'} секунд и попробуйте ещё раз.")
        else:
            try: text = json.dumps(raw, ensure_ascii=False, indent=2)[:1900]
            except Exception: text = str(raw)[:1900]
            bot.send_message(chat_id, f"⚠️ Токен пока не выдан. Статус: <code>{sc}</code>\nОтвет сервера:\n<code>{text}</code>", parse_mode="HTML")
    try: _open_token(bot, call)
    except Exception: pass

def _jwt_resend(bot, call):
    chat_id = call.message.chat.id
    st = _fsm.get(chat_id) or {}
    api_key = st.get("api_key"); phone = st.get("phone"); wallet_ver = st.get("wallet_version") or "W5"; words = st.get("mnemonic")
    try: bot.answer_callback_query(call.id, "Отправляю запрос…")
    except Exception: pass
    if not (api_key and phone and words):
        bot.send_message(chat_id, "Не хватает данных для повтора."); return
    jwt, raw, sc = _authenticate_fragment(api_key=api_key, phone_number=phone, version=wallet_ver, mnemonics=words, chat_id=chat_id)
    if jwt:
        _set_cfg(chat_id, fragment_jwt=jwt); bot.send_message(chat_id, "✅ Успешно: токен создан и привязан.")
        ver, bal, resp = _check_fragment_wallet(jwt, chat_id=chat_id)
        if ver is not None or bal is not None or resp is not None:
            _set_cfg(chat_id, wallet_version=ver, balance_ton=(round(bal, 6) if isinstance(bal, (int, float)) else None), last_wallet_raw=resp)
        _fsm.pop(chat_id, None); _open_token(bot, call); return
    is_tma, wait_sec = _is_too_many_attempts(raw)
    if sc == 400 and is_tma: bot.send_message(chat_id, f"Слишком много попыток входа. Подождите {wait_sec or 'несколько'} секунд и попробуйте снова.")
    else:
        try: pretty = json.dumps(raw, ensure_ascii=False, indent=2)
        except Exception: pretty = str(raw)
        bot.send_message(chat_id, "Ответ сервера:\n<code>{}</code>".format(pretty[:1900]), parse_mode="HTML")

# ========================================================================
# 8. ОБРАБОТЧИКИ СОБЫТИЙ FUNPAY (ЗАКАЗЫ, СООБЩЕНИЯ)
# ========================================================================
def _do_confirm_send(cardinal: "Cardinal", chat_id):
    _pls_require_license(cardinal)
    pend = _current(chat_id) or {}
    qty = int(pend.get("qty", 50))
    username = (pend.get("candidate") or "").strip()
    cfg = _get_cfg_for_orders(chat_id)
    jwt = cfg.get("fragment_jwt")

    if not pend:
        _safe_send(cardinal, chat_id, "Нет активного заказа. Если нужно — дождитесь нового сообщения о заказе.")
        return

    if not jwt:
        _safe_send(cardinal, chat_id, "⚠️ Токен Fragment не привязан. Покупка невозможна.")
        return

    if not username or not _validate_username(username):
        _safe_send(cardinal, chat_id, "⚠️ Некорректный username. Отправьте ник без @, пример: username")
        _update_current(chat_id, stage="await_username")
        return

    if qty < 50:
        _safe_send(cardinal, chat_id, "Минимум 50⭐. Уточните количество или лот.")
        return

    if not _check_username_exists(username, jwt, chat_id=chat_id):
        _safe_send(cardinal, chat_id, f'❌ Ник "{username}" не найден. Пришлите верный тег в формате @username.')
        return

    _safe_send(cardinal, chat_id, _tpl(chat_id, "sending", qty=qty, username=username.lstrip("@")))
    resp = _order_stars(jwt, username=username, quantity=qty, show_sender=False, chat_id=chat_id)

    if resp and resp.get("ok"):
        oid = pend.get("order_id")
        order_url = f"https://funpay.com/orders/{oid}/" if oid else ""
        _safe_send(cardinal, chat_id, _tpl(chat_id, "sent", qty=qty, username=username.lstrip('@'), order_url=order_url))

        _record_sale(cardinal, chat_id, qty, oid)
        _maybe_auto_deactivate(cardinal, cfg, chat_id)

        _update_current(chat_id, finalized=True)
        _pop_current(chat_id)
        if _has_queue(chat_id):
            nxt = _current(chat_id)
            qn = int(nxt.get("qty", 50))
            _safe_send(cardinal, chat_id, f"Следующий заказ: {qn}⭐.\nНапишите ваш Telegram-тег в формате @username одной строкой.")
    else:
        msg = _parse_fragment_error_text((resp or {}).get("text",""), status_code=(resp or {}).get("status",0))
        _safe_send(cardinal, chat_id, _tpl(chat_id, "failed", reason=msg))
        _update_current(chat_id, finalized=True)
        oid = pend.get("order_id")
        if cfg.get("auto_refund", False) and oid:
            _safe_send(cardinal, chat_id, "🔁 Пытаюсь оформить возврат…")
            _auto_refund_order(cardinal, oid, chat_id, reason=msg)
        else:
            _safe_send(cardinal, chat_id, "⏳ У продавца автовозврат отключён. Пожалуйста, дождитесь продавца.")
        _maybe_auto_deactivate(cardinal, cfg, chat_id)

def _cb_confirm_send(cardinal: "Cardinal", call):
    try: cardinal.telegram.bot.answer_callback_query(call.id)
    except Exception: pass
    _do_confirm_send(cardinal, call.message.chat.id)

def _cb_change_username(cardinal: "Cardinal", call):
    chat_id = call.message.chat.id
    try: cardinal.telegram.bot.answer_callback_query(call.id, "Измените ник сообщением.")
    except Exception: pass
    pend = _current(chat_id)
    if not pend:
        cardinal.telegram.bot.send_message(chat_id, "Нет активного заказа.")
        return
    _update_current(chat_id, stage="await_username")
    cardinal.telegram.bot.send_message(chat_id, "Введите новый тег в формате @username:")

def _cb_cancel_flow(cardinal: "Cardinal", call):
    chat_id = call.message.chat.id
    try: cardinal.telegram.bot.answer_callback_query(call.id, "Отменено.")
    except Exception: pass
    _pop_current(chat_id)
    if _has_queue(chat_id):
        nxt = _current(chat_id); qn = int(nxt.get("qty", 50))
        cardinal.telegram.bot.send_message(chat_id, f"Текущий заказ отменён. Следующий: {qn}⭐.\nПришлите тег в формате @username одной строкой.")
    else:
        cardinal.telegram.bot.send_message(chat_id, "Текущий заказ отменён.")

def _allowed_stages(item: dict) -> bool:
    return (str(item.get("stage")) in {"await_username", "await_confirm"} and not item.get("confirmed") and not item.get("finalized"))

def _find_order_for_back(chat_id: Any, order_id: Optional[str]) -> Optional[dict]:
    q = _q(chat_id)
    candidates = [x for x in q if _allowed_stages(x)]
    if order_id:
        for x in candidates:
            if str(x.get("order_id")) == str(order_id): return x
        return None
    if len(candidates) == 1: return candidates[0]
    return None

def _list_pending_oids(chat_id: Any) -> List[str]:
    return [str(x.get("order_id")) for x in _q(chat_id) if _allowed_stages(x) and x.get("order_id")]

def _funpay_is_system_paid_message(text: str) -> bool:
    if not text: return False
    t = text.lower()
    return ("оплатил заказ" in t) and ("звёзд" in t or "звезд" in t or "stars" in t or "⭐" in t)

def _funpay_extract_qty_and_order_id(text: str) -> tuple[Optional[int], Optional[str]]:
    qty = None; oid = None
    try:
        m = _re.search(r"заказ\s*#\s*([A-Za-z0-9]+)", text, _re.IGNORECASE)
        if m: oid = m.group(1)
        m2 = _re.search(r"(\d+)\s*зв[её]зд", text, _re.IGNORECASE)
        if m2: qty = int(m2.group(1))
    except Exception: pass
    return qty, oid

def _validate_username(u: str) -> bool:
    if not u: return False
    u = u.strip().lstrip('@')
    return bool(_re.fullmatch(r"[A-Za-z0-9_]{5,32}", u))

def _is_presale_question(text: str) -> bool:
    if not text: return False
    clean_text = _re.sub(r'[^\w\sа-яА-ЯёЁ]', '', text.lower()).strip()
    words = set(clean_text.split())
    trigger_words = {"тут", "здесь", "ау", "автовыдача", "выдаешь", "онлайн", "online", "привет", "здравствуйте", "ку", "живой", "работаешь"}
    if len(words) <= 6 and (words & trigger_words):
        return True
    return False

def new_order_handler(cardinal: Cardinal, event):
    _pls_require_license(cardinal)
    try:
        chat_id = getattr(event, "chat_id", None) or getattr(getattr(event, "order", None), "chat_id", None)
        if chat_id is None:
            return  # заказ будет обработан по системному сообщению FunPay в new_message_handler
        cfg = _get_cfg_for_orders(chat_id)
        if not cfg.get("plugin_enabled", True): return
        order = getattr(event, "order", None)
        if order is not None and not _order_is_stars(order): return
        title = (getattr(order, "title", None) or getattr(order, "name", None)
                 or getattr(order, "description", None) or "")
        qty = _extract_qty_from_title(title) or 50
        order_id = (getattr(order, "id", None) or getattr(order, "order_id", None) or getattr(event, "order_id", None))
        _push(chat_id, {"qty": qty, "order_id": order_id, "stage": "await_username", "candidate": None})

        chat_text = getattr(order, "buyer_message", None) or getattr(event, "message", None) or ""
        username = _extract_username_from_text(chat_text)
        jwt = cfg.get("fragment_jwt")

        if jwt and username and qty >= 50 and _check_username_exists(username, jwt, chat_id=chat_id):
            _safe_send(cardinal, chat_id, _tpl(chat_id, "sending", qty=qty, username=username.lstrip("@")))
            resp = _order_stars(jwt, username=username.lstrip("@"), quantity=qty, show_sender=False, chat_id=chat_id)
            if resp.get("ok"):
                order_url = f"https://funpay.com/orders/{order_id}/" if order_id else ""
                _safe_send(cardinal, chat_id, _tpl(chat_id, "sent", qty=qty, username=username.lstrip("@"), order_url=order_url))

                _record_sale(cardinal, chat_id, qty, order_id)
                _maybe_auto_deactivate(cardinal, cfg, chat_id)

                _mark_prompted(chat_id, order_id)
                _pop_current(chat_id)
                if _has_queue(chat_id):
                    nxt = _current(chat_id)
                    qn = int(nxt.get("qty", 50))
                    _safe_send(cardinal, chat_id, f"Следующий заказ: {qn}⭐.\nНапишите ваш Telegram-тег в формате @username одной строкой.")
            else:
                msg = _parse_fragment_error_text(resp.get("text", ""), status_code=resp.get("status", 0))
                _update_current(chat_id, finalized=True)
                _safe_send(cardinal, chat_id, _tpl(chat_id, "failed", reason=msg))
                if cfg.get("auto_refund", False) and order_id:
                    _safe_send(cardinal, chat_id, "🔁 Пытаюсь оформить возврат…")
                    _auto_refund_order(cardinal, order_id, chat_id, reason=msg)
                else:
                    _safe_send(cardinal, chat_id, "⏳ У продавца автовозврат отключён. Пожалуйста, дождитесь продавца.")
                _maybe_auto_deactivate(cardinal, cfg, chat_id)
    except Exception as e:
        logger.exception(f"new_order_handler error: {e}")

def new_message_handler(cardinal: Cardinal, event: NewMessageEvent):
    _pls_require_license(cardinal)
    try:
        my_user = (getattr(cardinal.account, "username", None) or "").lower()
        author  = (getattr(event.message, "author", "") or "").lower()
        chat_id = event.message.chat_id
        text = (event.message.text or "").strip()
        cfg = _get_cfg_for_orders(chat_id)

        if not cfg.get("plugin_enabled", True): return

        if author == "funpay":
            lot_match = _re.search(r'funpay\.com/lots/(\d+)', text, _re.I)
            if lot_match:
                lot_id = int(lot_match.group(1))
                if _is_stars_lot(cardinal, lot_id):
                    interested = getattr(cardinal, "_stars_interested_chats", set())
                    interested.add(str(chat_id))
                    setattr(cardinal, "_stars_interested_chats", interested)

        while _has_queue(chat_id):
            head = _current(chat_id) or {}
            if head.get("finalized") or not _allowed_stages(head):
                _pop_current(chat_id)
                continue
            break

        if author == "funpay" and _funpay_is_system_paid_message(text):
            qty, oid = _funpay_extract_qty_and_order_id(text)
            if qty is None: qty = 50
            if not (isinstance(chat_id, int) or str(chat_id).isdigit()): return
            if _was_prompted(chat_id, oid): return
            _push(chat_id, {"qty": qty, "order_id": oid, "stage": "await_username", "candidate": None})

            if len(_q(chat_id)) > 1:
                if oid: _safe_send(cardinal, chat_id, f"Принял ещё один заказ #{oid} на {qty}⭐. Обработаю сразу после текущего.")
                else: _safe_send(cardinal, chat_id, f"Принял ещё один заказ на {qty}⭐. Обработаю сразу после текущего.")
                return

            pend = _current(chat_id)
            if pend and not pend.get("prompted"):
                _safe_send(cardinal, chat_id, _tpl(chat_id, "purchase_created", qty=qty))
                _update_current(chat_id, prompted=True)
                _mark_prompted(chat_id, oid)
            return

        if author in ["funpay", my_user] or not text: return

        if cfg.get("presale_enabled", False):
            interested = getattr(cardinal, "_stars_interested_chats", set())
            if str(chat_id) in interested:
                if _is_presale_question(text):
                    reply = cfg.get("presale_text", "👋 Привет! Да, я тут. Выдача звёзд работает в автоматическом режиме 24/7. Можете оформлять заказ!")
                    _safe_send(cardinal, chat_id, reply)
                    interested.discard(str(chat_id))
                    return
                else:
                    interested.discard(str(chat_id))

        m_back = _re.match(r'^\s*!(?:бэк|бек|back)\b(?:\s*#?([A-Za-z0-9]{6,}))?\s*$', text, _re.I)
        if m_back:
            if not cfg.get("manual_refund_enabled", False):
                _safe_send(cardinal, chat_id, "Команда возврата выключена у продавца."); return
            if not cfg.get("manual_refund_priority", True) and not cfg.get("auto_refund", False):
                _safe_send(cardinal, chat_id, "Команда !бэк недоступна: приоритет ниже автовозврата, а автовозврат отключён."); return

            oid_arg = m_back.group(1)
            allowed_oids = _list_pending_oids(chat_id)
            if not allowed_oids: return
            target = _find_order_for_back(chat_id, oid_arg)

            if not target:
                if len(allowed_oids) > 1 and not oid_arg:
                    pretty = ", ".join(f"#{o}" for o in allowed_oids)
                    _safe_send(cardinal, chat_id, f"Несколько активных заказов: {pretty}\nУточните: !бэк #ORDERID")
                return
            if not _allowed_stages(target): return
            oid = target.get("order_id")
            if not oid: return

            ok = _auto_refund_order(cardinal, oid, chat_id, reason="Возврат по запросу покупателя (!бэк)")
            if ok:
                q = _q(chat_id)
                try: q.remove(target)
                except ValueError: pass
            return

        pend = _current(chat_id)
        if not pend: return

        if str(pend.get("stage")) == "await_confirm" and text.lower() in {"+", "++", "да", "ок", "ok"}:
            _update_current(chat_id, confirmed=True)
            _do_confirm_send(cardinal, chat_id)
            return

        username = _extract_username_from_text(text)
        if username: _safe_send(cardinal, chat_id, _tpl(chat_id, "username_received", username=username.lstrip("@")))
        else:
            _update_current(chat_id, stage="await_username")
            _safe_send(cardinal, chat_id, _tpl(chat_id, "username_invalid"))
            return

        qty = int(pend.get("qty", 0)) or 50
        if "qty" not in pend:
            cfg_tmp = _get_cfg_for_orders(chat_id)
            enabled_qty = [int(o["qty"]) for o in (cfg_tmp.get("star_lots") or []) if o.get("active")]
            if len(enabled_qty) == 1: qty = enabled_qty[0]

        _update_current(chat_id, qty=int(qty), candidate=username, stage="await_confirm")
        _safe_send(cardinal, chat_id,
            "Проверьте данные:\n"
            f"- Количество: {qty}⭐\n"
            f"- Ник: @{username.lstrip('@')}\n\n"
            'Если всё верно — ответьте "+".\n'
            "Чтобы изменить — пришлите другой тег в формате @username."
        )

    except Exception as e:
        logger.exception(f"new_message_handler error: {e}")

# ========================================================================
# 9. ИНИЦИАЛИЗАЦИЯ ПЛАГИНА
# ========================================================================
_CARDINAL_REF: Optional["Cardinal"] = None

def init_cardinal(cardinal: Cardinal):
    global _CARDINAL_REF
    _CARDINAL_REF = cardinal

    tg = cardinal.telegram
    bot = tg.bot

    try:
        cardinal.add_telegram_commands(UUID, [
            ("autostars", "Открыть панель AutoStars", True),
            ("autostars_help", "Инструкция AutoStars", True),
        ])
    except Exception:
        pass

    logger.info(f"🚀 Плагин {NAME} v{VERSION} запущен.")

    tg.msg_handler(lambda m: bot.send_message(
        m.chat.id, _about_text(), parse_mode="HTML",
        reply_markup=_home_kb(), disable_web_page_preview=True
    ), commands=["autostars"])

    tg.msg_handler(lambda m: bot.send_message(
        m.chat.id, HELP_TEXT, parse_mode="HTML",
        reply_markup=_help_kb(), disable_web_page_preview=True
    ), commands=["autostars_help"])

    # Обновленный фильтр состояний для поддержки ввода кастомного прокси
    tg.msg_handler(
        lambda m: _handle_fsm(m, cardinal),
        func=lambda m: (m.chat.id in _fsm and _fsm[m.chat.id].get("step") in {
            "jwt_api_key","jwt_phone","jwt_wallet_ver","jwt_seed",
            "set_min_balance","set_notify_bal","star_add_qty","star_add_lotid",
            "msg_edit_value","ton_price","frag_rate","presale_text","custom_proxy"
        })
    )

    # Стандартная карточка плагина (Деактивировать / Закрепить / Команды / Удалить / Назад)
    # рисуется самим Cardinal. Здесь открываем только нашу панель: по кнопке «⚙️ Настройки» и «🏠 Домой».
    tg.cbq_handler(
        lambda c: _open_home(bot, c),
        func=lambda c: (
            c.data.startswith(f"{CBT_PLUGIN_SETTINGS}:{UUID}")
            or c.data == f"{UUID}:0"
            or c.data == CBT_HOME
        )
    )

    tg.cbq_handler(lambda c: _open_settings(bot, c), func=lambda c: c.data == CBT_SETTINGS)
    tg.cbq_handler(lambda c: _open_help(bot, c), func=lambda c: c.data == CBT_HELP)
    tg.cbq_handler(lambda c: _open_token(bot, c), func=lambda c: c.data == CBT_TOKEN)
    tg.cbq_handler(lambda c: _open_stars(bot, c), func=lambda c: c.data == CBT_STARS)
    tg.cbq_handler(lambda c: _fsm_cancel(cardinal, c), func=lambda c: c.data == CBT_FSM_CANCEL)
    tg.cbq_handler(lambda c: _toggle_plugin(bot, c), func=lambda c: c.data == CBT_TOGGLE_PLUGIN)
    tg.cbq_handler(lambda c: _select_wallet_version(bot, c), func=lambda c: c.data.startswith(f"{UUID}:ver:"))
    tg.cbq_handler(lambda c: _toggle_lots(bot, c),   func=lambda c: c.data == CBT_TOGGLE_LOTS)
    tg.cbq_handler(lambda c: _toggle_refund(bot, c), func=lambda c: c.data == CBT_TOGGLE_REFUND)
    tg.cbq_handler(lambda c: _toggle_deact(bot, c),  func=lambda c: c.data == CBT_TOGGLE_DEACT)
    tg.cbq_handler(lambda c: _ask_set_min_balance(bot, c), func=lambda c: c.data == CBT_SET_MIN_BAL)
    tg.cbq_handler(lambda c: _ask_set_notify_balance(bot, c), func=lambda c: c.data == CBT_SET_NOTIFY_BAL)
    tg.msg_handler(lambda m: _cancel_cmd(cardinal, m.chat.id), commands=["cancel"])
    tg.cbq_handler(lambda c: _start_create_jwt(bot, c), func=lambda c: c.data == CBT_CREATE_JWT)
    tg.cbq_handler(lambda c: _jwt_confirmed(bot, c),   func=lambda c: c.data == CBT_JWT_CONFIRMED)
    tg.cbq_handler(lambda c: _jwt_resend(bot, c),      func=lambda c: c.data == CBT_JWT_RESEND)
    tg.cbq_handler(lambda c: _del_jwt(bot, c),     func=lambda c: c.data == CBT_DEL_JWT)
    tg.cbq_handler(lambda c: _star_add(bot, c),         func=lambda c: c.data == CBT_STAR_ADD)
    tg.cbq_handler(lambda c: _star_act_all(bot, c),     func=lambda c: c.data == CBT_STAR_ACT_ALL)
    tg.cbq_handler(lambda c: _star_deact_all(bot, c),   func=lambda c: c.data == CBT_STAR_DEACT_ALL)
    tg.cbq_handler(lambda c: _star_toggle(bot, c),      func=lambda c: c.data.startswith(CBT_STAR_TOGGLE_P))
    tg.cbq_handler(lambda c: _star_delete(bot, c),      func=lambda c: c.data.startswith(CBT_STAR_DEL_P))
    tg.cbq_handler(lambda c: _cb_confirm_send(cardinal, c),    func=lambda c: c.data == CBT_CONFIRM_SEND)
    tg.cbq_handler(lambda c: _cb_change_username(cardinal, c), func=lambda c: c.data == CBT_CHANGE_USERNAME)
    tg.cbq_handler(lambda c: _cb_cancel_flow(cardinal, c),     func=lambda c: c.data == CBT_CANCEL_FLOW)
    tg.cbq_handler(lambda c: _open_messages(bot, c), func=lambda c: c.data == CBT_MESSAGES)
    tg.cbq_handler(lambda c: _msg_edit_start(bot, c), func=lambda c: c.data.startswith(CBT_MSG_EDIT_P))
    tg.cbq_handler(lambda c: _msg_reset(bot, c), func=lambda c: c.data.startswith(CBT_MSG_RESET_P))
    tg.cbq_handler(lambda c: _toggle_manual_refund(bot, c), func=lambda c: c.data == CBT_TOGGLE_MANUAL_REFUND)
    tg.cbq_handler(lambda c: _toggle_back_priority(bot, c), func=lambda c: c.data == CBT_TOGGLE_BACK_PRIORITY)

    tg.cbq_handler(lambda c: _open_finance(bot, c), func=lambda c: c.data == CBT_FINANCE)
    tg.cbq_handler(lambda c: _ask_price(bot, c, "ton_price"), func=lambda c: c.data == CBT_SET_TON_PRICE)
    tg.cbq_handler(lambda c: _ask_price(bot, c, "frag_rate"), func=lambda c: c.data == CBT_SET_FRAG_RATE)

    tg.cbq_handler(lambda c: _open_presale(bot, c), func=lambda c: c.data == CBT_PRESALE_MENU)
    tg.cbq_handler(lambda c: _toggle_presale(bot, c), func=lambda c: c.data == CBT_TOGGLE_PRESALE)
    tg.cbq_handler(lambda c: _ask_presale_text(bot, c), func=lambda c: c.data == CBT_SET_PRESALE_TEXT)

    # Регистрация кнопок меню Прокси
    tg.cbq_handler(lambda c: _open_proxy(bot, c), func=lambda c: c.data == CBT_PROXY_MENU)
    tg.cbq_handler(lambda c: _set_proxy_mode(bot, c), func=lambda c: c.data.startswith(CBT_SET_PROXY_MODE))
    tg.cbq_handler(lambda c: _ask_custom_proxy(bot, c), func=lambda c: c.data == CBT_SET_CUSTOM_PROXY)
    tg.cbq_handler(lambda c: _check_proxy_cmd(cardinal, c), func=lambda c: c.data == CBT_CHECK_PROXY)

BIND_TO_PRE_INIT    = [init_cardinal]
BIND_TO_NEW_MESSAGE = [new_message_handler]
try:
    BIND_TO_NEW_ORDER = [new_order_handler]
except Exception:
    pass
BIND_TO_DELETE = None
