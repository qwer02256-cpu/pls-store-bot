import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
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
        self.photo = []
        self.from_user = SimpleNamespace(id=chat_id, is_bot=False)
        self.reply_markup = None

    async def answer(self, text, **kwargs):
        return await self.bot.send_message(self.chat.id, text, **kwargs)

    async def answer_photo(self, **kwargs):
        return await self.bot.send_photo(self.chat.id, **kwargs)


class ChatBot:
    def __init__(self):
        self.seq = 100
        self.messages = {}
        self.edits = []
        self.disabled = []
        self.fail_text = False
        self.refuse_edit = False

    def add(self, chat_id, text, kind="text", **kwargs):
        self.seq += 1
        m = ChatMessage(self, chat_id, self.seq, text)
        m.kwargs = kwargs
        m.reply_markup = kwargs.get("reply_markup")
        m.photo = ["photo"] if kind == "photo" else []
        m.from_user.is_bot = True
        self.messages[chat_id, m.message_id] = m
        return m

    def error(self):
        return TelegramBadRequest(
            method=EditMessageText(text="x"), message="message to edit not found"
        )

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_text:
            raise OSError("network")
        return self.add(chat_id, text, **kwargs)

    async def send_photo(self, chat_id, **kwargs):
        return self.add(chat_id, kwargs.get("caption", ""), kind="photo", **kwargs)

    async def send_document(self, chat_id, document, **kwargs):
        return self.add(chat_id, "DOCUMENT", **kwargs)

    async def delete_message(self, *args, **kwargs):
        raise AssertionError("Deletion causes the unwanted Telegram animation")

    def current(self, chat_id, message_id):
        if self.refuse_edit or (chat_id, message_id) not in self.messages:
            raise self.error()
        return self.messages[chat_id, message_id]

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        if self.fail_text:
            raise OSError("network")
        m = self.current(chat_id, message_id)
        if m.photo:
            raise TelegramBadRequest(
                method=EditMessageText(text="x"),
                message="there is no text in the message to edit",
            )
        m.text = text
        m.reply_markup = kwargs.get("reply_markup")
        self.edits.append(("text", message_id))
        return m

    async def edit_message_caption(self, chat_id, message_id, caption, **kwargs):
        m = self.current(chat_id, message_id)
        if not m.photo:
            raise self.error()
        m.text = caption
        m.reply_markup = kwargs.get("reply_markup")
        self.edits.append(("caption", message_id))
        return m

    async def edit_message_media(self, chat_id, message_id, media, **kwargs):
        m = self.current(chat_id, message_id)
        m.photo = ["photo"]
        m.text = media.caption
        m.reply_markup = kwargs.get("reply_markup")
        self.edits.append(("media", message_id))
        return m

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.disabled.append((chat_id, message_id))
        if (chat_id, message_id) in self.messages:
            self.messages[chat_id, message_id].reply_markup = reply_markup


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
        self.command = ChatMessage(self.bot, 10, 1, "/start")
        with db.db() as conn:
            pid = conn.execute(
                "INSERT INTO products(name,price,description) VALUES('Plugin','349','Description')"
            ).lastrowid
            self.lid = issue(conn, 10, pid)
            conn.execute(
                "UPDATE licenses SET funpay_id='13540460',rebind_requested=1 WHERE id=?",
                (self.lid,),
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

    async def test_home_license_list_detail_and_back_edit_one_message(self):
        home = await main.safe_edit_or_send(self.command, "Home")
        mid = home.message_id
        await licenses.list_licenses(self.call(home, "licenses"))
        await licenses.license_action(self.call(home, f"lic:view:{self.lid}"), self.bot)
        self.assertIn("FunPay ID: 13540460", home.text)
        await licenses.list_licenses(self.call(home, "licenses"))
        self.assertEqual(self.tracked(), {mid})
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(len(self.bot.edits), 3)

    async def test_each_command_sends_new_reply_without_deleting_user_commands(self):
        with patch.object(licenses, "ADMIN_ID", 10):
            for mid in (1, 2):
                await licenses.admin_action(
                    ChatMessage(self.bot, 10, mid, "/license_action 13540460 reset")
                )
        self.assertEqual(len(self.bot.messages), 2)
        self.assertEqual(self.bot.edits, [])
        self.assertEqual(len(self.tracked()), 1)
        last = self.bot.messages[10, max(self.tracked())]
        self.assertIn("/license_action 1 reset", last.text)
        self.assertEqual(owned(self.lid, 10)["funpay_id"], "13540460")

    async def test_profile_command_starts_new_screen_then_buttons_edit_it(self):
        old = await screen_send(self.bot, 10, "Old")
        new = await screen_reply(ChatMessage(self.bot, 10, 2, "/profile"), "Profile")
        self.assertNotEqual(new.message_id, old.message_id)
        self.assertEqual(old.text, "Old")
        self.assertIn((10, old.message_id), self.bot.disabled)
        await screen_reply(self.call(new, "licenses"), "Licenses")
        self.assertEqual(new.text, "Licenses")
        self.assertEqual(self.tracked(), {new.message_id})
        self.assertEqual(len(self.bot.messages), 2)

    async def test_regular_text_input_still_edits_current_screen(self):
        old = await screen_send(self.bot, 10, "Enter price")
        new = await screen_reply(ChatMessage(self.bot, 10, 2, "349"), "Saved")
        self.assertEqual(old.message_id, new.message_id)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_mentioned_command_with_arguments_also_sends_new_reply(self):
        old = await screen_send(self.bot, 10, "Old")
        new = await screen_reply(
            ChatMessage(self.bot, 10, 2, "/license@PLSStore1_bot 1"), "Result"
        )
        self.assertNotEqual(old.message_id, new.message_id)
        self.assertEqual(self.tracked(), {new.message_id})

    async def test_parallel_callbacks_reuse_same_message(self):
        old = await screen_reply(self.command, "Old")
        await asyncio.gather(
            screen_reply(self.call(old, "x"), "One"),
            screen_reply(self.call(old, "y"), "Two"),
        )
        self.assertEqual(self.tracked(), {old.message_id})
        self.assertEqual(len(self.bot.messages), 1)

    async def test_different_chats_remain_independent(self):
        other = await screen_send(self.bot, 20, "Other")
        await screen_reply(self.command, "First")
        await screen_reply(self.command, "Second")
        self.assertEqual(other.text, "Other")
        self.assertEqual(len(self.tracked(10)), 1)
        self.assertEqual(len(self.tracked(20)), 1)

    async def test_network_error_does_not_send_replacement_or_delete(self):
        old = await screen_reply(self.command, "Old")
        self.bot.fail_text = True
        with self.assertRaises(OSError):
            await screen_reply(self.call(old, "x"), "New")
        self.assertEqual(old.text, "Old")
        self.assertEqual(len(self.bot.messages), 1)

    async def test_uneditable_message_falls_back_without_deletion(self):
        old = await screen_reply(self.command, "Old")
        self.bot.refuse_edit = True
        new = await screen_reply(self.call(old, "x"), "New")
        self.assertNotEqual(old.message_id, new.message_id)
        self.assertIn((10, old.message_id), self.bot.disabled)
        self.assertEqual(self.tracked(), {new.message_id})

    async def test_photo_to_licenses_and_back_uses_caption_media_edits(self):
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        home = await screen_reply(self.command, "Home", photo=photo)
        mid = home.message_id
        await licenses.list_licenses(self.call(home, "licenses"))
        self.assertTrue(home.photo)
        self.assertIn(("media", mid), self.bot.edits)
        await screen_reply(self.call(home, "home"), "Home again", photo=photo)
        self.assertIn(("media", mid), self.bot.edits)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_text_message_can_become_photo_without_replacement(self):
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        old = await screen_reply(self.command, "Text")
        new = await screen_reply(self.call(old, "x"), "Banner", photo=photo)
        self.assertEqual(old.message_id, new.message_id)
        self.assertTrue(new.photo)

    async def test_long_banner_pair_is_reused_and_documents_preserved(self):
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        content = await screen_reply(self.command, "Text" * 400, photo=photo)
        pair = self.tracked()
        document = await self.bot.send_document(10, "plugin.py")
        await screen_reply(self.call(content, "x"), "Short", photo=photo)
        self.assertEqual(self.tracked(), pair)
        self.assertIn((10, document.message_id), self.bot.messages)
        self.assertEqual(len(self.bot.messages), 3)

    async def test_photo_long_text_adds_companion_once_then_reuses_it(self):
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        old = await screen_reply(self.command, "Home", photo=photo)
        long = await screen_reply(self.call(old, "x"), "Text" * 400)
        pair = self.tracked()
        self.assertEqual(len(pair), 2)
        await screen_reply(self.call(long, "x"), "Next short")
        self.assertEqual(self.tracked(), pair)
        self.assertEqual(len(self.bot.messages), 2)

    async def test_failed_long_text_send_keeps_photo_caption_and_buttons(self):
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        old = await screen_reply(self.command, "Usable menu", photo=photo)
        self.bot.fail_text = True
        with self.assertRaises(OSError):
            await screen_reply(self.call(old, "x"), "Long" * 400)
        self.assertEqual(old.text, "Usable menu")
        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.tracked(), {old.message_id})

    async def test_old_unknown_photo_record_is_detected_without_replacement(self):
        photo = Path(self.tmp.name) / "banner.png"
        photo.touch()
        old = await screen_reply(self.command, "Home", photo=photo)
        with db.db() as conn:
            conn.execute("UPDATE chat_screens SET kind='unknown'")
        await screen_reply(self.call(old, "x"), "Licenses")
        self.assertEqual(self.tracked(), {old.message_id})
        self.assertEqual(len(self.bot.messages), 1)

    async def test_not_modified_does_not_send_another_message(self):
        old = await screen_reply(self.command, "Home")
        with patch.object(
            self.bot,
            "edit_message_text",
            AsyncMock(
                side_effect=TelegramBadRequest(
                    method=EditMessageText(text="x"), message="message is not modified"
                )
            ),
        ):
            await screen_reply(self.call(old, "x"), "Home")
        self.assertEqual(len(self.bot.messages), 1)

    async def test_pending_payment_buttons_are_preserved(self):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Pay", url="https://example.com/pay")],
                [InlineKeyboardButton(text="Check", callback_data="paystatus:tx")],
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
        self.assertEqual(invoice.reply_markup, markup)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_home_clears_input_state(self):
        state = SimpleNamespace(clear=AsyncMock())
        with patch.object(
            main, "has_accepted_license", return_value=True
        ), patch.object(main, "show_home", AsyncMock()):
            await main.cb_home(self.call(self.command, "home"), state)
        state.clear.assert_awaited_once()

    async def test_admin_reset_confirmation_uses_same_screen(self):
        old = await screen_reply(self.command, "Admin")
        with patch.object(licenses, "ADMIN_ID", 10):
            await licenses.admin_license_callback(
                self.call(old, f"admlic:reset:{self.lid}")
            )
            self.assertEqual(owned(self.lid, 10)["funpay_id"], "13540460")
            await licenses.admin_license_callback(
                self.call(old, f"admlic:do_reset:{self.lid}")
            )
        self.assertIsNone(owned(self.lid, 10)["funpay_id"])
        self.assertEqual(len(self.bot.messages), 1)

    async def test_non_admin_cannot_reset(self):
        old = await screen_reply(self.command, "Buyer")
        with patch.object(licenses, "ADMIN_ID", 99):
            await licenses.admin_license_callback(
                self.call(old, f"admlic:do_reset:{self.lid}")
            )
        self.assertEqual(owned(self.lid, 10)["funpay_id"], "13540460")
