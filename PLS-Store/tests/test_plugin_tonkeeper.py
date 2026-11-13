"""Provider REST contract, durable replay prevention, and secret boundaries.

All credentials are synthetic test strings. No live wallet or purchases.
"""
import ast
import base64
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import Mock


class TonkeeperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        source = (Path(__file__).resolve().parents[1] / "plugins/AutoStars.py").read_text()
        tree = ast.parse(source)
        names = {"_parse_mnemonic", "_extract_wallet_address", "_auto_refund_order"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and (n.name.startswith("_space_") or n.name in names)]
        self.cfg = {"fragment_provider": "space", "fragment_jwt": "space-wallet"}
        self.requests = Mock()
        self.bot = Mock()
        self.env = dict(os=os, json=json, _re=re, _pls_base64=base64,
                        _seed_unicode=unicodedata, _SPACE_LOCK=threading.RLock(),
                        _SPACE_BASE="https://api.fragment-api.space",
                        PLUGIN_FOLDER=self.temp.name, requests=self.requests,
                        _pls_time=SimpleNamespace(sleep=Mock()), logger=Mock(),
                        _owner_key=lambda chat: "10", _get_cfg_for_orders=lambda chat: self.cfg,
                        _get_proxies_for_chat=Mock(return_value=None),
                        _set_cfg=Mock(side_effect=lambda chat, **kw: self.cfg.update(kw)),
                        _fsm={}, _kb_cancel_fsm=Mock(), _send_panel=Mock(),
                        _token_text=Mock(), _token_kb=Mock(), _safe_send=Mock())
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future]+nodes, type_ignores=[])),
                     "<plugin>", "exec"), self.env)
        self.address = "UQ" + "A"*46
        self.words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
        self.credentials = {"seed": base64.b64encode(self.words.encode()).decode(),
                            "wallet_address": self.address, "account_index": 0}
        self.env['_space_write']("wallet_credentials.json", {"10": self.credentials})
        self.requests.post.return_value = self.response({"request_id": "request-123"})
        self.requests.get.return_value = self.response({"status": "completed", "result": {"success": True}})

    def response(self, data, success=True, status=200):
        return SimpleNamespace(status_code=status, json=lambda: {"success": success, "data": data})

    def buy(self, username="testuser", quantity=50, order_id="FP123"):
        return self.env['_space_purchase'](username, quantity, "20", order_id)

    def test_12_words_sent_unchanged_with_address_and_ton(self):
        self.assertTrue(self.buy()['ok'])
        call = self.requests.post.call_args
        self.assertEqual(call.args, ('https://api.fragment-api.space/api/v1/stars/buy',))
        payload = call.kwargs['json']
        self.assertEqual(base64.b64decode(payload['seed']).decode(), self.words)
        self.assertEqual(payload['wallet_address'], self.address)
        self.assertEqual(payload['amount'], 50)
        self.assertEqual(payload['username'], '@testuser')
        self.assertEqual(payload['payment_method'], 'ton')
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertNotIn('params', call.kwargs)

    def test_repeated_notification_and_restart_do_not_post_again(self):
        self.assertTrue(self.buy()['ok'])
        # The journal, not in-memory state, governs the second event.
        self.assertTrue(self.buy()['ok'])
        self.requests.post.assert_called_once()

    def test_lost_response_keeps_reservation_and_never_reposts(self):
        self.requests.post.side_effect = RuntimeError(self.words)
        self.assertTrue(self.buy()['pending'])
        self.assertTrue(self.buy()['pending'])
        self.requests.post.assert_called_once()
        self.assertNotIn(self.words, str(self.env['logger'].mock_calls))
        self.assertEqual(self.env['_space_read']('provider_orders.json')['FP123']['state'], 'submitting')

    def test_polling_resumes_from_saved_request(self):
        self.requests.get.side_effect = TimeoutError('offline')
        self.assertTrue(self.buy()['pending'])
        self.requests.get.side_effect = None
        self.assertTrue(self.buy()['ok'])
        self.requests.post.assert_called_once()
        self.assertTrue(self.requests.get.call_args.args[0].endswith('/queue/request-123'))

    def test_queued_processing_not_success(self):
        for state in ('queued', 'processing', 'failed', 'timeout', 'unknown'):
            with self.subTest(state=state):
                self.requests.get.return_value = self.response({'status': state})
                result = self.buy()
                self.assertFalse(result['ok'])
                self.assertTrue(result['pending'])
        self.requests.post.assert_called_once()

    def test_completed_but_failed_result_never_delivered(self):
        for result in ({'success': False}, {'data': {'success': False}}):
            self.requests.get.return_value = self.response({'status': 'completed', 'result': result})
            self.assertTrue(self.buy()['pending'])

    def test_changed_recipient_or_quantity_never_reposts(self):
        self.assertTrue(self.buy()['ok'])
        self.assertTrue(self.buy(username='different')['pending'])
        self.assertTrue(self.buy(quantity=100)['pending'])
        self.requests.post.assert_called_once()

    def test_no_order_id_no_spending(self):
        self.assertTrue(self.buy(order_id=None)['pending'])
        self.requests.post.assert_not_called()

    def test_disk_failure_prevents_network(self):
        self.env['_space_write'] = Mock(side_effect=OSError('disk full'))
        self.assertTrue(self.buy()['pending'])
        self.requests.post.assert_not_called()

    def test_invalid_response_and_redirect_do_not_retry(self):
        self.requests.post.return_value = self.response({}, status=307)
        self.assertTrue(self.buy()['pending'])
        self.assertTrue(self.buy()['pending'])
        self.requests.post.assert_called_once()
        self.requests.get.assert_not_called()

    def test_private_permissions_and_no_seed_in_order_journal(self):
        self.assertTrue(self.buy()['ok'])
        for name in ('provider_orders.json', 'wallet_credentials.json'):
            self.assertEqual(os.stat(Path(self.temp.name)/name).st_mode & 0o777, 0o600)
        text = (Path(self.temp.name)/'provider_orders.json').read_text()
        self.assertNotIn('seed', text)
        self.assertNotIn(self.credentials['seed'], text)

    def test_refund_blocked_for_submitted_and_completed(self):
        cardinal = Mock()
        self.buy()
        self.assertFalse(self.env['_auto_refund_order'](cardinal, 'FP123', '20', 'test'))
        cardinal.account.refund.assert_not_called()
        self.assertTrue(self.env['_auto_refund_order'](cardinal, 'OTHER', '20', 'test'))
        cardinal.account.refund.assert_called_once_with('OTHER')

    def test_resolve_12_words_persists_only_private_credentials(self):
        self.requests.post.return_value = self.response({
            'wallet_address': self.address, 'wallet_version': 'v5r1',
            'seed_format': 'bip39-12', 'account_index': 3})
        state = {'step': 'space_seed', 'wallet_address': self.address}
        self.env['_fsm'][10] = state
        message = SimpleNamespace(chat=SimpleNamespace(id=10), message_id=1, text=self.words)
        cardinal = SimpleNamespace(telegram=SimpleNamespace(bot=self.bot))
        self.env['_space_fsm'](message, cardinal, state)
        self.assertEqual(self.cfg['fragment_provider'], 'space')
        credentials = self.env['_space_read']('wallet_credentials.json')['10']
        self.assertEqual(credentials['account_index'], 3)
        self.assertEqual(base64.b64decode(credentials['seed']).decode(), self.words)
        self.assertNotIn('seed', self.cfg)
        self.assertNotIn(10, self.env['_fsm'])
        self.assertNotIn(self.words, str(self.bot.send_message.mock_calls))
        self.bot.delete_message.assert_called_once_with(10, 1)
        self.assertTrue(self.requests.post.call_args.args[0].endswith('/wallet/resolve'))

    def test_mismatching_resolved_address_is_not_connected(self):
        self.cfg.clear()
        other = 'UQ' + 'B'*46
        self.requests.post.return_value = self.response({
            'wallet_address': other, 'wallet_version': 'v5r1',
            'seed_format': 'bip39-12', 'account_index': 0})
        state = {'step': 'space_seed', 'wallet_address': self.address}
        self.env['_space_fsm'](SimpleNamespace(chat=SimpleNamespace(id=10), message_id=1, text=self.words),
                               SimpleNamespace(telegram=SimpleNamespace(bot=self.bot)), state)
        self.env['_set_cfg'].assert_not_called()

    def test_wrong_mnemonic_type_is_not_connected(self):
        self.cfg.clear()
        self.requests.post.return_value = self.response({
            'wallet_address': self.address, 'wallet_version': 'v4r2',
            'seed_format': 'ton', 'account_index': 0})
        state = {'step': 'space_seed', 'wallet_address': self.address}
        self.env['_space_fsm'](SimpleNamespace(chat=SimpleNamespace(id=10), message_id=1, text=self.words),
                               SimpleNamespace(telegram=SimpleNamespace(bot=self.bot)), state)
        self.env['_set_cfg'].assert_not_called()
        self.assertNotIn(self.words, str(self.bot.mock_calls))

    def test_cancel_clears_state_and_no_seed_request_before_consent(self):
        state = {'step': 'space_consent'}
        self.env['_fsm'][10] = state
        message = SimpleNamespace(chat=SimpleNamespace(id=10), text='нет')
        cardinal = SimpleNamespace(telegram=SimpleNamespace(bot=self.bot))
        self.env['_space_fsm'](message, cardinal, state)
        self.assertEqual(state['step'], 'space_consent')
        message.text = '/cancel'
        self.env['_space_fsm'](message, cardinal, state)
        self.assertNotIn(10, self.env['_fsm'])
        self.requests.post.assert_not_called()

    def test_license_check_precedes_provider_calls_and_callbacks_registered(self):
        source = (Path(__file__).resolve().parents[1] / 'plugins/AutoStars.py').read_text()
        tree = ast.parse(source)
        order = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_order_stars')
        self.assertEqual(order.body[0].value.func.id, '_pls_require_license')
        self.assertIn('_start_space(bot, c)', source)
        self.assertIn('"space_consent","space_address","space_seed"', source)
