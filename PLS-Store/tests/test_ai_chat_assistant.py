import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/AIChatAssistant.py'


def load():
    spec = importlib.util.spec_from_file_location('ai_chat_test', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AIChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = load()
        self.p.ROOT = Path(self.tmp.name)
        self.p._init_db()
        self.p._key = Mock(return_value='synthetic-api-key')
        self.p._set('model', 'model-test')
        self.p._set('enabled', '1')
        self.card = SimpleNamespace(account=SimpleNamespace(id=10, chat_id_private=Mock(return_value=True), get_order=Mock()),
                                    send_message=Mock())
        self.p._request = Mock(return_value={'choices': [{'message': {'content': 'Здравствуйте! Чем помочь?'}}]})

    def event(self, text='Здравствуйте, как оформить заказ?', mid=1, author=20, kind='NON_SYSTEM', by_bot=False, chat=123):
        return SimpleNamespace(message=SimpleNamespace(text=text, id=mid, author_id=author,
            type=SimpleNamespace(name=kind), by_bot=by_bot, chat_id=chat))

    def enqueue(self, event=None):
        self.p.new_message_handler(self.card, event or self.event())
        with self.p._db() as conn:
            conn.execute('UPDATE jobs SET ready=0')

    def states(self):
        with self.p._db() as conn:
            return [r['state'] for r in conn.execute('SELECT state FROM jobs ORDER BY rowid')]

    def test_customer_question_produces_immediate_funpay_reply_without_approval(self):
        self.enqueue()
        self.assertTrue(self.p._process_one(self.card))
        self.card.send_message.assert_called_once_with(123, 'Здравствуйте! Чем помочь?')
        self.assertEqual(self.states(), ['sent'])
        payload = self.p._request.call_args.args[2]
        self.assertEqual(payload['model'], 'model-test')
        self.assertEqual(payload['messages'][-1]['content'], 'Здравствуйте, как оформить заказ?')

    def test_duplicate_event_and_restart_do_not_send_again(self):
        self.enqueue()
        self.enqueue()
        self.p._process_one(self.card)
        self.p._init_db()
        self.enqueue()
        self.assertFalse(self.p._process_one(self.card))
        self.card.send_message.assert_called_once()

    def test_newer_messages_supersede_old_jobs_but_keep_context(self):
        self.enqueue()
        self.enqueue(self.event('А сколько времени ждать?', mid=2))
        self.p._process_one(self.card)
        self.assertEqual(self.states(), ['superseded', 'sent'])
        texts = [m['content'] for m in self.p._request.call_args.args[2]['messages'] if m['role'] == 'user']
        self.assertEqual(texts, ['Здравствуйте, как оформить заказ?', 'А сколько времени ждать?'])
        self.card.send_message.assert_called_once()

    def test_new_message_while_model_is_generating_cancels_old_response(self):
        self.enqueue()
        def response(*args):
            self.enqueue(self.event('Уточню свой вопрос', mid=2))
            return {'choices': [{'message': {'content': 'old reply'}}]}
        self.p._request.side_effect = response
        self.p._process_one(self.card)
        self.card.send_message.assert_not_called()
        self.assertEqual(self.states(), ['cancelled', 'pending'])

    def test_delivery_inputs_and_credentials_never_reach_api_or_history(self):
        for i, text in enumerate(('@buyername', 'https://t.me/channel', '+', 'чек 1234', '/start', '12345',
                                  'PLS-example-license', 'sk-synthetic-secret-key', 'мой пароль: example')):
            self.enqueue(self.event(text, mid=i+1, chat=200+i))
        self.assertFalse(self.p._process_one(self.card))
        self.p._request.assert_not_called()
        with self.p._db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM history').fetchone()[0], 0)

    def test_delivery_input_cancels_pending_question(self):
        self.enqueue()
        self.enqueue(self.event('@recipient', mid=2))
        self.assertFalse(self.p._process_one(self.card))
        self.card.send_message.assert_not_called()
        self.assertEqual(self.states(), ['cancelled'])

    def test_seller_manual_reply_pauses_only_that_chat_for_thirty_minutes(self):
        self.enqueue()
        self.enqueue(self.event('Я сейчас помогу', mid=2, author=10))
        self.enqueue(self.event('Спасибо, а дальше?', mid=3))
        self.assertFalse(self.p._process_one(self.card))
        with self.p._db() as conn:
            pause = conn.execute('SELECT pause FROM chats WHERE chat=?', ('123',)).fetchone()[0]
        self.assertGreater(pause, self.p.time.time() + 1790)
        self.enqueue(self.event('Есть вопрос', mid=4, chat=456))
        self.p._process_one(self.card)
        self.card.send_message.assert_called_once()

    def test_bot_system_and_public_messages_are_not_customer_questions(self):
        self.enqueue(self.event('Ответ другого бота', by_bot=True))
        self.enqueue(self.event('Системное событие', author=0))
        self.card.account.chat_id_private.return_value = False
        self.enqueue(self.event())
        self.assertEqual(self.states(), [])

    def test_disable_during_generation_prevents_post(self):
        self.enqueue()
        def response(*args):
            self.p._set('enabled', '0')
            return {'choices': [{'message': {'content': 'Не отправлять'}}]}
        self.p._request.side_effect = response
        self.p._process_one(self.card)
        self.card.send_message.assert_not_called()

    def test_no_key_no_model_or_disabled_means_no_api_call(self):
        self.p._key.return_value = ''
        self.enqueue()
        self.p._key.return_value = 'synthetic'
        self.p._set('model', '')
        self.enqueue(self.event(mid=2))
        self.p._set('model', 'test')
        self.p._set('enabled', '0')
        self.enqueue(self.event(mid=3))
        self.assertEqual(self.states(), [])

    def test_only_verified_order_title_and_status_are_shared(self):
        self.enqueue(self.event('Покупатель оплатил заказ #AB12CD34.', author=0, kind='ORDER_PURCHASED'))
        self.card.account.get_order.return_value = SimpleNamespace(seller_id=10, buyer_id=20,
            short_description='100 Stars', full_description='private file or password', status=SimpleNamespace(name='PAID'))
        self.enqueue(self.event(mid=2))
        self.p._process_one(self.card)
        payload = json.dumps(self.p._request.call_args.args[2], ensure_ascii=False)
        self.assertIn('100 Stars', payload)
        self.assertIn('PAID', payload)
        self.assertNotIn('private file or password', payload)
        self.assertNotIn('synthetic-api-key', payload)

    def test_foreign_order_is_not_sent_to_model(self):
        self.enqueue(self.event('Заказ #AB12CD34.', author=0, kind='ORDER_PURCHASED'))
        self.card.account.get_order.return_value = SimpleNamespace(seller_id=10, buyer_id=99, short_description='OTHER BUYER')
        self.enqueue(self.event(mid=2))
        self.p._process_one(self.card)
        self.assertNotIn('OTHER BUYER', json.dumps(self.p._request.call_args.args[2]))

    def test_simple_greeting_uses_zero_paid_api_calls(self):
        self.enqueue(self.event('Привет!'))
        self.p._process_one(self.card)
        self.p._request.assert_not_called()
        self.card.send_message.assert_called_once()
        with self.p._db() as conn:
            row = conn.execute('SELECT usage,outcome FROM api_usage').fetchone()
        self.assertEqual(row['outcome'], 'local')
        self.assertEqual(json.loads(row['usage'])['total_tokens'], 0)

    def test_long_useful_answer_is_not_cut_to_old_character_limit(self):
        self.enqueue()
        reply = 'Подробное объяснение установки и настройки. ' * 60
        self.p._request.return_value = {'choices': [{'message': {'content': reply}}],
                                       'usage': {'prompt_tokens': 100, 'completion_tokens': 600, 'total_tokens': 700}}
        self.p._process_one(self.card)
        self.assertEqual(self.card.send_message.call_args.args[1], reply.strip())
        self.assertEqual(self.p._request.call_count, 1)
        self.assertEqual(self.p._request.call_args.args[2]['max_tokens'], 800)

    def test_provider_reference_is_replaced_before_actual_funpay_post(self):
        self.enqueue()
        self.p._request.return_value = {'choices': [{'message': {'content': 'Ответ сгенерирован через OpenAI, пишите @seller'}}]}
        self.p._process_one(self.card)
        reply = self.card.send_message.call_args.args[1]
        self.assertNotIn('OpenAI', reply)
        self.assertNotIn('@seller', reply)
        self.assertIn('дождитесь', reply)

    def test_provider_error_and_invalid_response_do_not_post_or_expose_error_body(self):
        self.enqueue()
        self.p._request.side_effect = RuntimeError('synthetic secret in HTTP error')
        self.p._process_one(self.card)
        self.card.send_message.assert_not_called()
        self.assertNotIn('synthetic secret', self.p._get('last_error'))
        self.assertEqual(self.states(), ['failed'])

    def test_funpay_timeout_is_not_retried_to_avoid_duplicate_message(self):
        self.enqueue()
        self.card.send_message.side_effect = TimeoutError('lost response')
        self.p._process_one(self.card)
        self.assertEqual(self.states(), ['uncertain'])
        self.p._init_db()
        self.assertFalse(self.p._process_one(self.card))
        self.card.send_message.assert_called_once()

    def test_restart_does_not_repeat_potentially_charged_generation_or_send(self):
        self.enqueue()
        with self.p._db() as conn:
            conn.execute("UPDATE jobs SET state='sending'")
        self.p._init_db()
        self.assertEqual(self.states(), ['uncertain'])
        with self.p._db() as conn:
            conn.execute("UPDATE jobs SET state='generating'")
        self.p._init_db()
        self.assertEqual(self.states(), ['failed'])

    def test_old_jobs_are_not_answered(self):
        self.enqueue()
        with self.p._db() as conn:
            conn.execute('UPDATE jobs SET created=0')
        self.assertFalse(self.p._process_one(self.card))
        self.assertEqual(self.states(), ['expired'])


class APITransportTests(unittest.TestCase):
    def test_fixed_https_endpoint_uses_header_auth_and_no_redirects(self):
        p = load()
        p._key = Mock(return_value='synthetic-only')
        with patch.object(p.requests, 'request', return_value=Mock(status_code=200, json=lambda: {'data': []})) as request:
            self.assertEqual(p._request('GET', '/models'), {'data': []})
        args, kwargs = request.call_args
        self.assertEqual(args, ('GET', 'https://tyler-tokens.life/v1/models'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer synthetic-only')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('verify', kwargs)

    def test_api_error_does_not_expose_response_content(self):
        p = load()
        p._key = lambda: 'synthetic'
        with patch.object(p.requests, 'request', return_value=Mock(status_code=401)):
            with self.assertRaisesRegex(ValueError, 'HTTP 401'):
                p._request('GET', '/models')

    def test_key_file_permissions_and_environment_priority(self):
        p = load()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            p.ROOT = Path(folder)
            p._save_key('synthetic-private-value')
            self.assertEqual(p._key(), 'synthetic-private-value')
            self.assertEqual((p.ROOT/'api_key.txt').stat().st_mode & 0o777, 0o600)
            os.environ['PLS_AI_API_KEY'] = 'synthetic-environment-key'
            self.assertEqual(p._key(), 'synthetic-environment-key')


class CatalogAndPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = load()
        self.p.ROOT = Path(self.tmp.name)
        self.p._init_db()
        sub = SimpleNamespace(type=SimpleNamespace(name='COMMON'), name='Telegram')
        self.lots = [SimpleNamespace(id=100, description='100 Telegram Stars', price=150.0, subcategory=sub, html='150 ₽'),
                     SimpleNamespace(id=200, description='Подписчики Telegram', price=20.0, subcategory=sub, html='20 ₽')]
        self.profile = SimpleNamespace(id=10, get_lots=lambda: self.lots)
        self.fields = SimpleNamespace(title_ru='100 Telegram Stars', description_ru='Выдача по правилам лота.',
                                      description_en='', active=True, fields={'delivery': 'NEVER_SHARE_PRIVATE_DELIVERY'})
        self.card = SimpleNamespace(account=SimpleNamespace(id=10, get_user=Mock(return_value=self.profile),
                                    get_lot_fields=Mock(return_value=self.fields)))

    def test_own_catalog_and_relevant_description_without_private_delivery_fields(self):
        context = self.p._catalog_context(self.card, 'Сколько стоят 100 звёзд?')
        self.card.account.get_user.assert_called_once_with(10)
        self.assertTrue(context['fresh'])
        self.assertEqual(context['items'][0]['id'], '100')
        self.assertEqual(context['items'][0]['price'], '150.0')
        self.assertEqual(context['items'][0]['currency'], 'RUB')
        self.assertIn('description', context['items'][0])
        self.assertNotIn('NEVER_SHARE_PRIVATE_DELIVERY', json.dumps(context))
        self.assertEqual(context['items'][0]['url'], 'https://funpay.com/lots/offer?id=100')

    def test_profile_and_descriptions_are_cached_without_refetch_for_each_question(self):
        self.p._catalog_context(self.card, '100 stars')
        self.p._catalog_context(self.card, '100 stars')
        self.card.account.get_user.assert_called_once()
        self.card.account.get_lot_fields.assert_called_once()

    def test_stale_catalog_never_claims_current_price_when_refresh_fails(self):
        self.p._refresh_catalog(self.card)
        with self.p._db() as conn:
            conn.execute('UPDATE catalog SET checked=0')
        self.card.account.get_user.side_effect = RuntimeError('No connection')
        context = self.p._catalog_context(self.card, '100 stars')
        self.assertFalse(context['fresh'])
        self.assertTrue(all(item['price'] is None for item in context['items']))
        self.assertTrue(all('description' not in item for item in context['items']))

    def test_removed_products_disappear_after_refresh(self):
        self.p._refresh_catalog(self.card)
        self.lots = self.lots[:1]
        catalog, fresh = self.p._refresh_catalog(self.card, force=True)
        self.assertTrue(fresh)
        self.assertEqual([item['id'] for item in catalog['items']], ['100'])

    def test_other_seller_profile_cannot_be_used(self):
        self.profile.id = 99
        catalog, fresh = self.p._refresh_catalog(self.card)
        self.assertFalse(fresh)
        self.assertEqual(catalog['items'], [])

    def test_currency_offers_do_not_use_common_lot_edit_endpoint(self):
        self.lots[0].subcategory = SimpleNamespace(type=SimpleNamespace(name='CURRENCY'), name='Currency')
        context = self.p._catalog_context(self.card, '100 stars')
        self.assertEqual(context['items'][0]['url'], 'https://funpay.com/chips/offer?id=100')
        self.assertNotIn('description', context['items'][0])
        self.card.account.get_lot_fields.assert_not_called()

    def test_inactive_lot_is_marked_and_unknown_currency_is_not_invented(self):
        self.fields.active = False
        self.lots[0].html = ''
        item = self.p._catalog_context(self.card, '100 stars')['items'][0]
        self.assertFalse(item['active'])
        self.assertEqual(item['currency'], '')

    def test_policy_filter_blocks_provider_names_external_contacts_and_links(self):
        allowed = {'https://funpay.com/lots/offer?id=100'}
        for text in ('Я работаю через OpenAI', 'Мой сервис tyler-tokens.life', 'Заказ через Fragment API',
                     'Пишите @seller', 'Оплата https://example.com', 'Напишите t.me/seller',
                     'Платите на funpay.com.evil.com', 'Откройте https://funpay.com@evil.com/path'):
            with self.subTest(text=text):
                result = self.p._customer_reply(text, allowed)
                self.assertIn('дождитесь', result)
        for text in ('Есть 100 Telegram Stars.', 'Оплата только на FunPay.', 'Оплатите на funpay.com',
                     'Товар: https://funpay.com/lots/offer?id=100'):
            self.assertEqual(self.p._customer_reply(text, allowed), text)
        self.assertIn('дождитесь', self.p._customer_reply('https://funpay.com/lots/offer?id=999', allowed))

    def test_public_text_removes_credentials_provider_names_and_external_links(self):
        text = self.p._public_text('Товар <b>stars</b> sk-synthetic-private https://example.com @seller OpenAI')
        self.assertNotIn('sk-synthetic-private', text)
        self.assertNotIn('https://example.com', text)
        self.assertNotIn('OpenAI', text)
        self.assertNotIn('@seller', text)

    def test_prompt_asks_for_direct_answers_and_keeps_transaction_on_funpay(self):
        prompt = self.p.DEFAULT_INSTRUCTIONS
        for phrase in ('полный полезный ответ', 'не спрашивай уже известное', 'ОДИН нужный вопрос',
                       'только на FunPay', 'до полного получения', 'провайдера или модели'):
            self.assertIn(phrase, prompt)


class EfficientContextTests(unittest.TestCase):
    def setUp(self):
        CatalogAndPolicyTests.setUp(self)
        self.card.account.get_chat = Mock(return_value=SimpleNamespace(id=123, looking_link='/lots/offer?id=100'))
        self.job = dict(seller=10, buyer=20, chat='123', message=1)
        self.p._set('enabled', '1')
        self.p._set('model', 'synthetic-model')
        self.p._key = lambda: 'synthetic-key'
        with self.p._db() as conn:
            conn.execute('INSERT INTO history VALUES (?,?,?,?,?)', (10,'123','user','Можно купить этот плагин?', self.p.time.time()))

    def test_exact_current_lot_is_used_instead_of_unrelated_rentals(self):
        rental = SimpleNamespace(id=300, description='Аренда игры 24 часа', price=25, html='25 ₽', subcategory=self.lots[0].subcategory)
        self.lots.append(rental)
        messages = self.p._messages(self.card, self.job)
        payload = messages[0]['content']
        self.card.account.get_chat.assert_called_once_with(123)
        self.assertIn('100 Telegram Stars', payload)
        self.assertIn('current_product', payload)
        self.assertNotIn('Аренда игры 24 часа', payload)
        self.assertLessEqual(self.p._input_bytes(messages), self.p._MAX_INPUT_BYTES)

    def test_unrelated_shop_catalog_does_not_fill_an_ambiguous_question(self):
        self.card.account.get_chat.return_value.looking_link = None
        self.lots[0].description = 'Аренда игры 1'
        self.lots[1].description = 'Аренда игры 2'
        messages = self.p._messages(self.card, self.job)
        self.assertNotIn('Аренда игры 1', messages[0]['content'])
        self.assertNotIn('Аренда игры 2', messages[0]['content'])
        self.assertIn('не угадывай товар', messages[0]['content'])

    def test_foreign_looking_link_never_becomes_current_product(self):
        for url in ('https://evil.com/lots/offer?id=100', 'https://funpay.com@evil.com/lots/offer?id=100'):
            self.card.account.get_chat.return_value.looking_link = url
            self.assertIsNone(self.p._current_product(self.card, '123'))

    def test_large_history_keeps_current_question_and_fits_input_budget(self):
        self.p._set('knowledge', 'Полезные правила. ' * 500)
        with self.p._db() as conn:
            conn.execute('DELETE FROM history')
            for index in range(8):
                conn.execute('INSERT INTO history VALUES (?,?,?,?,?)', (10, '123', 'assistant', 'Старая история ' * 500, self.p.time.time()))
            conn.execute('INSERT INTO history VALUES (?,?,?,?,?)', (10, '123', 'user', 'Можно купить этот плагин?', self.p.time.time()))
        messages = self.p._messages(self.card, self.job)
        self.assertLessEqual(self.p._input_bytes(messages), self.p._MAX_INPUT_BYTES)
        self.assertEqual(messages[-1]['content'], 'Можно купить этот плагин?')

    def test_greeting_has_no_shop_context(self):
        with self.p._db() as conn:
            conn.execute("UPDATE history SET text='Привет'")
        messages = self.p._messages(self.card, self.job)
        self.card.account.get_user.assert_not_called()
        self.assertNotIn('100 Telegram Stars', messages[0]['content'])

    def test_actual_usage_is_recorded_without_secret_content(self):
        self.p._record_usage(self.job, 'model', 1234,
            {'prompt_tokens': 200, 'completion_tokens': 80, 'total_tokens': 280,
             'completion_tokens_details': {'reasoning_tokens': 20}, 'secret': 'must-not-store'}, 'received')
        with self.p._db() as conn:
            row = conn.execute('SELECT * FROM api_usage').fetchone()
        self.assertEqual(json.loads(row['usage'])['total_tokens'], 280)
        self.assertEqual(json.loads(row['usage'])['completion_tokens_details']['reasoning_tokens'], 20)
        self.assertNotIn('must-not-store', row['usage'])
