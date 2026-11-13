"""No real wallet phrases or API requests: execute auth handlers with mocked I/O."""

import ast
import html
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import Mock


class Keyboard:
    def __init__(self):
        self.rows = []

    def row(self, *buttons):
        self.rows.append(buttons)

    def add(self, *buttons):
        self.rows.append(buttons)


class MnemonicTests(unittest.TestCase):
    def setUp(self):
        source = (
            Path(__file__).resolve().parents[1] / "plugins/AutoStars.py"
        ).read_text()
        tree = ast.parse(source)
        names = {
            "_parse_mnemonic",
            "_fragment_auth_error",
            "_extract_wallet_address",
            "_complete_fragment_auth",
            "_authenticate_fragment",
            "_handle_fsm",
            "_jwt_confirmed",
            "_jwt_resend",
            "_token_text",
        }
        nodes = [
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
        ]
        future = ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )
        self.requests = Mock()
        self.token = "dummy-token-for-tests-only"
        self.requests.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"token": self.token}
        )
        self.bot = Mock()
        self.cfg = {}
        self.address = "UQ" + "A" * 46
        self.env = dict(
            json=json,
            _re=re,
            _html=html,
            _seed_unicode=unicodedata,
            _pls_math=math,
            requests=self.requests,
            logger=Mock(),
            _get_proxies_for_chat=Mock(return_value=None),
            FRAGMENT_AUTH_URL="https://fragment.example/auth",
            _fsm={},
            _check_fragment_wallet=Mock(
                return_value=("W5", 5.5, {"wallet": {"address": self.address}})
            ),
            _set_cfg=Mock(side_effect=lambda uid, **kwargs: self.cfg.update(kwargs)),
            _get_cfg=lambda uid: self.cfg,
            _open_token=Mock(),
            K=Keyboard,
            B=lambda text, **kw: dict(text=text, **kw),
            CBT_JWT_CONFIRMED="confirmed",
            CBT_JWT_RESEND="resend",
            CBT_FSM_CANCEL="cancel",
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
        self.cardinal = SimpleNamespace(telegram=SimpleNamespace(bot=self.bot))
        self.words12 = (
            "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
        )
        self.words24 = self.words12 + [
            "nu",
            "xi",
            "omicron",
            "pi",
            "rho",
            "sigma",
            "tau",
            "upsilon",
            "phi",
            "chi",
            "psi",
            "omega",
        ]

    def auth(self, words):
        return self.env["_authenticate_fragment"](
            "dummy-api-key", "10000000000", "W5", words, 10
        )

    def enter(self, text):
        self.env["_fsm"][10] = {
            "step": "jwt_seed",
            "api_key": "dummy-api-key",
            "phone": "10000000000",
            "wallet_version": "W5",
        }
        message = SimpleNamespace(chat=SimpleNamespace(id=10), message_id=20, text=text)
        self.env["_handle_fsm"](message, self.cardinal)

    def test_accepts_twelve_and_twenty_four_without_padding_or_conversion(self):
        for words in (self.words12, self.words24):
            token, _, _ = self.auth(words)
            self.assertEqual(token, self.token)
            sent = self.requests.post.call_args.kwargs["json"]
            self.assertEqual(sent["mnemonics"], words)
            self.assertEqual(
                set(sent), {"api_key", "phone_number", "version", "mnemonics"}
            )
            self.assertEqual(
                self.requests.post.call_args.args[0], "https://fragment.example/auth"
            )

    def test_whitespace_case_and_numbered_lines_preserve_word_order(self):
        raw = "\n".join(f"{i+1}. {w.upper()}" for i, w in enumerate(self.words12))
        self.assertEqual(self.env["_parse_mnemonic"](raw), self.words12)
        self.assertEqual(
            self.env["_parse_mnemonic"]("\t\u00a0".join(self.words24)), self.words24
        )

    def test_invalid_count_or_characters_never_reaches_provider(self):
        for words in (
            self.words12[:11],
            self.words12 + ["extra"],
            self.words24 + ["extra"],
            ["123"] * 12,
            ["слово"] * 12,
        ):
            token, result, status = self.auth(words)
            self.assertIsNone(token)
            self.assertEqual(status, 422)
            self.assertEqual(result["code"], "invalid_mnemonic")
        self.requests.post.assert_not_called()

    def test_fsm_twelve_word_success_saves_token_and_address_not_seed(self):
        self.enter(" ".join(self.words12))
        self.assertEqual(self.cfg["fragment_jwt"], self.token)
        self.assertEqual(self.cfg["wallet_address"], self.address)
        self.assertEqual(self.cfg["mnemonic_word_count"], 12)
        self.assertNotIn("mnemonic", self.cfg)
        self.assertNotIn(10, self.env["_fsm"])
        self.assertIn(self.address, self.bot.send_message.call_args.args[1])
        self.assertNotIn(
            " ".join(self.words12), str(self.bot.send_message.call_args_list)
        )

    def test_fsm_twenty_four_word_path_is_unchanged(self):
        self.enter(" ".join(self.words24))
        self.assertEqual(
            self.requests.post.call_args.kwargs["json"]["mnemonics"], self.words24
        )
        self.assertEqual(self.cfg["mnemonic_word_count"], 24)

    def test_provider_rejecting_twelve_gets_clear_message_not_confirmation_prompt(self):
        self.requests.post.return_value = SimpleNamespace(
            status_code=422,
            json=lambda: {
                "detail": [
                    {
                        "loc": ["mnemonics"],
                        "msg": "minimum 24 words",
                        "input": self.words12,
                    }
                ]
            },
        )
        self.enter(" ".join(self.words12))
        message = self.bot.send_message.call_args.args[1]
        self.assertIn("12 слов", message)
        self.assertIn("BIP39", message)
        self.assertNotIn("Я подтвердил", message)
        self.assertNotIn("alpha", message)
        self.assertNotIn(10, self.env["_fsm"])
        self.assertNotIn("fragment_jwt", self.cfg)

    def test_401_does_not_echo_api_key_phone_or_seed_from_error(self):
        raw = {
            "error": "unauthorized",
            "api_key": "dummy-api-key",
            "phone": "10000000000",
            "mnemonics": self.words12,
        }
        self.requests.post.return_value = SimpleNamespace(
            status_code=401, json=lambda: raw
        )
        _, safe, _ = self.auth(self.words12)
        self.assertEqual(safe["code"], "credentials_rejected")
        self.assertNotIn("dummy-api-key", str(safe))
        self.assertNotIn("alpha", str(safe))

    def test_detail_message_is_not_mistaken_for_jwt(self):
        self.requests.post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {"detail": "Confirm your Telegram login before proceeding"},
        )
        token, result, _ = self.auth(self.words12)
        self.assertIsNone(token)
        self.assertEqual(result["code"], "confirmation_required")

    def test_confirmation_and_manual_retry_both_keep_twelve_words(self):
        call = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(id=10)), id="call"
        )
        for name in ("_jwt_confirmed", "_jwt_resend"):
            self.env["_fsm"][10] = {
                "api_key": "dummy-api-key",
                "phone": "10000000000",
                "wallet_version": "W5",
                "mnemonic": self.words12,
            }
            self.env[name](self.bot, call)
            self.assertEqual(
                self.requests.post.call_args.kwargs["json"]["mnemonics"], self.words12
            )
            self.assertNotIn(10, self.env["_fsm"])

    def test_confirmation_required_keeps_secret_only_in_runtime_state(self):
        self.requests.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"detail": "Confirm Telegram login"}
        )
        self.enter(" ".join(self.words12))
        self.assertEqual(self.env["_fsm"][10]["mnemonic"], self.words12)
        self.assertEqual(self.cfg, {})
        self.assertIn(
            "Я подтвердил вход",
            str(self.bot.send_message.call_args.kwargs["reply_markup"].rows),
        )

    def test_network_error_is_sanitized_in_chat_and_logs(self):
        self.requests.post.side_effect = OSError(" ".join(self.words12))
        self.enter(" ".join(self.words12))
        self.assertNotIn("alpha", str(self.bot.send_message.call_args))
        self.assertNotIn("alpha", str(self.env["logger"].warning.call_args))

    def test_missing_wallet_response_clears_old_balance_and_address(self):
        self.cfg.update(balance_ton=99, wallet_address=self.address)
        self.env["_check_fragment_wallet"].return_value = (None, None, None)
        self.enter(" ".join(self.words12))
        self.assertIsNone(self.cfg["balance_ton"])
        self.assertIsNone(self.cfg["wallet_address"])
        self.assertIn("API не сообщил адрес", self.bot.send_message.call_args.args[1])

    def test_nested_token_and_bearer_header_supported(self):
        self.requests.post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {"data": {"access_token": "Bearer " + self.token}},
        )
        self.assertEqual(self.auth(self.words12)[0], self.token)

    def test_wallet_address_extractor_ignores_arbitrary_provider_text(self):
        self.assertIsNone(
            self.env["_extract_wallet_address"]({"address": " ".join(self.words12)})
        )
        raw = "0:" + "a" * 64
        self.assertEqual(
            self.env["_extract_wallet_address"]({"result": {"wallet_address": raw}}),
            raw,
        )
