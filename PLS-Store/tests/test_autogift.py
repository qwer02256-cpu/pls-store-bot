import ast
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from services import db, license_files, license_leases
from services.license_setup import setup_autogift, setup_autostars
from services.licenses import add_routes, issue, mutate, owned
from services.product_article import GIFT_ARTICLE, GIFT_PREFIX, article_pages, publish_article, telegraph_content
from services.product_assets import normalize_source
from plugin_lease_support import GUARD_NAMES, defaults, _KEY, PUBLIC_KEY

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'plugins/AutoGift.py').read_text()
TREE = ast.parse(SOURCE)


def functions(*names, **context):
    nodes = [n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    context['Cardinal'] = object
    context['Dict'] = dict
    context['Any'] = object
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<AutoGift>', 'exec'), context)
    return context


class GiftIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, 'DB_PATH', Path(self.tmp.name)/'store.db')
        p.start(); self.addCleanup(p.stop)
        db.init_db(); db.ensure_user(10)
        setup_autogift(); setup_autostars()
        self.pid = int(db.get_setting(GIFT_PREFIX+'product_id'))
        with db.db() as conn:
            self.lid = issue(conn, 10, self.pid)
        self.key = owned(self.lid, 10)['license_key']

    async def client(self):
        app = web.Application(); add_routes(app)
        client = TestClient(TestServer(app)); await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def test_catalog_price_assets_and_idempotency(self):
        product = db.get_product(self.pid)
        self.assertEqual(product['price_rub'], 349)
        self.assertEqual(product['active'], 1)
        self.assertEqual(product['version'], '6.4.0')
        with db.db() as conn:
            row = conn.execute('SELECT * FROM license_assets WHERE product_id=?',(self.pid,)).fetchone()
            self.assertEqual(row['file_path'],'plugins/AutoGift.py')
            self.assertIn('Session String',row['instruction'])
            conn.execute("UPDATE products SET description='custom',price_rub=777,active=0 WHERE id=?",(self.pid,))
            conn.execute("UPDATE license_assets SET instruction='custom guide' WHERE product_id=?",(self.pid,))
        setup_autogift()
        self.assertEqual(db.get_product(self.pid)['description'],'custom')
        self.assertEqual(db.get_product(self.pid)['price_rub'],777)
        self.assertEqual(db.get_product(self.pid)['active'],0)
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT instruction FROM license_assets WHERE product_id=?',(self.pid,)).fetchone()[0],'custom guide')
            self.assertEqual(conn.execute("SELECT count(*) FROM products WHERE name='AutoGift'").fetchone()[0],1)

    async def test_license_binds_blocks_and_rejects_other_product(self):
        client = await self.client()
        payload = dict(key=self.key,funpay_id='123',product='autogift')
        reply = await client.post('/licenses/verify',json=payload)
        self.assertEqual(reply.status,200)
        data = await reply.json()
        self.assertEqual(data['approval']['product'],'autogift')
        self.assertEqual(owned(self.lid,10)['funpay_id'],'123')
        self.assertEqual((await client.post('/licenses/verify',json=dict(payload,product='autostars'))).status,403)
        self.assertEqual((await client.post('/licenses/verify',json=dict(payload,funpay_id='456'))).status,403)
        mutate(self.lid,'block',1)
        self.assertEqual((await client.post('/licenses/verify',json=payload)).status,403)
        mutate(self.lid,'unblock',1); mutate(self.lid,'reset',1)
        self.assertEqual((await client.post('/licenses/verify',json=dict(payload,funpay_id='456'))).status,200)
        star_pid=int(db.get_setting('autostars_article_product_id'))
        with db.db() as conn:
            lid=issue(conn,10,star_pid)
        self.assertEqual((await client.post('/licenses/verify',json=dict(payload,key=owned(lid,10)['license_key']))).status,403)

    async def test_downloaded_guard_accepts_real_server_response(self):
        client = await self.client()
        reply = await client.post('/licenses/verify',json=dict(key=self.key,funpay_id='123',product='autogift'))
        data = await reply.json()
        with patch.object(license_files,'LICENSE_PUBLIC_BASE','https://store.example'):
            filename, content = license_files.plugin_bytes('plugins/AutoGift.py')
        self.assertEqual(filename,'AutoGift.py')
        self.assertNotIn(b'__PLS_LICENSE_ENDPOINT__',content)
        self.assertNotIn(b'__PLS_LEASE_PUBLIC_KEY__',content)
        self.assertNotIn(b'PRIVATE KEY',content)
        tree=ast.parse(content)
        context=defaults()
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_PLS_PUBLIC_KEY' for t in node.targets):
                context['_PLS_PUBLIC_KEY']=ast.literal_eval(node.value)
        keypath=Path(self.tmp.name)/'storage/plugins/AutoGift/license.json'
        keypath.parent.mkdir(parents=True);keypath.write_text(json.dumps({'key':self.key,'session':'never-send'}))
        requests=Mock(); requests.post.return_value=SimpleNamespace(status_code=200,json=lambda:data)
        import time
        context.update(_PLS_ENDPOINT='https://store.example/licenses/verify',_PLS_LOCK=threading.Lock(),_PLS_ACCOUNT=None,_PLS_CHECKED=0, _PlsPath=lambda p:Path(self.tmp.name)/p, requests=requests,_pls_time=time)
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in GUARD_NAMES]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<downloaded guard>','exec'),context)
        context['_pls_require_license'](SimpleNamespace(account=SimpleNamespace(id=123)))
        self.assertEqual(requests.post.call_args.kwargs['json'],dict(key=self.key,funpay_id='123',product='autogift'))
        self.assertEqual(context['_PLS_MODE'],'online')
        self.assertIn('__PLS_LICENSE_ENDPOINT__',normalize_source(content.decode(),True))

    async def test_article_publishes_separately_recovers_and_keeps_operator_edits(self):
        calls=[]; created=False
        async def serve(request):
            nonlocal created
            from urllib.parse import parse_qs
            fields={k:v[0] for k,v in parse_qs(request.content.decode()).items()}
            calls.append(request.url.path)
            if request.url.path=='/createAccount': result={'access_token':'test-token'}
            elif request.url.path=='/getPageList': result={'pages':[{'title':GIFT_ARTICLE['title'],'url':'https://telegra.ph/AutoGift-test'}] if created else []}
            else:
                self.assertEqual(json.loads(fields['content']),telegraph_content(GIFT_ARTICLE))
                created=True
                raise httpx.ReadTimeout('lost reply')
            return httpx.Response(200,json={'ok':True,'result':result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await publish_article(client,GIFT_ARTICLE,GIFT_PREFIX)
            url=await publish_article(client,GIFT_ARTICLE,GIFT_PREFIX)
            await publish_article(client,GIFT_ARTICLE,GIFT_PREFIX)
            self.assertEqual(url,'https://telegra.ph/AutoGift-test')
            self.assertEqual(calls.count('/createPage'),1)
            self.assertEqual(calls.count('/createAccount'),1)
            self.assertEqual(db.get_product(self.pid)['article_url'],url)
            self.assertFalse(db.get_setting('autostars_article_access_token'))
            with db.db() as conn: conn.execute("UPDATE products SET article_url='https://example.org/custom' WHERE id=?",(self.pid,))
            self.assertIsNone(await publish_article(client,GIFT_ARTICLE,GIFT_PREFIX))
        self.assertTrue(all(len(page)<4096 for page in article_pages(GIFT_ARTICLE)))


    async def test_paid_purchase_delivers_gift_file_and_guide_once_licensed(self):
        from services.payment_ledger import settle_payment
        from handlers.licenses import deliver
        with db.db() as conn:
            conn.execute("INSERT INTO payments(user_id,amount,platega_id,purpose,product_id,product_name) VALUES(10,349,'gift-tx','purchase',?,'AutoGift')",(self.pid,))
        payload=dict(id='gift-tx',status='CONFIRMED',amount=349,currency='RUB')
        self.assertIsNotNone(settle_payment('gift-tx',payload))
        self.assertIsNone(settle_payment('gift-tx',payload))
        with db.db() as conn:
            rows=conn.execute('SELECT * FROM licenses WHERE purchase_id IS NOT NULL').fetchall()
        self.assertEqual(len(rows),1)
        bot=SimpleNamespace(send_message=AsyncMock(),send_document=AsyncMock())
        with patch.object(license_files,'LICENSE_PUBLIC_BASE','https://store.example'):
            self.assertTrue(await deliver(bot,rows[0]['id'],10))
        docs=bot.send_document.await_args_list
        self.assertEqual([call.args[1].filename for call in docs],['AutoGift.py','INSTALL.txt'])
        self.assertIn(b'product"\x3a "autogift"',docs[0].args[1].data)
        self.assertIn('AutoGift 6.4.0',docs[1].args[1].data.decode())
        self.assertIn(rows[0]['license_key'],bot.send_message.await_args.args[1])


class GiftPaymentBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_license_stops_before_telegram_or_payment_journal(self):
        guard=Mock(side_effect=RuntimeError('license denied'))
        env=functions('send_gift_raw',asyncio=asyncio,_pls_require_license=guard,CARDINAL_INSTANCE=object())
        app=Mock(); before=Mock()
        with self.assertRaisesRegex(RuntimeError,'license denied'):
            await env['send_gift_raw'](app,123,1,True,'',10,before)
        app.resolve_peer.assert_not_called(); app.invoke.assert_not_called();before.assert_not_called()

    async def test_license_expiry_during_preflight_prevents_payment_and_journal(self):
        import sys
        app=SimpleNamespace(resolve_peer=AsyncMock(return_value='peer'),invoke=AsyncMock(return_value=SimpleNamespace(form_id=10,invoice=SimpleNamespace(currency='XTR',prices=[SimpleNamespace(amount=10)]))))
        raw=SimpleNamespace(types=SimpleNamespace(InputInvoiceStarGift=Mock(return_value='invoice')),functions=SimpleNamespace(payments=SimpleNamespace(GetPaymentForm=Mock(return_value='get'),SendStarsForm=Mock())))
        guard=Mock(side_effect=[None,RuntimeError('blocked')]);before=Mock()
        env=functions('send_gift_raw',asyncio=asyncio,_pls_require_license=guard,CARDINAL_INSTANCE=object(),PreflightError=ValueError)
        with patch.dict(sys.modules,{'pyrogram':SimpleNamespace(raw=raw)}):
            with self.assertRaisesRegex(RuntimeError,'blocked'):
                await env['send_gift_raw'](app,123,1,True,'',10,before)
        self.assertEqual(app.invoke.await_count,1)
        raw.functions.payments.SendStarsForm.assert_not_called();before.assert_not_called()

    async def test_valid_license_keeps_one_journalled_payment(self):
        import sys
        class PaymentResult: pass
        raw=SimpleNamespace(types=SimpleNamespace(InputInvoiceStarGift=Mock(return_value='invoice'),payments=SimpleNamespace(PaymentResult=PaymentResult)),functions=SimpleNamespace(payments=SimpleNamespace(GetPaymentForm=Mock(return_value='get'),SendStarsForm=Mock(return_value='send'))))
        events=[]
        async def invoke(request,**kwargs):
            if request=='get':return SimpleNamespace(form_id=10,invoice=SimpleNamespace(currency='XTR',prices=[SimpleNamespace(amount=10)]))
            events.append('pay');return PaymentResult()
        app=SimpleNamespace(resolve_peer=AsyncMock(return_value='peer'),invoke=AsyncMock(side_effect=invoke))
        guard=Mock()
        env=functions('send_gift_raw',asyncio=asyncio,_pls_require_license=guard,CARDINAL_INSTANCE=object(),PreflightError=ValueError)
        with patch.dict(sys.modules,{'pyrogram':SimpleNamespace(raw=raw)}):
            result=await env['send_gift_raw'](app,123,1,True,'',10,lambda _:events.append('journal'))
        self.assertEqual(events,['journal','pay'])
        self.assertEqual(result['result_type'],'PaymentResult')
        self.assertEqual(guard.call_count,2)


class GiftArticleRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_article_is_served_without_telegraph_and_caption_links_it(self):
        from services.product_article import add_article_routes
        import config
        import main
        app=web.Application(); add_article_routes(app)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/articles/autogift')
            text=await response.text()
            self.assertEqual(response.status,200)
            self.assertIn(GIFT_ARTICLE['title'],text)
            self.assertIn('349 ₽',text)
            self.assertIn('viewport',text)
            self.assertNotIn('access_token',text)
        with patch.object(config,'LICENSE_PUBLIC_BASE','https://store.example'), patch('services.product_article.get_setting',return_value='42'), patch.object(main,'get_setting',return_value=''), patch.object(main,'agreement_emoji_html',return_value='📄 '), patch.object(main,'document_url',return_value='https://example.org/license'):
            caption=main.build_product_caption(dict(id=42,name='AutoGift',price='349 ₽',description='Gifts'))
            self.assertIn('Статья: <a href="https://store.example/articles/autogift">жми</a>',caption)


class GiftPanelTests(unittest.TestCase):
    def test_button_colors_and_branding_preserve_callbacks(self):
        class LegacyButton:
            def __init__(self,text,**kwargs):self.data=dict(text=text,**kwargs)
            def to_dict(self):return self.data.copy()
        names={'_PlsButton','InlineKeyboardButton','_pls_panel'}
        nodes=[n for n in TREE.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in names]
        env={'_TelegramButton':LegacyButton}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<gift ui>','exec'),env)
        for label,color in [('Настройки','primary'),('Добавить лот','success'),('Выключить','danger'),('Главное меню','danger')]:
            data=env['InlineKeyboardButton'](label,callback_data='ag:unchanged').to_dict()
            self.assertEqual(data['style'],color);self.assertEqual(data['callback_data'],'ag:unchanged')
        text=env['_pls_panel']('Настройки')
        self.assertTrue(text.startswith('<b>PLS Store · AutoGift</b>'))
        self.assertEqual(env['_pls_panel'](text),text)

    def test_activation_saves_private_key_file_and_checks_current_funpay(self):
        import os
        import re
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'storage/plugins/AutoGift/license.json'
            bot=Mock();access=Mock(return_value=True)
            env=functions('_pls_save_key',json=json,os=os,re=re,_PlsPath=lambda p:Path(tmp)/p,BOT=bot,_pls_clear_memory=Mock(),clear_state=Mock(),_pls_access=access,_pls_panel=lambda text:text,_pls_show_license=Mock())
            key='PLS-'+'a'*43
            message=SimpleNamespace(chat=SimpleNamespace(id=10),from_user=SimpleNamespace(id=10),message_id=9,text=key)
            env['_pls_save_key'](message)
            self.assertEqual(json.loads(path.read_text()),{'key':key})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            access.assert_called_once_with(10);bot.delete_message.assert_called_once_with(10,9)

    def test_non_operator_cannot_activate_or_change_license_state(self):
        from typing import Optional, Tuple, List
        message=SimpleNamespace(chat=SimpleNamespace(id=10,type='private'),from_user=SimpleNamespace(id=99),text='PLS-'+'a'*43)
        save=Mock();card=Mock()
        env=functions('_message_dispatch',Message=object,is_operator=lambda uid:False,_pls_save_key=save,CARDINAL_INSTANCE=card)
        env['_message_dispatch'](message)
        save.assert_not_called();card.telegram.get_state.assert_not_called()
