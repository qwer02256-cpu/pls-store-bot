import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import main
from handlers import licenses
from services import db
from services.licenses import issue, owned
from services.screens import screen_reply, screen_send


class ChatMessage:
    def __init__(self, bot, chat_id, mid, text=""):
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = mid
        self.text = text
        self.from_user = SimpleNamespace(id=chat_id)

    async def answer(self, text, **kwargs):
        return await self.bot.send_message(self.chat.id, text, **kwargs)

    async def answer_photo(self, **kwargs):
        return await self.bot.send_photo(self.chat.id, **kwargs)


class ChatBot:
    def __init__(self):
        self.seq = 100
        self.messages = {}
        self.deleted = []
        self.disabled = []
        self.fail_text = False
        self.delete_errors = {}

    def add(self, chat_id, text, **kwargs):
        self.seq += 1
        message = ChatMessage(self, chat_id, self.seq, text)
        message.kwargs = kwargs
        message.reply_markup = kwargs.get("reply_markup")
        self.messages[chat_id, self.seq] = message
        return message

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_text:
            raise OSError("network")
        return self.add(chat_id, text, **kwargs)

    async def send_photo(self, chat_id, **kwargs):
        return self.add(chat_id, kwargs.get("caption", ""), **kwargs)

    async def send_document(self, chat_id, document, **kwargs):
        return self.add(chat_id, "DOCUMENT", **kwargs)

    async def delete_message(self, chat_id, mid):
        if mid in self.delete_errors:
            raise self.delete_errors[mid]
        self.deleted.append((chat_id, mid))
        self.messages.pop((chat_id, mid), None)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.disabled.append((chat_id, message_id))


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "DB_PATH", Path(self.tmp.name) / "store.db")
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(10)
        self.bot = ChatBot()
        self.user = SimpleNamespace(id=10, username="buyer", first_name="Buyer")
        self.command = ChatMessage(self.bot, 10, 1, "/start")
        with db.db() as conn:
            pid = conn.execute(
                "INSERT INTO products(name,price,description) VALUES('Plugin','349','Description')"
            ).lastrowid
            self.lid = issue(conn, 10, pid)
            conn.execute(
                "UPDATE licenses SET funpay_id=?,rebind_requested=1 WHERE id=?",
                ("13540460", self.lid),
            )

    def call(self, message, data, uid=10):
        return SimpleNamespace(
            message=message,
            data=data,
            from_user=SimpleNamespace(id=uid, first_name="User"),
            answer=AsyncMock(),
        )

    def tracked(self, chat_id=10):
        with db.db() as conn:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT message_id FROM chat_screens WHERE chat_id=?", (chat_id,)
                )
            }

    async def test_home_license_list_detail_and_back_replace_each_other(self):
        home = await main.safe_edit_or_send(self.command, "Home")
        await licenses.list_licenses(self.call(home, "licenses"))
        listing = self.bot.messages[10, max(self.tracked())]
        await licenses.license_action(
            self.call(listing, f"lic:view:{self.lid}"), self.bot
        )
        detail = self.bot.messages[10, max(self.tracked())]
        self.assertIn("FunPay ID: 13540460", detail.text)
        await licenses.list_licenses(self.call(detail, "licenses"))
        self.assertEqual(len(self.tracked()), 1)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn((10, home.message_id), self.bot.deleted)
        self.assertIn((10, detail.message_id), self.bot.deleted)

    async def test_command_replies_remove_input_and_previous_error(self):
        with patch.object(licenses, "ADMIN_ID", 10):
            for mid in (1, 2):
                message = ChatMessage(
                    self.bot, 10, mid, "/license_action 13540460 reset"
                )
                await licenses.admin_action(message)
                last = self.bot.messages[10, max(self.tracked())]
                self.assertIn("не найдена", last.text)
                self.assertIn("/license_action 1 reset", last.text)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn((10, 1), self.bot.deleted)
        self.assertIn((10, 2), self.bot.deleted)
        self.assertEqual(owned(self.lid, 10)["funpay_id"], "13540460")

    async def test_persistent_ids_clean_previous_reply_from_different_source(self):
        first = await screen_send(self.bot, 10, "Old screen")
        await screen_reply(ChatMessage(self.bot, 10, 20, "/profile"), "New screen")
        self.assertIn((10, first.message_id), self.bot.deleted)
        self.assertEqual(len(self.tracked()), 1)

    async def test_parallel_callbacks_leave_one_screen(self):
        initial = await screen_reply(self.command, "Initial")
        await asyncio.gather(
            screen_reply(self.call(initial, "x"), "One"),
            screen_reply(self.call(initial, "y"), "Two"),
        )
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(len(self.tracked()), 1)

    async def test_different_chats_do_not_delete_each_other(self):
        other = await screen_send(self.bot, 20, "Other chat")
        await screen_reply(self.command, "First")
        await screen_reply(self.command, "Second")
        self.assertIn((20, other.message_id), self.bot.messages)
        self.assertEqual(len(self.tracked(10)), 1)
        self.assertEqual(len(self.tracked(20)), 1)

    async def test_failed_new_send_keeps_old_screen(self):
        old = await screen_reply(self.command, "Old")
        self.bot.fail_text = True
        with self.assertRaises(OSError):
            await screen_reply(self.call(old, "x"), "New")
        self.assertIn((10, old.message_id), self.bot.messages)
        self.assertEqual(self.tracked(), {old.message_id})

    async def test_transient_delete_failure_is_retried_on_next_screen(self):
        old = await screen_reply(self.command, "Old")
        self.bot.delete_errors[old.message_id] = OSError("network")
        middle = await screen_reply(self.call(old, "x"), "Middle")
        self.assertEqual(self.tracked(), {old.message_id, middle.message_id})
        self.bot.delete_errors.clear()
        new = await screen_reply(self.call(middle, "x"), "New")
        self.assertEqual(self.tracked(), {new.message_id})
        self.assertNotIn((10, old.message_id), self.bot.messages)

    async def test_telegram_refuses_deletion_stale_buttons_are_disabled(self):
        old = await screen_reply(self.command, "Old")
        self.bot.delete_errors[old.message_id] = TelegramBadRequest(
            method=DeleteMessage(chat_id=10, message_id=old.message_id),
            message="message can't be deleted",
        )
        new = await screen_reply(self.call(old, "x"), "New")
        self.assertIn((10, old.message_id), self.bot.disabled)
        self.assertEqual(self.tracked(), {new.message_id})

    async def test_long_banner_pair_is_removed_together_and_downloads_preserved(self):
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        content = await screen_reply(self.command, "Text" * 400, photo=photo)
        pair = self.tracked()
        self.assertEqual(len(pair), 2)
        document = await self.bot.send_document(10, document="plugin.py")
        new = await screen_reply(self.call(content, "x"), "Next")
        self.assertTrue(all((10, mid) in self.bot.deleted for mid in pair))
        self.assertIn((10, document.message_id), self.bot.messages)
        self.assertEqual(self.tracked(), {new.message_id})

    async def test_failed_long_screen_removes_partial_banner_and_keeps_old(self):
        old = await screen_reply(self.command, "Old")
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        self.bot.fail_text = True
        with self.assertRaises(OSError):
            await screen_reply(self.call(old, "x"), "Text" * 400, photo=photo)
        self.assertEqual(self.tracked(), {old.message_id})
        self.assertEqual(len(self.bot.messages), 1)

    async def test_pending_payment_status_preserves_pay_and_check_buttons(self):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Оплатить", url="https://example.com/pay")],
                [InlineKeyboardButton(text="Проверить", callback_data="paystatus:tx")],
            ]
        )
        invoice = await screen_reply(self.command, "Invoice", reply_markup=markup)
        with db.db() as conn:
            conn.execute(
                "INSERT INTO payments(user_id,amount,platega_id) VALUES(10,100,'tx')"
            )
        with patch.object(
            main,
            "platega",
            SimpleNamespace(get_status=AsyncMock(return_value={"status": "PENDING"})),
        ):
            await main.cb_pay_status(self.call(invoice, "paystatus:tx"), self.bot)
        current = self.bot.messages[10, max(self.tracked())]
        self.assertEqual(current.reply_markup, markup)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_home_button_clears_abandoned_input_state(self):
        state = SimpleNamespace(clear=AsyncMock())
        with patch.object(
            main, "has_accepted_license", return_value=True
        ), patch.object(main, "show_home", AsyncMock()):
            await main.cb_home(self.call(self.command, "home"), state)
        state.clear.assert_awaited_once()

    async def test_admin_reset_button_requires_confirmation_and_correct_license(self):
        initial = await screen_reply(self.command, "Admin")
        with patch.object(licenses, "ADMIN_ID", 10):
            await licenses.admin_license_callback(
                self.call(initial, f"admlic:reset:{self.lid}")
            )
            self.assertEqual(owned(self.lid, 10)["funpay_id"], "13540460")
            prompt = self.bot.messages[10, max(self.tracked())]
            self.assertIn("13540460", prompt.text)
            await licenses.admin_license_callback(
                self.call(prompt, f"admlic:do_reset:{self.lid}")
            )
        self.assertIsNone(owned(self.lid, 10)["funpay_id"])
        self.assertEqual(owned(self.lid, 10)["rebind_requested"], 0)
        self.assertEqual(len(self.bot.messages), 1)
        with db.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM license_audit WHERE action='reset'"
                ).fetchone()[0],
                1,
            )

    async def test_non_admin_cannot_reset_through_callback(self):
        initial = await screen_reply(self.command, "Buyer")
        with patch.object(licenses, "ADMIN_ID", 99):
            await licenses.admin_license_callback(
                self.call(initial, f"admlic:do_reset:{self.lid}")
            )
        self.assertEqual(owned(self.lid, 10)["funpay_id"], "13540460")

    async def test_correct_admin_command_resets_and_opens_management_card(self):
        message = ChatMessage(self.bot, 10, 1, f"/license_action {self.lid} reset")
        with patch.object(licenses, "ADMIN_ID", 10):
            await licenses.admin_action(message)
        self.assertIsNone(owned(self.lid, 10)["funpay_id"])
        last = self.bot.messages[10, max(self.tracked())]
        self.assertIn("Привязка FunPay сброшена", last.text)
        self.assertTrue(
            any(
                b.callback_data == f"admlic:reset:{self.lid}"
                for row in last.kwargs["reply_markup"].inline_keyboard
                for b in row
            )
        )
