"""Offline protocol fixtures and repricing integration; no live trading."""
import ast
import copy
import html
import re
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def load(**bindings):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'plugins/AutoStars.py').read_text())
    names = {'_money_decimal', '_total_stars_cost', '_target_lot_price', '_parse_fragment_quote', '_fragment_quote', '_repricing_cycle', '_autoprice_receive', '_autoprice_callback'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    env = dict(_re=re, _html=html, _REPRICE_LOCK=threading.RLock(),
               _REPRICE_CYCLE_LOCK=threading.Lock(), _REPRICE_STOP=threading.Event(), _pls_time=SimpleNamespace(time=lambda: 1000, monotonic=lambda: 1000))
    env.update(bindings)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future]+nodes, type_ignores=[])), '<prices>', 'exec'), env)
    return env


class PricingTests(unittest.TestCase):
    def test_profit_is_net_of_three_percent_and_rounded_up(self):
        price = load()['_target_lot_price']('0,23', '125', '20')
        self.assertEqual(price, Decimal('50.26'))
        self.assertGreaterEqual(price * Decimal('.97') - Decimal('28.75'), 20)
        self.assertLess((price-Decimal('.01'))*Decimal('.97')-Decimal('28.75'), 20)
        self.assertEqual(load()['_target_lot_price']('.23', '125', '0'), Decimal('29.64'))

    def test_api_and_network_expenses_are_included(self):
        env = load()
        cfg = dict(cost_fees_confirmed=True, cost_fee_percent='1', cost_fee_ton='0.01')
        cost = env['_total_stars_cost']('.23', cfg)
        self.assertEqual(cost, Decimal('.2423'))
        price = env['_target_lot_price'](cost, '125', '20')
        self.assertGreaterEqual(price * Decimal('.97') - cost * 125, 20)
        with self.assertRaises(ValueError):
            env['_total_stars_cost']('.23', {})

    def test_bad_inputs_cannot_produce_prices(self):
        for bad in ('NaN', 'Infinity', '-1', '0', '', True):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                load()['_target_lot_price'](bad, '125', '20')
        with self.assertRaises(ValueError):
            load()['_target_lot_price']('.23', '125', '-1')

    def test_quote_parser_separates_ton_from_dollars(self):
        # Representative fixture, not a captured live Fragment response.
        markup='<div class="tm-value icon-ton">0<span class="mini-frac">.2300</span></div><div>~$1.50</div>'
        self.assertEqual(load()['_parse_fragment_quote']({'ok':True,'cur_price':markup}), Decimal('.23'))
        self.assertEqual(load()['_parse_fragment_quote']({'ok':True,'cur_price':'<span class="icon-ton">1</span>'}), 1)
        for value in ('0.23','<div class="icon-ton">0</div>','<div class="icon-ton">NaN</div>',
                      '<div class="icon-ton">.23','<div class="icon-ton">1</div>'*2):
            with self.subTest(value=value), self.assertRaises(Exception):
                load()['_parse_fragment_quote']({'ok':True,'cur_price':value})
        with self.assertRaises(ValueError):
            load()['_parse_fragment_quote']({'ok':False})

    def test_quote_transport_only_requests_prices_without_wallet_credentials(self):
        session=Mock()
        session.get.return_value=Mock(status_code=200,text='"/api?hash=public_hash"')
        session.post.return_value=Mock(status_code=200)
        session.post.return_value.json.return_value={'ok':True,'cur_price':'<div class="icon-ton">0.23</div>'}
        factory=Mock()
        factory.return_value.__enter__=Mock(return_value=session)
        factory.return_value.__exit__=Mock(return_value=False)
        env=load(requests=SimpleNamespace(Session=factory),_get_proxies_for_chat=Mock(return_value=None))
        self.assertEqual(env['_fragment_quote'](10,100),Decimal('.23'))
        args,kwargs=session.post.call_args
        self.assertEqual(args[0],'https://fragment.com/api')
        self.assertEqual(kwargs['data'],dict(method='updateStarsPrices',stars=0,quantity=100))
        self.assertNotIn('Authorization',kwargs['headers'])
        self.assertFalse(kwargs['allow_redirects'])


