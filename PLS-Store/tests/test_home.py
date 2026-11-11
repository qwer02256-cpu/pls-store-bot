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
            self.assertNotIn(' · ',text)
            self.assertIn('FunPayCardinal',text)

    def test_existing_default_migrates_and_custom_greeting_survives(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db,'DB_PATH',Path(tmp)/'store.db'):
            db.init_db()
            with db.db() as conn:
                conn.execute("DELETE FROM bot_settings WHERE key='home_welcome_layout_version'")
            db.set_setting('welcome_text','<b>PLS Store</b>\nПлагины и дополнения для FunPayCardinal\n\nДобро пожаловать, {first_name}!')
            db.init_db()
            self.assertIn('{balance}',db.get_setting('welcome_text'))
            db.set_setting('welcome_text','My custom greeting {first_name}')
            db.init_db()
            self.assertEqual(db.get_setting('welcome_text'),'My custom greeting {first_name}')

    def test_saved_legacy_greeting_is_upgraded_and_backed_up_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db,'DB_PATH',Path(tmp)/'store.db'):
            db.init_db()
            with db.db() as conn:
                conn.execute("DELETE FROM bot_settings WHERE key='home_welcome_layout_version'")
            legacy='PLS Store — старый текст из админки'
            db.set_setting('welcome_text',legacy)
            db.init_db()
            self.assertIn('Главное меню',db.get_setting('welcome_text'))
            self.assertIn('{balance}',db.get_setting('welcome_text'))
            self.assertEqual(db.get_setting('welcome_text_before_home_v2'),legacy)
            db.set_setting('welcome_text','New owner greeting {balance}')
            db.init_db()
            self.assertEqual(db.get_setting('welcome_text'),'New owner greeting {balance}')
            self.assertEqual(db.get_setting('welcome_text_before_home_v2'),legacy)

    def test_existing_home_footer_updates_without_resetting_greeting(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db,'DB_PATH',Path(tmp)/'store.db'):
            db.init_db()
            db.set_setting('welcome_text','Hello {first_name}\nПлагины · Дополнения · FunPayCardinal')
            db.init_db()
            self.assertEqual(db.get_setting('welcome_text'),'Hello {first_name}\nПлагины для FunPayCardinal')
            db.init_db()
            self.assertEqual(db.get_setting('welcome_text'),'Hello {first_name}\nПлагины для FunPayCardinal')

if __name__ == '__main__':
    unittest.main()
