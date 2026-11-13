"""Regression: original 24-word auth and safe removal of the former provider."""
import ast
import json
import re
import requests
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class WalletAuthTests(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parents[1] / 'plugins/AutoStars.py').read_text()
        tree = ast.parse(self.source)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'_get_cfg', '_handle_fsm', '_run_fragment_auth', '_jwt_auth_keyboard', '_jwt_confirmed', '_jwt_resend'}]
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        self.settings = {}
        self.bot = Mock()
        self.env = dict(json=json, logger=Mock(), K=Mock(), B=Mock(), CBT_JWT_CONFIRMED="confirmed", CBT_JWT_RESEND="retry", CBT_FSM_CANCEL="cancel", _load_settings=lambda: self.settings,
                        _save_settings=Mock(), _default_templates=lambda: {},
                        STARS_CATEGORY_ID=2418, MIN_BALANCE_TON=5,
                        _fsm={10: {'step': 'jwt_seed', 'api_key': 'dummy', 'phone': '123', 'wallet_version': 'V4R2'}},
                        _authenticate_fragment=Mock(return_value=('dummy-jwt', {}, 200)),
                        _set_cfg=Mock(), _check_fragment_wallet=Mock(return_value=(None, None, None)))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future]+nodes, type_ignores=[])),
                     '<plugin>', 'exec'), self.env)
        self.cardinal = SimpleNamespace(telegram=SimpleNamespace(bot=self.bot))

    def send_words(self, count):
        message = SimpleNamespace(chat=SimpleNamespace(id=10), text=' '.join(['synthetic']*count), message_id=1)
        self.env['_handle_fsm'](message, self.cardinal)

    def test_12_words_are_rejected_before_provider_call(self):
        self.send_words(12)
        self.env['_authenticate_fragment'].assert_not_called()
        self.assertIn('24', self.bot.send_message.call_args.args[1])

    def test_24_words_use_original_auth_and_store_jwt(self):
        self.send_words(24)
        self.assertEqual(self.env['_authenticate_fragment'].call_args.kwargs['mnemonics'], ['synthetic']*24)
        self.env['_set_cfg'].assert_called_once_with(10, fragment_jwt='dummy-jwt')
        self.assertNotIn(10, self.env['_fsm'])

    def test_former_provider_marker_is_reset_and_processing_disabled(self):
        self.settings['10'] = dict(fragment_provider='space', fragment_jwt='space-wallet',
                                   plugin_enabled=True, lots_active=True, wallet_address='public',
                                   star_lots=[{'lot_id': 1, 'qty': 50}])
        cfg = self.env['_get_cfg'](10)
        self.assertIsNone(cfg['fragment_jwt'])
        self.assertFalse(cfg['plugin_enabled'])
        self.assertFalse(cfg['lots_active'])
        self.assertNotIn('fragment_provider', cfg)
        self.assertEqual(cfg['star_lots'], [{'lot_id': 1, 'qty': 50}])
        self.env['_save_settings'].assert_called_once()

    def test_original_provider_token_is_preserved(self):
        self.settings['10'] = dict(fragment_provider='legacy', fragment_jwt='existing-jwt', plugin_enabled=True)
        cfg = self.env['_get_cfg'](10)
        self.assertEqual(cfg['fragment_jwt'], 'existing-jwt')
        self.assertTrue(cfg['plugin_enabled'])

    def test_removed_provider_has_no_routes_or_buttons_and_guide_requires_tonhub(self):
        self.assertNotIn('api.fragment-api.space', self.source)
        self.assertNotIn('_space_purchase', self.source)
        self.assertNotIn('Tonkeeper', self.source)
        guide = (Path(__file__).resolve().parents[1] / 'docs/AUTOSTARS_INSTALL.txt').read_text()
        self.assertIn('необходимо использовать кошелёк Tonhub', guide)
        self.assertIn('с мнемонической фразой из 24 слов', guide)

    def test_progress_is_sent_before_the_request(self):
        def authenticate(**kwargs):
            self.assertIn('24 слова получены', self.bot.send_message.call_args.args[1])
            return None, {'code': 'timeout'}, 0
        self.env['_authenticate_fragment'].side_effect = authenticate
        self.send_words(24)
        result = self.bot.send_message.call_args
        self.assertIn('API не ответил вовремя', result.args[1])
        self.assertNotIn('Подтвердите вход', result.args[1])
        self.assertIn('reply_markup', result.kwargs)
        self.assertNotIn('auth_busy', self.env['_fsm'][10])

    def test_proxy_failure_and_confirmation_have_different_messages(self):
        self.env['_authenticate_fragment'].return_value = (None, {'code': 'proxy_error'}, 0)
        self.send_words(24)
        self.assertIn('через прокси', self.bot.send_message.call_args.args[1])
        self.env['_authenticate_fragment'].return_value = (None, {'code': 'confirmation_required'}, 400)
        self.env['_jwt_confirmed'](self.bot, SimpleNamespace(id='cb', message=SimpleNamespace(chat=SimpleNamespace(id=10))))
        self.assertIn('API просит подтверждение', self.bot.send_message.call_args.args[1])
        self.assertIn('reply_markup', self.bot.send_message.call_args.kwargs)

    def test_unexpected_exception_does_not_echo_credentials_or_leave_busy(self):
        self.env['_authenticate_fragment'].side_effect = RuntimeError('secret-seed-and-api-key')
        self.send_words(24)
        self.assertIn('внутренней ошибки', self.bot.send_message.call_args.args[1])
        self.assertNotIn('secret-seed', str(self.bot.mock_calls) + str(self.env['logger'].mock_calls))
        self.assertNotIn('auth_busy', self.env['_fsm'][10])

    def test_whitespace_from_phone_is_accepted(self):
        text = '\u00a0\t\n'.join(['synthetic']*24)
        self.env['_handle_fsm'](SimpleNamespace(chat=SimpleNamespace(id=10), text=text, message_id=1), self.cardinal)
        self.assertEqual(len(self.env['_authenticate_fragment'].call_args.kwargs['mnemonics']), 24)

    def test_cancelled_request_cannot_save_token(self):
        def authenticate(**kwargs):
            self.env['_fsm'].pop(10)
            return 'dummy-jwt', {}, 200
        self.env['_authenticate_fragment'].side_effect = authenticate
        self.send_words(24)
        self.env['_set_cfg'].assert_not_called()

    def test_retry_success_uses_same_24_words_and_clears_fsm(self):
        self.env['_authenticate_fragment'].return_value = (None, {'code': 'connection_error'}, 0)
        self.send_words(24)
        self.env['_authenticate_fragment'].return_value = ('dummy-jwt', {}, 200)
        self.env['_jwt_resend'](self.bot, SimpleNamespace(id='cb', message=SimpleNamespace(chat=SimpleNamespace(id=10))))
        self.assertEqual(self.env['_authenticate_fragment'].call_count, 2)
        self.assertNotIn(10, self.env['_fsm'])


