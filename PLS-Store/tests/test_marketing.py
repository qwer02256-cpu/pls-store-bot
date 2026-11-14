import tempfile
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
)
from aiogram.methods import SendMessage
import main
from handlers import marketing
from services import db, broadcasts
from services.checkout import prepare_checkout, balance_checkout
from services.pricing import product_price, price_label, percent, set_discount
from services.payment_ledger import settle_payment


class PricingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "DB_PATH", Path(self.tmp.name) / "db")
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(10)
        main.accept_license(10)
        path = Path(self.tmp.name) / "plugin.py"
        path.write_text("# plugin")
        with db.db() as conn:
            self.pid = conn.execute(
                "INSERT INTO products(name,price,price_rub,description) VALUES('Plugin','349₽ / Навсегда',349,'Description')"
            ).lastrowid
            conn.execute(
                "INSERT INTO license_assets VALUES(?,?,?)",
                (self.pid, str(path), "Guide"),
            )

    def product(self):
        return db.get_product(self.pid)

    def test_percent_validation_and_reset_preserve_base_price(self):
        set_discount(self.pid, "15,5%", 1)
        product = self.product()
        self.assertEqual(product["price_rub"], 349)
        self.assertEqual(product_price(product), Decimal("294.91"))
        set_discount(self.pid, 0, 1)
        self.assertEqual(product_price(self.product()), Decimal("349.00"))
        self.assertEqual(price_label(self.product()), "349₽ / Навсегда")
        with db.db() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM product_discount_log").fetchone()[0],
                2,
            )
        for value in ("-1", "100", "NaN", "Infinity", "1.234", "abc"):
            with self.assertRaises(ValueError):
                percent(value)

    def test_discount_rounds_half_up_and_price_never_zero(self):
        product = dict(self.product(), price_rub=1.05, discount_percent=50)
        self.assertEqual(product_price(product), Decimal(".53"))
        self.assertEqual(
            product_price(dict(product, price_rub=0.01, discount_percent=99)),
            Decimal(".01"),
        )

    async def test_catalog_card_and_buy_button_show_discounted_price(self):
        set_discount(self.pid, 10, 1)
        caption = main.build_product_caption(self.product())
        self.assertIn("<s>349₽ / Навсегда</s>", caption)
        self.assertIn("314.10₽", caption)
        call = SimpleNamespace(
            data=f"product:{self.pid}",
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
        )
        with patch.object(main, "safe_edit_or_send", AsyncMock()) as render:
            await main.cb_product(call)
        button = render.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertIn("314.10", button.text)

    def test_fully_funded_balance_purchase_uses_discount(self):
        db.change_balance(10, 349, "Initial")
        set_discount(self.pid, 10, 1)
        prepare_checkout(10, self.pid, "a" * 24)
        result = balance_checkout(10, "a" * 24)["result"]
        self.assertEqual(result["amount"], 314.1)
        self.assertEqual(db.get_user(10)["balance"], 34.9)
        self.assertEqual(db.get_user(10)["total_spent"], 314.1)

    async def test_discount_shortfall_and_price_snapshot_survive_sale_changes(self):
        db.change_balance(10, 100, "Initial")
        set_discount(self.pid, 10, 1)
        provider = SimpleNamespace(
            create_payment=AsyncMock(
                return_value={
                    "id": "sale",
                    "redirect": "https://app.platega.io/pay/sale",
                }
            )
        )
        message = SimpleNamespace(answer=AsyncMock())
        call = SimpleNamespace(
            data=f"purchasepay:2:{self.pid}:" + ("a" * 24),
            message=message,
            from_user=SimpleNamespace(id=10, username=None, first_name="Buyer"),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="store_bot"))
        )
        with patch.object(main, "platega", provider):
            await main.cb_purchase_method(call, bot)
        self.assertEqual(provider.create_payment.call_args.kwargs["amount"], 214.1)
        self.assertEqual(db.get_user(10)["balance"], 100)
        set_discount(self.pid, 50, 1)
        result = settle_payment(
            "sale", dict(status="CONFIRMED", amount=214.10, currency="RUB")
        )
        self.assertEqual(result["amount"], 314.1)
        self.assertEqual(db.get_user(10)["balance"], 0)
        self.assertIsNone(
            settle_payment("sale", dict(status="CONFIRMED", amount=214.10))
        )

    def test_new_checkout_gets_new_discount_old_checkout_keeps_quoted_price(self):
        first = prepare_checkout(10, self.pid, "a" * 24)
        set_discount(self.pid, 20, 1)
        self.assertEqual(
            prepare_checkout(10, self.pid, "a" * 24)["price"], first["price"]
        )
        self.assertEqual(prepare_checkout(10, self.pid, "b" * 24)["price"], 279.2)

    async def test_invalid_base_price_shows_unavailable_instead_of_crashing(self):
        with db.db() as conn:
            conn.execute(
                "UPDATE products SET price='0₽',price_rub=0 WHERE id=?", (self.pid,)
            )
        call = SimpleNamespace(
            data=f"product:{self.pid}",
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
        )
        with patch.object(main, "screen_reply", AsyncMock()) as render:
            await main.cb_product(call)
        self.assertIn("недоступен", render.call_args.args[1])
        with patch.object(marketing, "screen_reply", AsyncMock()) as render:
            await marketing.discount_card(call, self.pid)
        self.assertIn("базовую цену", render.call_args.args[1])

    async def test_buyer_cannot_change_discount_through_ui(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=10), text="90", answer=AsyncMock()
        )
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"discount_product_id": self.pid}),
            clear=AsyncMock(),
        )
        with patch.object(marketing, "ADMIN_ID", 1):
            await marketing.receive_discount(message, state)
        self.assertEqual(self.product()["discount_percent"], 0)
        state.get_data.assert_not_awaited()


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "DB_PATH", Path(self.tmp.name) / "db")
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        for uid in (1, 2, 3):
            db.ensure_user(uid)
        self.bot = SimpleNamespace(send_message=AsyncMock())

    def draft(self):
        return broadcasts.draft(1, "<b>Sale</b> &amp; news")

    async def drain(self):
        for _ in range(10):
            if await broadcasts.process_one(self.bot, now=100) is None:
                break

    async def test_draft_never_sends_and_enqueue_is_idempotent_with_audience_snapshot(
        self,
    ):
        bid = self.draft()
        self.assertIsNone(await broadcasts.process_one(self.bot, now=100))
        self.bot.send_message.assert_not_awaited()
        self.assertTrue(broadcasts.enqueue(bid, 1))
        self.assertFalse(broadcasts.enqueue(bid, 1))
        db.ensure_user(4)
        await self.drain()
        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertEqual(
            {c.args[0] for c in self.bot.send_message.await_args_list}, {1, 2, 3}
        )
        for call in self.bot.send_message.await_args_list:
            self.assertEqual(call.args[1], "<b>Sale</b> &amp; news")
            self.assertEqual(call.kwargs["parse_mode"], "HTML")
        stats = broadcasts.stats(bid, 1)
        self.assertEqual(stats["status"], "completed")
        self.assertEqual(stats["counts"]["sent"], 3)
        await self.drain()
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_forbidden_user_does_not_stop_other_recipients(self):
        bid = self.draft()
        broadcasts.enqueue(bid, 1)
        self.bot.send_message.side_effect = [
            TelegramForbiddenError(
                method=SendMessage(chat_id=1, text="x"), message="blocked"
            ),
            None,
            None,
        ]
        await self.drain()
        stats = broadcasts.stats(bid, 1)
        self.assertEqual(stats["counts"], {"blocked": 1, "sent": 2})
        self.assertEqual(stats["status"], "completed")

    async def test_rate_limit_schedules_retry_without_marking_sent(self):
        bid = self.draft()
        broadcasts.enqueue(bid, 1)
        self.bot.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="x"),
            message="rate limit",
            retry_after=10,
        )
        self.assertEqual(await broadcasts.process_one(self.bot, now=100), 10)
        with db.db() as conn:
            row = conn.execute(
                "SELECT * FROM broadcast_recipients WHERE user_id=1"
            ).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertGreaterEqual(row["next_attempt_at"], 110)
        self.bot.send_message.side_effect = None
        await broadcasts.process_one(self.bot, now=101)
        self.assertEqual(self.bot.send_message.call_args.args[0], 2)
        await broadcasts.process_one(self.bot, now=112)
        self.assertEqual(self.bot.send_message.call_args.args[0], 1)

    async def test_network_unknown_is_not_automatically_repeated(self):
        bid = self.draft()
        broadcasts.enqueue(bid, 1)
        self.bot.send_message.side_effect = [OSError("lost response"), None, None]
        await self.drain()
        await self.drain()
        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertEqual(broadcasts.stats(bid, 1)["counts"], {"unknown": 1, "sent": 2})

    async def test_restart_marks_inflight_uncertain_and_resumes_pending_only(self):
        bid = self.draft()
        broadcasts.enqueue(bid, 1)
        with db.db() as conn:
            conn.execute(
                "UPDATE broadcast_recipients SET status='sending' WHERE user_id=1"
            )
            conn.execute("UPDATE broadcasts SET status='running'")
        broadcasts.recover()
        await self.drain()
        self.assertEqual(
            {c.args[0] for c in self.bot.send_message.await_args_list}, {2, 3}
        )
        self.assertEqual(broadcasts.stats(bid, 1)["counts"], {"unknown": 1, "sent": 2})

    async def test_cancel_stops_remaining_sends(self):
        bid = self.draft()
        broadcasts.enqueue(bid, 1)
        await broadcasts.process_one(self.bot, now=100)
        broadcasts.cancel(bid, 1)
        await self.drain()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(broadcasts.stats(bid, 1)["status"], "cancelled")

    def test_wrong_owner_cannot_enqueue_or_cancel(self):
        bid = self.draft()
        with self.assertRaises(ValueError):
            broadcasts.enqueue(bid, 99)
        broadcasts.cancel(bid, 99)
        self.assertEqual(broadcasts.stats(bid, 1)["status"], "draft")
        self.assertIsNone(broadcasts.stats(bid, 99))

    async def test_ui_preview_does_not_enqueue_until_explicit_confirmation(self):
        state = SimpleNamespace(get_data=AsyncMock(return_value={}), clear=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            text="News <safe>",
            html_text="News &lt;safe&gt;",
            answer=AsyncMock(),
        )
        with patch.object(marketing, "ADMIN_ID", 1), patch.object(
            marketing, "screen_reply", AsyncMock()
        ) as render:
            await marketing.receive_broadcast(message, state)
            with db.db() as conn:
                row = conn.execute("SELECT * FROM broadcasts").fetchone()
                self.assertEqual(row["status"], "draft")
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM broadcast_recipients"
                    ).fetchone()[0],
                    0,
                )
            self.assertEqual(render.call_args.args[1], "News &lt;safe&gt;")
            call = SimpleNamespace(
                data=f"cast:{row['id']}:send",
                from_user=SimpleNamespace(id=1),
                answer=AsyncMock(),
            )
            await marketing.broadcast_action(call, state)
            await marketing.broadcast_action(call, state)
            self.assertEqual(broadcasts.stats(row["id"], 1)["total"], 3)
        self.bot.send_message.assert_not_awaited()

    async def test_buyer_cannot_start_broadcast_from_ui(self):
        bid = self.draft()
        call = SimpleNamespace(
            data=f"cast:{bid}:send", from_user=SimpleNamespace(id=2), answer=AsyncMock()
        )
        with patch.object(marketing, "ADMIN_ID", 1):
            await marketing.broadcast_action(call, SimpleNamespace())
        self.assertEqual(broadcasts.stats(bid, 1)["status"], "draft")

    async def test_broadcast_does_not_modify_store_screen(self):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO chat_screens(chat_id,message_id,kind) VALUES(1,555,'text')"
            )
        bid = self.draft()
        broadcasts.enqueue(bid, 1)
        await self.drain()
        with db.db() as conn:
            self.assertEqual(
                conn.execute("SELECT message_id FROM chat_screens").fetchone()[0], 555
            )
