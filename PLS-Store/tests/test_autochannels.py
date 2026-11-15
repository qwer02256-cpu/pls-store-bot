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
from services.license_setup import setup_autochannels, setup_autostars
from services.licenses import add_routes, issue, mutate, owned
from services.product_article import CHANNEL_ARTICLE, CHANNEL_PREFIX, article_pages, publish_article, telegraph_content
from services.product_assets import normalize_source
from plugin_lease_support import GUARD_NAMES, defaults, _KEY, PUBLIC_KEY

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'plugins/AutoChannels.py').read_text()
TREE = ast.parse(SOURCE)


def functions(*names, **context):
    nodes = [n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    context['Cardinal'] = object
    context['Dict'] = dict
    context['Any'] = object
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<AutoChannels>', 'exec'), context)
    return context


class CHANNELIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, 'DB_PATH', Path(self.tmp.name)/'store.db')
        p.start(); self.addCleanup(p.stop)
        db.init_db(); db.ensure_user(10)
        setup_autochannels(); setup_autostars()
        self.pid = int(db.get_setting(CHANNEL_PREFIX+'product_id'))
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
        self.assertEqual(product['price_rub'], 399)
        self.assertEqual(product['active'], 1)
        self.assertEqual(product['version'], '5.0.0')
        with db.db() as conn:
            row = conn.execute('SELECT * FROM license_assets WHERE product_id=?',(self.pid,)).fetchone()
            self.assertEqual(row['file_path'],'plugins/AutoChannels.py')
            self.assertIn('API-ключ',row['instruction'])
            conn.execute("UPDATE products SET description='custom',price_rub=777,active=0 WHERE id=?",(self.pid,))
            conn.execute("UPDATE license_assets SET instruction='custom guide' WHERE product_id=?",(self.pid,))
        setup_autochannels()
        self.assertEqual(db.get_product(self.pid)['description'],'custom')
        self.assertEqual(db.get_product(self.pid)['price_rub'],777)
        self.assertEqual(db.get_product(self.pid)['active'],0)
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT instruction FROM license_assets WHERE product_id=?',(self.pid,)).fetchone()[0],'custom guide')
            self.assertEqual(conn.execute("SELECT count(*) FROM products WHERE name='AutoChannels'").fetchone()[0],1)

    async def test_license_binds_blocks_and_rejects_other_product(self):
        client = await self.client()
        payload = dict(key=self.key,funpay_id='123',product='autochannels')
        reply = await client.post('/licenses/verify',json=payload)
        self.assertEqual(reply.status,200)
        data = await reply.json()
        self.assertEqual(data['approval']['product'],'autochannels')
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
        reply = await client.post('/licenses/verify',json=dict(key=self.key,funpay_id='123',product='autochannels'))
        data = await reply.json()
        with patch.object(license_files,'LICENSE_PUBLIC_BASE','https://store.example'):
            filename, content = license_files.plugin_bytes('plugins/AutoChannels.py')
        self.assertEqual(filename,'AutoChannels.py')
        self.assertNotIn(b'__PLS_LICENSE_ENDPOINT__',content)
        self.assertNotIn(b'__PLS_LEASE_PUBLIC_KEY__',content)
        self.assertNotIn(b'PRIVATE KEY',content)
        tree=ast.parse(content)
        context=defaults()
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_PLS_PUBLIC_KEY' for t in node.targets):
                context['_PLS_PUBLIC_KEY']=ast.literal_eval(node.value)
        keypath=Path(self.tmp.name)/'storage/plugins/AutoChannels/license.json'
        keypath.parent.mkdir(parents=True);keypath.write_text(json.dumps({'key':self.key,'session':'never-send'}))
        requests=Mock(); requests.post.return_value=SimpleNamespace(status_code=200,json=lambda:data)
        import time
        context.update(_PLS_ENDPOINT='https://store.example/licenses/verify',_PLS_LOCK=threading.Lock(),_PLS_ACCOUNT=None,_PLS_CHECKED=0, _PlsPath=lambda p:Path(self.tmp.name)/p, requests=requests,_pls_time=time)
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in GUARD_NAMES]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<downloaded guard>','exec'),context)
        context['_pls_require_license'](SimpleNamespace(account=SimpleNamespace(id=123)))
        self.assertEqual(requests.post.call_args.kwargs['json'],dict(key=self.key,funpay_id='123',product='autochannels'))
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
            elif request.url.path=='/getPageList': result={'pages':[{'title':CHANNEL_ARTICLE['title'],'url':'https://telegra.ph/AutoChannels-test'}] if created else []}
            else:
                self.assertEqual(json.loads(fields['content']),telegraph_content(CHANNEL_ARTICLE))
                created=True
                raise httpx.ReadTimeout('lost reply')
            return httpx.Response(200,json={'ok':True,'result':result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await publish_article(client,CHANNEL_ARTICLE,CHANNEL_PREFIX)
            url=await publish_article(client,CHANNEL_ARTICLE,CHANNEL_PREFIX)
            await publish_article(client,CHANNEL_ARTICLE,CHANNEL_PREFIX)
            self.assertEqual(url,'https://telegra.ph/AutoChannels-test')
            self.assertEqual(calls.count('/createPage'),1)
            self.assertEqual(calls.count('/createAccount'),1)
            self.assertEqual(db.get_product(self.pid)['article_url'],url)
            self.assertFalse(db.get_setting('autostars_article_access_token'))
            with db.db() as conn: conn.execute("UPDATE products SET article_url='https://example.org/custom' WHERE id=?",(self.pid,))
            self.assertIsNone(await publish_article(client,CHANNEL_ARTICLE,CHANNEL_PREFIX))
        self.assertTrue(all(len(page)<4096 for page in article_pages(CHANNEL_ARTICLE)))


    async def test_paid_purchase_delivers_gift_file_and_guide_once_licensed(self):
        from services.payment_ledger import settle_payment
        from handlers.licenses import deliver
        with db.db() as conn:
            conn.execute("INSERT INTO payments(user_id,amount,platega_id,purpose,product_id,product_name) VALUES(10,399,'gift-tx','purchase',?,'AutoChannels')",(self.pid,))
        payload=dict(id='gift-tx',status='CONFIRMED',amount=399,currency='RUB')
        self.assertIsNotNone(settle_payment('gift-tx',payload))
        self.assertIsNone(settle_payment('gift-tx',payload))
        with db.db() as conn:
            rows=conn.execute('SELECT * FROM licenses WHERE purchase_id IS NOT NULL').fetchall()
        self.assertEqual(len(rows),1)
        bot=SimpleNamespace(send_message=AsyncMock(),send_document=AsyncMock())
        with patch.object(license_files,'LICENSE_PUBLIC_BASE','https://store.example'):
            self.assertTrue(await deliver(bot,rows[0]['id'],10))
        docs=bot.send_document.await_args_list
        self.assertEqual([call.args[1].filename for call in docs],['AutoChannels.py','INSTALL.txt'])
        self.assertIn(b'product"\x3a "autochannels"',docs[0].args[1].data)
        self.assertIn('AutoChannels 5.0.0',docs[1].args[1].data.decode())
        self.assertIn(rows[0]['license_key'],bot.send_message.await_args.args[1])


class CHANNELArticleRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_bundled_article_is_served_without_telegraph_and_caption_links_it(self):
        from services.product_article import add_article_routes
        import config
        import main
        app=web.Application(); add_article_routes(app)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/articles/autochannels')
            text=await response.text()
            self.assertEqual(response.status,200)
            self.assertIn(CHANNEL_ARTICLE['title'],text)
            self.assertIn('399 ₽',text)
            self.assertIn('viewport',text)
            self.assertNotIn('access_token',text)
        with patch.object(config,'LICENSE_PUBLIC_BASE','https://store.example'), patch('services.product_article.get_setting',side_effect=lambda key, default='': '42' if key == CHANNEL_PREFIX+'product_id' else default), patch.object(main,'get_setting',return_value=''), patch.object(main,'agreement_emoji_html',return_value='📄 '), patch.object(main,'document_url',return_value='https://example.org/license'):
            caption=main.build_product_caption(dict(id=42,name='AutoChannels',price='399 ₽',description='CHANNELs'))
            self.assertIn('Статья: <a href="https://store.example/articles/autochannels">жми</a>',caption)






class ChannelRoutingTests(unittest.TestCase):
    def setUp(self):
        import os
        import re
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'orders.json'
        self.lots=[dict(lot_id='1234',tier_id='tier',token='a'*32)]
        self.card=SimpleNamespace(account=SimpleNamespace(id=99),send_message=Mock())
        self.guard=Mock()
        self.threads=Mock()
        names=('_read_orders','_write_orders','pending_put','pending_lookup','pending_clear','_message_chat','match_lot_tier','_normalize_channel_ids','on_new_message','on_new_order','_do_transfer')
        self.env=functions(*names,os=os,re=re,json=json,threading=self.threads,asyncio=asyncio,
            NewOrderEvent=object,PENDING_PATH=str(self.path),_order_lock=threading.RLock(),_transfer_lock=threading.Lock(),load_lots=lambda:self.lots,
            _pls_require_license=self.guard,load_config=Mock(return_value={}),_session_ready_for_transfer=Mock(return_value=(True,0)),log=Mock(),log_exc=Mock())
        self.msg=SimpleNamespace(text='@buyer_name',author_id=10,chat_id=100,by_bot=False)

    def put(self,order_id='ORDER1',**updates):
        row=dict(buyer_id='10',lot_id='1234',binding_token='a'*32,chat_keys=['100','users-99-10'],channels=[5],status='waiting_username')
        row.update(updates)
        records=self.env['_read_orders']();records[order_id]=row;self.env['_write_orders'](records)

    def test_lot_id_is_exact_and_keywords_never_match(self):
        match=self.env['match_lot_tier']
        self.assertEqual(match(SimpleNamespace(lot_id=1234))['tier_id'],'tier')
        self.assertIsNone(match(SimpleNamespace(lot_id=123,description='same title')))
        self.lots.append(dict(keyword='channel',tier_id='legacy'))
        self.assertIsNone(match(SimpleNamespace(description='channel')))
        self.assertIsNone(match(SimpleNamespace(lot_id=123,full_description='#PLSCH:'+'a'*32)))
        self.assertEqual(match(SimpleNamespace(full_description='#PLSCH:'+'a'*32))['lot_id'],'1234')

    def test_conflicting_lot_evidence_and_multiple_tags_are_ignored(self):
        self.assertIsNone(self.env['match_lot_tier'](SimpleNamespace(lot_id=1234,offer_id=7)))
        self.lots.append(dict(lot_id='5678',tier_id='other',token='b'*32))
        self.assertIsNone(self.env['match_lot_tier'](SimpleNamespace(full_description='#PLSCH:'+'a'*32+' #PLSCH:'+'b'*32)))

    def test_unrelated_usernames_seller_bots_and_partial_chat_ids_are_silent(self):
        self.put()
        for changes in ({'author_id':11},{'chat_id':1000},{'by_bot':True},{'author_id':99},{'text':'@ab'},{'text':'hello @buyer_name'}):
            message=SimpleNamespace(**dict(vars(self.msg),**changes))
            self.env['on_new_message'](self.card,SimpleNamespace(message=message))
        self.card.send_message.assert_not_called();self.threads.Thread.assert_not_called();self.guard.assert_not_called()

    def test_no_pending_or_removed_binding_means_no_reply(self):
        self.env['on_new_message'](self.card,SimpleNamespace(message=self.msg))
        self.put();self.lots.clear()
        self.env['on_new_message'](self.card,SimpleNamespace(message=self.msg))
        self.card.send_message.assert_not_called();self.threads.Thread.assert_not_called()

    def test_matching_buyer_and_exact_chat_start_only_their_transfer(self):
        self.put()
        self.env['on_new_message'](self.card,SimpleNamespace(message=self.msg))
        call=self.threads.Thread.call_args
        self.assertEqual(call.kwargs['args'][3:],('ORDER1',[5],'buyer_name'))
        self.guard.assert_called_once_with(self.card)

    def test_multiple_pending_orders_need_explicit_order_number(self):
        self.put();self.put('ORDER2',channels=[6])
        self.env['on_new_message'](self.card,SimpleNamespace(message=self.msg))
        self.threads.Thread.assert_not_called()
        self.msg.text='@buyer_name #ORDER2'
        self.env['on_new_message'](self.card,SimpleNamespace(message=self.msg))
        self.assertEqual(self.threads.Thread.call_args.kwargs['args'][3],'ORDER2')

    def test_duplicate_paid_event_reserves_stock_once(self):
        full=SimpleNamespace(id='ORDER1',lot_id=1234,buyer_id=10,amount=1)
        self.env.update(_paid_order=Mock(return_value=full),get_tier=Mock(return_value={'id':'tier'}),resolve_message_chat_id=Mock(return_value=['100','10']),
            take_ready_channels=Mock(return_value=[dict(channel_id=5,link='https://t.me/+example')]),build_delivery_instructions=lambda links:'delivery')
        event=SimpleNamespace(order=SimpleNamespace(id='ORDER1',lot_id=9999))
        self.env['on_new_order'](self.card,event);self.env['on_new_order'](self.card,event)
        self.env['take_ready_channels'].assert_called_once_with('tier',1)
        self.assertEqual(self.env['_read_orders']()['ORDER1']['channels'],[5])

    def test_unbound_full_order_never_reserves_stock(self):
        self.env.update(_paid_order=Mock(return_value=SimpleNamespace(id='OTHER',lot_id=9999)),take_ready_channels=Mock())
        self.env['on_new_order'](self.card,SimpleNamespace(order=SimpleNamespace(id='OTHER',lot_id=1234)))
        self.env['take_ready_channels'].assert_not_called();self.guard.assert_not_called()

    def test_corrupt_journal_does_not_become_empty(self):
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):self.env['_read_orders']()

    def test_partial_transfer_is_journalled_and_never_retried_automatically(self):
        self.put(channels=[5,6])
        self.env['_paid_order']=Mock(return_value=SimpleNamespace(lot_id=1234,buyer_id=10))
        calls=[]
        async def transfer(cfg,cid,user):
            self.assertEqual(self.env['_read_orders']()['ORDER1']['status'],'transferring')
            calls.append(cid)
            if cid==6:raise RuntimeError('ambiguous response')
        self.env['_transfer_ownership']=transfer
        self.env['_do_transfer'](self.card,{},'100','ORDER1',[5,6],'buyer_name')
        row=self.env['_read_orders']()['ORDER1']
        self.assertEqual(row['status'],'needs_review');self.assertEqual(row['channels'],[6])
        self.env['_do_transfer'](self.card,{},'100','ORDER1',[6],'buyer_name')
        self.assertEqual(calls,[5,6])
        self.env['pending_put']([7],order_id='ORDER1')
        self.assertEqual(self.env['_read_orders']()['ORDER1']['status'],'needs_review')

    def test_transfer_rechecks_payment_and_buyer_before_telegram(self):
        self.put()
        transfer=AsyncMock();self.env['_transfer_ownership']=transfer
        self.env['_paid_order']=Mock(return_value=SimpleNamespace(lot_id=1234,buyer_id=11))
        self.env['_do_transfer'](self.card,{},'100','ORDER1',[5],'buyer_name')
        transfer.assert_not_called()
        self.env['_paid_order'].side_effect=ValueError('refunded')
        with self.assertRaises(ValueError):self.env['_do_transfer'](self.card,{},'100','ORDER1',[5],'buyer_name')
        transfer.assert_not_called()


class ChannelBindingTests(unittest.TestCase):
    def test_binding_saves_funpay_id_only_after_marker_round_trip(self):
        import re
        import secrets
        saved=[]
        fields=SimpleNamespace(fields={'fields[desc][ru]':'Description','fields[desc][en]':'English'})
        account=SimpleNamespace(get_lot_fields=Mock(return_value=fields),save_lot=Mock())
        c=SimpleNamespace(account=account)
        bot=Mock()
        env=functions('pick_lot_tier',re=re,secrets=secrets,_PlsBotPanel=lambda c:bot,threading=threading,_lot_binding_locks={},log=Mock(),_pending_lot_keyword={10:{'lot_id':'1234','message_id':20}},get_tier=lambda tid:{'id':tid},load_lots=lambda:[],save_lots=lambda lots:saved.extend(lots),build_lots_menu=Mock())
        call=SimpleNamespace(id='call',message=SimpleNamespace(chat=SimpleNamespace(id=10),message_id=20),data='cad_lot_tier_tier')
        env['pick_lot_tier'](c,call)
        account.save_lot.assert_called_once_with(fields)
        self.assertEqual(saved[0]['lot_id'],'1234');self.assertEqual(saved[0]['tier_id'],'tier')
        self.assertIn('#PLSCH:'+saved[0]['token'],fields.fields['fields[desc][ru]'])
        self.assertNotIn('keyword',saved[0])

    def test_failed_marker_save_does_not_create_binding(self):
        import re
        import secrets
        fields=SimpleNamespace(fields={'fields[desc][ru]':'Description'})
        account=SimpleNamespace(get_lot_fields=Mock(side_effect=[fields,SimpleNamespace(fields={'fields[desc][ru]':'unchanged'})]),save_lot=Mock())
        save=Mock();c=SimpleNamespace(account=account)
        env=functions('pick_lot_tier',re=re,secrets=secrets,_PlsBotPanel=lambda c:Mock(),threading=threading,_lot_binding_locks={},log=Mock(),_pending_lot_keyword={10:{'lot_id':'1234','message_id':20}},get_tier=lambda tid:{'id':tid},load_lots=lambda:[],save_lots=save,build_lots_menu=Mock())
        env['pick_lot_tier'](c,SimpleNamespace(id='call',message=SimpleNamespace(chat=SimpleNamespace(id=10),message_id=20),data='cad_lot_tier_tier'))
        save.assert_not_called()


class ChannelBindingRetryTests(unittest.TestCase):
    def setUp(self):
        import re
        import secrets
        self.bot = Mock()
        self.fields = SimpleNamespace(fields={'fields[desc][ru]': 'Русское описание',
                                              'fields[desc][en]': 'English'},
                                      description_ru='Русское описание', description_en='English')
        self.remote = dict(self.fields.fields)
        def save(fields):
            # FunPayAPI.Account.save_lot calls LotFields.renew_fields().
            for lang in ('ru', 'en'):
                fields.fields[f'fields[desc][{lang}]'] = getattr(fields, 'description_' + lang)
            self.remote.update(fields.fields)
        def read(lot_id):
            return SimpleNamespace(fields=dict(self.remote),
                                   description_ru=self.remote['fields[desc][ru]'],
                                   description_en=self.remote['fields[desc][en]'])
        self.account = SimpleNamespace(get_lot_fields=Mock(side_effect=read), save_lot=Mock(side_effect=save))
        self.card = SimpleNamespace(account=self.account)
        self.save = Mock()
        self.env = functions('pick_lot_tier', re=re, secrets=secrets, threading=threading,
                             _lot_binding_locks={}, log=Mock(), _PlsBotPanel=lambda c:self.bot,
                             _pending_lot_keyword={10:{'lot_id':'1234','message_id':20}},
                             get_tier=lambda tid:{'id':tid}, load_lots=lambda:[],
                             save_lots=self.save, build_lots_menu=Mock())
        self.call = SimpleNamespace(id='call', message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20),
                                    data='cad_lot_tier_tier')

    def pick(self):
        self.env['pick_lot_tier'](self.card, self.call)

    def test_marker_survives_funpay_field_renewal_and_preserves_descriptions(self):
        self.pick()
        record = self.save.call_args.args[0][0]
        for lang, original in (('ru', 'Русское описание'), ('en', 'English')):
            self.assertEqual(self.remote[f'fields[desc][{lang}]'], original + '\n\n#PLSCH:' + record['token'])
        self.assertNotIn(10, self.env['_pending_lot_keyword'])

    def test_failed_save_can_be_retried_from_same_keyboard(self):
        real_save = self.account.save_lot.side_effect
        self.account.save_lot.side_effect = ValueError('private error')
        self.pick()
        self.save.assert_not_called()
        self.assertIn(10, self.env['_pending_lot_keyword'])
        self.assertNotIn('private error', str(self.bot.mock_calls))
        self.account.save_lot.side_effect = real_save
        self.pick()
        self.save.assert_called_once()

    def test_unconfirmed_marker_keeps_session_and_does_not_bind(self):
        self.account.save_lot.side_effect = None
        self.pick()
        self.save.assert_not_called()
        self.assertIn(10, self.env['_pending_lot_keyword'])
        self.assertIn('FunPay не подтвердил', str(self.bot.mock_calls))

    def test_stale_keyboard_cannot_bind_new_lot(self):
        self.call.message.message_id = 19
        self.pick()
        self.account.get_lot_fields.assert_not_called()
        self.save.assert_not_called()
        self.assertIn(10, self.env['_pending_lot_keyword'])

    def test_double_click_during_save_does_not_repeat_request(self):
        save = self.account.save_lot.side_effect
        def double_click(fields):
            self.pick()
            save(fields)
        self.account.save_lot.side_effect = double_click
        self.pick()
        self.account.save_lot.assert_called_once()
        self.save.assert_called_once()
        self.assertIn('уже выполняется', str(self.bot.mock_calls))

    def test_completed_keyboard_cannot_bind_again(self):
        self.pick()
        self.pick()
        self.save.assert_called_once()
        self.account.save_lot.assert_called_once()

    def test_new_session_during_request_is_preserved(self):
        save = self.account.save_lot.side_effect
        new_session = {'lot_id':'5678','message_id':21}
        def new_input(fields):
            self.env['_pending_lot_keyword'][10] = new_session
            save(fields)
        self.account.save_lot.side_effect = new_input
        self.pick()
        self.assertIs(self.env['_pending_lot_keyword'][10], new_session)

    def test_local_save_failure_keeps_session_for_retry(self):
        self.save.side_effect = OSError('disk full')
        self.pick()
        self.assertIn(10, self.env['_pending_lot_keyword'])
        self.assertIn('сохранение привязки на диске', str(self.bot.mock_calls))
