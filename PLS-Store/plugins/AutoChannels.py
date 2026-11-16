# -*- coding: utf-8 -*-
"""
Плагин для FunPayCardinal: заранее создаёт Telegram-каналы и крутит на них
подписчиков про запас ("склад"), а при оплате лота сразу выдаёт покупателю
уже готовые каналы с нужным числом подписчиков — без ожидания.
Количество каналов = order.amount (сколько единиц купил покупатель).

Обновлённая сборка AutoChannels 5.0.0: PLS Store, привязка по ID лота,
проверка оплаченного заказа/покупателя/чата и журнал передачи. Руководство
поставляется как INSTALL.txt; старые ожидания без данных заказа требуют
ручной проверки, настройки/тарифы/склад сохранены.
"""
import asyncio
import configparser
import io
import json
import os
import re
import threading
import time
import traceback

import qrcode

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton as _TelegramButton
import secrets
import html

def InlineKeyboardButton(text, *args, **kwargs):
    return _TelegramButton(text, *args, **kwargs)


def _pls_panel(text):
    if text.startswith("<b>PLS Store"):
        return text
    return "<b>PLS Store · AutoChannels</b>\n━━━━━━━━━━━━━━━━━━━━\n\n" + text




from FunPayAPI.updater.events import NewOrderEvent

from telethon import TelegramClient, functions
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import PeerChannel
from telethon.tl.types import ChatAdminRights
from telethon.tl.tlobject import TLObject, TLRequest
from telethon.errors import UserPrivacyRestrictedError, UserNotParticipantError, SessionPasswordNeededError, FloodWaitError
from telethon.password import compute_check
import requests


# channels.editCreator#8f38cd1f channel:InputChannel user_id:InputUser password:InputCheckPasswordSRP = Updates;
# В Telethon 1.45+ метод выпилили из сгенерированного TL, хотя Telegram API его принимает.
class _EditCreatorRequest(TLRequest):
    CONSTRUCTOR_ID = 0x8F38CD1F
    SUBCLASS_OF_ID = 0x8AF52AAC  # Updates

    def __init__(self, channel, user_id, password):
        self.channel = channel
        self.user_id = user_id
        self.password = password

    def to_dict(self):
        return {
            "_": "EditCreatorRequest",
            "channel": self.channel.to_dict() if isinstance(self.channel, TLObject) else self.channel,
            "user_id": self.user_id.to_dict() if isinstance(self.user_id, TLObject) else self.user_id,
            "password": self.password.to_dict() if isinstance(self.password, TLObject) else self.password,
        }

    def _bytes(self):
        assert isinstance(self.channel, TLObject) and isinstance(self.user_id, TLObject) and isinstance(self.password, TLObject)
        return b"".join((
            b"\x1f\xcd\x38\x8f",  # little-endian 0x8f38cd1f
            self.channel._bytes(),
            self.user_id._bytes(),
            self.password._bytes(),
        ))


def _get_edit_creator_request_cls():
    """Штатный EditCreatorRequest из Telethon, иначе наш raw-класс."""
    try:
        from telethon.tl.functions.channels import EditCreatorRequest as _ECR
        return _ECR
    except ImportError:
        pass
    try:
        import telethon.tl.functions.channels as _ch
        cls = getattr(_ch, "EditCreatorRequest", None)
        if cls is not None:
            return cls
    except Exception:
        pass
    try:
        cls = getattr(getattr(functions, "channels", None), "EditCreatorRequest", None)
        if cls is not None:
            return cls
    except Exception:
        pass
    return _EditCreatorRequest


# PLS_LICENSE_GUARD_V1: only key, FunPay account id and product leave this client.
import threading as _pls_threading
import time as _pls_time
from pathlib import Path as _PlsPath
import base64 as _pls_base64
import hashlib as _pls_hashlib
import hmac as _pls_hmac
import math as _pls_math
from requests.exceptions import RequestException as _PlsNetworkError

_PLS_ENDPOINT = "__PLS_LICENSE_ENDPOINT__"
_PLS_LOCK = _pls_threading.Lock()
_PLS_CHECKED = 0.0
_PLS_ACCOUNT = None

_PLS_PUBLIC_KEY = "__PLS_LEASE_PUBLIC_KEY__"
_PLS_GRACE_SECONDS = 12 * 60 * 60
_PLS_KEY_FP = None
_PLS_VALID_UNTIL = 0
_PLS_MODE = 'none'
_PLS_LAST_WALL = 0


def _pls_clear_memory():
    global _PLS_CHECKED, _PLS_ACCOUNT, _PLS_KEY_FP, _PLS_VALID_UNTIL, _PLS_MODE, _PLS_LAST_WALL
    _PLS_CHECKED = 0.0
    _PLS_ACCOUNT = None
    _PLS_KEY_FP = None
    _PLS_VALID_UNTIL = 0
    _PLS_MODE = "none"
    _PLS_LAST_WALL = 0


def _pls_store_status(data):
    path = _PlsPath("storage/plugins/AutoChannels/license_status.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output)
        os.chmod(str(temporary), 0o600)
        os.replace(str(temporary), str(path))
    finally:
        temporary.unlink(missing_ok=True)


def _pls_verify_approval(pack, fp, fid, now):
    try:
        approval = pack["approval"]
        signature = pack["signature"]
        if not isinstance(approval, dict) or set(approval) != {
            "v",
            "product",
            "fingerprint",
            "funpay_id",
            "issued_at",
            "expires_at",
        }:
            return False
        if (
            approval["v"] != 1
            or approval["product"] != "autochannels"
            or approval["fingerprint"] != fp
            or approval["funpay_id"] != fid
        ):
            return False
        issued = approval["issued_at"]
        expires = approval["expires_at"]
        if (
            type(issued) is not int
            or type(expires) is not int
            or expires - issued != _PLS_GRACE_SECONDS
            or issued > now + 300
            or now >= expires
        ):
            return False
        if (
            not isinstance(signature, str)
            or len(signature) > 1024
            or not isinstance(_PLS_PUBLIC_KEY, dict)
        ):
            return False
        n = int(_PLS_PUBLIC_KEY["n"])
        e = int(_PLS_PUBLIC_KEY["e"])
        if not 2048 <= n.bit_length() <= 4096 or e != 65537:
            return False
        raw = _pls_base64.b64decode(signature, validate=True)
        size = (n.bit_length() + 7) // 8
        if len(raw) != size or int.from_bytes(raw, "big") >= n:
            return False
        body = json.dumps(
            approval, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        digest = (
            bytes.fromhex("3031300d060960864801650304020105000420")
            + _pls_hashlib.sha256(body).digest()
        )
        expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
        actual = pow(int.from_bytes(raw, "big"), e, n).to_bytes(size, "big")
        return _pls_hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def _pls_remember(fid, fp, now, expires, mode):
    global _PLS_CHECKED, _PLS_ACCOUNT, _PLS_KEY_FP, _PLS_VALID_UNTIL, _PLS_MODE, _PLS_LAST_WALL
    _PLS_ACCOUNT = fid
    _PLS_KEY_FP = fp
    _PLS_CHECKED = _pls_time.monotonic()
    _PLS_VALID_UNTIL = expires
    _PLS_MODE = mode
    _PLS_LAST_WALL = now


def _pls_deny(fp, fid):
    _pls_clear_memory()
    try:
        _pls_store_status(dict(denied=True, fingerprint=fp, funpay_id=fid))
    except OSError:
        try:
            _PlsPath("storage/plugins/AutoChannels/license_status.json").unlink(
                missing_ok=True
            )
        except OSError:
            pass


def _pls_cached_approval(fp, fid, now):
    try:
        pack = json.loads(
            _PlsPath("storage/plugins/AutoChannels/license_status.json").read_text(
                encoding="utf-8"
            )
        )
        last_seen = pack["last_seen"]
        if (
            pack.get("denied")
            or pack.get("endpoint") != _PLS_ENDPOINT
            or type(last_seen) not in (int, float)
            or not _pls_math.isfinite(last_seen)
            or now < last_seen - 300
            or not _pls_verify_approval(pack, fp, fid, now)
        ):
            return None
        pack["last_seen"] = max(last_seen, now)
        # A read-only cache cannot provide a reliable persisted denial/clock marker.
        _pls_store_status(pack)
        return pack
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _pls_require_license(cardinal):
    global _PLS_LAST_WALL
    fid = str(getattr(cardinal.account, "id", "") or "")
    if not fid.isascii() or not fid.isdigit() or int(fid) <= 0:
        raise RuntimeError(
            "AutoChannels: FunPay ID ещё недоступен. Дождитесь входа Cardinal в FunPay."
        )
    if not _PLS_ENDPOINT.startswith("https://"):
        raise RuntimeError(
            "AutoChannels: адрес проверки не настроен. Скачайте плагин через раздел «Лицензии» в PLS Store."
        )
    with _PLS_LOCK:
        try:
            key = json.loads(
                _PlsPath("storage/plugins/AutoChannels/license.json").read_text(
                    encoding="utf-8"
                )
            )["key"]
            if not isinstance(key, str) or not key.startswith("PLS-"):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError):
            _pls_clear_memory()
            raise RuntimeError(
                "AutoChannels: введите лицензионный ключ в /autochannels."
            ) from None
        fp = _pls_hashlib.sha256(key.encode()).hexdigest()
        now = _pls_time.time()
        if (
            fid == _PLS_ACCOUNT
            and fp == _PLS_KEY_FP
            and _pls_time.monotonic() - _PLS_CHECKED < 60
            and now < _PLS_VALID_UNTIL
            and now >= _PLS_LAST_WALL - 300
        ):
            _PLS_LAST_WALL = max(now, _PLS_LAST_WALL)
            return
        transient = False
        pack = None
        try:
            response = requests.post(
                _PLS_ENDPOINT,
                json={"key": key, "funpay_id": fid, "product": "autochannels"},
                timeout=(5, 10),
                allow_redirects=False,
            )
        except (_PlsNetworkError, OSError):
            transient = True
        else:
            if (
                response.status_code in (404, 408, 429)
                or 500 <= response.status_code <= 599
            ):
                transient = True
            elif response.status_code == 200:
                try:
                    pack = response.json()
                except ValueError:
                    transient = True
                if (
                    not transient
                    and isinstance(pack, dict)
                    and pack.get("valid") is True
                    and _pls_verify_approval(pack, fp, fid, now)
                ):
                    state = dict(
                        approval=pack["approval"],
                        signature=pack["signature"],
                        endpoint=_PLS_ENDPOINT,
                        last_seen=now,
                    )
                    try:
                        _pls_store_status(state)
                    except OSError:
                        pass
                    _pls_remember(
                        fid, fp, now, pack["approval"]["expires_at"], "online"
                    )
                    return
            if not transient:
                # Includes explicit rejection, wrong account, and an invalid signature.
                # Such replies invalidate offline approval rather than enabling grace.
                _pls_deny(fp, fid)
                raise RuntimeError(
                    "AutoChannels: сервер не подтвердил лицензию. Проверьте статус, ключ и FunPay ID. Льготный период при отказе не действует."
                )
        if transient:
            state = _pls_cached_approval(fp, fid, now)
            if state is not None:
                _pls_remember(fid, fp, now, state["approval"]["expires_at"], "grace")
                return
        _pls_clear_memory()
        raise RuntimeError(
            "AutoChannels: сервер проверки недоступен, а действующего подтверждения за последние 12 часов нет. Выдача каналов остановлена."
        )



# Optional delivery dependency. Import only AFTER registering the control panel.
Client = None
ChatType = None



NAME = "AutoChannels"
VERSION = "5.0.0"
DESCRIPTION = (
    "Склад раскрученных Telegram-каналов и мгновенная выдача после оплаты. "
    "Привязка по ID лота FunPay, проверка покупателя и лицензия PLS Store."
)
CREDITS = "@uservkru"
AUTHOR_URL = "https://t.me/uservkru"
UUID = "d8d716e1-25f5-422a-aab7-8253a76dcb1a"  # новый, не конфликтует ни с одной из старых копий
SETTINGS_PAGE = True

CONFIG_PATH = "storage/plugins/ch_autodeliv_v3_fresh.cfg"
STOCK_PATH = "storage/plugins/ch_autodeliv_v3_fresh_stock.json"
PENDING_PATH = "storage/plugins/AutoChannels/orders.json"  # заказы, ждущие username покупателя
LOTS_PATH = "storage/plugins/ch_autodeliv_v3_fresh_lots.json"  # список лотов/ключевых слов -> тариф
TIERS_PATH = "storage/plugins/ch_autodeliv_v3_fresh_tiers.json"  # тарифы: разное кол-во подписчиков под разные лоты
LOG_PATH = "storage/plugins/ch_autodeliv_v4.log"  # свой лог плагина — не теряется среди общего лога Cardinal
LOG_MAX_LINES = 2000  # не даём файлу расти бесконечно

_stock_lock = threading.Lock()
_flood_wait_until = 0.0  # unix-время, до которого нельзя создавать каналы (после FloodWaitError)
_restock_running = threading.Lock()  # гарантирует, что пополнение склада не идёт в двух местах сразу


# ---------------------- Конфиг ----------------------

DEFAULTS = {
    "tg_api_id": "",
    "tg_api_hash": "",
    "tg_2fa_password": "",
    "tg_folder_id": "",      # ID папки Telegram, куда класть новые каналы (пусто = не раскладывать)
    "tg_folder_title": "",   # просто для отображения в меню
    "tg_session_created_at": "",  # unix-время последнего успешного входа (для правила "сессии 24ч")
    "smm_api_url": "",
    "smm_api_key": "",
    "channel_title_template": "Channel {n}",
    "restock_check_minutes": "5",  # как часто проверять склад
    "faq_enabled": "1",
    "faq_json": "",  # пусто = используются FAQ_DEFAULTS
}


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["settings"] = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cfg.read(CONFIG_PATH, encoding="utf-8")
    else:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        save_config(cfg)
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)


