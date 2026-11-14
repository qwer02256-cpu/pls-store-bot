"""Buyer self service and administrator license operations."""

import asyncio
import html
import logging
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from config import ADMIN_ID, DB_PATH
from services.db import db, ensure_user, get_user_language
from services.licenses import owned, mutate, issue, audit
from services.screens import screen_reply, screen_send

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
log = logging.getLogger(__name__)


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t,
                    callback_data=d,
                    style=(
                        "danger"
                        if t.startswith("←")
                        or t == "Отмена"
                        or d
                        in ("profile", "home", "licenses", "adm:licenses", "adm:home")
                        or d.startswith(
                            ("admlic:block:", "admlic:reset:", "admlic:do_reset:")
                        )
                        else (
                            "success"
                            if d.startswith(("lic:download:", "admlic:unblock:"))
                            else "primary"
                        )
                    ),
                )
                for t, d in r
            ]
            for r in rows
        ]
    )


def title(uid):
    return {"ru": "🔑 Лицензии", "en": "🔑 Licenses", "uk": "🔑 Ліцензії"}[
        get_user_language(uid)
    ]


def asset(conn, pid):
    return conn.execute(
        "SELECT * FROM license_assets WHERE product_id=?", (pid,)
    ).fetchone()


async def deliver(bot, lid, uid):
    row = owned(lid, uid)
    if not row or row["status"] != "active":
        return False
    with db() as conn:
        data = conn.execute(
            "SELECT a.*,p.name product_name,p.version product_version FROM license_assets a JOIN products p ON p.id=a.product_id WHERE a.product_id=?",
            (row["product_id"],),
        ).fetchone()
    if not data:
        await screen_send(
            bot, uid, "Файл готовится продавцом. Лицензия сохранена в «Лицензии»."
        )
        return False
    from services.license_files import plugin_bytes

    filename, content = plugin_bytes(data["file_path"])
    await screen_send(
        bot,
        uid,
        f"🔑 Бессрочная лицензия #{lid}\nКлюч: <code>{html.escape(row['license_key'])}</code>\nПривязка к FunPay при первом запуске.",
        reply_markup=keyboard([[("Лицензии", "licenses")]]),
    )
    await bot.send_document(uid, BufferedInputFile(content, filename=filename))
    await bot.send_document(
        uid, BufferedInputFile(data["instruction"].encode(), filename="INSTALL.txt")
    )
    with db() as conn:
        conn.execute("UPDATE licenses SET delivered=1 WHERE id=?", (lid,))
    return True


async def reconcile_deliveries(bot, cursor=0, limit=20):
    with db() as conn:
        rows = conn.execute(
            "SELECT l.id,l.user_id FROM licenses l JOIN license_assets a ON l.product_id=a.product_id "
            "WHERE delivered=0 AND status='active' AND l.id>? ORDER BY l.id LIMIT ?",
            (cursor, limit),
        ).fetchall()
    for r in rows:
        try:
            await deliver(bot, r["id"], r["user_id"])
        except Exception as exc:
            log.warning("License delivery %s failed: %s", r["id"], type(exc).__name__)
    return rows[-1]["id"] if len(rows) == limit else 0


async def retry_deliveries(bot):
    cursor = 0
    while True:
        try:
            cursor = await reconcile_deliveries(bot, cursor)
        except Exception as exc:
            log.warning("License delivery queue failed: %s", type(exc).__name__)
            cursor = 0
        await asyncio.sleep(60)


@router.message(Command("licenses"))
async def cmd_licenses(message):
    await show_list(message, message.from_user.id)


