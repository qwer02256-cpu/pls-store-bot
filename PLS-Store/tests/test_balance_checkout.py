import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from services import db
from services.checkout import prepare_checkout, balance_checkout
from services.payment_ledger import settle_payment
from services.payment_monitor import reconcile_pending
from services.platega import PlategaError


class BalanceCheckoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(db, "DB_PATH", self.root / "store.db")
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(10, username="buyer")
        main.accept_license(10)
        db.change_balance(10, 100, "Initial funds")
        plugin = self.root / "plugin.py"
        plugin.write_text("# test plugin")
        with db.db() as conn:
            self.pid = conn.execute(
                "INSERT INTO products(name,price,price_rub,description) VALUES('Auto plugin','349₽',349,'description')"
            ).lastrowid
            conn.execute(
                "INSERT INTO license_assets VALUES(?,?,?)",
                (self.pid, str(plugin), "Detailed installation guide"),
            )
        self.user = SimpleNamespace(id=10, username="buyer", first_name="Buyer")
        self.message = SimpleNamespace(answer=AsyncMock(), message_id=123)
        self.call = SimpleNamespace(
            answer=AsyncMock(),
            from_user=self.user,
            message=self.message,
            data=f"buyproduct:{self.pid}:" + ("a" * 24),
        )
        self.bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="store_bot")),
            send_message=AsyncMock(),
            send_document=AsyncMock(),
        )
        self.token = "a" * 24

    def product(self):
        return db.get_product(self.pid)

    def quote(self, token=None):
        token = token or self.token
        prepare_checkout(10, self.pid, token)
        return balance_checkout(10, token)

    def balance(self):
        return db.get_user(10)["balance"]

    def counts(self):
        with db.db() as conn:
            return tuple(
                conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ("purchases", "licenses", "orders")
            )

    async def invoice(self, tx="tx", token=None):
        provider = SimpleNamespace(
            create_payment=AsyncMock(
                return_value={"id": tx, "redirect": "https://app.platega.io/pay/" + tx}
            )
        )
        self.call.data = f"purchasepay:2:{self.pid}:{token or self.token}"
        with patch.object(main, "platega", provider):
            await main.cb_purchase_method(self.call, self.bot)
        return provider

    def confirm(self, tx="tx", amount=249):
        return settle_payment(
            tx, dict(id=tx, status="CONFIRMED", amount=amount, currency="RUB")
        )

    async def test_insufficient_balance_menu_shows_only_missing_amount_without_debit(
        self,
    ):
        with patch.object(main, "safe_edit_or_send", AsyncMock()) as render:
            await main.cb_buy_balance(self.call, self.bot)
        text = render.call_args.args[1]
        for line in (
            "Цена: <b>349.00₽",
            "С баланса: <b>100.00₽",
            "Доплатить: <b>249.00₽",
            "Если не оплатите",
        ):
            self.assertIn(line, text)
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertIn(
            self.token,
            render.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
        )

    async def test_full_balance_pays_without_provider_and_duplicate_click_is_safe(self):
        db.change_balance(10, 400, "Top up")
        with patch.object(main, "platega", None):
            await main.cb_buy_balance(self.call, self.bot)
            await main.cb_buy_balance(self.call, self.bot)
        self.assertEqual(self.balance(), 151)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(self.bot.send_document.await_count, 3)
        with db.db() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM payments").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT total_spent FROM users WHERE user_id=10"
                ).fetchone()[0],
                349,
            )

    async def test_exact_balance_is_spent_once(self):
        db.change_balance(10, 249, "Top up")
        await main.cb_buy_balance(self.call, self.bot)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_partial_invoice_requests_shortfall_and_preserves_balance_until_confirmed(
        self,
    ):
        provider = await self.invoice()
        self.assertEqual(provider.create_payment.call_args.kwargs["amount"], 249)
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.counts(), (0, 0, 0))
        with db.db() as conn:
            payment = conn.execute("SELECT * FROM payments").fetchone()
            self.assertEqual(
                (payment["amount"], payment["product_price"], payment["checkout_id"]),
                (249, 349, self.token),
            )
        result = self.confirm()
        self.assertEqual((result["amount"], result["balance"]), (349, 0))
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertIsNone(self.confirm())
        with db.db() as conn:
            deltas = [
                r[0] for r in conn.execute("SELECT delta FROM balance_log ORDER BY id")
            ]
            self.assertEqual(deltas, [100, -100])

    async def test_zero_balance_requests_entire_price(self):
        db.change_balance(10, -100, "Spent")
        provider = await self.invoice()
        self.assertEqual(provider.create_payment.call_args.kwargs["amount"], 349)
        self.confirm(amount=349)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_cancelled_and_expired_invoices_never_touch_balance(self):
        for status in ("CANCELED", "EXPIRED"):
            tx = status.lower()
            await self.invoice(tx)
            client = SimpleNamespace(
                get_status=AsyncMock(return_value={"id": tx, "status": status})
            )
            await reconcile_pending(client, AsyncMock())
            self.assertEqual(self.balance(), 100)
            self.assertEqual(self.counts(), (0, 0, 0))

    async def test_provider_failure_leaves_balance_and_order_untouched(self):
        provider = SimpleNamespace(
            create_payment=AsyncMock(side_effect=PlategaError("offline"))
        )
        self.call.data = f"purchasepay:2:{self.pid}:{self.token}"
        with patch.object(main, "platega", provider):
            await main.cb_purchase_method(self.call, self.bot)
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.counts(), (0, 0, 0))
        with db.db() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM payments").fetchone()[0], 0
            )

    async def test_balance_increases_before_method_selection_avoids_external_invoice(
        self,
    ):
        self.quote()
        db.change_balance(10, 249, "Top up")
        provider = await self.invoice()
        provider.create_payment.assert_not_awaited()
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_increase_after_invoice_keeps_extra_funds(self):
        await self.invoice()
        db.change_balance(10, 50, "Top up")
        self.confirm()
        self.assertEqual(self.balance(), 50)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_spent_balance_credits_paid_shortfall_then_checkout_can_resume(self):
        await self.invoice()
        db.change_balance(10, -100, "Other purchase")
        result = self.confirm()
        self.assertEqual(result["purpose"], "purchase_credit")
        self.assertEqual(self.balance(), 249)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertIsNone(self.confirm())
        db.change_balance(10, 100, "Restored funds")
        self.assertTrue(balance_checkout(10, self.token)["completed"])
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_second_paid_invoice_for_same_checkout_becomes_balance_credit(self):
        await self.invoice("tx1")
        await self.invoice("tx2")
        self.assertEqual(self.confirm("tx1")["purpose"], "purchase")
        self.assertEqual(self.confirm("tx2")["purpose"], "purchase_credit")
        self.assertEqual(self.balance(), 249)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_balance_purchase_while_external_invoice_open_is_not_charged_twice(
        self,
    ):
        await self.invoice()
        db.change_balance(10, 249, "Top up")
        balance_checkout(10, self.token)
        self.confirm()
        self.assertEqual(self.balance(), 249)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_concurrent_callbacks_commit_once(self):
        await self.invoice()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.confirm(), range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_parallel_distinct_checkouts_never_overdraw_shared_balance(self):
        await self.invoice("tx1", "a" * 24)
        await self.invoice("tx2", "b" * 24)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.confirm, ("tx1", "tx2")))
        self.assertEqual(
            sorted(r["purpose"] for r in results), ["purchase", "purchase_credit"]
        )
        self.assertEqual(self.balance(), 249)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_parallel_balance_purchases_for_one_checkout_commit_once(self):
        prepare_checkout(10, self.pid, self.token)
        db.change_balance(10, 598, "Top up")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: balance_checkout(10, self.token), range(4))
            )
        self.assertEqual(sum(r["result"] is not None for r in results), 1)
        self.assertEqual(self.balance(), 349)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_failure_rolls_back_debit_payment_license_and_purchase(self):
        await self.invoice()
        with db.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_license BEFORE INSERT ON licenses BEGIN SELECT RAISE(ABORT,'failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.confirm()
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.counts(), (0, 0, 0))
        with db.db() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM payments").fetchone()[0], "PENDING"
            )
            self.assertIsNone(
                conn.execute("SELECT purchase_id FROM checkouts").fetchone()[0]
            )
            conn.execute("DROP TRIGGER fail_license")
        self.assertEqual(self.confirm()["purpose"], "purchase")
        self.assertEqual(self.balance(), 0)

    def test_failure_rolls_back_full_balance_debit(self):
        prepare_checkout(10, self.pid, self.token)
        db.change_balance(10, 249, "Top up")
        with db.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_license BEFORE INSERT ON licenses BEGIN SELECT RAISE(ABORT,'failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            balance_checkout(10, self.token)
        self.assertEqual(self.balance(), 349)
        self.assertEqual(self.counts(), (0, 0, 0))

    async def test_fractional_shortfall_is_exact(self):
        db.change_balance(10, 249, "Top up")
        with db.db() as conn:
            conn.execute("UPDATE products SET price='349.01₽',price_rub=349.01")
        provider = await self.invoice()
        self.assertEqual(provider.create_payment.call_args.kwargs["amount"], 0.01)
        self.confirm(amount=0.01)
        self.assertEqual(self.balance(), 0)
        self.assertEqual(db.get_user(10)["total_spent"], 349.01)

    async def test_underpayment_and_wrong_currency_never_debit(self):
        await self.invoice()
        for data in [
            dict(status="CONFIRMED", amount=248.99, currency="RUB"),
            dict(status="CONFIRMED", amount=249, currency="USD"),
        ]:
            with self.assertRaises(ValueError):
                settle_payment("tx", data)
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.counts(), (0, 0, 0))

    async def test_legacy_full_price_invoice_does_not_add_new_balance_debit(self):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO payments(user_id,amount,platega_id,purpose,product_id,product_name) VALUES(10,349,'legacy','purchase',?,'Plugin')",
                (self.pid,),
            )
        self.confirm("legacy", 349)
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.counts(), (1, 1, 1))

    async def test_partial_payment_uses_frozen_price_and_name(self):
        await self.invoice()
        with db.db() as conn:
            conn.execute("UPDATE products SET name='New',price_rub=999,active=0")
        result = self.confirm()
        self.assertEqual(
            (result["amount"], result["product_name"]), (349, "Auto plugin")
        )
        self.assertEqual(self.balance(), 0)

    async def test_changed_balance_notification_offers_resume_with_exact_shortfall(
        self,
    ):
        await self.invoice()
        db.change_balance(10, -100, "Other purchase")
        self.assertTrue(
            await main.process_confirmed_payment(
                "tx", dict(status="CONFIRMED", amount=249), self.bot
            )
        )
        text = self.bot.send_message.call_args.args[1]
        self.assertIn("249.00", text)
        self.assertIn("100.00", text)
        self.assertIn(
            "buyproduct:",
            self.bot.send_message.call_args.kwargs["reply_markup"]
            .inline_keyboard[0][0]
            .callback_data,
        )
        self.assertEqual(self.balance(), 249)
        self.bot.send_document.assert_not_awaited()

    async def test_full_payment_receipt_has_balance_breakdown(self):
        await self.invoice()
        with patch.object(main, "ADMIN_ID", 0):
            await main.process_confirmed_payment(
                "tx", dict(status="CONFIRMED", amount=249), self.bot
            )
        receipt = self.bot.send_message.call_args.args[1]
        self.assertIn("Списано с баланса: <b>100.00₽", receipt)
        self.assertIn("Доплата через Platega: <b>249.00₽", receipt)
        self.assertIn("Остаток на балансе: <b>0.00₽", receipt)

    def test_migration_preserves_open_legacy_invoices_and_balance(self):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO payments(user_id,amount,platega_id,purpose,product_id,product_name) VALUES(10,349,'legacy','purchase',?,'Plugin')",
                (self.pid,),
            )
            conn.execute("ALTER TABLE payments DROP COLUMN product_price")
            conn.execute("ALTER TABLE payments DROP COLUMN checkout_id")
            conn.execute("DROP TABLE checkouts")
        db.init_db()
        self.assertEqual(self.balance(), 100)
        with db.db() as conn:
            row = conn.execute("SELECT * FROM payments").fetchone()
            self.assertEqual(
                (
                    row["amount"],
                    row["status"],
                    row["product_price"],
                    row["checkout_id"],
                ),
                (349, "PENDING", None, None),
            )
        self.confirm("legacy", 349)
        self.assertEqual(self.balance(), 100)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_checkout_tokens_enforce_owner_and_product(self):
        prepare_checkout(10, self.pid, self.token)
        db.ensure_user(11)
        with self.assertRaises(ValueError):
            prepare_checkout(11, self.pid, self.token)
        with self.assertRaises(ValueError):
            balance_checkout(11, self.token)
        with self.assertRaises(ValueError):
            prepare_checkout(10, self.pid + 1, self.token)
        self.assertEqual(self.balance(), 100)