# ---------------------- Склад каналов (JSON-файл) ----------------------
# Статусы записи: "boosting" (крутится) -> "ready" (готов, ждёт покупателя)
#                 -> "sold" (выдан)

_log_lock = threading.Lock()


def log(message: str) -> None:
    """Пишет строку в собственный лог-файл плагина, с меткой времени.
    Не зависит от общего лога Cardinal — здесь видно именно то, что делает
    этот плагин, шаг за шагом, включая случаи, когда он решает ничего не делать."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with _log_lock:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            return
        # Не даём файлу расти бесконечно: обрезаем до последних LOG_MAX_LINES строк
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > LOG_MAX_LINES:
                with open(LOG_PATH, "w", encoding="utf-8") as f:
                    f.writelines(lines[-LOG_MAX_LINES:])
        except Exception:
            pass


def log_exc(prefix: str, e: BaseException) -> None:
    """Логирует исключение вместе с полным traceback — удобно копировать и присылать."""
    tb = traceback.format_exc()
    log(f"{prefix}: {type(e).__name__}: {e}")
    for line in tb.strip().splitlines():
        log(f"  | {line}")


def read_log_tail(n: int = 60) -> str:
    if not os.path.exists(LOG_PATH):
        return "Лог пока пуст."
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[-n:]) or "Лог пока пуст."


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_stock() -> list:
    return _load_json(STOCK_PATH, [])


def save_stock(stock: list) -> None:
    _save_json(STOCK_PATH, stock)


def load_pending() -> dict:
    return _load_json(PENDING_PATH, {})


def save_pending(pending: dict) -> None:
    _save_json(PENDING_PATH, pending)


def load_lots() -> list:
    """Привязки {lot_id, tier_id, token}; старые keyword-записи не используются."""
    return _load_json(LOTS_PATH, [])


def save_lots(lots: list) -> None:
    _save_json(LOTS_PATH, lots)


def match_lot_tier(order):
    explicit = {str(getattr(order, field)) for field in ('lot_id', 'offer_id', 'funpay_lot_id') if getattr(order, field, None) is not None}
    matches = []
    for lot in load_lots():
        if not isinstance(lot, dict) or not lot.get('lot_id') or not lot.get('token'):
            continue
        marker = '#PLSCH:' + lot['token']
        full = str(getattr(order, 'full_description', '') or '')
        if explicit:
            valid = explicit == {str(lot['lot_id'])}
        else:
            valid = marker in full
        if valid:
            matches.append(lot)
    return matches[0] if len(matches) == 1 else None



def load_tiers() -> list:
    """Тарифы: [{"id": "...", "name": "...", "service_id": "...", "quantity": "...", "stock_target": "..."}]"""
    return _load_json(TIERS_PATH, [])


def save_tiers(tiers: list) -> None:
    _save_json(TIERS_PATH, tiers)


def get_tier(tier_id: str):
    for t in load_tiers():
        if t["id"] == tier_id:
            return t
    return None


# ---------------------- SMM-панель ----------------------

def smm_request(cfg, action: str, **params) -> dict:
    if action not in {"status", "balance", "services"}:
        _pls_require_license(_CARDINAL)
    s = cfg["settings"]
    data = {"key": s["smm_api_key"], "action": action, **params}
    r = requests.post(s["smm_api_url"], data=data, timeout=30, allow_redirects=False)
    r.raise_for_status()
    result = r.json()
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Ошибка SMM-панели: {result['error']}")
    return result


# ---------------------- Telegram (Telethon) ----------------------

async def _create_channel(cfg, title: str) -> tuple:
    """Создаёт приватный канал и возвращает (channel_id, invite_link).
    Публичный @username намеренно НЕ выставляется: именно эта операция
    (UpdateUsernameRequest) оказалась самой частой причиной FloodWait и
    упирается в лимит 10 публичных каналов на аккаунт. Инвайт-ссылки
    большинство SMM-панелей принимают так же, как и публичные ссылки."""
    await asyncio.to_thread(_pls_require_license, _CARDINAL)
    s = cfg["settings"]
    api_id, api_hash = int(s["tg_api_id"]), s["tg_api_hash"]
    async with TelegramClient("session_channel", api_id, api_hash) as client:
        result = await client(CreateChannelRequest(title=title, about="", broadcast=True))
        channel = result.chats[0]

        invite = await client(ExportChatInviteRequest(peer=channel))
        link = invite.link

        folder_id = s.get("tg_folder_id", "")
        if folder_id:
            try:
                await _add_to_folder(client, int(folder_id), channel)
            except Exception:
                pass  # не срываем создание канала из-за проблемы с папкой

        return channel.id, link


async def _add_to_folder(client, folder_id: int, channel) -> None:
    """Добавляет канал в существующую папку Telegram (по её числовому ID)."""
    filters = await client(functions.messages.GetDialogFiltersRequest())
    target = None
    for f in filters.filters:
        if getattr(f, "id", None) == folder_id:
            target = f
            break
    if target is None:
        return
    input_peer = await client.get_input_entity(channel)
    if not any(getattr(p, "channel_id", None) == input_peer.channel_id for p in target.include_peers):
        target.include_peers.append(input_peer)
        await client(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=target))


async def _list_folders(cfg) -> list:
    """Возвращает [(id, title), ...] существующих папок Telegram на аккаунте."""
    s = cfg["settings"]
    async with TelegramClient("session_channel", int(s["tg_api_id"]), s["tg_api_hash"]) as client:
        result = await client(functions.messages.GetDialogFiltersRequest())
        out = []
        for f in result.filters:
            fid = getattr(f, "id", None)
            title = getattr(f, "title", None)
            title_text = getattr(title, "text", title)  # в новых версиях title — объект с .text
            if fid and title_text:
                out.append((fid, title_text))
        return out


async def _transfer_ownership(cfg, channel_id: int, new_owner_username: str) -> None:
    s = cfg["settings"]
    api_id, api_hash = int(s["tg_api_id"]), s["tg_api_hash"]
    log(f"TRANSFER_TG step=connect channel_id={channel_id} user=@{new_owner_username}")
    async with TelegramClient("session_channel", api_id, api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram-сессия не авторизована. Откройте настройки плагина → "
                "«🔑 Привязать Telegram-аккаунт» и отсканируйте QR заново."
            )
        log(f"TRANSFER_TG step=get_channel channel_id={channel_id}")
        channel = await client.get_entity(PeerChannel(channel_id))
        log(f"TRANSFER_TG step=get_user @{new_owner_username}")
        user = await client.get_entity(new_owner_username)
        try:
            await client.get_permissions(channel, user)
            log(f"TRANSFER_TG step=already_participant @{new_owner_username}")
        except UserNotParticipantError:
            log(f"TRANSFER_TG step=invite @{new_owner_username}")
            try:
                await client(functions.channels.InviteToChannelRequest(channel, [user]))
            except UserPrivacyRestrictedError:
                raise RuntimeError(
                    "Настройки приватности покупателя не позволили добавить его в канал. "
                    "Попросите его сначала самому вступить по ссылке, потом пришлите username ещё раз."
                )
        rights = ChatAdminRights(
            change_info=True, post_messages=True, edit_messages=True,
            delete_messages=True, ban_users=True, invite_users=True,
            pin_messages=True, add_admins=True, manage_call=True, anonymous=False,
        )
        log(f"TRANSFER_TG step=edit_admin @{new_owner_username}")
        await client(functions.channels.EditAdminRequest(channel, user, rights, "owner"))
        if not s.get("tg_2fa_password"):
            raise RuntimeError(
                "В настройках плагина не заполнен пароль 2FA (облачный пароль). "
                "Без него Telegram не даст передать права владельца."
            )
        EditCreatorRequest = _get_edit_creator_request_cls()
        log(f"TRANSFER_TG step=edit_creator @{new_owner_username} cls={EditCreatorRequest.__name__}")
        pwd_info = await client(functions.account.GetPasswordRequest())
        srp_check = compute_check(pwd_info, s["tg_2fa_password"])
        # Нужны InputChannel / InputUser / InputCheckPasswordSRP — get_input_entity даёт первые два
        channel_input = await client.get_input_entity(channel)
        user_input = await client.get_input_entity(user)
        await asyncio.to_thread(_pls_require_license, _CARDINAL)
        await client(EditCreatorRequest(channel=channel_input, user_id=user_input, password=srp_check))
        log(f"TRANSFER_TG step=done @{new_owner_username} channel_id={channel_id}")


# ---------------------- Авторизация Telegram-аккаунта: вход по QR-коду ----------------------
# Ввод кода из SMS/приложения ЧЕРЕЗ ЧАТ В TELEGRAM не работает: Telegram сам
# блокирует такой вход как подозрение на фишинг (код "засвечен" внутри Telegram).
# Поэтому логинимся через QR: сканируете картинку камерой в приложении — никакой
# код никуда не вводится, и защита не срабатывает.

async def _do_qr_login(cfg, bot, chat_id) -> str:
    s = cfg["settings"]
    client = TelegramClient("session_channel", int(s["tg_api_id"]), s["tg_api_hash"])
    await client.connect()
    if await client.is_user_authorized():
        await client.disconnect()
        return "already"

    qr_login = await client.qr_login()
    for _ in range(4):  # несколько попыток, пока не истечёт общее время ожидания
        buf = io.BytesIO()
        qrcode.make(qr_login.url).save(buf, format="PNG")
        buf.seek(0)
        bot.send_photo(
            chat_id, buf,
            caption="Откройте Telegram на телефоне (на том аккаунте, который создаёт каналы) -> "
                    "Настройки -> Устройства -> Подключить устройство -> наведите камеру на этот QR. "
                    "Код действует около 30 секунд.",
        )
        try:
            await qr_login.wait(30)
            await client.disconnect()
            return "ok"
        except SessionPasswordNeededError:
            await client.disconnect()
            return "need_password"
        except asyncio.TimeoutError:
            await qr_login.recreate()
            continue
    await client.disconnect()
    return "timeout"


async def _qr_login_password(cfg, password: str) -> None:
    s = cfg["settings"]
    client = TelegramClient("session_channel", int(s["tg_api_id"]), s["tg_api_hash"])
    await client.connect()
    await client.sign_in(password=password)
    await client.disconnect()


def _record_session_start(cfg) -> None:
    cfg["settings"]["tg_session_created_at"] = str(time.time())
    save_config(cfg)


def _session_ready_for_transfer(cfg) -> tuple:
    """(готова_ли_сессия, сколько_секунд_ещё_ждать).
    Если время входа неизвестно (например, после переноса плагина в новый файл,
    когда сама Telegram-сессия старая, а счётчик начат заново) — не блокируем
    заранее, а полагаемся на реальную ошибку Telegram, если окажется рано."""
    raw = cfg["settings"].get("tg_session_created_at", "")
    if not raw:
        return True, 0
    remaining = 24 * 3600 - (time.time() - float(raw))
    return remaining <= 0, max(0, int(remaining))


def reset_session(c, call) -> None:
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    removed = []
    for suffix in ("", "-journal"):
        path = f"session_channel.session{suffix}"
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    cfg = load_config()
    cfg["settings"]["tg_session_created_at"] = ""
    save_config(cfg)
    if removed:
        bot.send_message(call.message.chat.id, f"Удалено: {', '.join(removed)}. Теперь нажмите «🔑 Привязать Telegram-аккаунт» заново.")
    else:
        bot.send_message(call.message.chat.id, "Файла сессии не найдено, можно сразу привязывать аккаунт заново.")


def start_login_flow(c, call) -> None:
    bot = _PlsBotPanel(c)
    cfg = load_config()
    s = cfg["settings"]
    bot.answer_callback_query(call.id)
    if not (s["tg_api_id"] and s["tg_api_hash"]):
        bot.send_message(call.message.chat.id, "Сначала заполните API ID и API Hash в разделе «Telegram-аккаунт».")
        return
    bot.send_message(call.message.chat.id, "Генерирую QR-код для входа...")
    threading.Thread(target=_run_qr_login, args=(c, cfg, call.message.chat.id), daemon=True).start()


def _run_qr_login(c, cfg, chat_id) -> None:
    bot = _PlsBotPanel(c)
    try:
        result = asyncio.run(_do_qr_login(cfg, bot, chat_id))
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка входа по QR: {e}")
        return

    if result in ("ok", "already"):
        _record_session_start(cfg)
        bot.send_message(
            chat_id,
            "✅ Аккаунт привязан, сессия сохранена. Можно пополнять склад.\n\n"
            "⚠️ Важно: сама Telegram запрещает передавать права владельца канала "
            "с сессии младше 24 часов (это защита от взлома, а не баг плагина). "
            "Создавать и крутить каналы можно сразу, а вот ПЕРЕДАЧА ПРАВ покупателям "
            "заработает только через сутки после этого входа."
        )
    elif result == "need_password":
        msg = bot.send_message(
            chat_id,
            "QR отсканирован. На аккаунте включена двухшаговая проверка — пришлите облачный пароль (2FA):",
        )
        bot.register_next_step_handler(msg, _login_step_password, c, cfg)
    elif result == "timeout":
        bot.send_message(chat_id, "QR не был отсканирован вовремя. Нажмите «🔑 Привязать Telegram-аккаунт» ещё раз.")


def _login_step_password(message, c, cfg) -> None:
    bot = _PlsBotPanel(c)
    password = message.text.strip()
    try:
        asyncio.run(_qr_login_password(cfg, password))
    except Exception as e:
        bot.send_message(message.chat.id, f"Не удалось войти с паролем: {e}")
        return
    _record_session_start(cfg)
    bot.send_message(
        message.chat.id,
        "✅ Аккаунт привязан, сессия сохранена. Можно пополнять склад.\n\n"
        "⚠️ Важно: Telegram запрещает передавать права владельца канала с сессии "
        "младше 24 часов. Передача прав покупателям заработает только через сутки после этого входа."
    )


# ---------------------- Пополнение склада ----------------------

def _next_channel_slug() -> str:
    # простой счётчик на основе времени, чтобы username'ы не повторялись
    return f"ch_{int(time.time() * 1000)}"


def create_and_boost_one(cfg, tier: dict) -> None:
    """Создаёт один канал под конкретный тариф, ставит заказ на накрутку и
    кладёт запись на склад со статусом 'boosting'."""
    global _flood_wait_until
    now = time.time()
    if now < _flood_wait_until:
        raise RuntimeError(
            f"Telegram временно ограничил создание каналов (FloodWait), "
            f"осталось ждать ~{int(_flood_wait_until - now)} сек."
        )

    s = cfg["settings"]
    slug = _next_channel_slug()
    title = s["channel_title_template"].format(n=slug[-5:])
    try:
        channel_id, link = asyncio.run(_create_channel(cfg, title))
    except FloodWaitError as e:
        _flood_wait_until = time.time() + e.seconds
        raise RuntimeError(f"Telegram ограничил действие на {e.seconds} сек. (FloodWait). Повторите позже.")

    order = smm_request(cfg, "add", service=int(tier["service_id"]), link=link, quantity=int(tier["quantity"]))
    smm_order_id = order.get("order") or order.get("order_id") or order.get("id")
    if smm_order_id is None:
        raise RuntimeError(f"Панель не вернула номер заказа, получено: {order}")

    with _stock_lock:
        stock = load_stock()
        stock.append({
            "channel_id": channel_id,
            "link": link,
            "smm_order_id": smm_order_id,
            "status": "boosting",
            "tier_id": tier["id"],
            "created_at": time.time(),
        })
        save_stock(stock)


def refresh_stock_statuses(cfg) -> None:
    """Проверяет накрутку у каналов со статусом 'boosting' и переводит
    готовые в 'ready'."""
    with _stock_lock:
        stock = load_stock()
        changed = False
        for item in stock:
            if item["status"] != "boosting":
                continue
            try:
                status = smm_request(cfg, "status", order=item["smm_order_id"])
            except Exception:
                continue
            if status.get("status") in ("Completed", "Partial"):
                item["status"] = "ready"
                changed = True
        if changed:
            save_stock(stock)


def take_ready_channel(tier_id: str):
    """Атомарно забирает один готовый канал нужного тарифа (помечает как 'sold')."""
    items = take_ready_channels(tier_id, 1)
    return items[0] if items else None


def take_ready_channels(tier_id: str, count: int) -> list:
    """Атомарно забирает до count готовых каналов нужного тарифа (помечает как 'sold')."""
    if count <= 0:
        return []
    taken = []
    with _stock_lock:
        stock = load_stock()
        for item in stock:
            if len(taken) >= count:
                break
            if item["status"] == "ready" and item.get("tier_id") == tier_id:
                item["status"] = "sold"
                taken.append(item)
        if taken:
            save_stock(stock)
    return taken


def count_ready(stock: list, tier_id: str) -> int:
    return sum(1 for i in stock if i.get("tier_id") == tier_id and i["status"] in ("ready", "boosting"))


def get_order_quantity(order) -> int:
    """Количество единиц в заказе FunPay. По умолчанию 1, если поле отсутствует/битое."""
    for attr in ("amount", "quantity", "count"):
        val = getattr(order, attr, None)
        if val is None:
            continue
        try:
            n = int(val)
            if n >= 1:
                return min(n, 50)  # жёсткий потолок на случай мусорных данных
        except (TypeError, ValueError):
            continue
    # запасной парсинг из описания: «x2», «× 3», «2 шт»
    desc = (getattr(order, "description", None) or "") + " " + (getattr(order, "short_description", None) or "")
    m = re.search(r"(?:x|×|\*)\s*(\d{1,2})\b", desc, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if n >= 1:
            return min(n, 50)
    m = re.search(r"\b(\d{1,2})\s*(?:шт|pcs|pc)\b", desc, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if n >= 1:
            return min(n, 50)
    return 1


def _ensure_stock(cfg, status_cb=None) -> int:
    """Пополняет склад до целей всех тарифов. Возвращает число созданных каналов.
    Вызывается и из фонового цикла, и из кнопки — обязательно под _restock_running,
    чтобы два места не создавали каналы одновременно."""
    s = cfg["settings"]
    tiers = load_tiers()
    if not all([s["tg_api_id"], s["tg_api_hash"], s["smm_api_url"], s["smm_api_key"]]) or not tiers:
        return 0
    if time.time() < _flood_wait_until:
        return 0

    refresh_stock_statuses(cfg)
    created = 0
    for tier in tiers:
        stock = load_stock()
        missing = int(tier["stock_target"]) - count_ready(stock, tier["id"])
        for _ in range(max(0, missing)):
            try:
                create_and_boost_one(cfg, tier)
                created += 1
            except RuntimeError as e:
                if status_cb:
                    status_cb(f"[{tier['name']}] {e}")
                break
            time.sleep(30)  # пауза между созданием каналов, чтобы не словить FloodWait
    return created


def restock_loop(cardinal) -> None:
    """Фоновый поток: следит, чтобы на складе каждого тарифа было достаточно каналов."""
    while True:
        try:
            cfg = load_config()
            s = cfg["settings"]
            if _restock_running.acquire(blocking=False):
                try:
                    _ensure_stock(cfg)
                finally:
                    _restock_running.release()
            interval = max(1, int(s["restock_check_minutes"])) * 60
        except Exception:
            traceback.print_exc()
            interval = 60
        time.sleep(interval)


def start_restock_thread(cardinal) -> None:
    threading.Thread(target=restock_loop, args=(cardinal,), daemon=True).start()


# ---------------------- Обработчики событий Cardinal ----------------------

def resolve_message_chat_id(cardinal, order) -> list:
    """Собирает ВСЕ возможные ключи chat_id для pending.

    Проблема: order.chat_id часто приходит как строка вида «users-13540460-21632075»,
    а в NewMessageEvent message.chat_id — числовой ID (например 291582642).
    Cardinal сам решает это через get_chat_by_name(buyer_username).
    Мы сохраняем pending под всеми известными ключами, чтобы lookup всегда находил запись.
    """
    keys = []
    raw = getattr(order, "chat_id", None)
    if raw is not None:
        keys.append(str(raw))

    buyer = getattr(order, "buyer_username", None) or getattr(order, "buyer", None)
    if buyer:
        try:
            chat = cardinal.account.get_chat_by_name(buyer)
            if chat is not None:
                cid = getattr(chat, "id", None)
                if cid is not None:
                    keys.append(str(cid))
                # на всякий случай — node-строка, если есть
                node = getattr(chat, "node", None) or getattr(chat, "name", None)
                if node and str(node) not in keys:
                    keys.append(str(node))
        except Exception as e:
            log(f"resolve_message_chat_id get_chat_by_name({buyer!r}) failed: {e}")

    buyer_id = getattr(order, "buyer_id", None)
    if buyer_id is not None:
        keys.append(str(buyer_id))

    # уникальные, с сохранением порядка
    seen = set()
    out = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _normalize_channel_ids(value) -> list:
    """Приводит значение pending к списку channel_id (int).
    Поддерживает старый формат (одно число) и новый (список)."""
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for v in value:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out
    try:
        return [int(value)]
    except (TypeError, ValueError):
        return []


_CARDINAL = None
_order_lock = threading.RLock()
_transfer_lock = threading.Lock()


def _read_orders():
    if not os.path.exists(PENDING_PATH):
        return {}
    with open(PENDING_PATH, encoding='utf-8') as source:
        records = json.load(source)
    if not isinstance(records, dict) or any(not isinstance(row, dict) for row in records.values()):
        raise RuntimeError('Повреждён журнал AutoChannels; требуется проверка продавца.')
    return records


def _write_orders(records):
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    temporary = PENDING_PATH + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as output:
        json.dump(records, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, PENDING_PATH)


def pending_put(channel_ids, *keys, order_id=None):
    if not order_id:
        raise ValueError('Требуется проверенный заказ')
    with _order_lock:
        records = _read_orders()
        row = records[str(order_id)]
        for cid in _normalize_channel_ids(channel_ids):
            if cid not in row['channels']:
                row['channels'].append(cid)
        if row['status'] in {'preparing', 'completed', 'waiting_username'}:
            row['status'] = 'waiting_username'
        _write_orders(records)


def _message_chat(msg):
    value = getattr(msg, 'chat_id', None)
    if value is None:
        value = getattr(getattr(msg, 'chat', None), 'id', None)
    return str(value) if value is not None else ''


def pending_lookup(msg, order_id=None):
    author = str(getattr(msg, 'author_id', '') or '')
    if not author or getattr(msg, 'by_bot', False):
        return None, []
    with _order_lock:
        records = _read_orders()
        candidates = [(key, row) for key, row in records.items()
            if (not order_id or key == str(order_id)) and row.get('buyer_id') == author
            and _message_chat(msg) in row.get('chat_keys', [])
            and row.get('status') == 'waiting_username' and row.get('channels')
            and any(str(lot.get('lot_id')) == row.get('lot_id') and lot.get('token') == row.get('binding_token') for lot in load_lots() if isinstance(lot, dict))]
    if len(candidates) != 1:
        return None, []
    return candidates[0][0], candidates[0][1]['channels']


def pending_clear(key, channel_ids=None):
    with _order_lock:
        records = _read_orders()
        row = records[str(key)]
        row['channels'] = [cid for cid in row['channels'] if cid not in set(channel_ids or [])]
        row['status'] = 'completed' if not row['channels'] else 'waiting_username'
        _write_orders(records)


def _paid_order(c, order_id):
    order = c.account.get_order(str(order_id))
    status = getattr(order, 'status', None)
    status = str(getattr(status, 'name', status) or '').lower().split('.')[-1]
    if status != 'paid':
        raise ValueError('Заказ не оплачен или уже закрыт/возвращён')
    return order


def _is_admin(c, event):
    users = getattr(c.telegram, 'authorized_users', {})
    return getattr(getattr(event, 'from_user', None), 'id', None) in {int(uid) for uid in users}


def on_new_order(cardinal, event: NewOrderEvent) -> None:
    order_id = str(getattr(event.order, 'id', '') or '')
    if not order_id:
        return
    # All identity and lot evidence comes from the authenticated full order.
    try:
        order = _paid_order(cardinal, order_id)
        lot = match_lot_tier(order)
        if not lot:
            return
        _pls_require_license(cardinal)
        buyer_id = str(getattr(order, 'buyer_id', '') or '')
        if not buyer_id.isdigit() or buyer_id == str(cardinal.account.id):
            return
        qty = int(getattr(order, 'amount', 0) or 0)
        if not 1 <= qty <= 50:
            return
        tier = get_tier(lot['tier_id'])
        if not tier:
            return
        chat_keys = [str(key) for key in resolve_message_chat_id(cardinal, order) if str(key) != buyer_id]
        if not chat_keys:
            return
        send_chat = next((key for key in chat_keys if key.isdigit()), chat_keys[0])
        with _order_lock:
            records = _read_orders()
            if order_id in records:
                return
            records[order_id] = dict(buyer_id=buyer_id, lot_id=str(lot['lot_id']), binding_token=lot['token'], chat_keys=chat_keys, channels=[], status='preparing', quantity=qty)
            _write_orders(records)  # Deduplicate BEFORE reserving stock or spending.
        cfg = load_config()
        items = take_ready_channels(lot['tier_id'], qty)
        if items:
            pending_put([item['channel_id'] for item in items], order_id=order_id)
            cardinal.send_message(send_chat, build_delivery_instructions([item['link'] for item in items]) + '\nЗаказ #' + order_id)
        missing = qty - len(items)
        if missing:
            cardinal.send_message(send_chat, 'Готовится каналов: ' + str(missing) + '. Заказ #' + order_id)
            threading.Thread(target=_create_on_demand, args=(cardinal, cfg, tier, send_chat, chat_keys, missing, order_id), daemon=True).start()
    except Exception as exc:
        log_exc('AutoChannels order not processed; inspect saved order/stock', exc)



def build_delivery_instructions(links) -> str:
    """Инструкция для покупателя. links — строка или список ссылок."""
    if isinstance(links, str):
        links = [links]
    links = [ln for ln in links if ln]
    if not links:
        return "Каналы готовятся, ссылки придут чуть позже."

    if len(links) == 1:
        head = f"🎉 Канал готов: {links[0]}\n\n"
        step1 = "1️⃣ Перейдите по ссылке выше и подпишитесь на канал.\n"
        done = "✅ После этого мы передадим вам канал и сообщим, когда всё будет готово.\n\n"
    else:
        numbered = "\n".join(f"{i}. {ln}" for i, ln in enumerate(links, 1))
        head = f"🎉 Готово каналов: {len(links)}\n{numbered}\n\n"
        step1 = "1️⃣ Перейдите по каждой ссылке выше и подпишитесь на все каналы.\n"
        done = (
            f"✅ После этого мы передадим вам права владельца на все {len(links)} "
            "канал(ов) и сообщим, когда всё будет готово.\n\n"
        )

    return (
        head
        + "📌 Чтобы получить права владельца, выполните 2 шага:\n\n"
        + step1
        + "2️⃣ Отправьте свой Telegram @username в этот чат FunPay "
        "(обязательно вместе с символом @).\n\n"
        + done
        + "Если возникнут вопросы — напишите сюда."
    )


def _create_on_demand(cardinal, cfg, tier: dict, chat_id, chat_keys=None, count: int = 1, order_id=None) -> None:
    """Запасной путь: создать count каналов и крутить прямо сейчас."""
    count = max(1, int(count or 1))
    keys = list(chat_keys) if chat_keys else [str(chat_id)]
    if str(chat_id) not in keys:
        keys.insert(0, str(chat_id))
    log(
        f"ON_DEMAND start chat_id={chat_id} keys={keys} count={count} "
        f"tier={tier.get('name')} id={tier.get('id')}"
    )
    delivered = 0
    try:
        for i in range(count):
            try:
                create_and_boost_one(cfg, tier)
            except Exception as e:
                log_exc(f"ON_DEMAND create failed ({i + 1}/{count}) chat_id={chat_id}", e)
                try:
                    cardinal.send_message(
                        chat_id,
                        f"Не удалось создать канал {i + 1}/{count}: {e}. "
                        "Напишите продавцу напрямую по оставшимся."
                    )
                except Exception as e2:
                    log_exc(f"ON_DEMAND create-error send_message failed chat_id={chat_id}", e2)
                break

            got = None
            for attempt in range(40):
                time.sleep(30)
                refresh_stock_statuses(cfg)
                got = take_ready_channel(tier["id"])
                if got:
                    break
            if not got:
                log(f"ON_DEMAND timeout chat_id={chat_id}: канал {i + 1}/{count} не готов за ~20 мин")
                try:
                    cardinal.send_message(
                        chat_id,
                        f"Накрутка канала {i + 1}/{count} ещё не завершилась, напишите продавцу напрямую."
                    )
                except Exception as e:
                    log_exc(f"ON_DEMAND timeout send_message failed chat_id={chat_id}", e)
                continue

            pending_put(got["channel_id"], *keys, order_id=order_id)
            delivered += 1
            log(
                f"ON_DEMAND ready chat_id={chat_id} channel_id={got['channel_id']} "
                f"({delivered}/{count}) → pending записан"
            )
            try:
                prefix = f"[{delivered}/{count}] " if count > 1 else ""
                cardinal.send_message(chat_id, prefix + build_delivery_instructions(got["link"]))
            except Exception as e:
                log_exc(f"ON_DEMAND send_message failed chat_id={chat_id}", e)

        if delivered == 0 and count > 0:
            try:
                cardinal.send_message(chat_id, "Не удалось подготовить каналы. Напишите продавцу напрямую.")
            except Exception as e:
                log_exc(f"ON_DEMAND zero-delivered send_message failed chat_id={chat_id}", e)
    except Exception as e:
        log_exc(f"ON_DEMAND error chat_id={chat_id}", e)
        try:
            cardinal.send_message(chat_id, f"Ошибка при создании каналов: {e}. Напишите продавцу напрямую.")
        except Exception as e2:
            log_exc(f"ON_DEMAND error send_message failed chat_id={chat_id}", e2)


FAQ_DEFAULTS = [
    # (ключевые слова через запятую, ответ)
    ("в наличии,есть в наличии,остались", "Да, товар в наличии, автовыдача работает круглосуточно."),
    ("сколько ждать,как быстро,сколько по времени", "Если канал уже готов на складе — выдаётся сразу после оплаты. Если нет — обычно 5-15 минут."),
    ("как получить,как забрать,инструкция", "После оплаты бот сам пришлёт ссылку на канал и попросит ваш @username для передачи прав."),
]


def load_faq(cfg) -> list:
    raw = cfg["settings"].get("faq_json", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return FAQ_DEFAULTS


def save_faq(cfg, faq: list) -> None:
    cfg["settings"]["faq_json"] = json.dumps(faq, ensure_ascii=False)
    save_config(cfg)


def try_faq_autoreply(cardinal, cfg, msg) -> bool:
    text = (msg.text or "").lower()
    if not text:
        return False
    for keywords, answer in load_faq(cfg):
        if any(kw.strip() in text for kw in keywords.split(",") if kw.strip()):
            cardinal.send_message(msg.chat_id, answer)
            return True
    return False


def _do_transfer(cardinal, cfg, chat_id, pending_key, channel_ids, buyer_username):
    with _transfer_lock:
        _pls_require_license(cardinal)
        order = _paid_order(cardinal, pending_key)
        lot = match_lot_tier(order)
        with _order_lock:
            records = _read_orders()
            row = records.get(str(pending_key))
            if not row or row.get('status') != 'waiting_username' or not lot:
                return
            if str(lot['lot_id']) != row['lot_id'] or str(getattr(order, 'buyer_id', '')) != row['buyer_id'] or lot['token'] != row['binding_token']:
                return
            ids = [cid for cid in channel_ids if cid in row['channels']]
            if not ids or (row.get('recipient') and row['recipient'] != buyer_username):
                return
            row['recipient'] = buyer_username
            row['status'] = 'transferring'
            _write_orders(records)
        for cid in ids:
            try:
                asyncio.run(_transfer_ownership(cfg, cid, buyer_username))
            except Exception as exc:
                with _order_lock:
                    records = _read_orders()
                    records[str(pending_key)]['status'] = 'needs_review'
                    _write_orders(records)
                log_exc('Transfer result requires seller review; do not retry blindly', exc)
                cardinal.send_message(chat_id, 'Передача требует проверки продавцом. Не повторяйте выдачу до сверки владельца канала.')
                return
            with _order_lock:
                records = _read_orders()
                row = records[str(pending_key)]
                row['channels'] = [value for value in row['channels'] if value != cid]
                _write_orders(records)
        with _order_lock:
            records = _read_orders()
            records[str(pending_key)]['status'] = 'completed' if not records[str(pending_key)]['channels'] else 'waiting_username'
            _write_orders(records)
        cardinal.send_message(chat_id, '✅ Права владельца переданы. Заказ #' + str(pending_key))



def on_new_message(cardinal, event) -> None:
    msg = event.message
    text = str(getattr(msg, 'text', '') or '').strip()
    match = re.fullmatch(r'@([A-Za-z][A-Za-z0-9_]{3,31})(?:\s+#([A-Za-z0-9_-]+))?', text)
    if not match or getattr(msg, 'by_bot', False) or str(getattr(msg, 'author_id', '')) == str(cardinal.account.id):
        return
    try:
        key, channels = pending_lookup(msg, match.group(2))
    except (OSError, ValueError, RuntimeError) as exc:
        log_exc('Order journal unavailable; username ignored', exc)
        return
    if not channels:
        return  # Never answer usernames or FAQ in unrelated chats/orders.
    try:
        _pls_require_license(cardinal)
        cfg = load_config()
        ready, remaining = _session_ready_for_transfer(cfg)
        if not ready:
            cardinal.send_message(_message_chat(msg), 'Telegram пока не разрешает передачу. Повторите позже. Заказ #' + key)
            return
        threading.Thread(target=_do_transfer, args=(cardinal, cfg, _message_chat(msg), key, channels, match.group(1)), daemon=True).start()
    except Exception as exc:
        log_exc('Username transfer blocked', exc)



# ---------------------- Настройки через Telegram ----------------------
# Поля сгруппированы по разделам, у каждого есть короткая подсказка,
# откуда взять значение — она показывается прямо перед вводом.

SECTIONS = {
    "tg": {
        "title": "Telegram-аккаунт (создаёт каналы)",
        "fields": [
            ("tg_api_id", "API ID", "Число с сайта my.telegram.org -> API development tools."),
            ("tg_api_hash", "API Hash", "Строка из букв и цифр с той же страницы my.telegram.org, рядом с API ID."),
            ("tg_2fa_password", "Пароль 2FA",
             "Облачный пароль двухшаговой проверки ЭТОГО Telegram-аккаунта "
             "(Telegram -> Настройки -> Конфиденциальность -> Двухшаговая проверка). "
             "Без него нельзя передать права на канал."),
        ],
    },
    "smm": {
        "title": "SMM-панель (накрутка подписчиков)",
        "fields": [
            ("smm_api_url", "Адрес API", "В личном кабинете панели, раздел API. Обычно заканчивается на /api/v2."),
            ("smm_api_key", "Ключ/токен API", "Там же, в разделе API. Иногда называется API Key или API Token — это одно и то же."),
        ],
    },
    "faq": {
        "title": "Автоответы в чате",
        "fields": [
            ("faq_enabled", "Включены (1/0)", "1 — отвечать на частые вопросы до оплаты, 0 — выключить."),
            ("faq_json", "Список вопрос-ответ (JSON)",
             'Формат: [["ключевые,слова,через запятую","текст ответа"], ...]. '
             'Пример: [["в наличии,остались","Да, в наличии!"]]'),
        ],
    },
}

SECTIONS.pop("faq", None)  # Username handling is limited to verified pending orders.

LOTS_HINT = ('Пришлите числовой ID своего лота FunPay (из offer?id=…). '
             'После выбора тарифа плагин добавит проверяемую метку в описание этого лота. '
             'Старые привязки по словам не используются — добавьте их заново по ID.')

TIERS_HINT = (
    "Тариф — это пакет: сколько подписчиков крутить и сколько таких каналов держать "
    "про запас. Можно сделать несколько тарифов (например, «500 подписчиков» и "
    "«1000 подписчиков») и привязать к ним разные лоты."
)

SECRET_FIELDS = {"tg_2fa_password", "smm_api_key"}


def _field_value_display(cfg, key: str) -> str:
    current = cfg["settings"].get(key, "")
    if not current:
        return "—"
    return "сохранено ●●●" if key in SECRET_FIELDS else current


def _section_filled(cfg, section_key: str) -> bool:
    return all(cfg["settings"].get(k, "") for k, _, _ in SECTIONS[section_key]["fields"])


def build_main_menu(cfg):
    kb = InlineKeyboardMarkup()
    for skey, section in SECTIONS.items():
        mark = "✅" if _section_filled(cfg, skey) else "⚠️"
        kb.add(InlineKeyboardButton(f"{mark} {section['title']}", callback_data=f"cad_sec_{skey}"))

    folder_title = cfg["settings"].get("tg_folder_title", "")
    kb.add(InlineKeyboardButton(
        f"📁 Папка для каналов: {folder_title or 'не выбрана'}", callback_data="cad_folder_pick"
    ))

    tiers = load_tiers()
    kb.add(InlineKeyboardButton(f"📦 Тарифы ({len(tiers)})", callback_data="cad_tiers"))

    lots = load_lots()
    lots_mark = "✅" if lots else "⚠️"
    kb.add(InlineKeyboardButton(f"{lots_mark} Лоты ({len(lots)})", callback_data="cad_lots"))

    stock = load_stock()
    ready = sum(1 for i in stock if i["status"] == "ready")
    boosting = sum(1 for i in stock if i["status"] == "boosting")
    kb.add(InlineKeyboardButton(f"Готово: {ready} · Крутится: {boosting}", callback_data="cad_noop"))
    kb.add(InlineKeyboardButton("🚀 Начать пополнение склада сейчас", callback_data="cad_restock_now"))
    kb.add(InlineKeyboardButton("🔑 Привязать Telegram-аккаунт", callback_data="cad_login_start"))
    kb.add(InlineKeyboardButton("🗑 Сбросить сессию входа", callback_data="cad_reset_session"))
    kb.add(InlineKeyboardButton("📋 Посмотреть лог (ошибки)", callback_data="cad_show_log"))
    kb.add(InlineKeyboardButton("🔑 Лицензия", callback_data="cad_license_check"))
    kb.add(InlineKeyboardButton("🛍 PLS Store", url="https://t.me/PLSStore1_bot"))
    kb.add(InlineKeyboardButton("👤 Автор", url=AUTHOR_URL))
    return kb


def build_lots_menu():
    lots = load_lots()
    tiers = {t["id"]: t["name"] for t in load_tiers()}
    kb = InlineKeyboardMarkup()
    for i, lot in enumerate(lots):
        if isinstance(lot, dict) and "lot_id" in lot and "tier_id" in lot:
            tier_name = tiers.get(lot["tier_id"], "⚠️ тариф удалён")
            label = f"🗑 ID {lot['lot_id']} → {tier_name}"
        else:
            # запись в старом формате (без привязки к тарифу) — показываем как есть, можно только удалить
            label = f"🗑 {lot} (старый формат, без тарифа — удалите и добавьте заново)"
        kb.add(InlineKeyboardButton(label, callback_data=f"cad_lot_del_{i}"))
    kb.add(InlineKeyboardButton("➕ Добавить лот", callback_data="cad_lot_add"))
    kb.add(InlineKeyboardButton("⬅ Назад", callback_data="cad_main"))
    return kb


def build_tiers_menu():
    kb = InlineKeyboardMarkup()
    for i, t in enumerate(load_tiers()):
        kb.add(InlineKeyboardButton(
            f"🗑 {t['name']} · {t['quantity']} подп. · запас {t['stock_target']}",
            callback_data=f"cad_tier_del_{i}",
        ))
    kb.add(InlineKeyboardButton("➕ Добавить тариф", callback_data="cad_tier_add"))
    kb.add(InlineKeyboardButton("⬅ Назад", callback_data="cad_main"))
    return kb


def build_section_menu(cfg, section_key: str):
    kb = InlineKeyboardMarkup()
    for key, label, _hint in SECTIONS[section_key]["fields"]:
        value = _field_value_display(cfg, key)
        kb.add(InlineKeyboardButton(f"{label}: {value}", callback_data=f"cad_set_{key}"))
    kb.add(InlineKeyboardButton("⬅ Назад", callback_data="cad_main"))
    return kb


def open_settings(c, call):
    """Вызывается Cardinal'ом по стандартному колбэку 47:<UUID>:<offset>."""
    bot = _PlsBotPanel(c)
    cfg = load_config()
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "Настройки автовыдачи каналов.\n"
        "✅ — раздел полностью заполнен, ⚠️ — чего-то не хватает.",
        reply_markup=build_main_menu(cfg),
    )


