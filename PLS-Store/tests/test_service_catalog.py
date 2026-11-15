import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from services import db
from services.checkout import prepare_checkout, balance_checkout
from services.payment_ledger import settle_payment
from services.service_catalog import setup_services


class ServiceCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        binding = patch.object(db, 'DB_PATH', Path(self.tmp.name) / 'store.db')
        binding.start()
        self.addCleanup(binding.stop)
        db.init_db()
        setup_services()
        self.pid = int(db.get_setting('fragment_verification_service_product_id'))
        db.ensure_user(42, username='buyer')
        self.actor = SimpleNamespace(id=42, username='buyer', first_name='Buyer')
        self.target = SimpleNamespace(from_user=self.actor)
        self.token = 'a' * 24

    def test_setup_is_idempotent_and_preserves_admin_edits(self):
        product = db.get_product(self.pid)
        self.assertEqual(product['price_rub'], 349)
        self.assertEqual(product['category'], 'services')
        self.assertIn('1–24', product['description'])
        self.assertIn('Telegram-аккаунт', product['description'])
        with db.db() as conn:
            conn.execute('UPDATE products SET active=0,price_rub=399 WHERE id=?', (self.pid,))
        setup_services()
        self.assertEqual(len(db.get_products(False)), 1)
        self.assertEqual(db.get_product(self.pid)['price_rub'], 399)
        self.assertEqual(db.get_product(self.pid)['active'], 0)
        db.init_db()
        self.assertEqual(db.get_product(self.pid)['category'], 'services')

    async def test_service_category_and_card_navigation(self):
        main.accept_license(42)
        render = AsyncMock()
        with patch.object(main, 'safe_edit_or_send', render):
            await main.render_catalog(self.target)
            buttons = render.call_args.kwargs['reply_markup'].inline_keyboard
            self.assertIn('catalog:services', [b.callback_data for r in buttons for b in r])
            await main.render_plugins(self.target)
            buttons = render.call_args.kwargs['reply_markup'].inline_keyboard
            self.assertNotIn(f'product:{self.pid}', [b.callback_data for r in buttons for b in r])
            await main.render_services(self.target)
            buttons = render.call_args.kwargs['reply_markup'].inline_keyboard
            self.assertIn(f'product:{self.pid}', [b.callback_data for r in buttons for b in r])
            callback = SimpleNamespace(from_user=self.actor, answer=AsyncMock(), data=f'product:{self.pid}')
            await main.cb_product(callback)
            self.assertIn('1–24', render.call_args.args[1])
            self.assertIn('349', render.call_args.args[1])
            buttons = render.call_args.kwargs['reply_markup'].inline_keyboard
            self.assertIn('catalog:services', [b.callback_data for r in buttons for b in r])

    def purchase(self):
        db.change_balance(42, 349, 'test credit')
        prepare_checkout(42, self.pid, self.token)
        return balance_checkout(42, self.token)['result']

    async def test_balance_purchase_sends_contact_without_license_and_records_request(self):
        result = self.purchase()
        capture = AsyncMock()
        with patch.object(main, 'screen_send', capture), patch.object(main, 'deliver', AsyncMock()) as deliver:
            await main.notify_purchase(result, SimpleNamespace())
            deliver.assert_not_awaited()
        message = next(c for c in capture.call_args_list if c.args[1] == 42)
        self.assertIn('@uservkru', message.args[2])
        self.assertIn('1–24', message.args[2])
        self.assertIn('Telegram-аккаунт', message.args[2])
        self.assertNotIn('Файл, ключ', message.args[2])
        buttons = message.kwargs['reply_markup'].inline_keyboard
        self.assertIn('https://t.me/uservkru', [b.url for r in buttons for b in r])
        self.assertEqual(db.get_user(42)['balance'], 0)
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM licenses').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT status FROM orders').fetchone()[0], 'new')
        self.assertTrue(balance_checkout(42, self.token)['completed'])
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchases').fetchone()[0], 1)

    async def test_external_payment_is_idempotent_and_sends_contact(self):
        db.change_balance(42, 100, 'test credit')
        prepare_checkout(42, self.pid, self.token)
        with db.db() as conn:
            conn.execute("INSERT INTO payments(user_id,amount,platega_id,purpose,product_id,product_name,product_price,checkout_id) VALUES(42,249,'service-pay','purchase',?,?,349,?)",
                         (self.pid, db.get_product(self.pid)['name'], self.token))
        payload = {'id':'service-pay','status':'CONFIRMED','amount':249,'currency':'RUB'}
        result = settle_payment('service-pay', payload)
        self.assertEqual(result['amount'], 349)
        self.assertEqual(db.get_user(42)['balance'], 0)
        self.assertIsNone(settle_payment('service-pay', payload))
        capture = AsyncMock()
        with patch.object(main, 'screen_send', capture):
            await main.notify_purchase(result, SimpleNamespace())
        self.assertIn('@uservkru', str(capture.call_args_list))

    async def test_paid_instruction_remains_in_history_after_product_edit(self):
        self.purchase()
        with db.db() as conn:
            conn.execute("UPDATE products SET fulfillment_message='Changed' WHERE id=?", (self.pid,))
        capture = AsyncMock()
        with patch.object(main, 'safe_edit_or_send', capture):
            await main.cb_mypurchases(SimpleNamespace(from_user=self.actor, answer=AsyncMock()))
        self.assertIn('@uservkru', capture.call_args.args[1])
        self.assertNotIn('Changed', capture.call_args.args[1])

    def test_service_without_instructions_cannot_be_charged(self):
        with db.db() as conn:
            conn.execute("UPDATE products SET fulfillment_message='' WHERE id=?", (self.pid,))
        db.change_balance(42, 349, 'test credit')
        with self.assertRaises(ValueError):
            prepare_checkout(42, self.pid, self.token)
        self.assertEqual(db.get_user(42)['balance'], 349)
