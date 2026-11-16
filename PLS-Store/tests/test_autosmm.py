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
from services.license_setup import setup_autosmm, setup_autostars
from services.licenses import add_routes, issue, mutate, owned
from services.product_article import SMM_ARTICLE, SMM_PREFIX, article_pages, publish_article, telegraph_content
from services.product_assets import normalize_source
from plugin_lease_support import GUARD_NAMES, defaults, _KEY, PUBLIC_KEY

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'plugins/AutoSMM.py').read_text()
TREE = ast.parse(SOURCE)


def functions(*names, **context):
    nodes = [n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    context['Cardinal'] = object
    context['Dict'] = dict
    context['Any'] = object
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<AutoSMM>', 'exec'), context)
    return context


class SMMIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, 'DB_PATH', Path(self.tmp.name)/'store.db')
        p.start(); self.addCleanup(p.stop)
        db.init_db(); db.ensure_user(10)
        setup_autosmm(); setup_autostars()
        self.pid = int(db.get_setting(SMM_PREFIX+'product_id'))
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
        self.assertEqual(product['version'], '23.2.0')
        with db.db() as conn:
            row = conn.execute('SELECT * FROM license_assets WHERE product_id=?',(self.pid,)).fetchone()
            self.assertEqual(row['file_path'],'plugins/AutoSMM.py')
            self.assertIn('API-ключ',row['instruction'])
            conn.execute("UPDATE products SET description='custom',price_rub=777,active=0 WHERE id=?",(self.pid,))
            conn.execute("UPDATE license_assets SET instruction='custom guide' WHERE product_id=?",(self.pid,))
        setup_autosmm()
        self.assertEqual(db.get_product(self.pid)['description'],'custom')
        self.assertEqual(db.get_product(self.pid)['price_rub'],777)
        self.assertEqual(db.get_product(self.pid)['active'],0)
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT instruction FROM license_assets WHERE product_id=?',(self.pid,)).fetchone()[0],'custom guide')
            self.assertEqual(conn.execute("SELECT count(*) FROM products WHERE name='AutoSMM'").fetchone()[0],1)

    async def test_license_binds_blocks_and_rejects_other_product(self):
        client = await self.client()
        payload = dict(key=self.key,funpay_id='123',product='autosmm')
        reply = await client.post('/licenses/verify',json=payload)
        self.assertEqual(reply.status,200)
        data = await reply.json()
        self.assertEqual(data['approval']['product'],'autosmm')
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
        reply = await client.post('/licenses/verify',json=dict(key=self.key,funpay_id='123',product='autosmm'))
        data = await reply.json()
        with patch.object(license_files,'LICENSE_PUBLIC_BASE','https://store.example'):
            filename, content = license_files.plugin_bytes('plugins/AutoSMM.py')
        self.assertEqual(filename,'AutoSMM.py')
        self.assertNotIn(b'__PLS_LICENSE_ENDPOINT__',content)
        self.assertNotIn(b'__PLS_LEASE_PUBLIC_KEY__',content)
        self.assertNotIn(b'PRIVATE KEY',content)
        tree=ast.parse(content)
        context=defaults()
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_PLS_PUBLIC_KEY' for t in node.targets):
                context['_PLS_PUBLIC_KEY']=ast.literal_eval(node.value)
        keypath=Path(self.tmp.name)/'storage/plugins/AutoSMM/license.json'
        keypath.parent.mkdir(parents=True);keypath.write_text(json.dumps({'key':self.key,'session':'never-send'}))
        requests=Mock(); requests.post.return_value=SimpleNamespace(status_code=200,json=lambda:data)
        import time
        context.update(_PLS_ENDPOINT='https://store.example/licenses/verify',_PLS_LOCK=threading.Lock(),_PLS_ACCOUNT=None,_PLS_CHECKED=0, _PlsPath=lambda p:Path(self.tmp.name)/p, requests=requests,_pls_time=time)
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in GUARD_NAMES]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<downloaded guard>','exec'),context)
        context['_pls_require_license'](SimpleNamespace(account=SimpleNamespace(id=123)))
        self.assertEqual(requests.post.call_args.kwargs['json'],dict(key=self.key,funpay_id='123',product='autosmm'))
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
            elif request.url.path=='/getPageList': result={'pages':[{'title':SMM_ARTICLE['title'],'url':'https://telegra.ph/AutoSMM-test'}] if created else []}
            else:
                self.assertEqual(json.loads(fields['content']),telegraph_content(SMM_ARTICLE))
                created=True
                raise httpx.ReadTimeout('lost reply')
            return httpx.Response(200,json={'ok':True,'result':result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await publish_article(client,SMM_ARTICLE,SMM_PREFIX)
            url=await publish_article(client,SMM_ARTICLE,SMM_PREFIX)
            await publish_article(client,SMM_ARTICLE,SMM_PREFIX)
            self.assertEqual(url,'https://telegra.ph/AutoSMM-test')
            self.assertEqual(calls.count('/createPage'),1)
            self.assertEqual(calls.count('/createAccount'),1)
            self.assertEqual(db.get_product(self.pid)['article_url'],url)
            self.assertFalse(db.get_setting('autostars_article_access_token'))
            with db.db() as conn: conn.execute("UPDATE products SET article_url='https://example.org/custom' WHERE id=?",(self.pid,))
            self.assertIsNone(await publish_article(client,SMM_ARTICLE,SMM_PREFIX))
        self.assertTrue(all(len(page)<4096 for page in article_pages(SMM_ARTICLE)))


    async def test_paid_purchase_delivers_gift_file_and_guide_once_licensed(self):
        from services.payment_ledger import settle_payment
        from handlers.licenses import deliver
        with db.db() as conn:
            conn.execute("INSERT INTO payments(user_id,amount,platega_id,purpose,product_id,product_name) VALUES(10,349,'gift-tx','purchase',?,'AutoSMM')",(self.pid,))
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
        self.assertEqual([call.args[1].filename for call in docs],['AutoSMM.py','INSTALL.txt'])
        self.assertIn(b'product"\x3a "autosmm"',docs[0].args[1].data)
        self.assertIn('AutoSMM 23.2.0',docs[1].args[1].data.decode())
        self.assertIn(rows[0]['license_key'],bot.send_message.await_args.args[1])


class SMMArticleRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_article_is_served_without_telegraph_and_caption_links_it(self):
        from services.product_article import add_article_routes
        import config
        import main
        app=web.Application(); add_article_routes(app)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/articles/autosmm')
            text=await response.text()
            self.assertEqual(response.status,200)
            self.assertIn(SMM_ARTICLE['title'],text)
            self.assertIn('349 ₽',text)
            self.assertIn('viewport',text)
            self.assertNotIn('access_token',text)
        with patch.object(config,'LICENSE_PUBLIC_BASE','https://store.example'), patch('services.product_article.get_setting',side_effect=lambda key, default='': '42' if key == SMM_PREFIX+'product_id' else default), patch.object(main,'get_setting',return_value=''), patch.object(main,'agreement_emoji_html',return_value='📄 '), patch.object(main,'document_url',return_value='https://example.org/license'):
            caption=main.build_product_caption(dict(id=42,name='AutoSMM',price='349 ₽',description='SMMs'))
            self.assertIn('Статья: <a href="https://store.example/articles/autosmm">жми</a>',caption)




class SMMPanelBoundaryTests(unittest.TestCase):
    def setUp(self):
        import requests
        import time
        from typing import Any, Dict, Optional, List
        self.guard=Mock()
        nodes=[n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name in {'PanelAPI','PanelError'}]
        self.context=dict(Any=Any,Dict=Dict,Optional=Optional,List=List,requests=requests,time=time,
            _pls_require_license=self.guard,cardinal_instance=object(),_int=lambda value,default=0:int(value or default),
            _proxy_kwargs=lambda proxy:{},VERSION='23.2.0')
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<panel api>','exec'),self.context)
        cls=self.context['PanelAPI'];self.api=cls.__new__(cls)
        self.api.cfg={};self.api.data={'api_key':'panel-private-key'};self.api.quirks={};self.api.proxy=None
        self.response=Mock(status_code=200);self.response.json.return_value={'order':123}
        self.api._request=Mock(return_value=self.response)

    def test_denied_license_never_sends_add_refill_or_other_mutations(self):
        self.guard.side_effect=RuntimeError('license denied')
        for action in ('add','refill','cancel'):
            with self.assertRaises(self.context['PanelError']) as raised:
                self.api.call(action)
            self.assertFalse(raised.exception.uncertain)
        self.api._request.assert_not_called()

    def test_valid_license_sends_order_once_and_does_not_retry_ambiguous_failure(self):
        import requests
        self.assertEqual(self.api.add('1','https://example.org/post',100),'123')
        self.guard.assert_called_once()
        self.assertEqual(self.api._request.call_count,1)
        self.api._error=lambda text,*args,**kwargs:self.context['PanelError'](text,uncertain=kwargs.get('uncertain',False))
        self.api._request.reset_mock();self.api._request.side_effect=requests.Timeout()
        with self.assertRaises(self.context['PanelError']) as raised:
            self.api.call('add',service='1',link='https://example.org/post',quantity=100)
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(self.api._request.call_count,1)

    def test_status_monitoring_remains_available_after_license_denial(self):
        self.guard.side_effect=RuntimeError('denied')
        self.response.json.return_value={'status':'In progress'}
        self.assertEqual(self.api.status('123')['status'],'In progress')
        self.guard.assert_not_called()

    def test_pending_order_not_modified_when_license_is_missing(self):
        guard=Mock(side_effect=RuntimeError('denied'))
        env=functions('_start_order_locked',_pls_require_license=guard,_notify_admins=Mock())
        # No queue, DB, SMM panel or mutable state is provided: the early denial must return.
        env['_start_order_locked'](object(),'42',False)
        env['_notify_admins'].assert_called_once()

    def test_original_callback_namespace_and_plain_buttons(self):
        from typing import Any
        class LegacyButton:
            def __init__(self,text,**kwargs):self.data=dict(text=text,**kwargs)
            def to_dict(self):return self.data.copy()
        nodes=[n for n in TREE.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in {'_PlsButton','_pls_button','InlineKeyboardButton','B','U','_pls_panel'}]
        env=dict(Any=Any,_TelegramButton=LegacyButton,logger=Mock())
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<smm ui>','exec'),env)
        for text in ['Настройки', 'Создать', 'Назад']:
            data=env['B'](text,'home').to_dict()
            self.assertEqual(data['callback_data'],'autosmm:home');self.assertNotIn('style', data)
        self.assertIn('PLS Store · AutoSMM',env['_pls_panel']('Меню'))


class SMMActivationTests(unittest.TestCase):
    def test_activation_saves_private_key_and_keeps_panel_credentials_out(self):
        import os
        import re
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'storage/plugins/AutoSMM/license.json'
            bot=Mock();access=Mock(return_value=True)
            env=functions('_pls_receive_key',json=json,os=os,re=re,_PlsPath=lambda p:Path(tmp)/p,bot=bot,_pls_clear_memory=Mock(),_clear=Mock(),_pls_access=access,_show=Mock())
            key='PLS-'+'a'*43
            message=SimpleNamespace(chat=SimpleNamespace(id=10),from_user=SimpleNamespace(id=10),message_id=9)
            env['_pls_receive_key'](message,key)
            self.assertEqual(json.loads(path.read_text()),{'key':key})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            access.assert_called_once_with(10);bot.delete_message.assert_called_once_with(10,9)

    def test_non_admin_and_group_input_cannot_replace_key(self):
        receive=Mock();state=Mock(return_value=('autosmm:license_key',{}))
        message=SimpleNamespace(chat=SimpleNamespace(id=10,type='group'),from_user=SimpleNamespace(id=99),text='PLS-'+'a'*43)
        env=functions('_input',_is_admin=lambda message:False,_state=state,_pls_receive_key=receive)
        env['_input'](message)
        state.assert_not_called();receive.assert_not_called()
        env['_is_admin']=lambda message:True
        env['_input'](message)
        receive.assert_not_called()