async def show_list(target, uid, page=0):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,status FROM licenses WHERE user_id=? ORDER BY id DESC LIMIT 21 OFFSET ?",
            (uid, page * 20),
        ).fetchall()
    buttons = [
        [(f"#{r['id']} · {r['status']}", f"lic:view:{r['id']}")] for r in rows[:20]
    ]
    if page:
        buttons.append([("←", f"licenses:page:{page-1}")])
    if len(rows) > 20:
        buttons.append([("→", f"licenses:page:{page+1}")])
    await screen_reply(
        target,
        f"<b>PLS Store</b>\n{title(uid)}\n━━━━━━━━━━━━━━━━━━━━\n\n"
        + ("Выберите лицензию." if rows else "Пока нет лицензий."),
        reply_markup=keyboard(
            buttons
            + [
                [
                    (
                        {"ru": "← Назад", "en": "← Back", "uk": "← Назад"}[
                            get_user_language(uid)
                        ],
                        "home",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("licenses:page:"))
async def license_page(call):
    await call.answer()
    try:
        page = int(call.data.split(":")[-1])
    except ValueError:
        return
    if 0 <= page < 100000:
        await show_list(call, call.from_user.id, page)


@router.callback_query(F.data == "licenses")
async def list_licenses(call):
    await call.answer()
    await show_list(call, call.from_user.id)


@router.callback_query(F.data.startswith("lic:"))
async def license_action(call, bot):
    try:
        _, action, lid = call.data.split(":")
        lid = int(lid)
    except (ValueError, TypeError):
        await call.answer()
        return
    row = owned(lid, call.from_user.id)
    if not row:
        await call.answer("Лицензия не найдена", show_alert=True)
        return
    await call.answer()
    if action == "request":
        changed = mutate(lid, "request", call.from_user.id, owner=call.from_user.id)
        await screen_reply(call, "Запрос сохранён. Дождитесь решения администратора.")
        if changed and ADMIN_ID:
            await screen_send(
                bot,
                ADMIN_ID,
                f"Запрос смены привязки #{lid}, покупатель {call.from_user.id}. /license {lid}",
            )
    elif action == "download":
        try:
            success = await deliver(bot, lid, call.from_user.id)
        except (ValueError, OSError, SyntaxError, UnicodeError):
            success = False
        if not success:
            await screen_reply(
                call,
                "Файл недоступен: лицензия заблокирована или файл ещё не загружен. Обратитесь в поддержку.",
            )
    elif action == "guide":
        with db() as conn:
            data = asset(conn, row["product_id"])
        if data:
            await call.message.answer_document(
                BufferedInputFile(data["instruction"].encode(), filename="INSTALL.txt")
            )
        else:
            await screen_reply(call, "Инструкция ещё не загружена продавцом.")
    else:
        await screen_reply(
            call,
            f"🔑 #{lid} · бессрочно\nСтатус: {row['status']}\nFunPay ID: {row['funpay_id'] or 'не привязан'}\nЗапрос смены: {'да' if row['rebind_requested'] else 'нет'}\nКлюч: <code>{html.escape(row['license_key'])}</code>",
            reply_markup=keyboard(
                [
                    [
                        ("Скачать", f"lic:download:{lid}"),
                        ("Инструкция", f"lic:guide:{lid}"),
                    ],
                    [("Запросить смену FunPay", f"lic:request:{lid}")],
                    [("← Лицензии", "licenses")],
                ]
            ),
        )


ADMIN_HELP = """🔑 Управление лицензиями
/license — последние лицензии
/license QUERY — поиск по номеру, Telegram ID, FunPay ID или полному ключу
/license_issue TELEGRAM_ID PRODUCT_ID — ручная выдача
/license_action ID block|unblock|reset — блокировка / разблокировка / сброс FunPay
/license_log [ID] — журнал
/license_file PRODUCT_ID — затем загрузите файл .py
/license_guide PRODUCT_ID — затем отправьте инструкцию текстом или .txt
Список товаров: /admin → Товары. Сброс разрешает привязать ключ к другому FunPay при следующем запуске."""


def admin_license_buttons(rows):
    return [
        [
            (
                f"Лицензия #{r['id']} · FunPay {r['funpay_id'] or '—'}",
                f"admlic:view:{r['id']}",
            )
        ]
        for r in rows
    ]


async def admin_license_card(target, lid, note=""):
    with db() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE id=?", (lid,)).fetchone()
    if not row:
        await screen_reply(
            target,
            "Лицензия не найдена",
            reply_markup=keyboard([[("← Лицензии", "adm:licenses")]]),
        )
        return
    toggle = (
        ("Заблокировать", f"admlic:block:{lid}")
        if row["status"] == "active"
        else ("Разблокировать", f"admlic:unblock:{lid}")
    )
    await screen_reply(
        target,
        f"🔑 <b>Лицензия #{lid}</b>\n\nСтатус: {row['status']}\nПокупатель Telegram ID: {row['user_id']}\n"
        f"Товар: #{row['product_id']}\nFunPay ID: {row['funpay_id'] or 'не привязан'}\nЗапрос смены: {'да' if row['rebind_requested'] else 'нет'}"
        + ("\n\n" + html.escape(note) if note else ""),
        reply_markup=keyboard(
            [
                [toggle],
                [("Сбросить FunPay", f"admlic:reset:{lid}")],
                [("Журнал", f"admlic:log:{lid}")],
                [("← Лицензии", "adm:licenses")],
            ]
        ),
    )


@router.callback_query(F.data == "adm:licenses")
async def admin_menu(call):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM licenses ORDER BY rebind_requested DESC,id DESC LIMIT 20"
        ).fetchall()
    await screen_reply(
        call,
        ADMIN_HELP,
        reply_markup=keyboard(
            admin_license_buttons(rows) + [[("← Админ-панель", "adm:home")]]
        ),
    )


@router.callback_query(F.data.startswith("admlic:"))
async def admin_license_callback(call):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    try:
        _, action, raw = call.data.split(":")
        lid = int(raw)
    except (ValueError, TypeError):
        await call.answer("Неверный номер лицензии", show_alert=True)
        return
    await call.answer()
    if action == "reset":
        with db() as conn:
            row = conn.execute("SELECT * FROM licenses WHERE id=?", (lid,)).fetchone()
        if not row:
            await admin_license_card(call, lid)
            return
        await screen_reply(
            call,
            f"Сбросить привязку лицензии <b>#{lid}</b> к FunPay <b>{row['funpay_id'] or '—'}</b>?\nСначала остановите старую копию Cardinal: следующий успешный запрос привяжет ключ заново.",
            reply_markup=keyboard(
                [
                    [("Да, сбросить", f"admlic:do_reset:{lid}")],
                    [("Отмена", f"admlic:view:{lid}")],
                ]
            ),
        )
        return
    if action in ("block", "unblock", "do_reset"):
        try:
            mutate(lid, "reset" if action == "do_reset" else action, ADMIN_ID)
        except ValueError:
            await admin_license_card(call, lid)
            return
        note = (
            "Привязка FunPay сброшена." if action == "do_reset" else "Статус изменён."
        )
        await admin_license_card(call, lid, note)
        return
    if action == "log":
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM license_audit WHERE license_id=? ORDER BY id DESC LIMIT 25",
                (lid,),
            ).fetchall()
        text = "\n".join(
            f"{r['created_at']} · {r['actor']}: {r['action']}" for r in rows
        )
        await screen_reply(
            call,
            html.escape(text) or "Журнал пуст",
            reply_markup=keyboard([[("← Лицензия", f"admlic:view:{lid}")]]),
        )
        return
    await admin_license_card(call, lid)


@router.message(Command("license"))
async def search(message):
    if message.from_user.id != ADMIN_ID:
        return
    q = message.text.split(maxsplit=1)
    with db() as conn:
        if len(q) == 1:
            rows = conn.execute(
                "SELECT * FROM licenses ORDER BY id DESC LIMIT 20"
            ).fetchall()
        else:
            q = q[1].strip()
            rows = conn.execute(
                "SELECT * FROM licenses WHERE CAST(id AS TEXT)=? OR CAST(user_id AS TEXT)=? OR funpay_id=? OR license_key=? LIMIT 20",
                (q, q, q, q),
            ).fetchall()
    text = "\n".join(
        f"#{r['id']} · TG {r['user_id']} · товар {r['product_id']} · {r['status']} · FP {r['funpay_id'] or '—'} · запрос {r['rebind_requested']}"
        for r in rows
    )
    await screen_reply(
        message,
        text or "Не найдено",
        reply_markup=keyboard(
            admin_license_buttons(rows) + [[("← Лицензии", "adm:licenses")]]
        ),
    )


@router.message(Command("license_issue"))
async def manual_issue(message, bot):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, uid, pid = message.text.split()
        uid = int(uid)
        pid = int(pid)
        if uid <= 0:
            raise ValueError()
        with db() as conn:
            if not conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
                raise ValueError()
        ensure_user(uid)
        with db() as conn:
            lid = issue(conn, uid, pid, actor=ADMIN_ID)
    except ValueError:
        await screen_reply(
            message,
            "Формат: /license_issue TELEGRAM_ID PRODUCT_ID (существующий товар)",
        )
        return
    await screen_reply(message, f"Выдана лицензия #{lid}")
    try:
        await deliver(bot, lid, uid)
    except Exception:
        await screen_reply(
            message, "Лицензия создана, отправка будет повторена автоматически."
        )


ADMIN_ACTION_USAGE = "Укажите номер лицензии, а не FunPay ID. Например: <code>/license_action 1 reset</code>\nДействия: block — блокировка, unblock — разблокировка, reset — сброс FunPay.\nДля поиска: <code>/license FUNPAY_ID</code> или /admin → Лицензии."


@router.message(Command("license_action"))
async def admin_action(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, raw, action = message.text.split()
        lid = int(raw)
        if lid <= 0 or action not in ("block", "unblock", "reset"):
            raise ValueError()
    except ValueError:
        await screen_reply(message, ADMIN_ACTION_USAGE)
        return
    try:
        mutate(lid, action, ADMIN_ID)
    except ValueError:
        await screen_reply(
            message, f"Лицензия <b>#{lid}</b> не найдена.\n\n" + ADMIN_ACTION_USAGE
        )
        return
    await admin_license_card(
        message,
        lid,
        "Привязка FunPay сброшена." if action == "reset" else "Статус изменён.",
    )


@router.message(F.text.regexp(r"^/license_action[^\s@]"))
async def malformed_admin_action(message):
    if message.from_user.id == ADMIN_ID:
        await screen_reply(message, ADMIN_ACTION_USAGE)


@router.message(Command("license_log"))
async def logs(message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM license_audit "
                + ("WHERE license_id=? " if len(args) > 1 else "")
                + "ORDER BY id DESC LIMIT 25",
                (int(args[1]),) if len(args) > 1 else (),
            ).fetchall()
    except ValueError:
        return
    await screen_reply(
        message,
        html.escape(
            "\n".join(
                f"{r['created_at']} #{r['license_id']} {r['actor']}: {r['action']}"
                for r in rows
            )
        )
        or "Журнал пуст",
    )


class Upload(StatesGroup):
    content = State()
    version = State()


@router.message(Command("license_file", "license_guide"))
async def start_upload(message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        command, pid = message.text.split()
        pid = int(pid)
        with db() as conn:
            if not conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
                raise ValueError()
    except ValueError:
        await screen_reply(message, "Укажите существующий PRODUCT_ID")
        return
    await state.set_state(Upload.content)
    await state.update_data(
        pid=pid, kind="file" if command.split("@")[0] == "/license_file" else "guide"
    )
    await screen_reply(
        message,
        (
            "Отправьте .py файл плагина (для AutoStars — AutoStars.py)."
            if "file" in command
            else "Отправьте подробную инструкцию текстом или .txt (до 100 КБ)."
        ),
    )


@router.message(Upload.content)
async def upload(message, state: FSMContext, bot):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await screen_reply(message, "Отменено")
        return
    data = await state.get_data()
    pid = data["pid"]
    kind = data["kind"]
    doc = message.document
    if kind == "file" and (not doc or not (doc.file_name or "").endswith(".py")):
        await screen_reply(message, "Нужен файл .py")
        return
    if kind == "guide" and doc and not (doc.file_name or "").lower().endswith(".txt"):
        await screen_reply(message, "Для инструкции нужен файл .txt в UTF-8.")
        return
    if doc and (
        not doc.file_size or doc.file_size > (2_000_000 if kind == "file" else 100_000)
    ):
        await screen_reply(message, "Файл слишком большой")
        return
    if kind == "file":
        import secrets
        from services.product_assets import (
            safe_filename,
            normalize_source,
            source_version,
            publish_file,
        )

        with db() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id=?", (pid,)
            ).fetchone()
            previous = asset(conn, pid)
        if not product:
            await screen_reply(message, "Товар не найден")
            return
        autostars = (
            product["name"].replace(" ", "").lower() == "autostars"
            or (previous and Path(previous["file_path"]).name == "AutoStars.py")
            or doc.file_name == "AutoStars.py"
        )
        try:
            filename = "AutoStars.py" if autostars else safe_filename(doc.file_name)
        except ValueError as exc:
            await screen_reply(message, str(exc))
            return
        target = (
            DB_PATH.parent
            / "license_files"
            / str(pid)
            / secrets.token_hex(8)
            / filename
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        await bot.download(doc, destination=target)
        try:
            source = normalize_source(target.read_text(encoding="utf-8-sig"), autostars)
            version = source_version(source)
            target.write_text(source, encoding="utf-8")
        except (ValueError, SyntaxError, UnicodeError):
            target.unlink(missing_ok=True)
            await screen_reply(
                message,
                "Файл отклонён: ошибка Python/UTF-8 или отсутствует проверка лицензии AutoStars.",
            )
            return
        value = str(target.resolve())
        if version is None:
            await state.set_state(Upload.version)
            await state.update_data(pending_file=value)
            await screen_reply(
                message,
                "Файл проверен. В нём не указана версия VERSION. Введите версию, например 2.3. До этого покупатели получают прежний файл.",
                reply_markup=keyboard([[("Отмена", f"prodasset:view:{pid}")]]),
            )
            return
        publish_file(pid, value, version, ADMIN_ID)
        await state.clear()
        await product_assets_card(
            message,
            pid,
            f"Файл обновлён до версии {version}. Прежние покупатели смогут скачать его с тем же ключом.",
        )
        return
    else:
        try:
            value = (
                (await bot.download(doc)).read().decode("utf-8-sig")
                if doc
                else (message.text or "")
            )
        except UnicodeError:
            await screen_reply(message, "Нужен UTF-8 текст")
            return
        if not value.strip():
            return
        if len(value.encode("utf-8")) > 100_000:
            await screen_reply(message, "Инструкция слишком большая")
            return
    with db() as conn:
        old = asset(conn, pid)
        path = value if kind == "file" else (old["file_path"] if old else "")
        guide = value if kind == "guide" else (old["instruction"] if old else "")
        conn.execute(
            "INSERT INTO license_assets(product_id,file_path,instruction) VALUES(?,?,?) ON CONFLICT(product_id) DO UPDATE SET file_path=excluded.file_path,instruction=excluded.instruction",
            (pid, path, guide),
        )
        audit(conn, None, ADMIN_ID, f"asset:{pid}:{kind}")
    await state.clear()
    await product_assets_card(
        message,
        pid,
        "TXT-инструкция обновлена. Для продажи нужны и файл, и инструкция.",
    )


async def product_assets_card(target, pid, note=""):
    with db() as conn:
        product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        data = asset(conn, pid)
    if not product:
        await screen_reply(target, "Товар не найден")
        return
    filename = (
        Path(data["file_path"]).name if data and data["file_path"] else "не загружен"
    )
    text = (
        f"📦 <b>{html.escape(product['name'])}</b>\n\nВерсия в каталоге: {html.escape(product['version'] or 'не указана')}\n"
        f"Файл: {html.escape(filename)}\nTXT-инструкция: {'готова' if data and data['instruction'].strip() else 'не загружена'}\n\n"
        "Выдача: файл плагина + ключ сообщением + INSTALL.txt. Обновление файла доступно по прежним лицензиям."
    )
    if note:
        text += "\n\n" + html.escape(note)
    await screen_reply(
        target,
        text,
        reply_markup=keyboard(
            [
                [("Обновить файл плагина", f"prodasset:file:{pid}")],
                [
                    ("Изменить версию", f"prodasset:version:{pid}"),
                    ("Обновить TXT-инструкцию", f"prodasset:guide:{pid}"),
                ],
                [("Скачать инструкцию", f"prodasset:preview:{pid}")],
                [("← Карточка товара", f"adm:p:{pid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("prodasset:"))
async def product_assets_action(call, state: FSMContext):
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
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone()
    if not exists:
        await screen_reply(call, "Товар не найден")
        return
    await state.clear()
    if action in ("file", "guide"):
        await state.set_state(Upload.content)
        await state.update_data(pid=pid, kind=action)
        await screen_reply(
            call,
            (
                "Отправьте новый .py файл. Версия VERSION будет прочитана автоматически, без запуска кода."
                if action == "file"
                else "Отправьте TXT-файл в UTF-8 с подробной установкой, подключением и настройкой функций плагина (до 100 КБ), либо текст инструкции."
            ),
            reply_markup=keyboard([[("Отмена", f"prodasset:view:{pid}")]]),
        )
        return
    if action == "version":
        await state.set_state(Upload.version)
        await state.update_data(pid=pid)
        await screen_reply(
            call,
            "Введите версию для каталога, например 2.3.",
            reply_markup=keyboard([[("Отмена", f"prodasset:view:{pid}")]]),
        )
        return
    if action == "preview":
        with db() as conn:
            data = asset(conn, pid)
        if data and data["instruction"]:
            await call.message.answer_document(
                BufferedInputFile(
                    data["instruction"].encode("utf-8"), filename="INSTALL.txt"
                )
            )
    await product_assets_card(call, pid)


@router.message(Upload.version)
async def asset_version(message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    from services.product_assets import valid_version, publish_file

    data = await state.get_data()
    pid = data["pid"]
    try:
        version = valid_version(message.text or "")
    except ValueError as exc:
        await screen_reply(message, str(exc))
        return
    if data.get("pending_file"):
        publish_file(pid, data["pending_file"], version, ADMIN_ID)
    else:
        with db() as conn:
            conn.execute("UPDATE products SET version=? WHERE id=?", (version, pid))
            audit(conn, None, ADMIN_ID, f"version:{pid}:{version}")
    await state.clear()
    await product_assets_card(message, pid, f"Версия {version} сохранена.")
