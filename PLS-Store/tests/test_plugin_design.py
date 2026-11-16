"""Exercise plugin UI without importing Cardinal or accessing wallets/settings."""

import ast
from pathlib import Path
import unittest
from unittest.mock import patch
from services.ui import main_menu_kb, profile_kb


class DesignTests(unittest.TestCase):
    def test_licenses_only_in_home_with_color_in_each_language(self):
        with patch("services.ui.get_setting", side_effect=lambda key, default: default):
            for lang, label in [
                ("ru", "🔑 Лицензии"),
                ("en", "🔑 Licenses"),
                ("uk", "🔑 Ліцензії"),
            ]:
                home = main_menu_kb(lang)
                self.assertEqual(home.inline_keyboard[-2][0].callback_data, "licenses")
                self.assertEqual(home.inline_keyboard[-1][0].callback_data, "support")
                self.assertFalse(
                    any(
                        b.callback_data == "licenses"
                        for row in profile_kb(lang).inline_keyboard
                        for b in row
                    )
                )
                for markup in [home]:
                    button = next(
                        b
                        for row in markup.inline_keyboard
                        for b in row
                        if b.callback_data == "licenses"
                    )
                    self.assertEqual(button.text, label)
                    self.assertEqual(button.style, "primary")

    def test_plugins_use_plain_buttons_and_keep_store_branding(self):
        # Legacy Cardinal constructors accept callbacks and URLs, but no style.
        class LegacyButton:
            def __init__(self, text, callback_data=None, url=None):
                self.data = dict(text=text, callback_data=callback_data, url=url)

            def to_dict(self):
                return {k: v for k, v in self.data.items() if v is not None}

        for plugin, helper in [
            ("AutoStars", "B"),
            ("AutoGift", "InlineKeyboardButton"),
            ("AutoChannels", "InlineKeyboardButton"),
            ("AutoSMM", "_pls_button"),
        ]:
            with self.subTest(plugin=plugin):
                tree = ast.parse(
                    (Path(__file__).resolve().parents[1] / "plugins" / f"{plugin}.py").read_text()
                )
                nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                         and n.name in {helper, "_pls_panel"}]
                env = {"_TelegramButton": LegacyButton}
                exec(compile(ast.Module(body=nodes, type_ignores=[]), "<ui>", "exec"), env)
                for text in ["⚙️ Настройки", "➕ Добавить лот", "◀️ Назад", "🗑"]:
                    self.assertEqual(
                        env[helper](text, callback_data="same-callback").to_dict(),
                        {"text": text, "callback_data": "same-callback"},
                    )
                self.assertEqual(
                    env[helper]("PLS Store", url="https://t.me/PLSStore1_bot").to_dict(),
                    {"text": "PLS Store", "url": "https://t.me/PLSStore1_bot"},
                )
                panel = env["_pls_panel"]("Настройки")
                self.assertTrue(panel.startswith(f"<b>PLS Store · {plugin}</b>"))
                self.assertEqual(env["_pls_panel"](panel), panel)
