import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from services import db
from services.payment_ledger import settle_payment


class CheckoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(db,'DB_PATH',Path(self.tmp.name)/'store.db');p.start();self.addCleanup(p.stop)
        db.init_db();db.ensure_user(2,username='buyer',first_name='Buyer')
        main.accept_license(2)
        db.change_balance(2,50,'initial')
        with db.db() as conn:
            cur=conn.execute("INSERT INTO products(name,price,price_rub,description) VALUES('Plugin','299₽',299,'Description')")
            self.product_id=cur.lastrowid
        plugin=Path(self.tmp.name)/'plugin.py';plugin.write_text('# fixture')
        with db.db() as conn:
            conn.execute('INSERT INTO license_assets VALUES(?,?,?)',(self.product_id,str(plugin),'Installation guide'))
        self.user=SimpleNamespace(id=2,username='buyer',first_name='Buyer')
        self.message=SimpleNamespace(answer=AsyncMock())
        self.callback=SimpleNamespace(from_user=self.user,message=self.message,answer=AsyncMock(),data='')
        self.bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username='store_bot')),send_message=AsyncMock(),send_document=AsyncMock())

    def provider(self,tx='purchase'):
        return SimpleNamespace(create_payment=AsyncMock(return_value={'id':tx,'redirect':'https://app.platega.io/pay/'+tx,'status':'PENDING'}))

    async def invoice(self,method=2,tx='purchase'):
        provider=self.provider(tx)
        self.callback.data=f'purchasepay:{method}:{self.product_id}'
        with patch.object(main,'platega',provider):
            await main.cb_purchase_method(self.callback,self.bot)
        return provider

    async def test_product_card_has_one_buy_action_and_no_manual_request(self):
        self.callback.data=f'product:{self.product_id}'
        with patch.object(main,'has_accepted_license',return_value=True),patch.object(main,'safe_edit_or_send',AsyncMock()) as render:
            await main.cb_product(self.callback)
        buttons=[b for row in render.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertEqual(sum(b.callback_data==f'buyproduct:{self.product_id}' for b in buttons),1)
        self.assertFalse(any((b.callback_data or '').startswith('buy:') or 'вручную' in b.text for b in buttons))

    async def test_buy_and_legacy_actions_show_methods_without_balance_gate(self):
        for prefix in ('buyproduct','buybal','buyok','buy'):
            self.callback.data=f'{prefix}:{self.product_id}'
            with patch.object(main,'safe_edit_or_send',AsyncMock()) as render:
                await main.cb_buy_balance(self.callback,self.bot)
            callbacks=[b.callback_data for row in render.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
            self.assertIn(f'purchasepay:2:{self.product_id}',callbacks)
            self.assertIn(f'purchasepay:13:{self.product_id}',callbacks)
            self.assertNotIn('topup',callbacks)
        with db.db() as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0],0)

    async def test_both_methods_create_product_invoice_instead_of_topup(self):
        for method in (2,13):
            provider=await self.invoice(method,f'tx{method}')
            self.assertEqual(provider.create_payment.call_args.kwargs['payment_method'],method)
            self.assertEqual(provider.create_payment.call_args.kwargs['amount'],299)
            self.assertNotIn('На баланс поступит',self.message.answer.call_args.args[0])
        with db.db() as conn:
            rows=conn.execute('SELECT purpose,amount,product_id,product_name FROM payments').fetchall()
            self.assertEqual([(r['purpose'],r['amount'],r['product_id'],r['product_name']) for r in rows],
                             [('purchase',299,self.product_id,'Plugin')]*2)
        self.assertEqual(db.get_user(2)['balance'],50)

    async def test_confirmed_purchase_is_atomic_idempotent_and_uses_invoice_snapshot(self):
        await self.invoice()
        with db.db() as conn:conn.execute("UPDATE products SET name='Changed',price_rub=999,active=0 WHERE id=?",(self.product_id,))
        data={'id':'purchase','status':'CONFIRMED','amount':324.42,'currency':'RUB'}
        result=settle_payment('purchase',data)
        self.assertEqual(result['purpose'],'purchase')
        self.assertEqual(db.get_user(2)['balance'],50)
        self.assertEqual(db.get_user(2)['total_spent'],299)
        self.assertEqual(db.get_user(2)['purchases_count'],1)
        self.assertIsNone(settle_payment('purchase',data))
        with db.db() as conn:
            purchase=conn.execute('SELECT * FROM purchases').fetchone()
            self.assertEqual((purchase['product_name'],purchase['price']),('Plugin',299))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT purchase_id FROM payments').fetchone()[0],purchase['id'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM balance_log').fetchone()[0],1)

    async def test_failed_order_write_rolls_back_purchase_and_can_retry(self):
        await self.invoice()
        with db.db() as conn:
            conn.execute("CREATE TRIGGER fail_order BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT,'simulate failure'); END")
        data={'id':'purchase','status':'CONFIRMED','amount':299,'currency':'RUB'}
        with self.assertRaises(sqlite3.IntegrityError):settle_payment('purchase',data)
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM purchases').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT status FROM payments').fetchone()[0],'PENDING')
            conn.execute('DROP TRIGGER fail_order')
        self.assertEqual(db.get_user(2)['total_spent'],0)
        self.assertIsNotNone(settle_payment('purchase',data))
        self.assertEqual(db.get_user(2)['balance'],50)

    async def test_purchase_notification_is_not_balance_topup_and_is_sent_once(self):
        await self.invoice()
        data={'id':'purchase','status':'CONFIRMED','amount':299,'currency':'RUB'}
        with patch.object(main,'ADMIN_ID',42):
            self.assertTrue(await main.process_confirmed_payment('purchase',data,self.bot))
            self.assertFalse(await main.process_confirmed_payment('purchase',data,self.bot))
        self.assertEqual(self.bot.send_message.await_count,3)
        self.assertEqual(self.bot.send_document.await_count,2)
        self.assertIn('Покупка оплачена',self.bot.send_message.await_args_list[1].args[1])
        self.assertNotIn('Баланс пополнен',self.bot.send_message.await_args_list[1].args[1])

    async def test_hidden_product_cannot_create_new_invoice(self):
        with db.db() as conn:conn.execute('UPDATE products SET active=0 WHERE id=?',(self.product_id,))
        provider=await self.invoice()
        provider.create_payment.assert_not_awaited()
