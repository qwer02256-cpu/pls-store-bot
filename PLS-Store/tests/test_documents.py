import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from services import db
from services.documents import DEFAULT_URLS, document_rows
from services.emoji import PackEmojiMiddleware


class DocumentTests(unittest.TestCase):
    def test_documents_are_always_available_without_optional_license_url(self):
        with patch('services.documents.get_setting',return_value=''),patch.object(main,'get_user_language',return_value='ru'):
            keyboard=main.license_gate_keyboard('',42)
        self.assertEqual(len(keyboard.inline_keyboard),5)
        self.assertEqual([row[0].url for row in keyboard.inline_keyboard[:4]],list(DEFAULT_URLS.values()))
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data,'license:accept')
        self.assertEqual(keyboard.inline_keyboard[-1][0].style,'success')

    def test_invalid_config_is_replaced_and_valid_custom_agreement_is_shared(self):
        settings={'document_privacy_url':'javascript:bad','document_agreement_url':'https://example.org/terms'}
        with patch('services.documents.get_setting',side_effect=lambda k,d='':settings.get(k,d)):
            rows=document_rows('en')
        self.assertEqual(rows[0][0].url,DEFAULT_URLS['privacy'])
        self.assertEqual(rows[1][0].url,'https://example.org/terms')
        self.assertIn('User Agreement',rows[1][0].text)

    def test_first_entry_text_and_buttons_use_pack_emoji(self):
        ids={'📄':'101','🔒':'102','📜':'103','💵':'104','✅':'105','🔑':'106'}
        with patch('services.emoji.get_setting',return_value=json.dumps(ids)):
            middleware=PackEmojiMiddleware()
        with patch('services.documents.get_setting',return_value=''),patch.object(main,'get_user_language',return_value='ru'):
            markup=middleware.keyboard(main.license_gate_keyboard('',42))
        self.assertEqual([r[0].icon_custom_emoji_id for r in markup.inline_keyboard],['102','103','106','104','105'])
        for lang in ('ru','uk','en'):
            self.assertIn('tg-emoji',middleware.render_html(main.I18N[lang]['license_title']))


class FirstEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_start_shows_documents_and_acceptance_unlocks_home(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db()
            actor=SimpleNamespace(id=42,username='buyer',first_name='Buyer')
            message=SimpleNamespace(from_user=actor,text='/start')
            with patch.object(main,'safe_edit_or_send',AsyncMock()) as render,patch.object(main,'show_home',AsyncMock()) as home:
                await main.cmd_start(message)
                home.assert_not_awaited()
                self.assertEqual(len(render.call_args.kwargs['reply_markup'].inline_keyboard),5)
                self.assertFalse(main.has_accepted_license(42))
                callback=SimpleNamespace(from_user=actor,answer=AsyncMock())
                await main.cb_license_accept(callback)
                self.assertTrue(main.has_accepted_license(42))
                await main.cmd_start(message)
                self.assertEqual(home.await_count,2)

    async def test_old_checkout_callback_cannot_bypass_first_entry_documents(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db();db.ensure_user(42)
            callback=SimpleNamespace(from_user=SimpleNamespace(id=42),data='purchasepay:2:1',answer=AsyncMock())
            with patch.object(main,'show_license_gate',AsyncMock()) as gate,patch.object(main,'create_payment_invoice',AsyncMock()) as create:
                await main.cb_purchase_method(callback,SimpleNamespace())
                gate.assert_awaited_once_with(callback,42)
                create.assert_not_awaited()

    async def test_admin_rejects_invalid_document_link_and_preserves_query_string(self):
        state=SimpleNamespace(get_data=AsyncMock(return_value={'design_key':'document_privacy_url'}),clear=AsyncMock())
        message=SimpleNamespace(from_user=SimpleNamespace(id=42),text='not a link',answer=AsyncMock(),entities=[])
        with patch.object(main,'ADMIN_ID',42),patch.object(main,'set_setting') as save:
            await main.adm_design_save(message,state)
            save.assert_not_called()
            state.clear.assert_not_awaited()
            message.text='https://example.org/privacy?lang=ru&version=1'
            await main.adm_design_save(message,state)
            save.assert_called_once_with('document_privacy_url',message.text)

    async def test_admin_can_preview_after_acceptance_without_resetting_it(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(db,'DB_PATH',Path(tmp)/'db'):
            db.init_db();db.ensure_user(42);main.accept_license(42)
            with db.db() as conn:
                conn.execute("UPDATE license_accepts SET accepted_at='2020-01-01 00:00:00' WHERE user_id=42")
            message=SimpleNamespace(from_user=SimpleNamespace(id=42))
            with patch.object(main,'ADMIN_ID',42),patch.object(main,'safe_edit_or_send',AsyncMock()) as render:
                await main.cmd_testdocs(message)
            self.assertEqual(len(render.call_args.kwargs['reply_markup'].inline_keyboard),5)
            self.assertTrue(main.has_accepted_license(42))
            with db.db() as conn:
                self.assertEqual(conn.execute('SELECT accepted_at FROM license_accepts WHERE user_id=42').fetchone()[0],'2020-01-01 00:00:00')

    async def test_non_admin_cannot_use_preview_command(self):
        with patch.object(main,'ADMIN_ID',42),patch.object(main,'show_license_gate',AsyncMock()) as show:
            await main.cmd_testdocs(SimpleNamespace(from_user=SimpleNamespace(id=99)))
            show.assert_not_awaited()