def open_section(c, call):
    section_key = call.data.replace("cad_sec_", "")
    bot = _PlsBotPanel(c)
    cfg = load_config()
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"{SECTIONS[section_key]['title']}:",
        reply_markup=build_section_menu(cfg, section_key),
    )


def back_to_main(c, call):
    bot = _PlsBotPanel(c)
    cfg = load_config()
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Настройки автовыдачи каналов:", reply_markup=build_main_menu(cfg))


def restock_now(c, call):
    bot = _PlsBotPanel(c)
    cfg = load_config()
    s = cfg["settings"]
    bot.answer_callback_query(call.id)
    tiers = load_tiers()
    if not all([s["tg_api_id"], s["tg_api_hash"], s["smm_api_url"], s["smm_api_key"]]):
        bot.send_message(call.message.chat.id, "Сначала заполните разделы «Telegram-аккаунт» и «SMM-панель».")
        return
    if not tiers:
        bot.send_message(call.message.chat.id, "Сначала добавьте хотя бы один тариф в разделе «📦 Тарифы».")
        return

    if not _restock_running.acquire(blocking=False):
        bot.send_message(call.message.chat.id, "Пополнение склада уже идёт (например, в фоне), подождите его завершения.")
        return

    def _run():
        try:
            if time.time() < _flood_wait_until:
                bot.send_message(
                    call.message.chat.id,
                    f"Telegram временно ограничил создание каналов. Подождите ~{int(_flood_wait_until - time.time())} сек. и попробуйте снова."
                )
                return
            try:
                created = _ensure_stock(cfg, status_cb=lambda msg: bot.send_message(call.message.chat.id, msg))
                if created:
                    bot.send_message(call.message.chat.id, f"Создано {created} канал(ов). Проверяйте статус в меню.")
                else:
                    bot.send_message(call.message.chat.id, "Склад уже полон, создавать нечего.")
            except Exception as e:
                log_exc("RESTOCK_NOW error", e)
                bot.send_message(call.message.chat.id, f"Ошибка при пополнении склада: {e}")
        finally:
            _restock_running.release()

    threading.Thread(target=_run, daemon=True).start()
    bot.send_message(call.message.chat.id, "Запускаю пополнение склада...")


