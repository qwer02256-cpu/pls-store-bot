import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from services import db
from services.payment_ledger import settle_payment, money
from services.platega import PlategaClient, PlategaError
import httpx


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(db,'DB_PATH',Path(self.tmp.name)/'store.db');p.start();self.addCleanup(p.stop)
        db.init_db();db.ensure_user(1);db.ensure_user(2,referrer_id=1)
        with db.db() as conn:
            conn.execute("INSERT INTO payments(user_id,amount,platega_id) VALUES (2, 100.25, 'tx')")
        self.confirmed={'status':'CONFIRMED','id':'tx','paymentDetails':{'amount':100.25,'currency':'RUB'}}

    def test_credit_referral_and_duplicate_are_one_transaction(self):
        result=settle_payment('tx',self.confirmed)
        self.assertEqual(result['balance'],100.25)
        self.assertEqual(db.get_user(2)['balance'],100.25)
        self.assertEqual(db.get_user(1)['balance'],10.02)
        self.assertEqual(db.get_user(1)['referral_earnings'],10.02)
        self.assertIsNone(settle_payment('tx',self.confirmed))
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM balance_log').fetchone()[0],2)
            self.assertEqual(conn.execute("SELECT status FROM payments").fetchone()[0],'CONFIRMED')

    def test_database_failure_rolls_back_credit_status_and_logs(self):
        with db.db() as conn:
            conn.execute("CREATE TRIGGER fail_credit BEFORE INSERT ON balance_log BEGIN SELECT RAISE(ABORT,'simulate failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):settle_payment('tx',self.confirmed)
        self.assertEqual(db.get_user(2)['balance'],0)
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT status FROM payments').fetchone()[0],'PENDING')
            conn.execute('DROP TRIGGER fail_credit')
        self.assertIsNotNone(settle_payment('tx',self.confirmed))

    def test_amount_currency_and_transaction_mismatches_do_not_credit(self):
        for data in [dict(status='PENDING'),dict(status='CONFIRMED',id='wrong'),dict(status='CONFIRMED',amount=1),dict(status='CONFIRMED',currency='USD')]:
            with self.assertRaises(ValueError):settle_payment('tx',data)
        self.assertEqual(db.get_user(2)['balance'],0)

    def test_iso_numeric_ruble_currency_settles_and_still_rejects_other_currencies(self):
        payload={'status':'CONFIRMED','id':'tx','paymentDetails':{'amount':'100.25','currency':643}}
        self.assertEqual(settle_payment('tx',payload)['balance'],100.25)

    def test_invalid_amounts_rejected(self):
        for value in [0,-1,'NaN','Infinity','-Infinity','1.001','bad','1e9999']:
            with self.assertRaises(ValueError):money(value)


class PaymentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_cannot_query_another_users_payment(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db();db.ensure_user(1)
            with db.db() as conn:conn.execute("INSERT INTO payments(user_id,amount,platega_id) VALUES(1,100,'tx')")
            provider=SimpleNamespace(get_status=AsyncMock())
            callback=SimpleNamespace(answer=AsyncMock(),message=SimpleNamespace(answer=AsyncMock()),from_user=SimpleNamespace(id=2),data='paystatus:tx')
            with patch.object(main,'platega',provider):await main.cb_pay_status(callback,SimpleNamespace())
            provider.get_status.assert_not_awaited()

    async def test_bad_provider_json_is_controlled_error(self):
        client=PlategaClient('merchant','secret')
        await client._client.aclose()
        client._client=httpx.AsyncClient(base_url='https://app.platega.io',transport=httpx.MockTransport(lambda request:httpx.Response(502,text='not JSON')))
        try:
            with self.assertRaises(PlategaError):await client.get_status('tx')
            with self.assertRaises(PlategaError):await client.create_payment(100)
        finally:await client.aclose()

    async def test_authenticated_callback_replay_credits_only_once(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db();db.ensure_user(2)
            with db.db() as conn:conn.execute("INSERT INTO payments(user_id,amount,platega_id) VALUES(2,100,'tx')")
            client=PlategaClient('merchant','secret')
            request=SimpleNamespace(app={'bot':SimpleNamespace(send_message=AsyncMock())},headers={'X-MerchantId':'merchant','X-Secret':'secret'},json=AsyncMock(return_value={'id':'tx','status':'CONFIRMED','amount':100,'currency':'RUB'}))
            try:
                with patch.object(main,'platega',client):
                    self.assertEqual((await main.platega_callback_handler(request)).status,200)
                    self.assertEqual((await main.platega_callback_handler(request)).status,200)
                    request.headers={}
                    self.assertEqual((await main.platega_callback_handler(request)).status,403)
                self.assertEqual(db.get_user(2)['balance'],100)
                request.app['bot'].send_message.assert_awaited_once()
            finally:await client.aclose()


class CallbackProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_probe_returns_ok_without_processing_payment_body(self):
        request=SimpleNamespace(app={'bot':SimpleNamespace()},
            headers={'User-Agent':'Platega-CallbackUrlCheck/1.0'},
            json=AsyncMock(return_value={'status':'CONFIRMED','id':'forged','amount':100}))
        with patch.object(main,'platega',SimpleNamespace()), patch.object(main,'process_confirmed_payment',AsyncMock()) as settle:
            response=await main.platega_callback_handler(request)
            self.assertEqual(response.status,200)
            self.assertEqual(response.text,'OK')
            request.json.assert_not_awaited()
            settle.assert_not_awaited()

    async def test_actual_callback_still_requires_credentials(self):
        provider=SimpleNamespace(verify_callback_headers=lambda headers:False)
        request=SimpleNamespace(app={'bot':SimpleNamespace()},headers={'User-Agent':'Platega-Callbacks'},json=AsyncMock())
        with patch.object(main,'platega',provider), patch.object(main,'process_confirmed_payment',AsyncMock()) as settle:
            self.assertEqual((await main.platega_callback_handler(request)).status,403)
            request.json.assert_not_awaited()
            settle.assert_not_awaited()


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_and_callback_share_idempotent_ledger(self):
        from services.payment_monitor import reconcile_pending
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db();db.ensure_user(2)
            with db.db() as conn:conn.execute("INSERT INTO payments(user_id,amount,platega_id) VALUES(2,100,'tx')")
            data={'id':'tx','status':'CONFIRMED','paymentDetails':{'amount':100,'currency':'RUB'}}
            client=SimpleNamespace(get_status=AsyncMock(return_value=data))
            bot=SimpleNamespace(send_message=AsyncMock())
            async def confirmed(tx,payload):await main.process_confirmed_payment(tx,payload,bot)
            await reconcile_pending(client,confirmed)
            await main.process_confirmed_payment('tx',data,bot)
            await reconcile_pending(client,confirmed)
            self.assertEqual(db.get_user(2)['balance'],100)
            bot.send_message.assert_awaited_once()
            client.get_status.assert_awaited_once()

    async def test_network_error_keeps_payment_pending_for_retry(self):
        from services.payment_monitor import reconcile_pending
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db();db.ensure_user(2)
            with db.db() as conn:conn.execute("INSERT INTO payments(user_id,amount,platega_id) VALUES(2,100,'tx')")
            client=SimpleNamespace(get_status=AsyncMock(side_effect=PlategaError('offline')))
            callback=AsyncMock()
            await reconcile_pending(client,callback)
            callback.assert_not_awaited()
            with db.db() as conn:self.assertEqual(conn.execute('SELECT status FROM payments').fetchone()[0],'PENDING')

    async def test_monitor_does_not_starve_later_pending_payments(self):
        from services.payment_monitor import reconcile_pending
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db();db.ensure_user(2)
            with db.db() as conn:
                for i in range(3):conn.execute('INSERT INTO payments(user_id,amount,platega_id) VALUES(2,100,?)',(str(i),))
            client=SimpleNamespace(get_status=AsyncMock(return_value={'status':'PENDING'}))
            callback=AsyncMock()
            cursor=await reconcile_pending(client,callback,limit=2)
            self.assertEqual(cursor,2)
            self.assertEqual(await reconcile_pending(client,callback,after_id=cursor,limit=2),0)
            self.assertEqual([call.args[0] for call in client.get_status.await_args_list],['0','1','2'])


class InvoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_invoice_then_monitor_settles_immediate_confirmation(self):
        from services.payment_monitor import reconcile_pending
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db()
            data={'transactionId':'tx','redirect':'https://app.platega.io/pay/tx','status':'CONFIRMED','paymentDetails':{'amount':100.25,'currency':'RUB'}}
            provider=SimpleNamespace(create_payment=AsyncMock(return_value=data),get_status=AsyncMock(return_value=data))
            bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username='store_bot')),send_message=AsyncMock())
            message=SimpleNamespace(answer=AsyncMock())
            actor=SimpleNamespace(id=2,username='buyer',first_name='Buyer')
            with patch.object(main,'platega',provider):
                await main.create_topup(message,actor,100.25,bot)
            with db.db() as conn:self.assertEqual(conn.execute('SELECT status FROM payments').fetchone()[0],'PENDING')
            self.assertIn('100.25₽',message.answer.call_args.args[0])
            keyboard=message.answer.call_args.kwargs['reply_markup']
            self.assertEqual(keyboard.inline_keyboard[0][0].url,data['redirect'])
            async def confirm(tx,payload):await main.process_confirmed_payment(tx,payload,bot)
            await reconcile_pending(provider,confirm)
            self.assertEqual(db.get_user(2)['balance'],100.25)

    async def test_malformed_invoice_does_not_create_local_payment(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db()
            provider=SimpleNamespace(create_payment=AsyncMock(return_value={'status':'PENDING'}))
            bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username='store_bot')))
            message=SimpleNamespace(answer=AsyncMock())
            with patch.object(main,'platega',provider):
                await main.create_topup(message,SimpleNamespace(id=2,username=None,first_name='Buyer'),100,bot)
            with db.db() as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM payments').fetchone()[0],0)


