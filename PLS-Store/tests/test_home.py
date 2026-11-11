import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from services import db


class HomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_locales_balance_and_html_escape(self):
        actor = SimpleNamespace(id=42, username='name<&>', first_name='Name')
        for lang, heading in [('ru','Главное меню'),('uk','Головне меню'),('en','Main menu')]:
            capture=AsyncMock()
            with patch.object(main,'get_user_language',return_value=lang), patch.object(main,'ensure_user',return_value={'balance':120.50}), patch.object(main,'get_setting',side_effect=lambda key, default: default), patch.object(main,'banner_path',return_value=None), patch.object(main,'main_menu_kb',return_value=None), patch.object(main,'safe_edit_or_send',capture):
                await main.show_home(SimpleNamespace(from_user=actor), 'Name')
            text=capture.call_args.args[1]
            self.assertIn(heading,text)
            self.assertIn('name&lt;&amp;&gt;',text)
            self.assertIn('120.5₽',text)
            self.assertNotIn('{balance}',text)

    def test_existing_default_migrates_and_custom_greeting_survives(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db,'DB_PATH',Path(tmp)/'store.db'):
            db.init_db()
            db.set_setting('welcome_text','<b>PLS Store</b>\nПлагины и дополнения для FunPayCardinal\n\nДобро пожаловать, {first_name}!')
            db.init_db()
            self.assertIn('{balance}',db.get_setting('welcome_text'))
            db.set_setting('welcome_text','My custom greeting {first_name}')
            db.init_db()
            self.assertEqual(db.get_setting('welcome_text'),'My custom greeting {first_name}')

if __name__ == '__main__':
    unittest.main()
