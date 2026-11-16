import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/AutoReviewReply.py'


def load_plugin():
    spec = importlib.util.spec_from_file_location('review_reply_test', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = load_plugin()
        self.p.DB_PATH = Path(self.tmp.name) / 'state.db'
        self.p._init_db()
        self.review = SimpleNamespace(stars=5, reply=None)
        self.order = SimpleNamespace(seller_id=10, short_description='🌟 100 Telegram Stars', review=self.review)
        self.account = SimpleNamespace(id=10, get_order=Mock(return_value=self.order), send_review=Mock())
        self.account.send_review.side_effect = lambda oid, text: setattr(self.review, 'reply', text)
        self.card = SimpleNamespace(account=self.account)

    def event(self, kind='NEW_FEEDBACK', author=0):
        return SimpleNamespace(message=SimpleNamespace(author_id=author, type=SimpleNamespace(name=kind),
            text='Покупатель Buyer написал отзыв к заказу #AB12CD34.'))

    def enqueue(self):
        self.p.new_message_handler(self.card, self.event())

    def test_reply_uses_real_order_title_and_is_public_review_answer(self):
        self.enqueue()
        self.p._process_one(self.card)
        self.account.send_review.assert_called_once_with('AB12CD34', 'Спасибо за заказ и отзыв! 💜\n\n🛍 🌟 100 Telegram Stars')

    def test_only_five_stars_and_changed_rating_can_be_reconsidered(self):
        for stars in (1, 2, 3, 4):
            self.review.stars = stars
            self.enqueue()
            self.p._process_one(self.card)
        self.account.send_review.assert_not_called()
        self.review.stars = 5
        self.p.new_message_handler(self.card, self.event('FEEDBACK_CHANGED'))
        self.p._process_one(self.card)
        self.account.send_review.assert_called_once()

    def test_no_overwrite_of_existing_answer(self):
        self.review.reply = 'Мой ручной ответ'
        self.enqueue()
        self.p._process_one(self.card)
        self.account.send_review.assert_not_called()

    def test_repeated_events_and_restart_do_not_send_again(self):
        self.enqueue()
        self.enqueue()
        self.p._process_one(self.card)
        self.review.reply = None  # The persisted record also blocks a replay.
        self.p = load_plugin()
        self.p.DB_PATH = Path(self.tmp.name) / 'state.db'
        self.p._init_db()
        self.enqueue()
        self.p._process_one(self.card)
        self.account.send_review.assert_called_once()

    def test_buyers_system_lookalike_text_and_non_review_events_ignored(self):
        self.p.new_message_handler(self.card, self.event(author=99))
        for kind in ('ORDER_CONFIRMED', 'NON_SYSTEM', 'NEW_FEEDBACK_ANSWER', 'FEEDBACK_DELETED'):
            self.p.new_message_handler(self.card, self.event(kind))
        self.assertFalse(self.p._process_one(self.card))
        self.account.get_order.assert_not_called()

    def test_disabled_plugin_does_not_enqueue_or_send(self):
        self.p._set_setting('enabled', '0')
        self.enqueue()
        self.assertFalse(self.p._process_one(self.card))
        self.account.get_order.assert_not_called()

    def test_not_our_sale_cannot_be_answered(self):
        self.order.seller_id = 55
        self.enqueue()
        self.p._process_one(self.card)
        self.account.send_review.assert_not_called()

    def test_lost_response_is_reconciled_before_retry(self):
        def uncertain_send(oid, text):
            self.review.reply = text
            raise TimeoutError('Response lost')
        self.account.send_review.side_effect = uncertain_send
        self.enqueue()
        self.p._process_one(self.card)
        with self.p._db() as conn:
            conn.execute('UPDATE jobs SET next_at=0')
        self.p._process_one(self.card)
        self.account.send_review.assert_called_once()
        with self.p._db() as conn:
            self.assertEqual(conn.execute('SELECT state FROM jobs').fetchone()['state'], 'answered')

    def test_unavailable_order_retries_without_reply(self):
        self.account.get_order.side_effect = RuntimeError('Unavailable')
        self.enqueue()
        self.p._process_one(self.card)
        self.account.send_review.assert_not_called()
        with self.p._db() as conn:
            row = conn.execute('SELECT attempts, state FROM jobs').fetchone()
        self.assertEqual((row['attempts'], row['state']), (1, 'pending'))

    def test_title_is_plain_text_not_a_template_and_length_is_bounded(self):
        text = self.p._reply_text(self.p.DEFAULT_TEMPLATE, '{price} <b>Test</b>')
        self.assertIn('{price} <b>Test</b>', text)
        text = self.p._reply_text(self.p.DEFAULT_TEMPLATE, 'x' * 4000)
        self.assertLessEqual(len(text), 1000)
        self.assertTrue(text.endswith('…'))
        for template in ('Спасибо!', '{lot_title.__class__}', '{bad}', '{lot_title!r}', '{lot_title:9999}'):
            with self.subTest(template=template), self.assertRaises(ValueError):
                self.p._validate_template(template)

    def test_concurrent_events_produce_one_reply(self):
        threads = [threading.Thread(target=self.enqueue) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        workers = [threading.Thread(target=self.p._process_one, args=(self.card,)) for _ in range(2)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join()
        self.account.send_review.assert_called_once()

    def test_cardinal_registration_and_settings_input(self):
        import sys
        from unittest.mock import patch
        tg = SimpleNamespace(bot=Mock(), msg_handler=Mock(), cbq_handler=Mock())
        self.card.telegram = tg
        self.card.add_telegram_commands = Mock()
        fake_thread = Mock()
        fake_module = SimpleNamespace(CBT=SimpleNamespace(PLUGIN_SETTINGS='43'))
        with patch.dict(sys.modules, {'tg_bot': fake_module}), patch.object(self.p.threading, 'Thread', return_value=fake_thread):
            self.p.init_cardinal(self.card)
        fake_thread.start.assert_called_once()
        commands = self.card.add_telegram_commands.call_args.args[1]
        self.assertEqual(commands[0][0], 'reviewreply')
        callbacks = tg.cbq_handler.call_args_list
        self.assertTrue(any(call.kwargs['func'](SimpleNamespace(data='43:' + self.p.UUID)) for call in callbacks))
        self.p._WAITING.add(10)
        message = SimpleNamespace(chat=SimpleNamespace(id=10), text='Благодарю! {lot_title}')
        self.p._panel = Mock()
        self.p._receive_template(self.card, message)
        self.assertEqual(self.p._setting('template'), 'Благодарю! {lot_title}')
        self.assertNotIn(10, self.p._WAITING)