def show_log(c, call):
    """Кнопка «📋 Посмотреть лог» — последние строки лога плагина прямо в Telegram."""
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    tail = read_log_tail(50)
    # Telegram лимит ~4096 символов на сообщение; режем с запасом
    if len(tail) > 3500:
        tail = "...\n" + tail[-3500:]
    text = (
        "📋 <b>Лог плагина Ch AutoDelivery</b> (последние строки)\n"
        f"<code>{_escape_html(tail)}</code>\n\n"
        "Скопируйте блок с ошибкой (TRANSFER FAIL / NEW_ORDER / MSG) и пришлите разработчику."
    )
    try:
        bot.send_message(call.message.chat.id, text, parse_mode="HTML")
    except Exception:
        # fallback без HTML, если что-то пойдёт не так с разметкой
        bot.send_message(
            call.message.chat.id,
            "📋 Лог плагина (последние строки):\n\n" + tail[:3500]
            + "\n\nСкопируйте блок с ошибкой и пришлите разработчику.",
        )


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def open_lots(c, call):
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    if not load_tiers():
        bot.send_message(call.message.chat.id, "Сначала создайте хотя бы один тариф в разделе «📦 Тарифы».")
        return
    bot.send_message(
        call.message.chat.id,
        "Привязанные ID лотов FunPay → тариф.\n"
        "Нажмите на лот, чтобы удалить его, или добавьте новый:",
        reply_markup=build_lots_menu(),
    )