class RepricingTests(unittest.TestCase):
    def setUp(self):
        self.settings={'10':dict(plugin_enabled=True,ton_buy_price=125,ton_price_confirmed=True,cost_fees_confirmed=True,cost_fee_percent="0",cost_fee_ton="0",
            star_lots=[dict(lot_id=123,qty=100,active=False,autoprice_enabled=True,target_profit_rub='20')])}
        self.fields=SimpleNamespace(price=45.0,active=False,title_ru='Title',description_ru='Description',amount=7)
        self.account=SimpleNamespace(get_lot_fields=Mock(return_value=self.fields),save_lot=Mock())
        self.card=SimpleNamespace(account=self.account)
        self.quotes=Mock(side_effect=lambda chat,qty:Decimal('.115') if qty==50 else Decimal('.23'))
        self.env=load(_load_settings=lambda:copy.deepcopy(self.settings),
                      _get_cfg=lambda chat:copy.deepcopy(self.settings[str(chat)]),
                      _set_cfg=Mock(side_effect=lambda chat,**kw:self.settings[str(chat)].update(kw)),
                      _pls_require_license=Mock(),_is_stars_lot=Mock(return_value=True))
        self.env['_fragment_quote']=self.quotes

    def run_cycle(self):
        self.env['_repricing_cycle'](self.card)

    def test_price_save_verified_and_other_fields_preserved(self):
        self.run_cycle()
        self.assertEqual(vars(self.fields),dict(price=50.26,active=False,title_ru='Title',description_ru='Description',amount=7))
        self.account.save_lot.assert_called_once_with(self.fields)
        self.assertEqual(self.account.get_lot_fields.call_count,2)
        self.assertEqual({c.args[1] for c in self.quotes.call_args_list},{50,100})
        self.assertIn('50.26',self.settings['10']['autoprice_status']['123'])
        self.account.save_lot.reset_mock()
        self.run_cycle()
        self.account.save_lot.assert_not_called()

    def test_failed_quote_cannot_use_old_rate_or_touch_funpay(self):
        self.settings['10']['fragment_rate']=.001
        self.quotes.side_effect=ValueError('bad quote')
        self.run_cycle()
        self.account.get_lot_fields.assert_not_called()
        self.assertTrue(self.settings['10']['autoprice_error'])

    def test_license_and_confirmed_ton_price_are_required(self):
        self.env['_pls_require_license'].side_effect=RuntimeError('denied')
        self.run_cycle()
        self.quotes.assert_not_called()
        self.env['_pls_require_license'].side_effect=None
        self.settings['10']['ton_price_confirmed']=False
        self.run_cycle()
        self.quotes.assert_not_called()
        self.account.save_lot.assert_not_called()

    def test_disabled_rule_or_changed_rule_cannot_update(self):
        self.settings['10']['star_lots'][0]['autoprice_enabled']=False
        self.run_cycle()
        self.quotes.assert_not_called()
        self.settings['10']['star_lots'][0]['autoprice_enabled']=True
        def quote(chat,qty):
            self.settings['10']['star_lots'][0]['autoprice_enabled']=False
            return Decimal('.23')
        self.quotes.side_effect=quote
        self.run_cycle()
        self.account.save_lot.assert_not_called()

    def test_wrong_category_and_duplicate_rules_are_blocked(self):
        self.env['_is_stars_lot'].return_value=False
        self.run_cycle()
        self.account.save_lot.assert_not_called()
        self.env['_is_stars_lot'].return_value=True
        self.settings['11']=copy.deepcopy(self.settings['10'])
        self.run_cycle()
        self.account.save_lot.assert_not_called()

    def test_unknown_expenses_or_stale_quote_block_updates(self):
        self.settings['10']['cost_fees_confirmed'] = False
        self.run_cycle()
        self.quotes.assert_not_called()
        self.settings['10']['cost_fees_confirmed'] = True
        self.env['_pls_time'].monotonic = Mock(side_effect=[0, 121])
        self.run_cycle()
        self.account.save_lot.assert_not_called()

    def test_save_failure_is_reported(self):
        self.account.save_lot.side_effect=RuntimeError('save failed')
        self.run_cycle()
        self.assertIn('Ошибка',self.settings['10']['autoprice_status']['123'])


class PricingInputTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(star_lots=[dict(lot_id=123, qty=100)])
        self.bot = Mock()
        self.card = SimpleNamespace(telegram=SimpleNamespace(bot=self.bot))
        self.env = load(_fsm={}, _get_cfg=lambda chat: self.cfg,
                        _set_cfg=Mock(side_effect=lambda chat, **kw: self.cfg.update(kw)),
                        _autoprice_kb=Mock(return_value='keyboard'), _safe_edit=Mock(),
                        _autoprice_text=Mock(return_value='text'), _pls_require_license=Mock(),
                        _kb_cancel_fsm=Mock(return_value='cancel'))
        self.call = SimpleNamespace(id='callback', data='uuid:apon:123',
                                    message=SimpleNamespace(id=1, chat=SimpleNamespace(id=10)))

    def send(self, text):
        self.env['_autoprice_receive'](self.card, SimpleNamespace(chat=SimpleNamespace(id=10), text=text))

    def test_profit_is_per_lot_and_does_not_enable_pricing(self):
        self.env['_fsm'][10] = dict(step='autoprice_profit', lot_id=123)
        self.send('20,5')
        self.assertEqual(self.cfg['star_lots'][0]['target_profit_rub'], '20.5')
        self.assertFalse(self.cfg['star_lots'][0].get('autoprice_enabled', False))
        self.assertNotIn(10, self.env['_fsm'])

    def test_invalid_fees_stay_in_input_and_cancel_does_not_save(self):
        self.env['_fsm'][10] = dict(step='autoprice_fees')
        self.send('NaN 0')
        self.env['_set_cfg'].assert_not_called()
        self.assertIn(10, self.env['_fsm'])
        self.send('/cancel')
        self.env['_set_cfg'].assert_not_called()
        self.assertNotIn(10, self.env['_fsm'])

    def test_fee_input_and_enable_require_all_inputs(self):
        self.env['_fsm'][10] = dict(step='autoprice_fees')
        self.send('1 0,01')
        self.assertEqual(self.cfg['cost_fee_percent'], '1')
        self.assertEqual(self.cfg['cost_fee_ton'], '0.01')
        self.assertTrue(self.cfg['cost_fees_confirmed'])
        self.env['_autoprice_callback'](self.card, self.call)
        self.assertFalse(self.cfg['star_lots'][0].get('autoprice_enabled', False))
        self.cfg.update(ton_price_confirmed=True)
        self.cfg['star_lots'][0]['target_profit_rub'] = '20'
        self.env['_autoprice_callback'](self.card, self.call)
        self.assertTrue(self.cfg['star_lots'][0]['autoprice_enabled'])
        self.env['_autoprice_callback'](self.card, self.call)
        self.assertFalse(self.cfg['star_lots'][0]['autoprice_enabled'])
