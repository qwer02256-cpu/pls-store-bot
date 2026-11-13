"""Exercise plugin UI without importing Cardinal or accessing wallets/settings."""

import ast
from pathlib import Path
import unittest
from unittest.mock import patch
from services.ui import main_menu_kb, profile_kb


class DesignTests(unittest.TestCase):
    def test_licenses_in_home_and_profile_with_color_in_each_language(self):
        with patch("services.ui.get_setting", side_effect=lambda key, default: default):
            for lang, label in [
                ("ru", "🔑 Лицензии"),
                ("en", "🔑 Licenses"),
                ("uk", "🔑 Ліцензії"),
            ]:
                home = main_menu_kb(lang)
                self.assertEqual(home.inline_keyboard[-2][0].callback_data, "licenses")
                self.assertEqual(home.inline_keyboard[-1][0].callback_data, "support")
                for markup in [home, profile_kb(lang)]:
                    button = next(
                        b
                        for row in markup.inline_keyboard
                        for b in row
                        if b.callback_data == "licenses"
                    )
                    self.assertEqual(button.text, label)
                    self.assertEqual(button.style, "primary")

    def test_plugin_serializes_styles_with_legacy_button_constructor(self):
        # Legacy Cardinal versions have no style parameter; preserve URL/callback.
        class LegacyButton:
            def __init__(self, text, callback_data=None, url=None):
                self.data = dict(text=text, callback_data=callback_data, url=url)

            def to_dict(self):
                return {k: v for k, v in self.data.items() if v is not None}

        tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "plugins/AutoStars.py").read_text()
        )
        names = {"_PlsButton", "B", "_pls_panel", "_about_text"}
        nodes = [
            n
            for n in tree.body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names
        ]
        env = {"_TelegramButton": LegacyButton, "VERSION": "2.1", "CREDITS": "@author"}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<ui>", "exec"), env)
        for text, style in [
            ("⚙️ Настройки", "primary"),
            ("➕ Добавить лот", "success"),
            ("◀️ Назад", "danger"),
            ("🗑", "danger"),
        ]:
            data = env["B"](text, callback_data="same-callback").to_dict()
            self.assertEqual(data["style"], style)
            self.assertEqual(data["callback_data"], "same-callback")
        self.assertEqual(
            env["B"]("Магазин", url="https://example.com", style="success").to_dict()[
                "url"
            ],
            "https://example.com",
        )
        panel = env["_about_text"]()
        self.assertTrue(panel.startswith("<b>PLS Store · AutoStars</b>"))
        self.assertEqual(env["_pls_panel"](panel), panel)