def ask_new_lot(c, call):
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    if not load_tiers():
        bot.send_message(call.message.chat.id, "Сначала создайте хотя бы один тариф в разделе «📦 Тарифы».")
        return
    msg = bot.send_message(call.message.chat.id, f"➕ Новый лот\n{LOTS_HINT}\n\nПришлите текст одним сообщением.")
    bot.register_next_step_handler(msg, _lot_step_keyword, c)


def _lot_step_keyword(message, c):
    bot = _PlsBotPanel(c)
    keyword = (message.text or "").strip()
    if not keyword.isascii() or not keyword.isdigit() or int(keyword) <= 0:
        bot.send_message(message.chat.id, "Нужен положительный числовой ID лота.")
        return
    keyword = str(int(keyword))
    tiers = load_tiers()
    kb = InlineKeyboardMarkup()
    for t in tiers:
        kb.add(InlineKeyboardButton(t["name"], callback_data=f"cad_lot_tier_{t['id']}"))
    # Publish state before the keyboard can be clicked. Bind it to this keyboard.
    session = {'lot_id': keyword, 'message_id': None}
    _pending_lot_keyword[message.chat.id] = session
    msg = bot.send_message(message.chat.id, "Выберите тариф для этого лота:", reply_markup=kb)
    session['message_id'] = msg.message_id