class AuthRequestTests(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'plugins/AutoStars.py').read_text()
        tree = ast.parse(source)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'_authenticate_fragment', '_is_too_many_attempts'}]
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        self.requests = Mock(exceptions=requests.exceptions)
        self.env = dict(json=json, _re=re, requests=self.requests, logger=Mock(),
                        _get_proxies_for_chat=Mock(return_value=None), FRAGMENT_AUTH_URL='https://api.example/auth')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future]+nodes, type_ignores=[])), '<auth>', 'exec'), self.env)

    def authenticate(self):
        return self.env['_authenticate_fragment']('dummy-api-key', '123', 'V4R2', ['synthetic']*24, 10)

    def test_transport_errors_are_distinguished_and_never_echo_secrets(self):
        for error, code in ((requests.exceptions.Timeout, 'timeout'),
                            (requests.exceptions.ProxyError, 'proxy_error'),
                            (requests.exceptions.SSLError, 'tls_error'),
                            (requests.exceptions.ConnectionError, 'connection_error')):
            with self.subTest(code=code):
                self.requests.post.side_effect = error('synthetic-secret')
                token, result, status = self.authenticate()
                self.assertIsNone(token)
                self.assertEqual(status, 0)
                self.assertEqual(result, {'code': code})
                self.assertNotIn('synthetic-secret', str(self.env['logger'].mock_calls))

    def test_detail_message_is_not_mistaken_for_jwt(self):
        self.requests.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'detail': 'Confirm login in Telegram please'})
        token, result, status = self.authenticate()
        self.assertIsNone(token)
        self.assertEqual(result['code'], 'confirmation_required')

    def test_nested_access_token_and_prefix_are_supported(self):
        self.requests.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'data': {'access_token': 'Bearer dummy-token-for-test-only'}})
        self.assertEqual(self.authenticate()[0], 'dummy-token-for-test-only')
        kwargs = self.requests.post.call_args.kwargs
        self.assertEqual(kwargs['timeout'], (8, 40))
        self.assertFalse(kwargs['allow_redirects'])

    def test_rejected_response_does_not_echo_seed(self):
        self.requests.post.return_value = SimpleNamespace(status_code=400, json=lambda: {'mnemonics': ['secret phrase'], 'phone': 'secret-phone'})
        token, result, status = self.authenticate()
        self.assertEqual(result, {'code': 'api_rejected'})
        self.assertEqual(status, 400)
        self.assertIsNone(token)

    def test_non_json_response_is_clear_error(self):
        response = Mock(status_code=502)
        response.json.side_effect = ValueError('private response')
        self.requests.post.return_value = response
        self.assertEqual(self.authenticate(), (None, {'code': 'invalid_response'}, 502))
