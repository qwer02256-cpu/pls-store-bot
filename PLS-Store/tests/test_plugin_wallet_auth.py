"""Regression: original 24-word auth and safe removal of the former provider."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class WalletAuthTests(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parents[1] / 'plugins/AutoStars.py').read_text()
        tree = ast.parse(self.source)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'_get_cfg', '_handle_fsm'}]
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        self.settings = {}
        self.bot = Mock()
        self.env = dict(json=json, _load_settings=lambda: self.settings,
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