_pending_lot_keyword = {}


_lot_binding_locks = {}


def pick_lot_tier(c, call):
    bot = _PlsBotPanel(c)
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    lock = _lot_binding_locks.setdefault(chat_id, threading.Lock())
    if not lock.acquire(blocking=False):
        bot.send_message(chat_id, "Привязка уже выполняется. Дождитесь результата.")
        return
    try:
        session = _pending_lot_keyword.get(chat_id)
        if not session:
            bot.send_message(chat_id, "Этот выбор тарифа уже завершён или сброшен после перезапуска. Добавьте лот заново.")
            return
        if session['message_id'] is not None and session['message_id'] != call.message.message_id:
            bot.send_message(chat_id, "Это старое меню. Выберите тариф в последнем сообщении добавления лота.")
            return
        keyword = session['lot_id']
        tier_id = call.data.replace("cad_lot_tier_", "", 1)
        if not get_tier(tier_id):
            bot.send_message(chat_id, 'Тариф удалён. Добавьте лот заново и выберите существующий тариф.')
            return
        token = secrets.token_hex(16)
        marker = '#PLSCH:' + token
        stage = 'чтение лота'
        try:
            fields = c.account.get_lot_fields(int(keyword))
            for lang in ('ru', 'en'):
                field = 'fields[desc][' + lang + ']'
                description = getattr(fields, 'description_' + lang, fields.fields.get(field, '')) or ''
                if lang == 'ru' or description:
                    description = re.sub(r'#PLSCH:[0-9a-f]{32}', '', description).rstrip()
                    description = description + '\n\n' + marker
                    setattr(fields, 'description_' + lang, description)
                    fields.fields[field] = description
            stage = 'сохранение описания в FunPay'
            c.account.save_lot(fields)
            stage = 'проверка сохранённого описания'
            verified = c.account.get_lot_fields(int(keyword))
            if marker not in verified.fields.get('fields[desc][ru]', ''):
                bot.send_message(chat_id, 'FunPay не подтвердил сохранение метки в описании лота. '
                                 'Привязка не создана. Можно повторно нажать тариф; '
                                 'если ошибка повторяется, проверьте описание лота в FunPay.')
                return
            stage = 'сохранение привязки на диске'
            lots = load_lots()
            lots = [lot for lot in lots if not isinstance(lot, dict) or str(lot.get('lot_id')) != keyword]
            lots.append({'lot_id': keyword, 'tier_id': tier_id, 'token': token})
            save_lots(lots)
        except Exception as exc:
            # Do not expose exception text: API errors can contain authenticated URLs.
            log('LOT_BIND_FAILED lot_id=' + keyword + ' stage=' + stage + ' error=' + type(exc).__name__)
            bot.send_message(chat_id, 'Лот не привязан. Этап: ' + stage + '; ошибка: ' + type(exc).__name__ +
                             '. Выбор сохранён — можно повторно нажать тариф. '
                             'Если не удаётся прочитать лот, проверьте ID, владельца и вход Cardinal в FunPay.')
            return
        # A new ID might have been entered while the network request was running.
        if _pending_lot_keyword.get(chat_id) is session:
            _pending_lot_keyword.pop(chat_id, None)
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        bot.send_message(chat_id, "✅ Добавлено.")
        bot.send_message(chat_id, "Лоты:", reply_markup=build_lots_menu())
    finally:
        lock.release()


