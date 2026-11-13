"""Run the actual activation and send entrypoint functions with isolated I/O."""

import ast
import html
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "storage/plugins/AutoStars/license.json"
        source = (
            Path(__file__).resolve().parents[1] / "plugins/AutoStars.py"
        ).read_text()
        tree = ast.parse(source)
        names = {
            "_pls_require_license",
            "_pls_has_key",
            "_pls_show_activation",
            "_pls_gate",
            "_pls_command_home",
            "_pls_open_license",
            "_pls_start_key",
            "_pls_check_key",
            "_pls_receive_key",
            "_handle_fsm",
            "_order_stars",
            "init_cardinal",
            "_toggle_plugin",
        }
        nodes = [
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
        ]
        self.requests = Mock()
        self.requests.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"valid": True}
        )
        self.bot = Mock()
        self.cardinal = SimpleNamespace(
            account=SimpleNamespace(id=123), telegram=SimpleNamespace(bot=self.bot)
        )
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=10), message_id=99, text=""
        )
        self.call = SimpleNamespace(
            id="call", message=SimpleNamespace(chat=SimpleNamespace(id=10), id=99)
        )
        self.key = "PLS-" + "a" * 43
        self.env = dict(
            json=json,
            os=os,
            _re=re,
            _html=html,
            _PLS_LOCK=threading.Lock(),
            _PLS_CHECKED=0.0,
            _PLS_ACCOUNT=None,
            _PlsPath=lambda p: self.root / p,
            _PLS_ENDPOINT="https://store.example/licenses/verify",
            requests=self.requests,
            _pls_time=SimpleNamespace(monotonic=lambda: 1000),
            _fsm={},
            VERSION="2.2",
            NAME="AutoStars",
            UUID="uuid",
            logger=Mock(),
            _get_cfg=Mock(return_value={"plugin_enabled": False}),
            _set_cfg=Mock(),
            _open_settings=Mock(),
            _send_panel=Mock(),
            _safe_edit=Mock(),
            _pls_license_kb=Mock(return_value="activate"),
            _home_kb=Mock(return_value="home"),
            _about_text=Mock(return_value="HOME"),
            _kb_cancel_fsm=Mock(return_value="cancel"),
            _CARDINAL_REF=self.cardinal,
            _get_proxies_for_chat=Mock(return_value=None),
            FRAGMENT_ORDER_STARS="https://fragment.example/order",
        )
        future = ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=[future] + nodes, type_ignores=[])
                ),
                "<plugin>",
                "exec",
            ),
            self.env,
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"key": self.key}))

    def test_opening_home_without_key_shows_activation_and_never_home(self):
        self.env["_pls_command_home"](self.cardinal, self.message)
        self.assertIn("Ключ: <b>не введён", self.env["_send_panel"].call_args.args[2])
        self.assertIn("Ввести ключ", self.env["_send_panel"].call_args.args[2])
        self.env["_about_text"].assert_not_called()
        self.requests.post.assert_not_called()

    def test_saved_valid_key_is_checked_on_opening_home(self):
        self.save()
        self.env["_pls_command_home"](self.cardinal, self.message)
        self.assertEqual(self.env["_send_panel"].call_args.args[2], "HOME")
        self.assertEqual(
            self.requests.post.call_args.kwargs["json"],
            dict(key=self.key, funpay_id="123", product="autostars"),
        )

    def test_saved_key_for_different_funpay_does_not_open_home(self):
        self.save()
        self.requests.post.return_value = SimpleNamespace(
            status_code=403, json=lambda: {"valid": False}
        )
        self.env["_pls_command_home"](self.cardinal, self.message)
        self.assertIn("требуется проверка", self.env["_send_panel"].call_args.args[2])
        self.env["_about_text"].assert_not_called()

    def test_input_saves_key_only_then_checks_and_opens_home(self):
        self.env["_pls_start_key"](self.cardinal, self.call)
        self.message.text = self.key
        self.env["_handle_fsm"](self.message, self.cardinal)
        self.assertEqual(json.loads(self.path.read_text()), {"key": self.key})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(10, self.env["_fsm"])
        self.bot.delete_message.assert_called_once_with(10, 99)
        self.assertEqual(self.env["_PLS_ACCOUNT"], "123")
        self.assertEqual(
            set(self.requests.post.call_args.kwargs["json"]),
            {"key", "funpay_id", "product"},
        )
        self.assertIn("HOME", self.env["_send_panel"].call_args.args[2])

    def test_wrong_input_cannot_store_fragment_secret_or_call_server(self):
        self.env["_pls_start_key"](self.cardinal, self.call)
        self.message.text = "seed words fragment JWT secret"
        self.env["_handle_fsm"](self.message, self.cardinal)
        self.assertFalse(self.path.exists())
        self.requests.post.assert_not_called()
        self.assertEqual(self.env["_fsm"][10]["step"], "pls_license_key")
        self.assertNotIn(self.message.text, self.env["_send_panel"].call_args.args[2])

    def test_network_failure_keeps_saved_key_unverified_and_retry_recovers(self):
        self.message.text = self.key
        self.requests.post.side_effect = OSError("offline")
        self.env["_pls_receive_key"](self.cardinal, self.message)
        self.assertTrue(self.path.exists())
        self.assertIsNone(self.env["_PLS_ACCOUNT"])
        self.env["_about_text"].assert_not_called()
        self.requests.post.side_effect = None
        self.env["_pls_check_key"](self.cardinal, self.call)
        self.assertEqual(self.env["_PLS_ACCOUNT"], "123")
        self.assertEqual(self.env["_safe_edit"].call_args.args[3], "HOME")

    def test_replacing_key_invalidates_previous_cached_approval(self):
        self.save()
        self.env["_pls_require_license"](self.cardinal)
        self.message.text = "PLS-" + "b" * 43
        self.requests.post.return_value = SimpleNamespace(
            status_code=403, json=lambda: {"valid": False}
        )
        self.env["_pls_receive_key"](self.cardinal, self.message)
        self.assertIsNone(self.env["_PLS_ACCOUNT"])
        self.assertEqual(self.env["_PLS_CHECKED"], 0)

    def test_missing_key_blocks_actual_send_before_fragment_or_proxy_access(self):
        with self.assertRaises(RuntimeError):
            self.env["_order_stars"]("sensitive-jwt", "username", 50)
        self.requests.post.assert_not_called()
        self.env["_get_proxies_for_chat"].assert_not_called()

    def test_server_denial_blocks_actual_send_and_never_transmits_fragment_jwt(self):
        self.save()
        self.requests.post.return_value = SimpleNamespace(
            status_code=403, json=lambda: {"valid": False}
        )
        with self.assertRaises(RuntimeError):
            self.env["_order_stars"]("sensitive-jwt", "username", 50)
        self.assertEqual(self.requests.post.call_count, 1)
        self.assertEqual(
            self.requests.post.call_args.args[0],
            "https://store.example/licenses/verify",
        )
        self.assertNotIn("sensitive-jwt", str(self.requests.post.call_args))
        self.env["_get_proxies_for_chat"].assert_not_called()

    def test_missing_funpay_login_keeps_home_locked(self):
        self.save()
        self.cardinal.account.id = None
        self.env["_pls_command_home"](self.cardinal, self.message)
        self.env["_about_text"].assert_not_called()
        self.requests.post.assert_not_called()

    def test_real_registered_command_and_input_filter_use_activation(self):
        tg = self.cardinal.telegram
        tg.msg_handler = Mock()
        tg.cbq_handler = Mock()
        self.env["init_cardinal"](self.cardinal)
        command = next(
            c.args[0]
            for c in tg.msg_handler.call_args_list
            if c.kwargs.get("commands") == ["autostars"]
        )
        command(self.message)
        self.env["_about_text"].assert_not_called()
        self.env["_pls_start_key"](self.cardinal, self.call)
        self.message.text = self.key
        state_call = next(
            c for c in tg.msg_handler.call_args_list if c.kwargs.get("func")
        )
        self.assertTrue(state_call.kwargs["func"](self.message))
        state_call.args[0](self.message)
        self.assertEqual(json.loads(self.path.read_text()), {"key": self.key})
        self.assertEqual(self.env["_PLS_ACCOUNT"], "123")

    def test_enabling_plugin_without_key_does_not_set_enabled(self):
        self.env["_toggle_plugin"](self.bot, self.call)
        self.env["_set_cfg"].assert_not_called()
        self.requests.post.assert_not_called()

    def test_raw_repository_file_explains_missing_endpoint(self):
        self.save()
        self.env["_PLS_ENDPOINT"] = "__PLS_LICENSE_ENDPOINT__"
        self.env["_pls_command_home"](self.cardinal, self.message)
        self.assertIn("Скачайте плагин", self.env["_send_panel"].call_args.args[2])
        self.requests.post.assert_not_called()
