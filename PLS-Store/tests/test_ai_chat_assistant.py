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

    def test_restart_during_send_is_uncertain_and_during_generation_is_recoverable(self):
        self.enqueue()
        with self.p._db() as conn:
            conn.execute("UPDATE jobs SET state='sending'")
        self.p._init_db()
        self.assertEqual(self.states(), ['uncertain'])
        with self.p._db() as conn:
            conn.execute("UPDATE jobs SET state='generating'")
        self.p._init_db()
        self.assertEqual(self.states(), ['pending'])

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