def delete_lot(c, call):
    idx = int(call.data.replace("cad_lot_del_", ""))
    lots = load_lots()
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    if 0 <= idx < len(lots):
        removed = lots.pop(idx)
        save_lots(lots)
        removed_label = removed.get("lot_id", removed.get("keyword", "старый формат")) if isinstance(removed, dict) else removed
        bot.send_message(call.message.chat.id, f"Удалено: {removed_label}")
    bot.send_message(call.message.chat.id, "Лоты:", reply_markup=build_lots_menu())


# ---------------------- Тарифы ----------------------

def open_tiers(c, call):
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"📦 Тарифы.\n{TIERS_HINT}", reply_markup=build_tiers_menu())


def ask_new_tier(c, call):
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Название тарифа (например: 500 подписчиков):")
    bot.register_next_step_handler(msg, _tier_step_name, c)


def _tier_step_name(message, c):
    bot = _PlsBotPanel(c)
    name = message.text.strip()
    msg = bot.send_message(message.chat.id, "ID услуги в SMM-панели (число):")
    bot.register_next_step_handler(msg, _tier_step_service, c, name)


def _tier_step_service(message, c, name):
    bot = _PlsBotPanel(c)
    service_id = message.text.strip()
    msg = bot.send_message(message.chat.id, "Сколько подписчиков заказывать на один канал этого тарифа:")
    bot.register_next_step_handler(msg, _tier_step_quantity, c, name, service_id)


def _tier_step_quantity(message, c, name, service_id):
    bot = _PlsBotPanel(c)
    quantity = message.text.strip()
    msg = bot.send_message(message.chat.id, "Сколько готовых каналов этого тарифа держать про запас:")
    bot.register_next_step_handler(msg, _tier_step_stock, c, name, service_id, quantity)


def _tier_step_stock(message, c, name, service_id, quantity):
    bot = _PlsBotPanel(c)
    stock_target = message.text.strip()
    tiers = load_tiers()
    new_id = f"tier_{int(time.time() * 1000)}"
    tiers.append({
        "id": new_id, "name": name, "service_id": service_id,
        "quantity": quantity, "stock_target": stock_target,
    })
    save_tiers(tiers)
    bot.send_message(message.chat.id, "✅ Тариф добавлен.")
    bot.send_message(message.chat.id, f"📦 Тарифы.\n{TIERS_HINT}", reply_markup=build_tiers_menu())


def delete_tier(c, call):
    idx = int(call.data.replace("cad_tier_del_", ""))
    tiers = load_tiers()
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    if 0 <= idx < len(tiers):
        removed = tiers.pop(idx)
        save_tiers(tiers)
        # заодно подчистим лоты, которые ссылались на удалённый тариф
        lots = [l for l in load_lots() if l["tier_id"] != removed["id"]]
        save_lots(lots)
        bot.send_message(call.message.chat.id, f"Удалено: {removed['name']} (и привязанные к нему лоты, если были).")
    bot.send_message(call.message.chat.id, f"📦 Тарифы.\n{TIERS_HINT}", reply_markup=build_tiers_menu())