class MinimumTopupTests(unittest.IsolatedAsyncioTestCase):
    def test_existing_minimum_updates_once_and_preset_contains_one(self):
        from services.ui import topup_amounts_kb
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db()
            with db.db() as conn:conn.execute("DELETE FROM bot_settings WHERE key='min_topup_one_ruble_applied'")
            db.set_setting('min_topup','100')
            db.init_db()
            self.assertEqual(db.get_setting('min_topup'),'1')
            buttons=[b for row in topup_amounts_kb().inline_keyboard for b in row]
            self.assertTrue(any(b.callback_data=='topup:1' and b.text=='1₽' for b in buttons))
            db.set_setting('min_topup','10')
            db.init_db()
            self.assertEqual(db.get_setting('min_topup'),'10')

    async def test_one_ruble_invoice_is_accepted_and_sub_ruble_rejected(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db()
            provider=SimpleNamespace(create_payment=AsyncMock(return_value={'id':'one','redirect':'https://app.platega.io/pay/one','status':'PENDING'}))
            bot=SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username='store_bot')))
            message=SimpleNamespace(answer=AsyncMock())
            user=SimpleNamespace(id=2,username=None,first_name='Buyer')
            with patch.object(main,'platega',provider):
                await main.create_topup(message,user,1,bot)
                self.assertEqual(provider.create_payment.call_args.kwargs['amount'],1)
                provider.create_payment.reset_mock()
                await main.create_topup(message,user,0.99,bot)
                provider.create_payment.assert_not_awaited()
            with db.db() as conn:
                rows=conn.execute('SELECT amount FROM payments').fetchall()
                self.assertEqual([r['amount'] for r in rows],[1])
