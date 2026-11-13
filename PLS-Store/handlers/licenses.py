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

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
log = logging.getLogger(__name__)


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows
        ]
    )


def title(uid):
    return {"ru": "🔑 Мои лицензии", "en": "🔑 My licenses", "uk": "🔑 Мої ліцензії"}[
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
        data = asset(conn, row["product_id"])
    if not data:
        await bot.send_message(
            uid, "Файл готовится продавцом. Лицензия сохранена в «Мои лицензии»."
        )
        return False
    from services.license_files import plugin_bytes

    filename, content = plugin_bytes(data["file_path"])
    await bot.send_message(
        uid,
        f"🔑 Бессрочная лицензия #{lid}\nКлюч: <code>{html.escape(row['license_key'])}</code>\nПривязка к FunPay при первом запуске.",
        reply_markup=keyboard([[("Мои лицензии", "licenses")]]),
    )
    await bot.send_document(uid, BufferedInputFile(content, filename=filename))
    if filename == "AutoStars.py":
        import json

        await bot.send_document(
            uid,
            BufferedInputFile(
                json.dumps({"key": row["license_key"]}).encode(),
                filename="license.json",
            ),
        )
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


async def show_list(message, uid, page=0):
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
    await message.answer(
        title(uid) if rows else title(uid) + "\nПока нет лицензий.",
        reply_markup=keyboard(buttons + [[("← Профиль", "profile")]]),
    )


@router.callback_query(F.data.startswith("licenses:page:"))
async def license_page(call):
    await call.answer()
    try:
        page = int(call.data.split(":")[-1])
    except ValueError:
        return
    if 0 <= page < 100000:
        await show_list(call.message, call.from_user.id, page)


@router.callback_query(F.data == "licenses")
async def list_licenses(call):
    await call.answer()
    await show_list(call.message, call.from_user.id)


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
        await call.message.answer("Запрос сохранён. Дождитесь решения администратора.")
        if changed and ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                f"Запрос смены привязки #{lid}, покупатель {call.from_user.id}. /license {lid}",
            )
    elif action == "download":
        try:
            success = await deliver(bot, lid, call.from_user.id)
        except (ValueError, OSError, SyntaxError, UnicodeError):
            success = False
        if not success:
            await call.message.answer(
                "Файл недоступен: лицензия заблокирована или файл ещё не загружен. Обратитесь в поддержку."
            )
    elif action == "guide":
        with db() as conn:
            data = asset(conn, row["product_id"])
        if data:
            await call.message.answer_document(
                BufferedInputFile(data["instruction"].encode(), filename="INSTALL.txt")
            )
        else:
            await call.message.answer("Инструкция ещё не загружена продавцом.")
    else:
        await call.message.answer(
            f"🔑 #{lid} · бессрочно\nСтатус: {row['status']}\nFunPay ID: {row['funpay_id'] or 'не привязан'}\nЗапрос смены: {'да' if row['rebind_requested'] else 'нет'}\nКлюч: <code>{html.escape(row['license_key'])}</code>",
            reply_markup=keyboard(
                [
                    [
                        ("Скачать", f"lic:download:{lid}"),
                        ("Инструкция", f"lic:guide:{lid}"),
                    ],
                    [("Запросить смену FunPay", f"lic:request:{lid}")],
                    [("← Мои лицензии", "licenses")],
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


@router.callback_query(F.data == "adm:licenses")
async def admin_menu(call):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    await call.message.answer(ADMIN_HELP)


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
    await message.answer(text or "Не найдено")


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
        await message.answer(
            "Формат: /license_issue TELEGRAM_ID PRODUCT_ID (существующий товар)"
        )
        return
    await message.answer(f"Выдана лицензия #{lid}")
    try:
        await deliver(bot, lid, uid)
    except Exception:
        await message.answer(
            "Лицензия создана, отправка будет повторена автоматически."
        )


@router.message(Command("license_action"))
async def admin_action(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, lid, action = message.text.split()
        if action not in ("block", "unblock", "reset"):
            raise ValueError()
        mutate(int(lid), action, ADMIN_ID)
    except ValueError:
        await message.answer("Формат: /license_action ID block|unblock|reset")
        return
    await message.answer("Изменение сохранено")


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
    await message.answer(
        html.escape(
            "\n".join(
                f"{r['created_at']} #{r['license_id']} {r['actor']}: {r['action']}"
                for r in rows
            )
        )
        or "Журнал пуст"
    )


class Upload(StatesGroup):
    content = State()


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
        await message.answer("Укажите существующий PRODUCT_ID")
        return
    await state.set_state(Upload.content)
    await state.update_data(
        pid=pid, kind="file" if command.split("@")[0] == "/license_file" else "guide"
    )
    await message.answer(
        "Отправьте .py файл плагина (для AutoStars — AutoStars.py)."
        if "file" in command
        else "Отправьте подробную инструкцию текстом или .txt (до 100 КБ)."
    )


@router.message(Upload.content)
async def upload(message, state: FSMContext, bot):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено")
        return
    data = await state.get_data()
    pid = data["pid"]
    kind = data["kind"]
    doc = message.document
    if kind == "file" and (not doc or not (doc.file_name or "").endswith(".py")):
        await message.answer("Нужен файл .py")
        return
    if doc and (
        not doc.file_size or doc.file_size > (2_000_000 if kind == "file" else 100_000)
    ):
        await message.answer("Файл слишком большой")
        return
    if kind == "file":
        import secrets

        target = (
            DB_PATH.parent
            / "license_files"
            / str(pid)
            / secrets.token_hex(8)
            / ("AutoStars.py" if doc.file_name == "AutoStars.py" else "plugin.py")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        await bot.download(doc, destination=target)
        try:
            import ast

            source = target.read_text(encoding="utf-8")
            ast.parse(source)
            if target.name == "AutoStars.py" and (
                "PLS_LICENSE_GUARD_V1" not in source
                or "__PLS_LICENSE_ENDPOINT__" not in source
            ):
                raise ValueError("AutoStars без проверки лицензии")
        except (ValueError, SyntaxError, UnicodeError):
            target.unlink(missing_ok=True)
            await message.answer(
                "Файл отклонён: ошибка Python/UTF-8 или отсутствует проверка лицензии AutoStars."
            )
            return
        value = str(target.resolve())
    else:
        try:
            value = (
                (await bot.download(doc)).read().decode("utf-8")
                if doc
                else (message.text or "")
            )
        except UnicodeError:
            await message.answer("Нужен UTF-8 текст")
            return
        if not value.strip():
            return
        if len(value.encode("utf-8")) > 100_000:
            await message.answer("Инструкция слишком большая")
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
    await message.answer("Сохранено. Для продажи нужны и файл, и инструкция.")