# ---------------------- Папка Telegram для новых каналов ----------------------

def open_folder_picker(c, call):
    bot = _PlsBotPanel(c)
    cfg = load_config()
    s = cfg["settings"]
    bot.answer_callback_query(call.id)
    if not (s["tg_api_id"] and s["tg_api_hash"]):
        bot.send_message(call.message.chat.id, "Сначала привяжите Telegram-аккаунт.")
        return
    bot.send_message(call.message.chat.id, "Загружаю список папок...")

    def _run():
        try:
            folders = asyncio.run(_list_folders(cfg))
        except Exception as e:
            bot.send_message(call.message.chat.id, f"Не удалось получить папки: {e}")
            return
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🚫 Не использовать папку", callback_data="cad_folder_set_|"))
        for fid, title in folders:
            short_title = title[:30]
            kb.add(InlineKeyboardButton(title, callback_data=f"cad_folder_set_{fid}|{short_title}"))
        if not folders:
            bot.send_message(call.message.chat.id, "На этом аккаунте пока нет ни одной папки. Создайте папку в Telegram и попробуйте снова.")
            return
        bot.send_message(call.message.chat.id, "Выберите папку для новых каналов:", reply_markup=kb)

    threading.Thread(target=_run, daemon=True).start()


def set_folder(c, call):
    bot = _PlsBotPanel(c)
    cfg = load_config()
    raw = call.data.replace("cad_folder_set_", "")
    folder_id, _, title = raw.partition("|")
    bot.answer_callback_query(call.id)
    cfg["settings"]["tg_folder_id"] = folder_id
    cfg["settings"]["tg_folder_title"] = title
    save_config(cfg)
    if folder_id:
        bot.send_message(call.message.chat.id, "✅ Папка выбрана.")
    else:
        bot.send_message(call.message.chat.id, "Папка отключена, новые каналы раскладываться не будут.")
    bot.send_message(call.message.chat.id, "Настройки автовыдачи каналов:", reply_markup=build_main_menu(cfg))


def _find_field(key: str):
    for section_key, section in SECTIONS.items():
        for k, label, hint in section["fields"]:
            if k == key:
                return section_key, label, hint
    return None, key, ""


def ask_value(c, call):
    key = call.data.replace("cad_set_", "")
    section_key, label, hint = _find_field(key)
    bot = _PlsBotPanel(c)
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        f"✏️ {label}\n{hint}\n\nПришлите значение одним сообщением.",
    )
    bot.register_next_step_handler(msg, save_value, c, key, section_key)


def save_value(message, c, key, section_key):
    cfg = load_config()
    cfg["settings"][key] = message.text.strip()
    save_config(cfg)
    bot = _PlsBotPanel(c)
    bot.send_message(message.chat.id, "✅ Сохранено.")
    bot.send_message(
        message.chat.id,
        f"{SECTIONS[section_key]['title']}:",
        reply_markup=build_section_menu(cfg, section_key),
    )


def _register_original(c, bot):
    # Стандартный колбэк Cardinal для кнопки "⚙️ Настройки" плагина: 47:<UUID>:<offset>
    bot.register_callback_query_handler(
        lambda call: open_settings(c, call),
        func=lambda call: call.data.startswith(f"47:{UUID}:"),
    )
    bot.register_callback_query_handler(
        lambda call: open_section(c, call),
        func=lambda call: call.data.startswith("cad_sec_"),
    )
    bot.register_callback_query_handler(
        lambda call: back_to_main(c, call),
        func=lambda call: call.data == "cad_main",
    )
    bot.register_callback_query_handler(
        lambda call: open_lots(c, call),
        func=lambda call: call.data == "cad_lots",
    )
    bot.register_callback_query_handler(
        lambda call: restock_now(c, call),
        func=lambda call: call.data == "cad_restock_now",
    )
    bot.register_callback_query_handler(
        lambda call: start_login_flow(c, call),
        func=lambda call: call.data == "cad_login_start",
    )
    bot.register_callback_query_handler(
        lambda call: reset_session(c, call),
        func=lambda call: call.data == "cad_reset_session",
    )
    bot.register_callback_query_handler(
        lambda call: ask_new_lot(c, call),
        func=lambda call: call.data == "cad_lot_add",
    )
    bot.register_callback_query_handler(
        lambda call: delete_lot(c, call),
        func=lambda call: call.data.startswith("cad_lot_del_"),
    )
    bot.register_callback_query_handler(
        lambda call: pick_lot_tier(c, call),
        func=lambda call: call.data.startswith("cad_lot_tier_"),
    )
    bot.register_callback_query_handler(
        lambda call: open_tiers(c, call),
        func=lambda call: call.data == "cad_tiers",
    )
    bot.register_callback_query_handler(
        lambda call: ask_new_tier(c, call),
        func=lambda call: call.data == "cad_tier_add",
    )
    bot.register_callback_query_handler(
        lambda call: delete_tier(c, call),
        func=lambda call: call.data.startswith("cad_tier_del_"),
    )
    bot.register_callback_query_handler(
        lambda call: open_folder_picker(c, call),
        func=lambda call: call.data == "cad_folder_pick",
    )
    bot.register_callback_query_handler(
        lambda call: set_folder(c, call),
        func=lambda call: call.data.startswith("cad_folder_set_"),
    )
    bot.register_callback_query_handler(
        lambda call: ask_value(c, call),
        func=lambda call: call.data.startswith("cad_set_"),
    )
    bot.register_callback_query_handler(
        lambda call: bot.answer_callback_query(call.id),
        func=lambda call: call.data == "cad_noop",
    )
    bot.register_callback_query_handler(
        lambda call: show_log(c, call),
        func=lambda call: call.data == "cad_show_log",
    )
    log(f"INIT plugin {NAME} v{VERSION} UUID={UUID}")
    start_restock_thread(c)




class _PlsBotPanel:
    def __init__(self, c):
        self.c = c
        self.raw = c.telegram.bot

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def send_message(self, chat_id, text, *args, **kwargs):
        if 'parse_mode' not in kwargs:
            text = html.escape(str(text))
        kwargs['parse_mode'] = 'HTML'
        return self.raw.send_message(chat_id, _pls_panel(text), *args, **kwargs)

    def register_callback_query_handler(self, callback, **kwargs):
        def checked(call):
            if not _is_admin(self.c, call) or call.message.chat.type != 'private':
                return
            if _pls_access(self.c, call.message.chat.id):
                callback(call)
        return self.raw.register_callback_query_handler(checked, **kwargs)

    def register_next_step_handler(self, message, callback, *args, **kwargs):
        def checked(reply, *arguments):
            if not _is_admin(self.c, reply) or reply.chat.type != 'private':
                return
            if _pls_access(self.c, reply.chat.id):
                callback(reply, *arguments)
        return self.raw.register_next_step_handler(message, checked, *args, **kwargs)


def _pls_license_panel(c, chat_id, error=''):
    kb = InlineKeyboardMarkup().row(InlineKeyboardButton('🔑 Ввести ключ', callback_data='cad_license_enter'), InlineKeyboardButton('🔄 Проверить', callback_data='cad_license_check'))
    _PlsBotPanel(c).send_message(chat_id, 'Лицензия AutoChannels на один FunPay-аккаунт.\n' + error, reply_markup=kb)


def _pls_access(c, chat_id):
    try:
        _pls_require_license(c)
        return True
    except RuntimeError as exc:
        _pls_license_panel(c, chat_id, str(exc))
        return False


def _pls_receive_key(message, c):
    if not _is_admin(c, message) or message.chat.type != 'private':
        return
    key = str(message.text or '').strip()
    if not re.fullmatch(r'PLS-[A-Za-z0-9_-]{20,96}', key):
        _pls_license_panel(c, message.chat.id, 'Неверный формат ключа.')
        return
    try:
        c.telegram.bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    path = _PlsPath('storage/plugins/AutoChannels/license.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump({'key': key}, output)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    _pls_clear_memory()
    if _pls_access(c, message.chat.id):
        _PlsBotPanel(c).send_message(message.chat.id, '✅ Ключ активен. Меню: /autochannels')


def init_commands(c):
    global _CARDINAL
    _CARDINAL = c
    raw = c.telegram.bot
    def license_callback(call):
        if not _is_admin(c, call) or call.message.chat.type != 'private':
            return
        raw.answer_callback_query(call.id)
        if call.data == 'cad_license_enter':
            message = _PlsBotPanel(c).send_message(call.message.chat.id, 'Отправьте ключ PLS-… из магазина.')
            raw.register_next_step_handler(message, _pls_receive_key, c)
        else:
            _pls_clear_memory()
            if _pls_access(c, call.message.chat.id):
                _pls_license_panel(c, call.message.chat.id, '✅ Лицензия активна.')
    raw.register_callback_query_handler(license_callback, func=lambda call: call.data in {'cad_license_enter', 'cad_license_check'})
    def command(message):
        if _is_admin(c, message) and message.chat.type == 'private' and _pls_access(c, message.chat.id):
            _PlsBotPanel(c).send_message(message.chat.id, 'Настройки AutoChannels', reply_markup=build_main_menu(load_config()))
    raw.register_message_handler(command, commands=['autochannels'])
    c.add_telegram_commands(UUID, [('autochannels', 'AutoChannels — меню', True)])
    _register_original(c, _PlsBotPanel(c))


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_DELETE = None
